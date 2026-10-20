"""Deterministic, source-preserving chunking. No model or network dependency."""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re


NUMBER = r'[一二三四五六七八九十百千零〇两0-9]+'
ARTICLE = re.compile(rf'(?:^|(?<=[。！？；\n]))第{NUMBER}条[ \t\u3000]*')
ITEM = re.compile(rf'(?:^|(?<=[。；\n]))(?:[（(]{NUMBER}[）)]|\d+[.、])')
CHAPTER = re.compile(rf'^第{NUMBER}章\s*[^。；:：！？]+$')
SECTION = re.compile(rf'^第{NUMBER}节\s*[^。；:：！？]+$')
ENUM_HEADING = re.compile(rf'^{NUMBER}、[^。；:：！？]+$')
ATTACHMENT = re.compile(r'^附件\s*[一二三四五六七八九十0-9]+$')
DOCUMENT_REFERENCE = re.compile(r'^[^。；！？\n]{1,40}(?:〔\d{4}〕\s*\d+\s*号|第\s*\d+\s*号)$')


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def heading_role(kind: str, text: str, limit: int) -> str | None:
    """Extraction types are hints; article labels and prose are never headings."""
    if kind == 'doc_title':
        return 'document'
    if ATTACHMENT.fullmatch(text):
        return 'attachment'
    if len(text) > limit:
        return None
    if CHAPTER.fullmatch(text) or ENUM_HEADING.fullmatch(text):
        return 'chapter'
    if SECTION.fullmatch(text):
        return 'section'
    return None


def split_long(text: str, start: int, end: int, limit: int, intro_limit: int):
    """Keep source offsets; repeat a short article lead for oversized item lists."""
    intro = ''
    article = ARTICLE.match(text, start, end)
    items = list(ITEM.finditer(text, start, end))
    if article and items and items[0].start() - start <= intro_limit:
        intro = text[start:items[0].start()].rstrip()
    if article and not intro:
        intro = article.group().strip()
    cursor = start
    while cursor < end:
        repeated = intro if cursor > start else ''
        budget = limit - len(repeated) - (1 if repeated else 0)
        stop = min(cursor + budget, end)
        if stop < end:
            # Prefer complete subitems; otherwise complete sentences, then a hard cut.
            candidates = [m.start() for m in items if cursor < m.start() <= stop]
            sentences = [m.end() for m in re.finditer(r'[。！？；]\s*', text[cursor:stop])]
            if candidates:
                stop = max(candidates)
            elif sentences:
                stop = cursor + max(sentences)
        yield cursor, stop, repeated
        cursor = stop


def ranges(text: str, limit: int, intro_limit: int):
    """Pack whole articles; split a single article only when it exceeds the limit."""
    boundaries = sorted({0, len(text), *(m.start() for m in ARTICLE.finditer(text))})
    pending = None
    for left, right in zip(boundaries, boundaries[1:]):
        if right - left > limit:
            if pending is not None:
                yield *pending, ''
                pending = None
            yield from split_long(text, left, right, limit, intro_limit)
        elif pending is not None and right - pending[0] <= limit:
            pending = pending[0], right
        else:
            if pending is not None:
                yield *pending, ''
            pending = left, right
    if pending is not None:
        yield *pending, ''


