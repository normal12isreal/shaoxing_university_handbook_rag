"""Bounded JSON POST shared by the answer and native web-search clients."""

import json
import re
import urllib.error
import urllib.request

if __package__:
    from .embedding_api import ApiFailure
else:
    from embedding_api import ApiFailure


def post_json(opener, endpoint, api_key, payload, timeout):
    request = urllib.request.Request(endpoint,
                data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                headers={'Authorization': 'Bearer ' + api_key, 'Content-Type': 'application/json'},
                method='POST')
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ApiFailure('API response exceeds 1 MB')
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError('Expected JSON object')
        return body
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
        raise ApiFailure(f'HTTP {status}; API error code: {code}; no automatic retry') from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ApiFailure('API network or TLS connection failed; no automatic retry') from None
    except (ValueError, TypeError):
        raise ApiFailure('Invalid API JSON response; no automatic retry') from None
