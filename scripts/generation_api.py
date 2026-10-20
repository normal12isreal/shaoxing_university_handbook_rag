"""One non-thinking Qwen chat request; no automatic retries or hidden extra calls."""

from __future__ import annotations

import json
import math
from pathlib import Path
import re
import urllib.error
import urllib.request

if __package__:
    from .embedding_api import ApiFailure, NoRedirect, validate_base_url
else:
    from embedding_api import ApiFailure, NoRedirect, validate_base_url


def load_generation_config(root: Path) -> dict:
    config = json.loads((root / 'config/generation-v1.json').read_text(encoding='utf-8'))
    if (config.get('schema_version') != 1 or config.get('model') != 'qwen3.7-plus'
            or config.get('enable_thinking') is not False):
        raise ValueError('Unsupported answer model or thinking configuration')
    for key, maximum in {'max_completion_tokens': 3000, 'timeout_seconds': 60,
                         'max_prompt_chars': 16000}.items():
        if type(config.get(key)) is not int or not 1 <= config[key] <= maximum:
            raise ValueError(f'Generation {key} must be between 1 and {maximum}')
    if (type(config.get('temperature')) not in (int, float)
            or not math.isfinite(config['temperature']) or not 0 <= config['temperature'] <= 1):
        raise ValueError('Generation temperature must be between 0 and 1')
    return config


def validate_chat_response(body: dict, model: str) -> dict:
    returned = body.get('model')
    if not isinstance(returned, str) or not (returned == model or returned.startswith(model + '-')):
        raise ValueError('Unexpected answer model')
    choices = body.get('choices')
    if not isinstance(choices, list) or len(choices) != 1 or choices[0].get('index') != 0:
        raise ValueError('Expected exactly one answer')
    choice = choices[0]
    message = choice.get('message', {})
    text = message.get('content')
    if message.get('role') != 'assistant' or not isinstance(text, str) or not text.strip():
        raise ValueError('Missing answer text')
    if message.get('reasoning_content'):
        raise ValueError('Unexpected thinking content in non-thinking mode')
    finish = choice.get('finish_reason')
    if finish not in ('stop', 'length'):
        raise ValueError('Unexpected answer finish reason')
    usage = body.get('usage', {})
    for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
        if type(usage.get(key)) is not int or usage[key] < 0:
            raise ValueError('Missing or invalid answer usage')
    return {'answer': text, 'response_model': returned, 'finish_reason': finish,
            'complete': finish == 'stop',
            'usage': {k: usage[k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')}}


class GenerationClient:
    def __init__(self, base_url: str, api_key: str, config: dict, *, opener=None):
        self.endpoint = validate_base_url(base_url) + '/chat/completions'
        self.api_key = api_key
        self.config = config
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.attempts = 0

    def generate(self, messages: list[dict]) -> dict:
        if (not isinstance(messages, list) or len(messages) != 2
                or [m.get('role') for m in messages] != ['system', 'user']
                or any(not isinstance(m.get('content'), str) or not m['content'].strip() for m in messages)):
            raise ValueError('Expected a system prompt and one user message')
        if sum(len(m['content']) for m in messages) > self.config['max_prompt_chars']:
            raise ValueError('Answer prompt exceeds the configured character limit')
        if self.attempts >= 1:
            raise ApiFailure('Generation is limited to one HTTP attempt per run; no automatic retry')
        payload = {k: self.config[k] for k in ('model', 'enable_thinking', 'temperature', 'max_completion_tokens')}
        payload.update(messages=messages, stream=False)
        request = urllib.request.Request(self.endpoint,
                    data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                    headers={'Authorization': 'Bearer ' + self.api_key, 'Content-Type': 'application/json'},
                    method='POST')
        self.attempts += 1
        try:
            with self.opener.open(request, timeout=self.config['timeout_seconds']) as response:
                raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ApiFailure('Answer response exceeds 1 MB')
            try:
                return validate_chat_response(json.loads(raw), self.config['model'])
            except (ValueError, TypeError, AttributeError):
                raise ApiFailure('Invalid answer response; no automatic retry') from None
        except urllib.error.HTTPError as error:
            status = error.code
            raw = error.read(4096)
            error.close()
            try:
                code = json.loads(raw).get('error', {}).get('code', '')
            except (ValueError, AttributeError):
                code = ''
            code = code if isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', code) else 'unknown'
            raise ApiFailure(f'Answer HTTP {status}; API error code: {code}; no automatic retry') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ApiFailure('Answer network or TLS connection failed; no automatic retry') from None
