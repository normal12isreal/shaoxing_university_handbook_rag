"""Bounded retrieval context and source-scoped table association regressions."""

import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

from scripts.chunk_document import Chunker
from scripts.context_expansion import HARD_LIMITS, expand_context
from scripts.link_context import attachment_number, build_artifacts, link_chunks


ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / 'config/table-context-v1.json').read_text(encoding='utf-8'))
CHUNK_CONFIG = json.loads((ROOT / 'config/chunking-v1.json').read_text(encoding='utf-8'))


def simple_chunk(cid, text='正文', related=()):
    return {'chunk_id': cid, 'text': text, 'source_file': 'source.pdf',
            'pdf_pages': [1], 'related_chunk_ids': list(related)}


class ContextExpansionTests(unittest.TestCase):
    def test_expansion_is_one_hop_and_deduplicates_seeds(self):
        chunks = {'a': simple_chunk('a', related=['b']),
                  'b': simple_chunk('b', related=['a', 'c']), 'c': simple_chunk('c')}
        result = expand_context(['a', 'a'], chunks, HARD_LIMITS)
        self.assertEqual([c['chunk_id'] for c in result['chunks']], ['a', 'b'])
        self.assertEqual(result['added_chunk_ids'], ['b'])
        self.assertNotIn('related_chunk_ids', result['chunks'][0])

    def test_large_group_stops_at_four_additions_and_reports_missing_context(self):
        chunks = {str(i): simple_chunk(str(i), related=[str(j) for j in range(12) if j != i])
                  for i in range(12)}
        result = expand_context(['0'], chunks, HARD_LIMITS)
        self.assertEqual(len(result['added_chunk_ids']), 4)
        self.assertEqual(len(result['omitted_chunk_ids']), 7)
        self.assertFalse(result['complete'])

    def test_character_budget_does_not_cut_rows_or_drop_seed_for_neighbors(self):
        chunks = {'a': simple_chunk('a', '甲' * 40, ['b', 'c']),
                  'b': simple_chunk('b', '乙' * 30), 'c': simple_chunk('c', '丙' * 10)}
        result = expand_context(['a'], chunks, {**HARD_LIMITS, 'max_total_chars': 55})
        self.assertEqual(result['total_chars'], 50)
        self.assertEqual([c['chunk_id'] for c in result['chunks']], ['a', 'c'])
        self.assertEqual(result['omitted_chunk_ids'], ['b'])
        self.assertEqual(len(result['chunks'][0]['text']), 40)

    def test_seed_budget_exhaustion_is_explicit(self):
        chunks = {'a': simple_chunk('a', '甲' * 80)}
        result = expand_context(['a'], chunks, {**HARD_LIMITS, 'max_total_chars': 50})
        self.assertEqual(result['chunks'], [])
        self.assertEqual(result['omitted_chunk_ids'], ['a'])
        self.assertFalse(result['complete'])

    def test_candidate_budget_and_hard_limits(self):
        chunks = {str(i): simple_chunk(str(i)) for i in range(101)}
        chunks['0']['related_chunk_ids'] = [str(i) for i in range(1, 101)]
        result = expand_context(['0'], chunks, HARD_LIMITS)
        self.assertEqual(result['relations_not_examined'], 36)
        self.assertFalse(result['complete'])
        for key, maximum in HARD_LIMITS.items():
            with self.subTest(key=key), self.assertRaises(ValueError):
                expand_context(['0'], chunks, {**HARD_LIMITS, key: maximum + 1})
        with self.assertRaises(ValueError):
            expand_context(list(chunks)[:9], chunks, HARD_LIMITS)


class ContextLinkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.artifacts = build_artifacts(ROOT)
        cls.linked = {c['chunk_id']: c for c in map(json.loads, cls.artifacts['chunks.jsonl'].splitlines())}
        cls.relations = json.loads(cls.artifacts['relations.json'])

    def test_real_tables_bind_both_directions_and_preserve_original_data(self):
        originals = list(map(json.loads, (ROOT / 'output/chunks-v1/chunks.jsonl').read_text(encoding='utf-8').splitlines()))
        for original in originals:
            linked = self.linked[original['chunk_id']]
            for key, value in original.items():
                self.assertEqual(linked[key], value)
            for related in linked['related_chunk_ids']:
                self.assertIn(original['chunk_id'], self.linked[related]['related_chunk_ids'])
        group = next(g for g in self.relations['groups'] if 'pdf-p193-b8' in g['table_ids'])
        table = group['table_chunk_ids'][0]
        intro = next(cid for cid in group['context_chunk_ids'] if '2.各类型读者的借书册数和借期' in self.linked[cid]['text'])
        self.assertIn(intro, self.linked[table]['related_chunk_ids'])
        self.assertIn(table, self.linked[intro]['related_chunk_ids'])
        self.assertTrue(self.linked[table]['retrieval_text'].startswith('2.各类型读者的借书册数和借期'))
        expanded = expand_context([table], self.linked, HARD_LIMITS)
        self.assertIn(intro, expanded['added_chunk_ids'])
        from_intro = expand_context([intro], self.linked, HARD_LIMITS)
        self.assertIn(table, from_intro['added_chunk_ids'])
        for note in group['context_chunk_ids']:
            if note != intro:
                self.assertIn(note, from_intro['added_chunk_ids'])

    def test_attachment_uses_matching_chapter_and_does_not_bind_preceding_final_provisions(self):
        group = next(g for g in self.relations['groups'] if 'pdf-p128-b2' in g['table_ids'])
        self.assertTrue(group['context_chunk_ids'])
        for cid in group['context_chunk_ids']:
            self.assertIn('第二章 德育评价', self.linked[cid]['text'])
            self.assertNotIn('第九章 附 则', self.linked[cid]['text'])
        for missing in self.relations['unresolved_references']:
            # References to form attachments elsewhere must not match evaluation
            # tables in an entirely different document with the same annex number.
            self.assertFalse(self.linked[missing['chunk_id']]['context_group_ids'])

    def test_explicit_annex_reference_is_scoped_to_its_own_document(self):
        table = {'table_id': 'synthetic', 'section': '制度甲', 'title': '额度表',
                 'subtables': [{'title': '额度', 'columns': ['金额'], 'rows': [{'金额': '150元'}]}]}
        document = {'source': {'pdf': 'source.pdf'}, 'pages': [{'pdf_page': 1, 'blocks': [
            {'type': 'doc_title', 'content': '制度甲'}, {'type': 'text', 'content': '第一条 额度详见附件一。'},
            {'type': 'header', 'content': '附件 1'}, {'type': 'paragraph_title', 'content': '额度表'},
            {'type': 'table', 'content': table}, {'type': 'doc_title', 'content': '制度乙'},
            {'type': 'text', 'content': '第一条 请填写附件一。'}]}]}
        chunks, audit = Chunker(document, CHUNK_CONFIG).build()
        linked, relations = link_chunks(chunks, audit, document, CONFIG)
        group = relations['groups'][0]
        self.assertEqual(len(group['context_chunk_ids']), 1)
        context = next(c for c in linked if c['chunk_id'] == group['context_chunk_ids'][0])
        self.assertIn('制度甲', context['text'])
        self.assertIn('explicit_attachment_number_same_document', group['bindings'][0]['rules'])
        self.assertEqual(attachment_number('十二'), '12')

    def test_outputs_are_reproducible_and_large_attachment_is_bounded(self):
        self.assertEqual(self.artifacts, build_artifacts(ROOT))
        group = next(g for g in self.relations['groups'] if 'pdf-p133-b2' in g['table_ids'])
        table = group['table_chunk_ids'][0]
        expanded = expand_context([table], self.linked, HARD_LIMITS)
        self.assertLessEqual(len(expanded['added_chunk_ids']), 4)
        self.assertLessEqual(expanded['total_chars'], 6000)
        self.assertFalse(expanded['complete'])
        self.assertTrue(expanded['omitted_chunk_ids'])

    def test_outputs_match_across_process_hash_seeds(self):
        code = ('from pathlib import Path; import hashlib; '
                'from scripts.link_context import build_artifacts; '
                'print(hashlib.sha256(build_artifacts(Path.cwd())["relations.json"].encode("utf-8")).hexdigest())')
        hashes = []
        for seed in ('1', '7', '42'):
            hashes.append(subprocess.check_output([sys.executable, '-c', code], cwd=ROOT,
                          env={**os.environ, 'PYTHONHASHSEED': seed}, text=True, timeout=15).strip())
        self.assertEqual(len(set(hashes)), 1)


if __name__ == '__main__':
    unittest.main()
