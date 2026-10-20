"""Regression cases for extraction artifacts and lost retrieval context."""

import json
from pathlib import Path
import unittest

from scripts.chunk_document import Chunker, build_artifacts, heading_role, ranges


ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / 'config/chunking-v1.json').read_text(encoding='utf-8'))


def document(*pages):
    return {'source': {'pdf': 'source.pdf'}, 'pages': [
        {'pdf_page': i + 1, 'blocks': [{'type': kind, 'content': text} for kind, text in page]}
        for i, page in enumerate(pages)]}


class ChunkingTests(unittest.TestCase):
    def test_cross_page_word_and_false_heading_stay_in_one_article(self):
        source = document([('doc_title', '学籍管理规定'), ('paragraph_title', '第一章 注册'),
                           ('text', '第四条 保留入学资格时限为')],
                          [('paragraph_title', '1 年，期间不具有学籍。')])
        chunks, _ = Chunker(source, CONFIG).build()
        self.assertEqual(len(chunks), 1)
        self.assertIn('时限为1 年，期间不具有学籍。', chunks[0]['text'])
        self.assertEqual(chunks[0]['pdf_pages'], [1, 2])
        self.assertIsNone(heading_role('paragraph_title', '第十四条 评价目标', 45))

    def test_only_adjacent_same_page_title_prefix_is_joined(self):
        source = document([('doc_title', '文明公约'), ('paragraph_title', '爱护公物 爱校如家')],
                          [('doc_title', '学生管理规定'), ('text', '第一条 正文。')],
                          [('paragraph_title', '某学院'), ('doc_title', '学籍管理规定'),
                           ('text', '第二条 正文。')])
        chunks, _ = Chunker(source, CONFIG).build()
        self.assertIn('爱护公物 爱校如家', chunks[0]['text'])
        self.assertTrue(chunks[1]['text'].startswith('学生管理规定\n'))
        self.assertTrue(chunks[2]['text'].startswith('某学院学籍管理规定\n'))

    def test_embedded_article_boundary_preserves_whole_articles(self):
        first = '第一条 ' + '甲' * 50 + '。'
        second = '第二条 ' + '乙' * 50 + '。'
        source = first + second
        pieces = list(ranges(source, 80, 20))
        self.assertEqual([source[a:b] for a, b, _ in pieces], [first, second])
        self.assertEqual(''.join(source[a:b] for a, b, _ in pieces), source)

    def test_oversized_article_keeps_lead_and_whole_subitems(self):
        intro = '第五条 申请人须符合下列条件：\n'
        source = intro + '\n'.join(f'（{n}）' + '条件' * 15 + '；' for n in '一二三四五六')
        pieces = list(ranges(source, 100, 50))
        self.assertGreater(len(pieces), 1)
        self.assertEqual(''.join(source[a:b] for a, b, _ in pieces), source)
        for left, right, repeated in pieces:
            self.assertLessEqual(right - left + len(repeated) + bool(repeated), 100)
            if left:
                self.assertEqual(repeated, intro.strip())
                self.assertTrue(source[left:].startswith('（'))

    def test_long_unpunctuated_source_has_bounded_lossless_fallback(self):
        source = '甲' * 2501
        pieces = list(ranges(source, 1000, 200))
        self.assertEqual([b - a for a, b, _ in pieces], [1000, 1000, 501])
        self.assertEqual(''.join(source[a:b] for a, b, _ in pieces), source)

    def test_split_table_repeats_shared_conditions_without_splitting_rows(self):
        sub = {'title': '单项奖', 'columns': ['项目', '金额'],
               'rows': [{'项目': '项目' + str(i), '金额': None if i == 2 else '150元'} for i in range(8)],
               'shared_values': {'评选比例': '20%'}, 'source_notes': ['同一学生仅评一次。'],
               'editorial_notes': ['这条整理说明不可进入检索正文']}
        table = {'table_id': 'test-table', 'section': '奖励办法', 'title': '奖励表', 'subtables': [sub]}
        config = {**CONFIG, 'max_body_chars': 100}
        chunks, audit = Chunker(document([('doc_title', '奖励办法'), ('table', table)]), config).build()
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertIn('评选比例：20%', chunk['text'])
            self.assertIn('同一学生仅评一次。', chunk['text'])
            self.assertNotIn('整理说明', chunk['text'])
        self.assertIn('金额：（原表未列值）', '\n'.join(c['text'] for c in chunks))
        self.assertEqual(sum(len(a['table_rows']) for a in audit['chunks']), 8)

    def test_full_handbook_is_reproducible_and_has_actual_procedure_example(self):
        artifacts = build_artifacts(ROOT)
        self.assertEqual(artifacts, build_artifacts(ROOT))
        chunks = [json.loads(line) for line in artifacts['chunks.jsonl'].splitlines()]
        example = [c for c in chunks if '课程学分认定管理办法' in c['text'] and '第三章 认定程序' in c['text']]
        self.assertEqual(len(example), 1)
        self.assertEqual(example[0]['pdf_pages'], [82])
        for marker in ('第十条', '第十一条', '1.学生', '2.学院', '3.教务处'):
            self.assertIn(marker, example[0]['text'])
        for chunk in chunks:
            self.assertEqual(set(chunk), {'chunk_id', 'text', 'source_file', 'pdf_pages'})
        audit = json.loads(artifacts['audit.json'])
        self.assertEqual(audit['summary']['table_rows'], 231)
        # Reconstruct each body block from its spans, then confirm its characters
        # appear in the emitted text in order (ignoring layout whitespace).
        source = json.loads((ROOT / 'output/reviewed-v1/document.json').read_text(encoding='utf-8'))
        originals = {f'p{p["pdf_page"]:03d}/b{i:02d}': b['content'].strip()
                     for p in source['pages'] for i, b in enumerate(p['blocks']) if isinstance(b.get('content'), str)}
        for chunk, entry in zip(chunks, audit['chunks']):
            cursor = 0
            body = ''.join(chunk['text'].split())
            for span in entry['source_spans']:
                part = originals[span['block']][span['start']:span['end']]
                part = ''.join(part.split())
                found = body.find(part, cursor)
                self.assertGreaterEqual(found, cursor, (chunk['chunk_id'], span['block']))
                cursor = found + len(part)


if __name__ == '__main__':
    unittest.main()
