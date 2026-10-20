"""Source-backed checks for the reviewed handbook tables (physical PDF pages)."""

from decimal import Decimal
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
TABLES = {t['pdf_page']: t for t in json.loads((ROOT / 'data/table_reviews.json').read_text(encoding='utf-8'))['tables']}


class ReviewedTableFacts(unittest.TestCase):
    def test_page37_grade_systems_keep_independent_values(self) -> None:
        rows = TABLES[37]['subtables'][0]['rows']
        mappings = {(r['计分方式'], r['成绩或等级']): r['绩点'] for r in rows}
        self.assertEqual(len(mappings), 15)
        self.assertEqual(mappings['百分制', '100'], '5')
        self.assertEqual(mappings['百分制', '95～99'], '4.5')
        self.assertEqual(mappings['百分制', '90～94'], '4')
        self.assertEqual(mappings['五级制', '优'], '4.5')
        self.assertEqual(mappings['百分制', '59分及以下'], '0')

    def test_page78_team_shares_and_absent_positions(self) -> None:
        for row in TABLES[78]['subtables'][0]['rows']:
            people = int(row['人数'])
            shares = [row[f'排序{i}'] for i in range(1, 6)]
            self.assertTrue(all(v is not None for v in shares[:people]))
            self.assertTrue(all(v is None for v in shares[people:]))
            self.assertEqual(sum(Decimal(v) for v in shares if v is not None), Decimal('1'))

    def test_pages121_122_weights_do_not_count_morality_as_points(self) -> None:
        for page in (121, 122):
            for row in TABLES[page]['subtables'][0]['rows']:
                fields = ('智育评价', '体育评价', '美育评价', '劳动教育评价', '发展评价')
                self.assertEqual(sum(Decimal(row[k].removesuffix('%')) for k in fields), 100)
                self.assertIn('不列入学生综合评价总分', row['德育评价'])

    def test_page134_publication_roles_and_project_limit_are_retained(self) -> None:
        publishing, research, languages, competition = TABLES[134]['subtables']
        self.assertEqual([publishing['rows'][0][k] for k in ('独著', '合著', '主编', '参编')], ['60', '40', '48', '32'])
        horizontal = next(r for r in research['rows'] if r['类别或级别'].startswith('横向课题'))
        self.assertIn('仅限非毕业班学生', horizontal['类别或级别'])
        self.assertEqual(horizontal['分值标准'], '+3-10分（根据项目经费，由学院设定）')
        self.assertIn('分别加50%', horizontal['备注'])
        self.assertEqual(len(languages['rows']), 10)
        self.assertIn('银奖即亚军和季军', competition['source_notes'][0])

    def test_page138_single_awards_keep_shared_percentage(self) -> None:
        single = TABLES[138]['subtables'][0]
        self.assertEqual(len(single['rows']), 6)
        self.assertEqual(single['shared_values']['评选比例'], '20%')
        self.assertEqual(single['shared_values']['奖金额'], '150元')
        self.assertTrue(all(set(row) == {'奖项名称'} for row in single['rows']))


if __name__ == '__main__':
    unittest.main()
