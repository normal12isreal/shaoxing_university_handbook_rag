"""Cache candidate-level reranking and map API indices back to source chunks."""

from __future__ import annotations

import json

if __package__:
    from .embedding_store import sha256, write_json
    from .rerank_api import validate_input, validate_rerank_response
else:
    from embedding_store import sha256, write_json
    from rerank_api import validate_input, validate_rerank_response


def digest(value) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8'))


def rerank_hits(question, candidates, chunks_by_id, identity, config, client, cache_dir, *, offline=False):
    question = question.strip()
    documents = [chunks_by_id[h['chunk_id']][config['text_field']] for h in candidates]
    validate_input(question, documents)
    basis = {'schema_version': 1, 'corpus_identity': identity, 'endpoint': client.endpoint,
             'model': config['model'], 'instruct': config['instruct'], 'question': question,
             'text_field': config['text_field'], 'candidate_ids': [h['chunk_id'] for h in candidates],
             'document_sha256': [sha256(d.encode('utf-8')) for d in documents]}
    path = cache_dir / f'{digest(basis)}.json'
    if path.exists():
        cached = json.loads(path.read_text(encoding='utf-8'))
        content = {k: v for k, v in cached.items() if k != 'content_sha256'}
        if cached.get('basis') != basis or cached.get('content_sha256') != digest(content):
            raise ValueError('Reranking cache identity or checksum differs')
        result = cached['result']
        cache_hit, attempts = True, 0
    else:
        if offline:
            raise ValueError('No cached reranking for these candidates; --offline forbids API calls')
        before = client.attempts
        result = client.rerank(question, documents)
        cache_hit, attempts = False, client.attempts - before
    ranked, usage = validate_rerank_response({'output': {'results': result['results']},
                                            'usage': result['usage']}, len(candidates))
    if not cache_hit:
        cached = {'basis': basis, 'result': {'results': ranked, 'usage': usage}}
        cached['content_sha256'] = digest(cached)
        cache_dir.mkdir(parents=True, exist_ok=True)
        write_json(path, cached)
    hits = []
    for rank, item in enumerate(ranked[:config['top_n']], 1):
        original = candidates[item['index']]
        hits.append({**original, 'cosine_rank': original['rank'], 'rank': rank,
                     'rerank_score': item['relevance_score']})
    metadata = {'model': config['model'], 'text_field': config['text_field'],
                'instruct': config['instruct'], 'candidate_count': len(candidates),
                'top_n': config['top_n'], 'returned_count': len(hits),
                'cache_hit': cache_hit, 'http_attempts': attempts,
                'usage_this_run': {} if cache_hit else usage,
                'candidate_scores': ranked,
                'score_note': 'Relevance scores rank this candidate set; not probabilities or cosine scores.'}
    return hits, metadata
