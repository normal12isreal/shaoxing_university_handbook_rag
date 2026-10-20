"""Small standard-library client for the DashScope compatible embedding API."""

from __future__ import annotations

import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request


class ApiFailure(RuntimeError):
    """Safe error text: never include request headers or remote response bodies."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_base_url(base_url: str) -> str:
    base_url = base_url.rstrip('/')
    parsed = urllib.parse.urlsplit(base_url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.port not in (None, 443)
            or not (parsed.hostname.endswith('.cn-beijing.maas.aliyuncs.com')
                    or parsed.hostname == 'dashscope.aliyuncs.com')
            or parsed.path != '/compatible-mode/v1'):
        raise ValueError('Base URL must be a Beijing DashScope HTTPS compatible endpoint')
    return base_url


def validate_response(response: dict, count: int, dimensions: int, model: str):
    if response.get('model') != model:
        raise ValueError('API returned an unexpected model')
    data = response.get('data')
    if not isinstance(data, list) or len(data) != count:
        raise ValueError('API vector count differs from the input batch')
    ordered = [None] * count
    for item in data:
        index = item.get('index')
        if type(index) is not int or not 0 <= index < count or ordered[index] is not None:
            raise ValueError('Missing, duplicate or invalid API response index')
        vector = item.get('embedding')
        if (not isinstance(vector, list) or len(vector) != dimensions
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector)
                or not any(v != 0 for v in vector)):
            raise ValueError('API returned an invalid, non-finite or zero embedding vector')
        ordered[index] = vector
    usage = response.get('usage', {})
    for key in ('prompt_tokens', 'total_tokens'):
        if key in usage and (type(usage[key]) is not int or usage[key] < 0):
            raise ValueError('Invalid API usage count')
    return ordered, {key: usage[key] for key in ('prompt_tokens', 'total_tokens') if key in usage}


class EmbeddingClient:
    def __init__(self, base_url: str, api_key: str, config: dict, *, opener=None, sleep=time.sleep):
        self.base_url = validate_base_url(base_url)
        self.api_key = api_key
        self.config = config
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.sleep = sleep
        self.attempts = 0

    def embed(self, texts: list[str]) -> dict:
        payload = {'model': self.config['model'], 'input': texts,
                   'dimensions': self.config['dimensions'], 'encoding_format': 'float'}
        last = 'Request failed'
        for attempt in range(self.config['max_attempts']):
            if self.attempts >= self.config['max_requests_per_run']:
                raise ApiFailure('Request budget reached; saved batches can be resumed on the next run')
            self.attempts += 1
            request = urllib.request.Request(self.base_url + '/embeddings',
                        data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                        headers={'Authorization': 'Bearer ' + self.api_key,
                                 'Content-Type': 'application/json'}, method='POST')
            try:
                with self.opener.open(request, timeout=self.config['timeout_seconds']) as response:
                    raw = response.read(8_000_001)
                if len(raw) > 8_000_000:
                    raise ApiFailure('Response exceeds the 8 MB safety limit')
                try:
                    parsed = json.loads(raw)
                    vectors, usage = validate_response(parsed, len(texts), self.config['dimensions'], self.config['model'])
                except (ValueError, TypeError, AttributeError, OverflowError):
                    raise ApiFailure('Invalid embedding response; batch was not saved') from None
                return {'model': self.config['model'], 'vectors': vectors, 'usage': usage}
            except urllib.error.HTTPError as error:
                status = error.code
                raw = error.read(4096)
                error.close()
                try:
                    code = json.loads(raw).get('error', {}).get('code', '')
                except (ValueError, AttributeError):
                    code = ''
                code = code if isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', code) else 'unknown'
                last = f'HTTP {status}; API error code: {code}'
                if status not in (408, 429, 500, 502, 503, 504):
                    raise ApiFailure(last + '; no automatic retry') from None
            except (urllib.error.URLError, TimeoutError, OSError):
                last = 'Network or TLS connection failed'
            if attempt + 1 < self.config['max_attempts']:
                self.sleep(min(2 ** attempt, 4))
        raise ApiFailure(last + '; retry limit reached; completed batches remain saved')
