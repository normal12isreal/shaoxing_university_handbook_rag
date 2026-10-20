"""Merge source-checked JSON table records into the full extracted document."""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re

from pypdf import PdfReader


class PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == 'br':
            self.parts.append('\n')


def plain(value: object) -> object:
    """Remove presentation markup while retaining text and block structure."""
    if isinstance(value, str):
        if re.search(r'</?[A-Za-z][^>]*>', value):
            parser = PlainText()
            parser.feed(value)
            value = ''.join(parser.parts)
        return value.replace('**', '')
    if isinstance(value, list):
        return [plain(item) for item in value]
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    return value


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def build(root: Path) -> tuple[dict, dict, dict]:
    registry_path = root / 'data/table_reviews.json'
    registry = json.loads(registry_path.read_text(encoding='utf-8'))
    source_dir = root / registry['source_extraction']
    raw = json.loads((source_dir / 'middle_json.json').read_text(encoding='utf-8'))
    extracted = json.loads((source_dir / 'structured_content.json').read_text(encoding='utf-8'))
    if digest(root / registry['source_pdf']) != registry['source_pdf_sha256']:
        raise ValueError('Source PDF differs from the visually reviewed edition')
    source_pages = len(PdfReader(root / registry['source_pdf']).pages)
    if not extracted['is_full_document'] or [p['page_idx'] for p in extracted['pages']] != list(range(source_pages)):
        raise ValueError('The source extraction does not cover the complete PDF in order')
    originals = {(page['page_idx'], block['index']): block
                 for page in raw['pages'] for block in page.get('blocks', []) if block['type'] == 'table'}
    reviews = registry['tables']
    keys = [(table['page_idx'], table['block_index']) for table in reviews]
    if len(keys) != len(set(keys)) or set(keys) != set(originals):
        raise ValueError('The review registry must cover every extracted table exactly once')
    tables = deepcopy(reviews)
    by_position = {}
    for table in tables:
        original = originals[table['page_idx'], table['block_index']]
        body = [part for part in original['content'] if part['type'] == 'table_body']
        if len(body) != 1:
            raise ValueError(f"Unexpected source table structure: {table['table_id']}")
        original_hash = hashlib.sha256(body[0]['content'].encode('utf-8')).hexdigest()
        if original_hash != table['original_body_sha256']:
            raise ValueError(f"Source table changed after review: {table['table_id']}")
        if table['pdf_page'] != table['page_idx'] + 1:
            raise ValueError('Inconsistent physical page numbering')
        if table['review_status'] not in {'verified', 'corrected'} or not table['subtables']:
            raise ValueError('Unreviewed or empty table')
        for subtable in table['subtables']:
            columns = subtable['columns']
            if not columns or len(columns) != len(set(columns)) or not subtable['rows']:
                raise ValueError('Invalid subtable columns or rows')
            for row in subtable['rows']:
                if set(row) != set(columns) or any(value is not None and not isinstance(value, str) for value in row.values()):
                    raise ValueError('Rows must contain the named columns and source strings or null')
        table['source_bbox'] = original['bbox']
        position = table['page_idx'], tuple(original['bbox'])
        if position in by_position:
            raise ValueError('Ambiguous table position')
        by_position[position] = table
    source = {
        'pdf': registry['source_pdf'], 'sha256': registry['source_pdf_sha256'],
        'extraction': registry['source_extraction'], 'review_registry': 'data/table_reviews.json',
        'review_registry_sha256': digest(registry_path),
    }
    table_export = {'schema': 'student_handbook.reviewed_tables', 'schema_version': 1,
                    'source': source, 'scope': registry['scope'], 'value_policy': registry['value_policy'], 'tables': tables}
    pages = []
    merged = []
    non_table_count = 0
    for page in extracted['pages']:
        blocks = []
        for block in page['blocks']:
            if block['type'] == 'table':
                table = by_position[page['page_idx'], tuple(block['bbox'])]
                blocks.append({'type': 'table', 'bbox': block['bbox'], 'table_id': table['table_id'],
                               'content': deepcopy(table)})
                merged.append(table['table_id'])
            else:
                blocks.append(plain(block))
                non_table_count += 1
        pages.append({'page_idx': page['page_idx'], 'pdf_page': page['page_idx'] + 1, 'blocks': blocks})
    if set(merged) != {table['table_id'] for table in tables} or len(merged) != len(tables):
        raise ValueError('Reviewed tables were not merged one-to-one into the document')
    document = {'schema': 'student_handbook.reviewed_document', 'schema_version': 1, 'source': source,
                'is_full_document': extracted['is_full_document'], 'pages': pages}
    if re.search(r'</?[A-Za-z][^>]*>', encode([document, table_export])):
        raise ValueError('Presentation HTML remains in the reviewed data')
    summary = {'pdf_pages': len(pages), 'reviewed_source_table_blocks': len(tables),
               'corrected_source_table_blocks': sum(t['review_status'] == 'corrected' for t in tables),
               'subtables': sum(len(t['subtables']) for t in tables),
               'row_records': sum(len(s['rows']) for t in tables for s in t['subtables']),
               'preserved_non_table_blocks': non_table_count,
               'checks': ['source PDF hash', 'source table hashes', 'complete one-to-one table coverage',
                          'named row columns and value types', 'all source pages retained', 'no HTML tags']}
    if [p['page_idx'] for p in pages] != [p['page_idx'] for p in extracted['pages']]:
        raise ValueError('Source page order changed')
    return document, table_export, summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Verify saved outputs without changing files')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    document, tables, summary = build(root)
    output = root / 'output/reviewed-v1'
    artifacts = {'document.json': document, 'tables.json': tables}
    if args.check:
        for name, expected in artifacts.items():
            if json.loads((output / name).read_text(encoding='utf-8')) != expected:
                raise ValueError(f'Saved output is stale or modified: {name}')
        receipt = json.loads((output / 'run.json').read_text(encoding='utf-8'))
        if receipt['summary'] != summary or any(digest(output / name) != value for name, value in receipt['artifact_sha256'].items()):
            raise ValueError('Saved output receipt differs from the reviewed artifacts')
    else:
        output.mkdir(parents=True, exist_ok=True)
        for name, content in artifacts.items():
            (output / name).write_text(encode(content), encoding='utf-8')
        receipt = {'status': 'completed', 'built_at_utc': datetime.now(timezone.utc).isoformat(),
                   'source': document['source'], 'summary': summary,
                   'artifact_sha256': {name: digest(output / name) for name in artifacts}}
        (output / 'run.json').write_text(encode(receipt), encoding='utf-8')
    print(encode(summary))


if __name__ == '__main__':
    main()
