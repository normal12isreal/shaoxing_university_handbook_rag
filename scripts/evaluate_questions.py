"""Run the fixed ten questions through existing RAG components, with resumable records."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from time import perf_counter

if __package__:
    from .answer_question import build_messages, citation_audit
    from .context_expansion import expand_context
    from .embed_chunks import ROOT, load_config, load_local_credentials
    from .embedding_api import ApiFailure, EmbeddingClient
    from .embedding_store import output_lock, sha256, write_json
    from .generation_api import GenerationClient, load_generation_config
    from .keyword_retrieval import keyword_top_one
    from .rerank_api import RerankClient, load_rerank_config
    from .rerank_chunks import rerank_hits
    from .retrieve_chunks import load_corpus, run_query
else:
    from answer_question import build_messages, citation_audit
    from context_expansion import expand_context
    from embed_chunks import ROOT, load_config, load_local_credentials
    from embedding_api import ApiFailure, EmbeddingClient
    from embedding_store import output_lock, sha256, write_json
    from generation_api import GenerationClient, load_generation_config
    from keyword_retrieval import keyword_top_one
    from rerank_api import RerankClient, load_rerank_config
    from rerank_chunks import rerank_hits
    from retrieve_chunks import load_corpus, run_query

DATASET = ROOT / 'data/evaluation/sample-v1'


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8'))


def save_record(path, record):
    record['content_sha256'] = digest({k: v for k, v in record.items() if k != 'content_sha256'})
    write_json(path, record)


def read_record(path):
    record = json.loads(path.read_text(encoding='utf-8'))
    if record.get('content_sha256') != digest({k: v for k, v in record.items() if k != 'content_sha256'}):
        raise ValueError('Evaluation record checksum differs')
    return record


def load_dataset():
    manifest = json.loads((DATASET / 'manifest.json').read_text(encoding='utf-8'))
    raw = (DATASET / 'questions.jsonl').read_bytes()
    rules = json.loads((DATASET / 'scoring_rules.json').read_text(encoding='utf-8'))
    questions = [json.loads(line) for line in raw.decode('utf-8').splitlines()]
    if sha256(raw) != manifest['questions_sha256'] or [q['question_id'] for q in questions] != rules['question_ids']:
        raise ValueError('Question set changed; review and version it before evaluation')
    if len(questions) != 10 or rules['expand_dataset']:
        raise ValueError('Only the fixed ten-question evaluation is supported')
    for source in manifest['sources']:
        if sha256((ROOT / source['path']).read_bytes()) != source['sha256']:
            raise ValueError('Evaluation source changed; evidence mapping needs review')
    expected = {'vector_top_k': 10, 'keyword_top_k': 1, 'deduplicate_keyword_hit': True,
                'backfill_duplicate_keyword_hit': False, 'rerank_top_n': 5,
                'expand_related_context': True, 'crag_enabled': False, 'web_enabled': False}
    if any(rules['baseline'].get(k) != v for k, v in expected.items()):
        raise ValueError('This runner implements the agreed baseline only')
    return questions, manifest, rules


def keyword_only_query(question, chunks, identity, rerank, client):
    """Ablation: keep the existing single BM25 hit; never encode a query vector."""
    candidate = keyword_top_one(question, chunks)
    candidates = [{**candidate, 'rank': 1}] if candidate else []
    hits, metadata = (rerank_hits(question, candidates, {c['chunk_id']: c for c in chunks},
        identity, rerank, client, ROOT / '.runtime/reranking-v1') if candidates else
        ([], {'usage_this_run': {}, 'http_attempts': 0, 'candidate_count': 0}))
    return {'question': question, 'retrieval_mode': 'keyword_only',
            'algorithm': 'bm25_cjk_bigram_then_rerank', 'corpus_identity': identity,
            'corpus_chunks': len(chunks), 'top_k': 1, 'vector_candidates': [],
            'query_embedding': {'status': 'disabled', 'usage_this_run': {}, 'http_attempts': 0},
            'keyword_candidates': candidates, 'rerank_candidates': candidates,
            'hits': hits, 'reranking': metadata, 'reranker_used': bool(candidates),
            'final_top_n': len(hits), 'answer_model_used': False}


def execute(output, limit, retrieval_mode='hybrid'):
    if retrieval_mode not in ('hybrid', 'keyword_only'):
        raise ValueError('Unsupported retrieval mode')
    questions, manifest, rules = load_dataset()
    config = {**load_config(ROOT), 'max_requests_per_run': 3}
    rerank = {**load_rerank_config(ROOT), 'max_requests_per_run': 3}
    generation = load_generation_config(ROOT)
    if rerank['top_n'] != 5:
        raise ValueError('The agreed baseline returns five reranked chunks')
    key, base_url = load_local_credentials(ROOT)
    if not key:
        raise ValueError('Configure local model credentials first')
    chunks, normalized, identity, receipt = load_corpus(ROOT, config, base_url)
    by_id = {c['chunk_id']: c for c in chunks}
    limits = json.loads((ROOT / 'config/table-context-v1.json').read_text(encoding='utf-8'))['expansion']
    prompt = (ROOT / 'config/answer-system-v1.txt').read_text(encoding='utf-8')
    source_names = ['answer_question.py', '../student_handbook_rag/prompts.py',
                    'context_expansion.py', 'retrieve_chunks.py',
                    'rerank_chunks.py', 'keyword_retrieval.py', 'generation_api.py',
                    'embedding_api.py', 'rerank_api.py', 'evaluate_questions.py']
    basis = {'questions_sha256': manifest['questions_sha256'],
             'rules_sha256': sha256((DATASET / 'scoring_rules.json').read_bytes()),
             'corpus_identity': identity, 'vectors_sha256': receipt['vectors_sha256'],
             'embedding': config, 'rerank': rerank, 'generation': generation,
             'expansion': limits, 'system_prompt_sha256': sha256(prompt.encode('utf-8')),
             'source_hashes': {n: sha256((ROOT / 'scripts' / n).read_bytes()) for n in source_names},
             'retrieval_mode': retrieval_mode,
             'candidate_budget': {'vector': 10 if retrieval_mode == 'hybrid' else 0, 'keyword': 1},
             'crag_enabled': False, 'web_enabled': False}
    run_identity = digest(basis)
    manifest_path = output / 'manifest.json'
    if manifest_path.exists():
        prior = json.loads(manifest_path.read_text(encoding='utf-8'))
        if prior['identity'] != run_identity or prior['basis'] != basis:
            raise ValueError('Evaluation identity changed; choose a new output directory')
    else:
        write_json(manifest_path, {'identity': run_identity, 'basis': basis,
                   'created_at': datetime.now(timezone.utc).isoformat(), 'dataset_count': 10})
    records_dir = output / 'records'
    records_dir.mkdir(exist_ok=True)
    for q in questions[:limit]:
        qid, question = q['question_id'], q['question']
        path = records_dir / f'{qid}.json'
        record = read_record(path) if path.exists() else {
            'question_id': qid, 'question': question, 'identity': run_identity,
            'status': 'pending', 'retrieval': None, 'sources': [],
            'timing_seconds': {}, 'attempts': {'embedding': 0, 'rerank': 0, 'generation': 0}}
        if record['identity'] != run_identity or record['question'] != question:
            raise ValueError('Saved question identity differs')
        if record['status'] == 'completed':
            print(f'{qid}: reuse completed', flush=True)
            continue
        phase = 'retrieval'
        embed_client = EmbeddingClient(base_url, key, config)
        rerank_client = RerankClient(base_url, key, rerank)
        answer_client = GenerationClient(base_url, key, generation)
        try:
            print(f'{qid}: retrieving', flush=True)
            if record['retrieval'] is None:
                start = perf_counter()
                try:
                    if retrieval_mode == 'keyword_only':
                        record['retrieval'] = keyword_only_query(question, chunks, identity, rerank, rerank_client)
                    else:
                        record['retrieval'] = run_query(question, chunks, normalized, identity, config,
                            embed_client, ROOT / '.runtime/query-embeddings-v1', top_k=10,
                            rerank_config=rerank, rerank_client=rerank_client,
                            rerank_cache_dir=ROOT / '.runtime/reranking-v1')
                finally:
                    record['timing_seconds']['retrieval'] = perf_counter() - start
                save_record(path, record)
            phase = 'context'
            start = perf_counter()
            seed_ids = [h['chunk_id'] for h in record['retrieval']['hits']]
            record['answer_context'] = (expand_context(seed_ids, by_id, limits) if seed_ids else
                {'chunks': [], 'seed_chunk_ids': [], 'added_chunk_ids': [], 'omitted_chunk_ids': [],
                 'relations_not_examined': 0, 'complete': True, 'total_chars': 0,
                 'limits': limits, 'expansion_hops': 1})
            record['timing_seconds']['context'] = perf_counter() - start
            messages, sources = (build_messages(question, record['answer_context'], prompt)
                                 if record['answer_context']['chunks'] else ([], []))
            record.update(messages=messages, sources=sources, status='prepared')
            save_record(path, record)
            phase = 'generation'
            print(f'{qid}: generating', flush=True)
            start = perf_counter()
            try:
                record['generation'] = (answer_client.generate(messages) if messages else
                    {'answer': '现有资料没有提供足够依据，暂时无法确定这个问题的答案。',
                     'complete': True, 'usage': {}, 'response_model': None, 'finish_reason': 'no_evidence'})
            finally:
                record['timing_seconds']['generation'] = perf_counter() - start
            record['citation_audit'] = citation_audit(record['generation']['answer'], len(sources))
            record['status'] = 'completed' if record['generation']['complete'] else 'truncated'
            record.pop('error', None)
            record.pop('failed_phase', None)
        except (ApiFailure, ValueError, OSError) as exc:
            record.update(status='failed', failed_phase=phase,
                          error=str(exc) if isinstance(exc, ApiFailure) else type(exc).__name__)
        finally:
            for name, client in [('embedding', embed_client), ('rerank', rerank_client), ('generation', answer_client)]:
                record['attempts'][name] += client.attempts
            record['updated_at'] = datetime.now(timezone.utc).isoformat()
            save_record(path, record)
        print(f'{qid}: {record["status"]}' + (f' ({record["error"]})' if record.get('error') else ''), flush=True)
        if record.get('error', '').startswith(('HTTP 401', 'HTTP 402', 'HTTP 403')):
            print('Stopped after credential/account refusal; remaining questions not attempted.', flush=True)
            break
    records = [read_record(p) for p in sorted(records_dir.glob('q*.json'))]
    write_json(output / 'run_summary.json', {'identity': run_identity, 'expected_questions': 10,
        'completed': sum(r['status'] == 'completed' for r in records),
        'failed': sum(r['status'] == 'failed' for r in records),
        'truncated': sum(r['status'] == 'truncated' for r in records),
        'not_attempted': 10 - len(records), 'output': str(output)})
    print(json.dumps(json.loads((output / 'run_summary.json').read_text(encoding='utf-8')), ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'output/evaluation-v1')
    parser.add_argument('--limit', type=int, choices=range(1, 11), default=10)
    parser.add_argument('--retrieval-mode', choices=['hybrid', 'keyword_only'], default='hybrid')
    args = parser.parse_args()
    with output_lock(args.output):
        execute(args.output, args.limit, args.retrieval_mode)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
