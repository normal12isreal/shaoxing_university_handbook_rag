"""Atomic batch persistence, ordered resume and source identity verification."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path

if __package__:
    from .embedding_api import validate_response
else:
    from embedding_api import validate_response


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def batch_digest(batch: dict) -> str:
    content = {key: value for key, value in batch.items() if key != 'content_sha256'}
    return sha256(json.dumps(content, sort_keys=True, ensure_ascii=False, allow_nan=False).encode('utf-8'))


def write_json(path: Path, value: object) -> None:
    temp = path.with_suffix(path.suffix + '.tmp')
    with temp.open('w', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


@contextmanager
def output_lock(output: Path):
    """OS-owned lock releases even if the Python process crashes."""
    output.mkdir(parents=True, exist_ok=True)
    with (output / 'run.lock').open('a+b') as stream:
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError('Another embedding process is using this output directory') from None
        try:
            yield
        finally:
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def validate_batch(batch: dict, source: list[dict], start: int, config: dict, identity: str):
    if batch.get('content_sha256') != batch_digest(batch):
        raise ValueError('Saved batch content checksum differs; refusing to reuse its vectors')
    records = batch.get('records')
    if (batch.get('identity') != identity or batch.get('start') != start
            or batch.get('model') != config['model'] or batch.get('dimensions') != config['dimensions']
            or not isinstance(records, list) or not 0 < len(records) <= config['batch_size']
            or start + len(records) > len(source)):
        raise ValueError('Saved batch does not match the source/configuration')
    for index, record in enumerate(records):
        expected = source[start + index]
        if (record.get('position') != start + index or record.get('chunk_id') != expected['chunk_id']
                or record.get('text_sha256') != sha256(expected[config['text_field']].encode('utf-8'))):
            raise ValueError('Saved vectors are out of order or source text has changed')
    validate_response({'model': batch['model'], 'data': [
        {'index': i, 'embedding': r['embedding']} for i, r in enumerate(records)],
        'usage': batch.get('usage', {})}, len(records), config['dimensions'], config['model'])


def saved_batches(output: Path, source: list[dict], config: dict, identity: str):
    batches, cursor = [], 0
    for path in sorted((output / 'batches').glob('*.json')):
        if path.name != f'{cursor:06d}.json':
            raise ValueError('Saved batch filenames have a gap or unexpected ordering')
        batch = json.loads(path.read_text(encoding='utf-8'))
        validate_batch(batch, source, cursor, config, identity)
        batches.append(batch)
        cursor += len(batch['records'])
    return batches, cursor


def persist_batch(output: Path, source: list[dict], start: int, result: dict, config: dict, identity: str):
    batch = {'schema_version': 1, 'identity': identity, 'model': config['model'],
             'dimensions': config['dimensions'], 'start': start, 'usage': result['usage'],
             'records': [{'position': start + i, 'chunk_id': source[start + i]['chunk_id'],
                          'text_sha256': sha256(source[start + i][config['text_field']].encode('utf-8')),
                          'embedding': vector} for i, vector in enumerate(result['vectors'])]}
    batch['content_sha256'] = batch_digest(batch)
    validate_batch(batch, source, start, config, identity)
    directory = output / 'batches'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{start:06d}.json'
    if path.exists():
        raise ValueError('Refusing to overwrite an existing batch')
    write_json(path, batch)
    return batch


def export_vectors(output: Path, batches: list[dict]) -> str:
    path = output / 'vectors.jsonl'
    temp = output / 'vectors.jsonl.tmp'
    with temp.open('w', encoding='utf-8', newline='\n') as stream:
        for batch in batches:
            for record in batch['records']:
                stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    return sha256(path.read_bytes())