class Chunker:
    def __init__(self, document: dict, config: dict) -> None:
        self.document = document
        self.config = config
        self.context: dict[str, tuple[str, list[str]]] = {}
        self.chunks: list[dict] = []
        self.audit: list[dict] = []
        self.blocks: dict[str, dict] = {}
        self.coverage: dict[str, list[tuple[int, int]]] = {}
        self.heading_uses: set[str] = set()
        self.expected_rows: set[str] = set()
        self.emitted_rows: list[str] = []
        self.pending: list[tuple[str, str]] = []
        self.toc_pages: list[int] = []
        self.empty_context = False

    def emit(self, body: str, refs: list[str], *, spans=None, rows=None,
             extra: list[str] | None = None, repeated: str = '') -> None:
        headings = [value[0] for value in self.context.values()]
        heading_refs = [ref for value in self.context.values() for ref in value[1]]
        self.heading_uses.update(heading_refs)
        parts = list(dict.fromkeys(headings + (extra or [])))
        if repeated:
            parts.append(repeated)
        if body.strip():
            parts.append(body.strip())
        if not parts:
            return
        pages = sorted({self.blocks[ref]['page'] for ref in refs + heading_refs})
        # Only the body/rows locate the evidence; earlier heading pages are context.
        evidence_pages = sorted({self.blocks[ref]['page'] for ref in refs})
        chunk_id = f'chunk-{len(self.chunks) + 1:04d}'
        self.chunks.append({'chunk_id': chunk_id, 'text': '\n'.join(parts),
                            'source_file': self.document['source']['pdf'],
                            'pdf_pages': evidence_pages or pages})
        self.audit.append({'chunk_id': chunk_id, 'heading_blocks': heading_refs,
                           'source_blocks': refs, 'source_spans': spans or [],
                           'table_rows': rows or [], 'repeated_article_intro': repeated,
                           'body_chars': len(body.strip()) + len(repeated) + bool(repeated),
                           'over_limit_atomic_row': bool(rows and len(body) > self.config['max_body_chars'])})
        self.empty_context = False

    def flush(self) -> None:
        text = ''
        spans = []
        for ref, content in self.pending:
            sep = ''
            short_label = (self.blocks[ref]['type'] == 'paragraph_title' and len(content) <= 20
                           and not re.search(r'[，。；:：！？\d]', content))
            if text and (short_label or ARTICLE.match(content) or ITEM.match(content) or text[-1] in '。；:：！？'):
                sep = '\n'
            text += sep
            spans.append((len(text), len(text) + len(content), ref))
            text += content
        self.pending.clear()
        for left, right, repeated in ranges(text, self.config['max_body_chars'],
                                             self.config['max_repeated_article_intro_chars']):
            source_spans = []
            refs = []
            for begin, end, ref in spans:
                a, b = max(begin, left), min(end, right)
                if a < b:
                    local = a - begin, b - begin
                    self.coverage.setdefault(ref, []).append(local)
                    source_spans.append({'block': ref, 'start': local[0], 'end': local[1]})
                    refs.append(ref)
            self.emit(text[left:right], refs, spans=source_spans, repeated=repeated)

    def heading(self, role: str, text: str, refs: list[str]) -> None:
        self.flush()
        previous = self.context.copy()
        if role == 'document':
            self.context.clear()
        elif role == 'attachment':
            self.context = {k: v for k, v in self.context.items() if k == 'document'}
        elif role == 'chapter':
            self.context = {k: v for k, v in self.context.items() if k in {'document', 'attachment'}}
        self.context[role] = text, refs
        removed = {ref for value in previous.values() for ref in value[1]} - {
            ref for value in self.context.values() for ref in value[1]}
        if removed - self.heading_uses:
            current, self.context = self.context, previous
            self.emit('', [ref for value in previous.values() for ref in value[1]])
            self.context = current
        for ref in refs:
            self.blocks[ref]['disposition'] = 'heading'
        self.empty_context = True

    def table(self, table: dict, ref: str) -> None:
        self.flush()
        self.blocks[ref]['disposition'] = 'table'
        for si, sub in enumerate(table['subtables']):
            extra = [table['section'], table['title'], sub['title']]
            shared = [f'{key}：{value}' for key, value in sub.get('shared_values', {}).items()]
            shared += list(sub.get('source_notes', []))
            common = '\n'.join(shared)
            group, keys = [], []

            def emit_group():
                if group:
                    body = '\n'.join(([common] if common else []) + group)
                    self.emit(body, [ref], rows=list(keys), extra=extra)
                    self.emitted_rows.extend(keys)

            for ri, row in enumerate(sub['rows']):
                key = f'{table["table_id"]}/subtable-{si}/row-{ri}'
                self.expected_rows.add(key)
                line = '；'.join(f'{column}：{row[column] if row[column] is not None else "（原表未列值）"}'
                                for column in sub['columns'])
                candidate = '\n'.join(([common] if common else []) + group + [line])
                if group and len(candidate) > self.config['max_body_chars']:
                    emit_group()
                    group, keys = [], []
                group.append(line)
                keys.append(key)
            emit_group()

    def build(self) -> tuple[list[dict], dict]:
        flattened = []
        for page in self.document['pages']:
            is_toc = any(b['type'] == 'index' for b in page['blocks'])
            if is_toc:
                self.toc_pages.append(page['pdf_page'])
            for index, block in enumerate(page['blocks']):
                ref = f'p{page["pdf_page"]:03d}/b{index:02d}'
                kind, value = block['type'], block.get('content', '')
                disposition = 'toc' if is_toc else kind if kind in {'image', 'page_number'} else 'pending'
                text = value.strip() if isinstance(value, str) else ''
                self.blocks[ref] = {'page': page['pdf_page'], 'type': kind, 'disposition': disposition,
                                    'source_chars': len(text)}
                if disposition == 'pending':
                    flattened.append((ref, kind, text, value))
        for i, (ref, kind, text, value) in enumerate(flattened):
            if kind == 'table':
                self.table(value, ref)
                continue
            if not text:
                self.blocks[ref]['disposition'] = 'empty'
                continue
            # A short line immediately before a document title is its prefix.
            if (kind == 'paragraph_title' and i + 1 < len(flattened)
                    and flattened[i + 1][1] == 'doc_title' and len(text) <= 20
                    and self.blocks[ref]['page'] == self.blocks[flattened[i + 1][0]]['page']
                    and not re.search(r'[，。；:：！？\d]', text)):
                self.flush()
                self.blocks[ref]['disposition'] = 'title_prefix'
                continue
            if (set(self.context) == {'document'} and not self.pending
                    and DOCUMENT_REFERENCE.fullmatch(text)):
                label, label_refs = self.context['document']
                self.context['document'] = label + '\n' + text, label_refs + [ref]
                self.blocks[ref]['disposition'] = 'heading'
                continue
            role = heading_role(kind, text, self.config['max_heading_chars'])
            refs = [ref]
            if role == 'document' and i and self.blocks[flattened[i - 1][0]]['disposition'] == 'title_prefix':
                previous = flattened[i - 1]
                text = previous[2] + text
                refs.insert(0, previous[0])
            if (not role and kind == 'paragraph_title' and self.empty_context
                    and 'attachment' in self.context and not re.search(r'[，。；:：！？\d]', text)
                    and len(text) <= self.config['max_heading_chars']):
                role = 'section'
            if role:
                self.heading(role, text, refs)
            else:
                self.blocks[ref]['disposition'] = 'body'
                self.pending.append((ref, text))
        self.flush()
        if self.empty_context:
            self.emit('', [ref for value in self.context.values() for ref in value[1]])
        self.validate()
        lengths = sorted(len(c['text']) for c in self.chunks)
        summary = {'chunks': len(self.chunks), 'table_chunks': sum(bool(a['table_rows']) for a in self.audit),
                   'table_rows': len(self.emitted_rows), 'toc_pdf_pages': self.toc_pages,
                   'block_dispositions': dict(Counter(b['disposition'] for b in self.blocks.values())),
                   'text_chars': {'min': min(lengths), 'median': lengths[len(lengths) // 2],
                                  'max': max(lengths)},
                   'body_limit_exceptions': [a['chunk_id'] for a in self.audit if a['over_limit_atomic_row']],
                   'checks': ['all included body characters covered exactly once before whitespace trimming',
                              'all headings used as context', 'all table rows emitted exactly once',
                              'body character limit (atomic table rows exempt)', 'no model calls']}
        return self.chunks, {'summary': summary, 'blocks': self.blocks, 'chunks': self.audit}

    def validate(self) -> None:
        for ref, block in self.blocks.items():
            if block['disposition'] == 'pending':
                raise ValueError(f'Unhandled block: {ref}')
            if block['disposition'] == 'heading' and ref not in self.heading_uses:
                raise ValueError(f'Unused heading: {ref}')
            if block['disposition'] != 'body':
                continue
            cursor = 0
            for left, right in sorted(self.coverage.get(ref, [])):
                if left != cursor or right <= left:
                    raise ValueError(f'Missing or duplicate source characters: {ref}')
                cursor = right
            if cursor != block['source_chars']:
                raise ValueError(f'Incomplete source block: {ref}')
        if set(self.emitted_rows) != self.expected_rows or len(self.emitted_rows) != len(self.expected_rows):
            raise ValueError('Missing or duplicated table rows')
        for audit in self.audit:
            if audit['body_chars'] > self.config['max_body_chars'] and not audit['over_limit_atomic_row']:
                raise ValueError(f'Overlong body: {audit["chunk_id"]}')


def build_artifacts(root: Path):
    source = root / 'output/reviewed-v1/document.json'
    config_path = root / 'config/chunking-v1.json'
    config = json.loads(config_path.read_text(encoding='utf-8'))
    if config['max_body_chars'] <= config['max_repeated_article_intro_chars'] + 1:
        raise ValueError('Body budget must exceed the repeated article introduction budget')
    chunks, audit = Chunker(json.loads(source.read_text(encoding='utf-8')), config).build()
    jsonl = ''.join(json.dumps(c, ensure_ascii=False) + '\n' for c in chunks)
    plain = '\n\n'.join(f'[{c["chunk_id"]} | PDF 页码：{", ".join(map(str, c["pdf_pages"]))}]\n{c["text"]}'
                          for c in chunks) + '\n'
    run = {'schema_version': 1, 'source': str(source.relative_to(root)).replace('\\', '/'),
           'source_sha256': digest(source.read_bytes()), 'config': config,
           'config_sha256': digest(config_path.read_bytes()),
           'script_sha256': digest(Path(__file__).read_bytes()), 'summary': audit['summary'],
           'chunks_sha256': digest(jsonl.encode('utf-8'))}
    return {'chunks.jsonl': jsonl, 'chunks.txt': plain, 'audit.json': encode(audit), 'run.json': encode(run)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Rebuild in memory and compare saved files')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    artifacts = build_artifacts(root)
    output = root / 'output/chunks-v1'
    if not args.check:
        output.mkdir(parents=True, exist_ok=True)
    for name, data in artifacts.items():
        if args.check:
            if (output / name).read_text(encoding='utf-8') != data:
                raise ValueError(f'Stale or modified output: {name}')
        else:
            (output / name).write_text(data, encoding='utf-8', newline='\n')
    print(encode(json.loads(artifacts['run.json'])['summary']))


if __name__ == '__main__':
    main()
