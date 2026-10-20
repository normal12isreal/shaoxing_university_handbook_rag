"""Bounded DashScope text reranking; independent of the embedding endpoint."""

from __future__ import annotations

import json
import math
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request

if __package__:
    from .embedding_api import ApiFailure, NoRedirect, validate_base_url
else:
    from embedding_api import ApiFailure, NoRedirect, validate_base_url


def load_rerank_config(root: Path) -> dict:
    config = json.loads((root / 'config/reranking-v1.json').read_text(encoding='utf-8'))
    if (config.get('schema_version') != 1 or config.get('model') != 'qwen3.7-text-rerank'
            or config.get('text_field') != 'retrieval_text'
            or not isinstance(config.get('instruct'), str) or not 1 <= len(config['instruct']) <= 500):
        raise ValueError('Unsupported reranking configuration')
    for key, maximum in {'top_n': 8, 'timeout_seconds': 30, 'max_attempts': 3,
                         'max_requests_per_run': 24}.items():
        if type(config.get(key)) is not int or not 1 <= config[key] <= maximum:
            raise ValueError(f'Reranking {key} must be between 1 and {maximum}')
    return config


def rerank_endpoint(base_url: str) -> str:
    parsed = urllib.parse.urlsplit(validate_base_url(base_url))
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc,
                                   '/api/v1/services/rerank/text-rerank/text-rerank', '', ''))


def validate_input(question: str, documents: list[str]) -> None:
    if not isinstance(question, str) or not question.strip() or len(question) > 500:
        raise ValueError('Reranking question must contain 1..500 characters')
    if not isinstance(documents, list) or not 1 <= len(documents) <= 20:
        raise ValueError('Reranking requires 1..20 candidate documents')
    if any(not isinstance(d, str) or not d.strip() or len(d) > 2000 for d in documents):
        raise ValueError('Reranking documents must contain 1..2000 characters each')


def validate_rerank_response(response: dict, count: int) -> tuple[list[dict], dict]:
    """Request all candidates so index coverage can be verified before selecting five."""
    if not isinstance(response, dict) or response.get('code'):
        raise ValueError('Invalid reranking response')
    results = response.get('output', {}).get('results')
    if not isinstance(results, list) or len(results) != count:
        raise ValueError('Reranking result count differs from the candidate count')
    seen, ranked = set(), []
    for item in results:
        index, score = item.get('index'), item.get('relevance_score')
        if type(index) is not int or not 0 <= index < count or index in seen:
            raise ValueError('Duplicate, missing or invalid reranking index')
        if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
            raise ValueError('Invalid reranking relevance score')
        seen.add(index)
        ranked.append({'index': index, 'relevance_score': score})
    # Stable ties use the original cosine rank, not remote response order.
    ranked.sort(key=lambda r: (-r['relevance_score'], r['index']))
    usage = response.get('usage')
    if not isinstance(usage, dict) or type(usage.get('total_tokens')) is not int or usage['total_tokens'] < 0:
        raise ValueError('Missing or invalid reranking usage')
    if 'prompt_tokens' in usage and (type(usage['prompt_tokens']) is not int or usage['prompt_tokens'] < 0):
        raise ValueError('Invalid reranking prompt usage')
    return ranked, {k: usage[k] for k in ('prompt_tokens', 'total_tokens') if k in usage}


class RerankClient:
    def __init__(self, base_url: str, api_key: str, config: dict, *, opener=None, sleep=time.sleep):
        self.endpoint = rerank_endpoint(base_url)
        self.api_key = api_key
        self.config = config
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.sleep = sleep
        self.attempts = 0

    def rerank(self, question: str, documents: list[str]) -> dict:
        validate_input(question, documents)
        payload = {'model': self.config['model'], 'input': {'query': question, 'documents': documents},
                   'parameters': {'top_n': len(documents), 'instruct': self.config['instruct']}}
        last = 'Reranking request failed'
        for attempt in range(self.config['max_attempts']):
            if self.attempts >= self.config['max_requests_per_run']:
                raise ApiFailure('Reranking HTTP request budget reached')
            self.attempts += 1
            request = urllib.request.Request(self.endpoint,
                       data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                       headers={'Authorization': 'Bearer ' + self.api_key,
                                'Content-Type': 'application/json'}, method='POST')
            try:
                with self.opener.open(request, timeout=self.config['timeout_seconds']) as response:
                    raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ApiFailure('Reranking response exceeds 1 MB')
                try:
                    results, usage = validate_rerank_response(json.loads(raw), len(documents))
                except (ValueError, TypeError, AttributeError, OverflowError):
                    raise ApiFailure('Invalid reranking response; result was not saved') from None
                return {'results': results, 'usage': usage}
            except urllib.error.HTTPError as error:
                status = error.code
                raw = error.read(4096)
                error.close()
                try:
                    body = json.loads(raw)
                    code = body.get('code') or body.get('error', {}).get('code', '')
                except (ValueError, AttributeError):
                    code = ''
                code = code if isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', code) else 'unknown'
                last = f'Reranking HTTP {status}; API error code: {code}'
                if status not in (408, 429, 500, 502, 503, 504):
                    raise ApiFailure(last + '; no automatic retry') from None
            except (urllib.error.URLError, TimeoutError, OSError):
                last = 'Reranking network or TLS connection failed'
            if attempt + 1 < self.config['max_attempts']:
                self.sleep(min(2 ** attempt, 4))
        raise ApiFailure(last + '; retry limit reached')
