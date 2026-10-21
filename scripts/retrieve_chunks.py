"""Exact cosine candidates followed by Qwen reranking; return the best five."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

if __package__:
    from .context_expansion import expand_context
    from .embed_chunks import ROOT, OUTPUT, identity_for, load_config, load_local_credentials, load_source
    from .embedding_api import ApiFailure, EmbeddingClient, validate_response
    from .embedding_store import saved_batches, sha256, write_json
    from .rerank_api import RerankClient, load_rerank_config
    from .rerank_chunks import rerank_hits
    from .keyword_retrieval import supplement_one
else:
    from context_expansion import expand_context
    from embed_chunks import ROOT, OUTPUT, identity_for, load_config, load_local_credentials, load_source
    from embedding_api import ApiFailure, EmbeddingClient, validate_response
    from embedding_store import saved_batches, sha256, write_json
    from rerank_api import RerankClient, load_rerank_config
    from rerank_chunks import rerank_hits
    from keyword_retrieval import supplement_one


def unit_vector(vector: list[float], dimensions: int) -> list[float]:
    if (not isinstance(vector, list) or len(vector) != dimensions
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector)):
        raise ValueError('Vector dimensions or values are invalid')
    norm = math.hypot(*vector)
    if norm == 0 or not math.isfinite(norm):
        raise ValueError('Vector norm must be finite and nonzero')
    return [value / norm for value in vector]


def cosine_top_k(query: list[float], normalized_documents: list[list[float]], chunks: list[dict], top_k: int):
    if type(top_k) is not int or not 1 <= top_k <= 20:
        raise ValueError('top_k must be between 1 and 20')
    if len(normalized_documents) != len(chunks) or not normalized_documents:
        raise ValueError('Document vectors and chunks must match one-to-one')
    query_unit = unit_vector(query, len(normalized_documents[0]))
    scores = []
    for position, vector in enumerate(normalized_documents):
        if len(vector) != len(query_unit):
            raise ValueError('Cannot compare vectors of different dimensions')
        score = max(-1.0, min(1.0, math.fsum(a * b for a, b in zip(query_unit, vector))))
        scores.append((position, score))
    scores.sort(key=lambda item: (-item[1], item[0]))
    return [{'rank': rank, 'cosine_similarity': score, **{
            key: chunks[position][key] for key in ('chunk_id', 'text', 'source_file', 'pdf_pages')}}
            for rank, (position, score) in enumerate(scores[:top_k], 1)]


def load_corpus(root: Path, config: dict, base_url: str):
    chunks, source_sha = load_source(root, config)
    basis, identity = identity_for(source_sha, config, base_url)
    output = root / OUTPUT
    manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
    receipt = json.loads((output / 'run.json').read_text(encoding='utf-8'))
    if (manifest != {'schema_version': 1, 'basis': basis, 'identity': identity}
            or receipt.get('identity') != identity or receipt.get('basis') != basis
            or receipt.get('status') != 'completed' or receipt.get('completed_chunks') != len(chunks)
            or receipt.get('total_chunks') != len(chunks)):
        raise ValueError('Corpus is incomplete or does not match the model, dimensions, endpoint or source')
    batches, count = saved_batches(output, chunks, config, identity)
    if count != len(chunks) or len(batches) != receipt.get('saved_batches'):
        raise ValueError('Saved batch coverage differs from the completed receipt')
    raw = (output / 'vectors.jsonl').read_bytes()
    if sha256(raw) != receipt.get('vectors_sha256'):
        raise ValueError('Vector export checksum differs from the receipt')
    records = [json.loads(line) for line in raw.decode('utf-8').splitlines()]
    if records != [record for batch in batches for record in batch['records']]:
        raise ValueError('Vector export differs from ordered source batches')
    normalized = [unit_vector(record['embedding'], config['dimensions']) for record in records]
    return chunks, normalized, identity, receipt


def query_vector(question: str, identity: str, config: dict, cache_dir: Path, client, *, offline=False):
    if not isinstance(question, str) or not question.strip() or len(question) > 500:
        raise ValueError('Question must contain between 1 and 500 characters')
    question = question.strip()
    # Explicit encoding version prevents reuse after changing query instructions.
    basis = {'corpus_identity': identity, 'model': config['model'], 'dimensions': config['dimensions'],
             'encoding': 'compatible_plain_text_v1', 'question': question}
    key = sha256(json.dumps(basis, ensure_ascii=False, sort_keys=True).encode('utf-8'))
    path = cache_dir / f'{key}.json'
    if path.exists():
        cached = json.loads(path.read_text(encoding='utf-8'))
        if cached.get('basis') != basis:
            raise ValueError('Cached question identity differs')
        content = {k: v for k, v in cached.items() if k != 'content_sha256'}
        if cached.get('content_sha256') != sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode('utf-8')):
            raise ValueError('Cached query checksum differs')
        validate_response({'model': config['model'], 'data': [{'index': 0, 'embedding': cached['vector']}],
                           'usage': cached['usage']}, 1, config['dimensions'], config['model'])
        return cached['vector'], {'cache_hit': True, 'http_attempts': 0, 'usage_this_run': {}}
    if offline:
        raise ValueError('No cached vector for this question; --offline forbids API calls')
    before = client.attempts
    result = client.embed([question])
    if len(result['vectors']) != 1:
        raise ValueError('Expected exactly one query vector')
    unit_vector(result['vectors'][0], config['dimensions'])
    cached = {'basis': basis, 'vector': result['vectors'][0], 'usage': result['usage']}
    cached['content_sha256'] = sha256(json.dumps(cached, sort_keys=True, ensure_ascii=False).encode('utf-8'))
    cache_dir.mkdir(parents=True, exist_ok=True)
    write_json(path, cached)
    return result['vectors'][0], {'cache_hit': False, 'http_attempts': client.attempts - before,
                                 'usage_this_run': result['usage']}


def run_query(question, chunks, normalized, identity, config, client, cache_dir,
              *, top_k=10, offline=False, expand_top=0, expansion_limits=None,
              rerank_config=None, rerank_client=None, rerank_cache_dir=None, keyword=True):
    if not 1 <= top_k <= 20:
        raise ValueError('top_k must be between 1 and 20')
    if keyword and top_k > 19:
        raise ValueError('Use at most 19 vector candidates with keyword supplementation, or --no-keyword')
    final_count = min(top_k, len(chunks), rerank_config['top_n']) if rerank_config else min(top_k, len(chunks))
    if rerank_config and (rerank_client is None or rerank_cache_dir is None):
        raise ValueError('Reranking requires a client and cache directory')
    if not 0 <= expand_top <= min(8, final_count):
        raise ValueError('expand_top must be between 0 and min(8, final result count)')
    vector, query_info = query_vector(question, identity, config, cache_dir, client, offline=offline)
    hits = cosine_top_k(vector, normalized, chunks, top_k)
    vector_hits = list(hits)
    if keyword:
        hits, keyword_info = supplement_one(question, hits, chunks)
    report = {'question': question.strip(), 'model': config['model'], 'dimensions': config['dimensions'],
              'corpus_identity': identity, 'corpus_chunks': len(chunks), 'algorithm': 'exact_cosine',
              'top_k': top_k, 'query_embedding': query_info, 'hits': hits,
              'score_note': 'Cosine similarity is a ranking score, not an answer probability.',
              'query_encoding': 'compatible_plain_text_v1', 'reranker_used': False,
              'answer_model_used': False}
    if keyword:
        report.update(keyword_retrieval=keyword_info, vector_candidates=vector_hits,
                      algorithm='exact_cosine_plus_keyword')
    if rerank_config:
        report['vector_candidates'] = vector_hits
        report['rerank_candidates'] = hits
        hits, metadata = rerank_hits(question, hits, {c['chunk_id']: c for c in chunks}, identity,
                                     rerank_config, rerank_client, rerank_cache_dir, offline=offline)
        report.update(hits=hits, reranking=metadata, reranker_used=True,
                      algorithm='exact_cosine_plus_keyword_then_rerank' if keyword else 'exact_cosine_then_rerank',
                      final_top_n=len(hits))
    if expand_top:
        report['answer_context'] = expand_context([h['chunk_id'] for h in hits[:expand_top]],
                                                   {c['chunk_id']: c for c in chunks}, expansion_limits)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--question', action='append', help='Repeat for up to 8 questions; omitted = prompt once')
    parser.add_argument('--top-k', type=int, default=10, help='Vector candidates before reranking (default 10, max 20)')
    parser.add_argument('--no-rerank', action='store_true', help='Disable reranking; combine --no-keyword for cosine-only')
    parser.add_argument('--no-keyword', action='store_true', help='Disable the single keyword supplement')
    parser.add_argument('--expand-top', type=int, default=0, help='Expand final hits separately under existing limits')
    parser.add_argument('--offline', action='store_true', help='Only cached query vectors and reranking; no API calls')
    args = parser.parse_args()
    questions = args.question or [input('请输入问题：').strip()]
    if not 1 <= len(questions) <= 8 or any(not q.strip() or len(q) > 500 for q in questions):
        raise ValueError('Provide 1..8 questions, each 1..500 characters long')
    config = load_config(ROOT)
    # Query runs have their own stricter total HTTP-attempt cap.
    query_config = {**config, 'max_requests_per_run': 24}
    key, base_url = load_local_credentials(ROOT)
    if not args.offline and (not key or any(c.isspace() for c in key)):
        raise ValueError('Configure the local DashScope API key before querying')
    chunks, normalized, identity, receipt = load_corpus(ROOT, config, base_url)
    client = EmbeddingClient(base_url, key, query_config)
    rerank_config = None if args.no_rerank else load_rerank_config(ROOT)
    rerank_client = RerankClient(base_url, key, rerank_config) if rerank_config else None
    limits = json.loads((ROOT / 'config/table-context-v1.json').read_text(encoding='utf-8'))['expansion']
    reports = []
    for question in questions:
        report = run_query(question, chunks, normalized, identity, query_config, client,
                           ROOT / '.runtime/query-embeddings-v1', top_k=args.top_k, offline=args.offline,
                           expand_top=args.expand_top, expansion_limits=limits,
                           rerank_config=rerank_config, rerank_client=rerank_client,
                           rerank_cache_dir=ROOT / '.runtime/reranking-v1', keyword=not args.no_keyword)
        reports.append(report)
        print(f'\n问题：{question}')
        print(f'算法：{report["algorithm"]}；查询缓存：{report["query_embedding"]["cache_hit"]}')
        if report['reranker_used']:
            meta = report['reranking']
            print(f'重排：{meta["model"]}；候选 {meta["candidate_count"]} → 返回 {meta["returned_count"]}；缓存：{meta["cache_hit"]}')
        for hit in report['hits']:
            title = hit['text'].splitlines()[0]
            pages = ','.join(map(str, hit['pdf_pages']))
            origin = f'原向量第 {hit["cosine_rank"]} 名' if hit.get('cosine_rank') else '关键词补充'
            score = (f'重排 {hit["rerank_score"]:.6f} / {origin}' if 'rerank_score' in hit else
                     f'关键词 {hit["keyword_score"]:.6f}' if 'keyword_score' in hit else
                     f'余弦 {hit["cosine_similarity"]:.6f}')
            print(f'{hit["rank"]:2d}. {score}  {hit["chunk_id"]}  PDF {pages}  {title}')
        if 'answer_context' in report:
            context = report['answer_context']
            print(f'补读：{context["added_chunk_ids"]}；字符：{context["total_chars"]}；完整：{context["complete"]}')
        print(f'本次查询用量：{report["query_embedding"]["usage_this_run"]}')
        if report['reranker_used']:
            print(f'本次重排用量：{report["reranking"]["usage_this_run"]}')
    output = ROOT / 'output/retrieval-v1'
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / 'last_run.json', {'schema_version': 2,
               'run_at_utc': datetime.now(timezone.utc).isoformat(),
               'vectors_sha256': receipt['vectors_sha256'], 'reports': reports})
    print(f'\n完整结果：{output / "last_run.json"}')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Stopped.', file=sys.stderr)
        sys.exit(130)
    except (ApiFailure, ValueError, OSError) as error:
        print(f'Error: {error}' if isinstance(error, (ApiFailure, ValueError)) else
              f'Local file operation failed ({type(error).__name__}).', file=sys.stderr)
        sys.exit(1)
