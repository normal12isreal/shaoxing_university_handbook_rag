"""Public Python integration: reuse, terminal routes, errors and run isolation."""
from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from student_handbook_rag import HandbookRAG, ModelAPIError
from scripts.embed_chunks import ROOT, load_local_credentials


def model_response(answer, tokens):
    return {'answer': json.dumps(answer, ensure_ascii=False) if isinstance(answer, dict) else answer,
            'complete': True, 'response_model': 'qwen3.7-plus', 'finish_reason': 'stop',
            'usage': {'total_tokens': tokens}}


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        shutil.copytree(ROOT / 'config', self.root / 'config')
        self.chunks = [{'chunk_id': 'a', 'text': '第七条 具有中华人民共和国国籍。',
                        'source_file': 'handbook.pdf', 'pdf_pages': [146, 147]}]
        self.context = {'complete': True, 'chunks': self.chunks, 'total_chars': 24}
        self.assessment = {'decision': 'sufficient', 'reason': '条款直接支持回答。', 'support_ids': ['a'],
            'search_query': '', 'question_status': 'clear', 'missing_aspects': [],
            'next_action': 'answer_local', 'retrieval_query': '', 'followup_question': ''}
        self.review = {'verdict': 'pass', 'issues': []}
        self.draft_error = None
        self.clients = []

        def fake_client(base_url, key, config):
            class Client:
                attempts = 0

                def generate(client, messages):
                    client.attempts += 1
                    if config['max_completion_tokens'] == 900:
                        return model_response(self.assessment, 3)
                    if config['max_completion_tokens'] == 1000:
                        return model_response(self.review, 5)
                    if self.draft_error:
                        raise self.draft_error
                    return model_response('不可以。第七条要求具有中华人民共和国国籍。[资料1]', 7)
            client = Client()
            self.clients.append(client)
            return client

        def mock(name, **kwargs):
            return self.stack.enter_context(patch('student_handbook_rag.client.' + name, **kwargs))

        mock('load_local_credentials', return_value=('fake-secret',
             'https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1'))
        self.corpus = mock('load_corpus', return_value=(self.chunks, [[1.0]], 'identity',
                                                       {'vectors_sha256': 'sha'}))
        self.retrieve = mock('run_query', side_effect=lambda *a, **k: deepcopy({
            'hits': [{'chunk_id': 'a'}],
            'query_embedding': {'usage_this_run': {}, 'http_attempts': 0},
            'reranking': {'usage_this_run': {}, 'http_attempts': 0}}))
        mock('expand_context', side_effect=lambda *a: deepcopy(self.context))
        mock('GenerationClient', side_effect=fake_client)

    def test_repeated_calls_reuse_index_but_not_request_budgets_and_keep_distinct_records(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            rag = HandbookRAG(self.root)
            first = rag.ask('外国人能申请国家奖学金吗？', allow_web=False)
            second = rag.ask('国家奖学金需要中国国籍吗？', allow_web=False)
        self.assertEqual(stdout.getvalue(), '')
        self.assertEqual(self.corpus.call_count, 1)
        self.assertEqual(first['status'], 'answered')
        self.assertEqual(first['route'], 'local')
        self.assertEqual(first['reflection'], 'passed')
        self.assertEqual(first['sources'][0]['pdf_pages'], [146, 147])
        self.assertEqual(first['usage']['reported_total_tokens'], 15)
        self.assertEqual([c.attempts for c in self.clients], [1] * 6)
        self.assertNotEqual(first['request_id'], second['request_id'])
        for result in (first, second):
            saved = Path(result['record_path']).read_text(encoding='utf-8')
            self.assertNotIn('fake-secret', saved)
            self.assertEqual(json.loads(saved)['generation']['answer'], result['answer'])
        self.assertEqual(json.loads((self.root / 'output/answers-v1/last_run.json').read_text(
            encoding='utf-8'))['request_id'], second['request_id'])
        self.assertNotIn('messages', first)
        json.dumps(first, ensure_ascii=False)

    def test_clarification_and_refusal_are_normal_results_without_draft_calls(self):
        self.assessment.update(decision='partial', question_status='needs_clarification',
            next_action='ask_user', followup_question='你想问哪一种奖学金？')
        rag = HandbookRAG(self.root)
        result = rag.ask('我能申请奖学金吗？')
        self.assertEqual(result['status'], 'needs_clarification')
        self.assertEqual(result['answer'], '你想问哪一种奖学金？')
        self.assertEqual(result['sources'], [])
        self.assertEqual(sum(c.attempts for c in self.clients), 1)
        self.assessment.update(question_status='clear', next_action='abstain', followup_question='')
        refused = rag.ask('手册没有这个规定怎么办？')
        self.assertEqual(refused['status'], 'abstained')
        self.assertEqual(sum(c.attempts for c in self.clients), 2)

    def test_revision_is_counted_once_and_keeps_unchecked_status(self):
        self.review = {'verdict': 'revise', 'issues': [{'kind': 'scope', 'answer_excerpt': '不可以。',
                        'explanation': '明确指出所指奖种。'}]}
        result = HandbookRAG(self.root).ask('外国人可以申请国家奖学金吗？', allow_web=False)
        self.assertEqual(result['reflection'], 'revised_not_rechecked')
        self.assertEqual(result['usage']['reported_total_tokens'], 22)
        self.assertEqual(sum(s['http_attempts'] for s in result['usage']['stages']), 4)

    def test_invalid_input_never_retrieves_or_sends_requests(self):
        rag = HandbookRAG(self.root)
        for question in (None, '', '   ', 'x' * 501):
            with self.subTest(question=question):
                with self.assertRaises(ValueError):
                    rag.ask(question)
        with self.assertRaises(ValueError):
            rag.ask('问题', allow_web='false')
        self.retrieve.assert_not_called()
        self.assertEqual(self.clients, [])
        self.assertFalse((self.root / 'output').exists())

    def test_upstream_failure_is_catchable_and_persisted(self):
        self.draft_error = ModelAPIError('simulated upstream failure')
        rag = HandbookRAG(self.root)
        with self.assertRaises(ModelAPIError):
            rag.ask('问题', crag=False, reflection=False)
        record = json.loads((self.root / 'output/answers-v1/last_run.json').read_text(encoding='utf-8'))
        self.assertEqual(record['status'], 'generation_failed')
        self.assertEqual(record['generation_http_attempts'], 1)
        self.assertTrue(Path(record['record_path']).is_file())
        # A failed request must not leave the instance locked or exhaust later budgets.
        self.draft_error = None
        self.assertEqual(rag.ask('再次提问', crag=False, reflection=False)['status'], 'answered')

    def test_records_can_be_disabled(self):
        result = HandbookRAG(self.root, save_records=False).ask('问题', crag=False, reflection=False)
        self.assertIsNone(result['record_path'])
        self.assertFalse((self.root / 'output').exists())

    def test_explicit_endpoint_can_be_combined_with_environment_key(self):
        url = 'https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1'
        with patch.dict('os.environ', {'DASHSCOPE_API_KEY': 'env-secret'}, clear=True):
            key, endpoint = load_local_credentials(self.root, base_url=url)
        self.assertEqual((key, endpoint), ('env-secret', url))
        # Fully explicit settings do not depend on an unrelated local credential file.
        (self.root / '.runtime').mkdir()
        (self.root / '.runtime/dashscope.local.json').write_text('invalid JSON', encoding='utf-8')
        self.assertEqual(load_local_credentials(self.root, api_key='explicit', base_url=url), ('explicit', url))

    def test_missing_endpoint_points_to_configuration_without_creating_files(self):
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaisesRegex(ValueError, 'config/dashscope.example.json'):
                load_local_credentials(self.root)
        self.assertFalse((self.root / '.runtime').exists())

    def test_missing_key_stops_before_loading_index_or_calling_models(self):
        for key in ('', 'invalid key'):
            with self.subTest(key=key):
                with patch('student_handbook_rag.client.load_local_credentials',
                           return_value=(key, 'https://dashscope.aliyuncs.com/compatible-mode/v1')):
                    with self.assertRaisesRegex(ValueError, 'DashScope API key'):
                        HandbookRAG(self.root)
        self.corpus.assert_not_called()
        self.assertEqual(self.clients, [])


if __name__ == '__main__':
    unittest.main()
