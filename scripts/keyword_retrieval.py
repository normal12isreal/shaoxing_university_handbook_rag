"""Small in-memory BM25 index: exactly one lexical candidate, with no backfill."""

from collections import Counter
import math
import re


def terms(text: str) -> list[str]:
    """Chinese overlapping bigrams and Latin/number tokens; no extra dependency."""
    result = []
    for token in re.findall(r'[\u4e00-\u9fff]+|[a-z0-9]+', text.lower()):
        if re.fullmatch(r'[\u4e00-\u9fff]+', token) and len(token) > 1:
            result.extend(token[i:i + 2] for i in range(len(token) - 1))
        else:
            result.append(token)
    return result


def keyword_top_one(question: str, chunks: list[dict]):
    documents = [Counter(terms(c.get('retrieval_text', c['text']))) for c in chunks]
    query = sorted(set(terms(question)))
    if not documents or not query:
        return None
    lengths = [sum(d.values()) for d in documents]
    average = sum(lengths) / len(lengths) or 1
    frequencies = Counter(t for d in documents for t in query if t in d)
    scores = []
    for position, doc in enumerate(documents):
        score = 0.0
        for term in query:
            tf = doc.get(term, 0)
            if tf:
                idf = math.log(1 + (len(documents) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
                score += idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * lengths[position] / average))
        scores.append(score)
    position = max(range(len(scores)), key=lambda i: (scores[i], -i))
    if scores[position] <= 0:
        return None
    return {**{k: chunks[position][k] for k in ('chunk_id', 'text', 'source_file', 'pdf_pages')},
            'keyword_score': scores[position], 'keyword_rank': 1, 'retrieval_route': 'keyword',
            'cosine_rank': None, 'cosine_similarity': None}


def supplement_one(question: str, vector_hits: list[dict], chunks: list[dict]):
    candidate = keyword_top_one(question, chunks)
    merged = list(vector_hits)
    duplicate = bool(candidate and any(
        candidate['chunk_id'] == hit['chunk_id']
        or re.sub(r'\s+', '', candidate['text']) == re.sub(r'\s+', '', hit['text']) for hit in merged))
    if candidate and not duplicate:
        if len(merged) >= 20:
            raise ValueError('Keyword supplementation requires at most 19 vector candidates')
        merged.append({**candidate, 'rank': len(merged) + 1})
    return merged, {'algorithm': 'bm25_cjk_bigram', 'requested_count': 1,
                    'hit_chunk_id': candidate['chunk_id'] if candidate else None,
                    'score': candidate['keyword_score'] if candidate else None,
                    'duplicate': duplicate, 'added_count': int(bool(candidate and not duplicate)),
                    'note': 'Only the best keyword hit is considered; duplicates are not backfilled.'}
