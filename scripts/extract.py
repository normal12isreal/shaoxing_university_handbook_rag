"""Run MinerU locally and retain its original document and page metadata."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import time

from mineru.parser import ParseResult, parse
from mineru.parser.page_range import parse_page_range
from mineru.parser.writer import FileBasedDataWriter
from pypdf import PdfReader


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tier', choices=['flash', 'basic', 'standard'], default='flash')
    parser.add_argument('--pages', default='all')
    parser.add_argument('--output-name', default='flash-v1')
    parser.add_argument('--ocr-mode', choices=['auto', 'txt', 'ocr'])
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    source = root / 'data/input/student-handbook-2025.pdf'
    output = (root / 'output' / args.output_name).resolve()
    if not output.is_relative_to(root / 'output') or output == root / 'output':
        parser.error('output-name must name a folder inside output/')
    if output.exists():
        parser.error(f'Output already exists; choose another --output-name: {output}')
    ocr_mode = args.ocr_mode or ('txt' if args.tier == 'flash' else 'auto')
    started = datetime.now(timezone.utc).isoformat()
    clock = time.perf_counter()
    print(f'MinerU: tier={args.tier}, ocr_mode={ocr_mode}, pages={args.pages}', flush=True)
    result = parse(source, tier=args.tier, ocr_mode=ocr_mode, page_range=args.pages)
    output.mkdir(parents=True)
    result.save(FileBasedDataWriter(str(output)))
    middle = json.loads((output / 'middle_json.json').read_text(encoding='utf-8'))
    restored = ParseResult.from_dict(middle)
    page_indices = [page['page_idx'] for page in middle['pages']]
    source_pages = len(PdfReader(source).pages)
    if len(page_indices) != len(set(page_indices)) or page_indices != sorted(page_indices):
        raise ValueError('Duplicate or unordered PDF page indices')
    expected_pages = parse_page_range(args.pages, source_pages)
    if page_indices != expected_pages:
        raise ValueError(f'Page coverage differs from requested range: {page_indices} != {expected_pages}')
    if len(restored.pages) != len(page_indices):
        raise ValueError('Saved document failed its schema round trip')
    markdown = (output / 'markdown.md').read_text(encoding='utf-8')
    if not markdown.strip():
        raise ValueError('Markdown is empty')
    blocks = Counter(block['type'] for page in middle['pages'] for block in page.get('blocks', []))
    manifest = {
        'status': 'completed',
        'started_at_utc': started,
        'elapsed_seconds': round(time.perf_counter() - clock, 3),
        'source_relative_path': source.relative_to(root).as_posix(),
        'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'source_pdf_pages': source_pages,
        'requested_pages': args.pages,
        'parsed_pages': len(page_indices),
        'page_numbering': 'page_idx is zero-based physical PDF page; printed page number is separate',
        'mineru_version': importlib.metadata.version('mineru'),
        'docvortex_version': importlib.metadata.version('docvortex'),
        'mineru_git_commit': subprocess.check_output(
            ['git', '-C', str(root / 'tools/MinerU'), 'rev-parse', 'HEAD'], text=True
        ).strip(),
        'tier': args.tier,
        'ocr_mode': ocr_mode,
        'actual_mineru_metadata': middle.get('extensions', {}).get('mineru', {}),
        'execution': 'local Python SDK; no remote document or LLM endpoint configured',
        'markdown_characters': len(markdown),
        'top_level_block_types': dict(blocks),
        'pages_without_blocks': [p['page_idx'] + 1 for p in middle['pages'] if not p.get('blocks')],
        'checks': ['nonempty markdown', 'ordered unique pages', 'requested page coverage', 'schema round trip'],
        'limitations': [
            'Page coverage does not prove every word was extracted correctly.',
            ('Flash/txt reads the PDF text layer without OCR or VLM; tables, images, formulas and headings need review.'
             if args.tier == 'flash' and ocr_mode == 'txt'
             else 'Model-based extraction still needs source comparison, especially tables and formulas.'),
            'This is an extraction artifact; RAG chunking, embedding and indexing have not been performed.',
        ],
    }
    (output / 'run.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(manifest, ensure_ascii=False, indent=2), flush=True)
    print(f'Output: {output}', flush=True)


if __name__ == '__main__':
    main()
