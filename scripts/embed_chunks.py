"""Send ordered local chunks to Qwen embeddings, checkpoint batches and resume."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

if __package__:
    from .embedding_api import ApiFailure, EmbeddingClient, validate_base_url
    from .embedding_store import export_vectors, output_lock, persist_batch, saved_batches, sha256, write_json
else:
    from embedding_api import ApiFailure, EmbeddingClient, validate_base_url
    from embedding_store import export_vectors, output_lock, persist_batch, saved_batches, sha256, write_json


ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path('output/linked-v1/chunks.jsonl')
OUTPUT = Path('output/embeddings-v1')


def load_config(root: Path) -> dict:
    config = json.loads((root / 'config/embedding-v1.json').read_text(encoding='utf-8'))
    if (config.get('schema_version') != 1 or config.get('model') != 'qwen3.7-text-embedding'
            or config.get('dimensions') not in (256, 512, 768, 1024, 1536, 2048, 2560)
            or config.get('text_field') != 'retrieval_text'):
        raise ValueError('Unsupported model/dimension/text field configuration')
    for key, maximum in {'batch_size': 20, 'timeout_seconds': 30, 'max_attempts': 3,
                         'max_requests_per_run': 90, 'max_chunks': 1000,
                         'max_text_chars': 2000, 'max_total_chars': 200000}.items():
        if type(config.get(key)) is not int or not 1 <= config[key] <= maximum:
            raise ValueError(f'{key} must be between 1 and {maximum}')
    if type(config.get('pause_seconds')) not in (int, float) or not 0 <= config['pause_seconds'] <= 5:
        raise ValueError('pause_seconds must be between 0 and 5')
    return config


def load_local_credentials(root: Path, *, api_key: str | None = None,
                           base_url: str | None = None) -> tuple[str, str]:
    path = root / '.runtime/dashscope.local.json'
    local = (json.loads(path.read_text(encoding='utf-8'))
             if (api_key is None or base_url is None) and path.exists() else {})
    key = api_key if api_key is not None else os.environ.get('DASHSCOPE_API_KEY') or local.get('api_key', '')
    url = base_url if base_url is not None else os.environ.get('DASHSCOPE_BASE_URL') or local.get('base_url', '')
    if not isinstance(key, str) or not isinstance(url, str):
        raise ValueError('API key and base URL must be strings')
    return key.strip(), validate_base_url(url.strip())


def load_source(root: Path, config: dict):
    raw = (root / SOURCE).read_bytes()
    receipt = json.loads((root / 'output/linked-v1/run.json').read_text(encoding='utf-8'))
    if sha256(raw) != receipt['artifact_sha256']['chunks.jsonl']:
        raise ValueError('Linked input differs from its receipt; rebuild/check context links first')
    source = [json.loads(line) for line in raw.decode('utf-8').splitlines()]
    if not source or len(source) > config['max_chunks']:
        raise ValueError('Source chunk count is outside the configured limit')
    ids = [chunk['chunk_id'] for chunk in source]
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate source chunk IDs')
    for chunk in source:
        text = chunk.get(config['text_field'])
        if not isinstance(text, str) or not text.strip() or len(text) > config['max_text_chars']:
            raise ValueError('Empty or overlong source text; no network request was sent')
    if sum(len(c[config['text_field']]) for c in source) > config['max_total_chars']:
        raise ValueError('Total source text exceeds the configured limit')
    return source, sha256(raw)


def identity_for(source_sha: str, config: dict, base_url: str):
    basis = {'source': SOURCE.as_posix(), 'source_sha256': source_sha,
             'base_url': base_url, 'model': config['model'], 'dimensions': config['dimensions'],
             'text_field': config['text_field'], 'batch_size': config['batch_size'], 'encoding_format': 'float'}
    return basis, sha256(json.dumps(basis, sort_keys=True).encode('utf-8'))


def update_receipt(output, basis, identity, batches, total, config, *, vector_sha=None):
    completed = sum(len(batch['records']) for batch in batches)
    usage_known = all('total_tokens' in batch['usage'] for batch in batches)
    receipt = {'schema_version': 1, 'identity': identity, 'basis': basis, 'settings': config,
               'updated_at_utc': datetime.now(timezone.utc).isoformat(),
               'status': 'completed' if completed == total else 'partial',
               'completed_chunks': completed, 'total_chunks': total, 'saved_batches': len(batches),
               'usage_tokens_from_saved_batches': sum(batch['usage'].get('total_tokens', 0) for batch in batches),
               'usage_complete_for_saved_batches': usage_known,
               'usage_note': 'Failed, timed-out or interrupted requests may be billed but are not included here.',
               'vectors_sha256': vector_sha}
    write_json(output / 'run.json', receipt)
    return receipt


def run_batches(source, config, output, basis, identity, client, *, limit=None, progress=print, sleep=time.sleep):
    """Limit is the number of new chunks this run, not the final corpus size."""
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError('--limit must be a positive number of new chunks')
    with output_lock(output):
        manifest = output / 'manifest.json'
        if manifest.exists():
            saved = json.loads(manifest.read_text(encoding='utf-8'))
            if saved != {'schema_version': 1, 'basis': basis, 'identity': identity}:
                raise ValueError('Saved vectors use different input, endpoint, model or dimensions; use a new output directory')
        else:
            if any((output / 'batches').glob('*.json')):
                raise ValueError('Saved batches exist without their manifest')
            write_json(manifest, {'schema_version': 1, 'basis': basis, 'identity': identity})
        batches, cursor = saved_batches(output, source, config, identity)
        stop = min(len(source), cursor + limit) if limit is not None else len(source)
        update_receipt(output, basis, identity, batches, len(source), config)
        progress(f'Existing: {cursor}/{len(source)}; new chunks scheduled: {stop - cursor}')
        while cursor < stop:
            texts = [c[config['text_field']] for c in source[cursor:min(cursor + config['batch_size'], stop)]]
            result = client.embed(texts)
            batch = persist_batch(output, source, cursor, result, config, identity)
            batches.append(batch)
            cursor += len(texts)
            update_receipt(output, basis, identity, batches, len(source), config)
            progress(f'Saved {cursor}/{len(source)} chunks; batch tokens: {result["usage"].get("total_tokens", "unknown")}')
            if cursor < stop:
                sleep(config['pause_seconds'])
        vector_sha = export_vectors(output, batches) if cursor == len(source) else None
        return update_receipt(output, basis, identity, batches, len(source), config, vector_sha=vector_sha)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='Validate inputs and preview counts, without network or writes')
    parser.add_argument('--check', action='store_true', help='Validate saved batches without network or writes')
    parser.add_argument('--limit', type=int, help='Process at most N new chunks (for a small trial)')
    parser.add_argument('--output', type=Path, default=OUTPUT, help='Output directory; relative paths are project-relative')
    args = parser.parse_args()
    if args.dry_run and args.check:
        parser.error('--dry-run and --check are mutually exclusive')
    config = load_config(ROOT)
    source, source_sha = load_source(ROOT, config)
    key, base_url = load_local_credentials(ROOT)
    basis, identity = identity_for(source_sha, config, base_url)
    output = args.output if args.output.is_absolute() else ROOT / args.output
    if args.dry_run:
        print(json.dumps({'network_requests': 0, 'chunks': len(source),
              'text_chars': sum(len(c[config['text_field']]) for c in source),
              'full_run_batches': (len(source) + config['batch_size'] - 1) // config['batch_size'],
              'model': config['model'], 'dimensions': config['dimensions'], 'output': str(output)}, ensure_ascii=False))
        return
    if args.check:
        manifest = json.loads((output / 'manifest.json').read_text(encoding='utf-8'))
        if manifest != {'schema_version': 1, 'basis': basis, 'identity': identity}:
            raise ValueError('Saved manifest differs from the source/configuration')
        batches, cursor = saved_batches(output, source, config, identity)
        receipt = json.loads((output / 'run.json').read_text(encoding='utf-8'))
        if (receipt['identity'] != identity or receipt['completed_chunks'] != cursor
                or receipt['saved_batches'] != len(batches) or receipt['total_chunks'] != len(source)
                or receipt['status'] != ('completed' if cursor == len(source) else 'partial')
                or receipt['usage_tokens_from_saved_batches'] != sum(b['usage'].get('total_tokens', 0) for b in batches)):
            raise ValueError('Saved receipt is stale; rerun normally to recover batch progress')
        if cursor == len(source):
            raw = (output / 'vectors.jsonl').read_bytes()
            if sha256(raw) != receipt['vectors_sha256']:
                raise ValueError('Combined vector export differs from its receipt')
            expected = [record for batch in batches for record in batch['records']]
            if [json.loads(line) for line in raw.decode('utf-8').splitlines()] != expected:
                raise ValueError('Combined export differs from saved batches')
        print(json.dumps({'status': receipt['status'], 'validated_chunks': cursor, 'total_chunks': len(source), 'network_requests': 0}))
        return
    if not key or any(c.isspace() for c in key):
        raise ValueError('Configure DASHSCOPE_API_KEY or .runtime/dashscope.local.json before running')
    client = EmbeddingClient(base_url, key, config)
    receipt = run_batches(source, config, output, basis, identity, client, limit=args.limit)
    print(json.dumps({'status': receipt['status'], 'completed_chunks': receipt['completed_chunks'],
                      'total_chunks': len(source), 'output': str(output)}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Stopped; completed batches are saved. Run the same command to resume.', file=sys.stderr)
        sys.exit(130)
    except (ApiFailure, ValueError, OSError) as error:
        # Local exceptions can contain configuration values; emit only safe
        # validation messages, never library/network request representations.
        print(f'Error: {error}' if isinstance(error, (ApiFailure, ValueError)) else
              f'Local file operation failed ({type(error).__name__}); completed batches are retained.', file=sys.stderr)
        sys.exit(1)
