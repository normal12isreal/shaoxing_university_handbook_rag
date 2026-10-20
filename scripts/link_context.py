"""Link existing chunks by table structure without model calls or text rewriting."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re

if __package__:
    from .context_expansion import expand_context, validate_limits
else:
    from context_expansion import expand_context, validate_limits


ROOT = Path(__file__).resolve().parents[1]
ATTACHMENT = re.compile(r'附件\s*([0-9一二三四五六七八九十]+)')
HEADING_NUMBER = re.compile(r'^(?:第[一二三四五六七八九十百0-9]+[章节]|[一二三四五六七八九十]+、)')


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def normalize(value: str) -> str:
    return re.sub(r'\s+', '', value)


def attachment_number(value: str) -> str:
    translated = value.translate(str.maketrans('一二三四五六七八九', '123456789'))
    if '十' in translated:
        left, right = translated.split('十', 1)
        return str(int(left or '1') * 10 + int(right or '0'))
    return str(int(translated))


def link_chunks(chunks: list[dict], audit: dict, document: dict, config: dict):
    validate_limits(config['expansion'])
    if config['max_neighbor_page_gap'] not in (0, 1) or not 0 <= config['max_intro_chars'] <= 200:
        raise ValueError('Neighbor page gap is limited to 0/1 and introductory text to 200 characters')
    entries = {entry['chunk_id']: entry for entry in audit['chunks']}
    by_id = {chunk['chunk_id']: chunk for chunk in chunks}
    if len(by_id) != len(chunks) or set(entries) != set(by_id):
        raise ValueError('Chunk IDs and audit entries must match one-to-one')
    positions = {chunk['chunk_id']: i for i, chunk in enumerate(chunks)}
    blocks, tables = {}, {}
    for page in document['pages']:
        for i, block in enumerate(page['blocks']):
            ref = f'p{page["pdf_page"]:03d}/b{i:02d}'
            blocks[ref] = block
            if block['type'] == 'table':
                tables[block['content']['table_id']] = block['content']

    def headings(cid):
        return entries[cid]['heading_blocks']

    def owner(cid):
        return next((ref for ref in headings(cid) if blocks[ref]['type'] == 'doc_title'), None)

    def attachment(cid):
        return next((attachment_number(m.group(1)) for ref in headings(cid)
                     if (m := ATTACHMENT.fullmatch(blocks[ref]['content']))), None)

    def body(cid):
        # Spans use stripped source block strings, as defined by the chunker.
        return '\n'.join(blocks[s['block']]['content'].strip()[s['start']:s['end']]
                         for s in entries[cid]['source_spans'])

    bodies = {cid: body(cid) for cid in by_id}
    # Bound offline matching to candidates within one document, never all chapters.
    body_by_owner = defaultdict(list)
    for cid in by_id:
        if not entries[cid]['table_rows'] and entries[cid]['source_spans']:
            body_by_owner[owner(cid)].append(cid)
    grouped = {}
    for cid, entry in entries.items():
        for tid in dict.fromkeys(row.split('/')[0] for row in entry['table_rows']):
            table = tables[tid]
            if owner(cid) is None:
                raise ValueError(f'Table chunk lacks a document heading: {cid}')
            key = owner(cid), table.get('continuation_group') or tid
            group = grouped.setdefault(key, {'table_ids': [], 'table_chunk_ids': [], 'bindings': []})
            if tid not in group['table_ids']:
                group['table_ids'].append(tid)
            if cid not in group['table_chunk_ids']:
                group['table_chunk_ids'].append(cid)
    if {tid for group in grouped.values() for tid in group['table_ids']} != set(tables):
        raise ValueError('Every reviewed source table must be covered by chunk audit rows')

    groups = []
    unresolved_references = []
    for (document_owner, continuation), group in grouped.items():
        table_ids = group['table_chunk_ids']
        table_ids.sort(key=positions.get)
        bindings = defaultdict(set)
        # A neighboring body chunk must have the exact same heading scope.
        first, last = positions[table_ids[0]], positions[table_ids[-1]]
        for neighbor_pos, anchor in ((first - 1, table_ids[0]), (last + 1, table_ids[-1])):
            if not 0 <= neighbor_pos < len(chunks):
                continue
            cid = chunks[neighbor_pos]['chunk_id']
            if entries[cid]['table_rows'] or not entries[cid]['source_spans']:
                continue
            gap = min(abs(a - b) for a in by_id[cid]['pdf_pages'] for b in by_id[anchor]['pdf_pages'])
            if headings(cid) == headings(anchor) and gap <= config['max_neighbor_page_gap']:
                bindings[cid].add('adjacent_body_same_heading_scope')
        attachment_ids = {attachment(cid) for cid in table_ids} - {None}
        attachment_titles = [normalize(blocks[ref]['content']) for cid in table_ids for ref in headings(cid)
                             if blocks[ref]['type'] == 'paragraph_title'
                             and not HEADING_NUMBER.match(normalize(blocks[ref]['content']))
                             and not ATTACHMENT.fullmatch(blocks[ref]['content'])]
        for cid in body_by_owner[document_owner]:
            if attachment(cid):
                continue
            matches = {attachment_number(m.group(1)) for m in ATTACHMENT.finditer(bodies[cid])}
            if matches & attachment_ids:
                bindings[cid].add('explicit_attachment_number_same_document')
            if attachment_ids:
                topics = [HEADING_NUMBER.sub('', normalize(blocks[ref]['content'])) for ref in headings(cid)
                          if HEADING_NUMBER.match(normalize(blocks[ref]['content']))]
                # Exact normalized chapter name as attachment-title prefix;
                # no fuzzy keyword or embedding matching.
                if any(len(topic) >= 4 and title.startswith(topic) for topic in topics for title in attachment_titles):
                    bindings[cid].add('chapter_title_matches_attachment_title_prefix')
        group['group_id'] = f'table-context-{len(groups) + 1:03d}'
        group['document_heading_block'] = document_owner
        group['continuation_group'] = continuation
        group['context_chunk_ids'] = sorted(bindings, key=positions.get)
        group['bindings'] = [{'chunk_id': cid, 'rules': sorted(bindings[cid])} for cid in group['context_chunk_ids']]
        group['has_body_context'] = bool(bindings)
        groups.append(group)

    attachment_targets = defaultdict(set)
    for group in groups:
        for cid in group['table_chunk_ids']:
            if number := attachment(cid):
                attachment_targets[owner(cid)].add(number)
    for cid in by_id:
        if entries[cid]['table_rows']:
            continue
        for number in sorted({attachment_number(m.group(1)) for m in ATTACHMENT.finditer(bodies[cid])}):
            if number not in attachment_targets[owner(cid)]:
                unresolved_references.append({'chunk_id': cid, 'attachment_number': number,
                                              'reason': 'no_table_attachment_with_same_number_in_this_document'})

    relations, memberships, intros = defaultdict(dict), defaultdict(list), defaultdict(list)
    for group in groups:
        members = group['table_chunk_ids'] + group['context_chunk_ids']
        for cid in members:
            memberships[cid].append(group['group_id'])
        # Members of this specific table group are directly related, including
        # its introduction and following notes. Never infer cross-group links.
        table_members = set(group['table_chunk_ids'])
        for source_id in members:
            for cid in members:
                if cid == source_id:
                    continue
                priority = 0 if (source_id in table_members) != (cid in table_members) else 1
                relations[source_id][cid] = min(relations[source_id].get(cid, 2), priority)
        for table_id in group['table_chunk_ids']:
            for cid in group['context_chunk_ids']:
                # Take whole original fragments only, never generate a summary.
                fragments = bodies[cid].splitlines()
                if positions[cid] < positions[table_id]:
                    fragments.reverse()
                for fragment in fragments:
                    fragment = fragment.strip()
                    if fragment and len(fragment) <= config['max_intro_chars']:
                        intros[table_id].append(fragment)
                        break
    linked = []
    for chunk in chunks:
        cid = chunk['chunk_id']
        related = sorted(relations[cid], key=lambda other: (relations[cid][other],
                         abs(positions[other] - positions[cid]), positions[other]))
        selected_intros, size = [], 0
        for fragment in dict.fromkeys(intros[cid]):
            if size + len(fragment) + bool(selected_intros) <= config['max_intro_chars']:
                selected_intros.append(fragment)
                size += len(fragment) + (len(selected_intros) > 1)
        retrieval_text = '\n'.join(selected_intros + [chunk['text']])
        linked.append({**chunk, 'context_group_ids': memberships[cid],
                       'related_chunk_ids': related, 'retrieval_text': retrieval_text})
    linked_by_id = {c['chunk_id']: c for c in linked}
    for cid, chunk in linked_by_id.items():
        for related in chunk['related_chunk_ids']:
            if related == cid or cid not in linked_by_id[related]['related_chunk_ids']:
                raise ValueError('Relations must be symmetric and have no self-links')
        if any(chunk[key] != by_id[cid][key] for key in ('text', 'source_file', 'pdf_pages')):
            raise ValueError('Source chunk content changed while linking')
    summary = {'chunks': len(linked), 'table_chunks': sum(bool(e['table_rows']) for e in entries.values()),
               'context_groups': len(groups), 'groups_with_body_context': sum(g['has_body_context'] for g in groups),
               'chunks_with_relations': sum(bool(c['related_chunk_ids']) for c in linked),
               'table_chunks_with_intro': sum(bool(intros[c['chunk_id']]) for c in linked),
               'groups_without_body_context': [g['group_id'] for g in groups if not g['has_body_context']],
               'unresolved_attachment_references': len(unresolved_references),
               'binding_rules': dict(Counter(rule for g in groups for b in g['bindings'] for rule in b['rules'])),
               'expansion_limits': config['expansion']}
    return linked, {'schema_version': 1, 'groups': groups, 'unresolved_references': unresolved_references,
                    'summary': summary}


def build_artifacts(root: Path):
    paths = {'chunks': root / 'output/chunks-v1/chunks.jsonl',
             'audit': root / 'output/chunks-v1/audit.json',
             'document': root / 'output/reviewed-v1/document.json',
             'config': root / 'config/table-context-v1.json'}
    raw = {key: path.read_bytes() for key, path in paths.items()}
    chunks = [json.loads(line) for line in raw['chunks'].decode('utf-8').splitlines()]
    config = json.loads(raw['config'])
    chunk_receipt = json.loads((root / 'output/chunks-v1/run.json').read_text(encoding='utf-8'))
    if (hashlib.sha256(raw['document']).hexdigest() != chunk_receipt['source_sha256']
            or hashlib.sha256(raw['chunks']).hexdigest() != chunk_receipt['chunks_sha256']):
        raise ValueError('Chunk source or chunk output differs from the saved chunking receipt')
    linked, relations = link_chunks(chunks, json.loads(raw['audit']), json.loads(raw['document']), config)
    outputs = {'chunks.jsonl': ''.join(json.dumps(c, ensure_ascii=False) + '\n' for c in linked),
               'relations.json': encode(relations)}
    receipt = {'schema_version': 1, 'summary': relations['summary'],
               'source_sha256': {str(paths[key].relative_to(root)).replace('\\', '/'): hashlib.sha256(data).hexdigest()
                                 for key, data in raw.items()},
               'script_sha256': {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                  for path in (Path(__file__), root / 'scripts/context_expansion.py')},
               'artifact_sha256': {name: hashlib.sha256(data.encode('utf-8')).hexdigest() for name, data in outputs.items()}}
    outputs['run.json'] = encode(receipt)
    return outputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--expand', nargs='+', metavar='CHUNK_ID', help='Preview bounded direct expansion; read-only')
    args = parser.parse_args()
    if args.expand:
        config = json.loads((ROOT / 'config/table-context-v1.json').read_text(encoding='utf-8'))
        chunks = [json.loads(line) for line in (ROOT / 'output/linked-v1/chunks.jsonl').read_text(encoding='utf-8').splitlines()]
        print(encode(expand_context(args.expand, {c['chunk_id']: c for c in chunks}, config['expansion'])))
        return
    artifacts = build_artifacts(ROOT)
    output = ROOT / 'output/linked-v1'
    if not args.check:
        output.mkdir(parents=True, exist_ok=True)
    for name, data in artifacts.items():
        if args.check:
            if (output / name).read_text(encoding='utf-8') != data:
                raise ValueError(f'Stale or modified linked output: {name}')
        else:
            (output / name).write_text(data, encoding='utf-8', newline='\n')
    print(encode(json.loads(artifacts['relations.json'])['summary']))


if __name__ == '__main__':
    main()
