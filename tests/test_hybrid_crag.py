"""Bounded lexical supplement, evidence branches and native web provenance."""

import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.crag import apply_crag, load_crag_config, parse_assessment
from scripts.embed_chunks import ROOT
from scripts.embedding_api import ApiFailure
from scripts.keyword_retrieval import supplement_one
from scripts.rerank_api import load_rerank_config
from scripts.retrieve_chunks import run_query
from scripts.web_search import WebSearchClient, approved_url, parse_web_response
from tests.test_reranking import FakeEmbedding, FakeReranker


def chunk(cid, text):
    return {'chunk_id': cid, 'text': text, 'retrieval_text': text, 'source_file': 'test.pdf', 'pdf_pages': [1]}


CONFIG = load_crag_config(ROOT)
CONTEXT = {'chunks': [chunk('a', '具有中华人民共和国国籍')], 'complete': True, 'total_chars': 13}


def assessment(decision):
    return {'decision': decision, 'reason': '依据是否覆盖问题',
            'support_ids': ['a'] if decision != 'insufficient' else [], 'search_query': '绍兴大学 更名'}


class Evaluator:
    config = {'model': 'test'}

    def __init__(self, decision, error=False):
        self.decision, self.error, self.attempts = decision, error, 0

    def generate(self, messages):
        self.attempts += 1
        if self.error:
            raise ApiFailure('test failed')
        return {'answer': json.dumps(assessment(self.decision)), 'complete': True, 'usage': {'total_tokens': 5}}


class Web:
    def __init__(self, fail=False):
        self.fail, self.attempts = fail, 0

    def search(self, query):
        self.attempts += 1
        if self.fail:
            raise ApiFailure('test failed')
        return {'status': 'supplemented', 'chunk': {**chunk('web-1', '官方网页摘要'), 'kind': 'web_search_summary'}}


def web_response():
    return {'output': {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant',
              'content': '更名已经批准。[ref_1]\n没有引用的模型补充不采纳。\n其他说法。[ref_2]'}}],
              'search_info': {'search_results': [
                  {'index': 1, 'title': '学校公告', 'url': 'https://www.usx.edu.cn/info/1.htm#top'},
                  {'index': 2, 'title': '第三方', 'url': 'https://usx.edu.cn.attacker.example/a'}]}},
            'usage': {'input_tokens': 30, 'output_tokens': 20, 'total_tokens': 50, 'plugins': {'search': {'count': 1}}}}


class KeywordTests(unittest.TestCase):
    def test_only_one_best_is_added_and_duplicates_are_not_backfilled(self):
        chunks = [chunk('a', '国家奖学金国籍条件'), chunk('b', '图书馆借阅规定'), chunk('c', '国家奖学金其他条件')]
        hits, meta = supplement_one('国家奖学金国籍条件', [chunks[1]], chunks)
        self.assertEqual([h['chunk_id'] for h in hits], ['b', 'a'])
        self.assertEqual(meta['added_count'], 1)
        hits, meta = supplement_one('国家奖学金国籍条件', [chunks[0]], chunks)
        self.assertEqual([h['chunk_id'] for h in hits], ['a'])
        self.assertTrue(meta['duplicate'])
        self.assertEqual(meta['added_count'], 0)

    def test_same_text_different_id_and_no_overlap(self):
        chunks = [chunk('a', '国家奖学金'), chunk('b', '国家 奖学金')]
        hits, meta = supplement_one('国家奖学金', [chunks[1]], chunks)
        self.assertEqual(len(hits), 1)
        self.assertTrue(meta['duplicate'])
        hits, meta = supplement_one('zzzz', [chunks[0]], chunks)
        self.assertIsNone(meta['hit_chunk_id'])

    def test_eleventh_candidate_can_be_reranked_to_first_with_correct_source(self):
        chunks = [chunk(str(i), f'其他内容{i}') for i in range(10)] + [chunk('lexical', '国家奖学金国籍条件')]
        reranker = FakeReranker()
        with TemporaryDirectory() as directory:
            report = run_query('国家奖学金国籍条件', chunks, [[1, 0]] * 10 + [[0, 1]], 'identity',
                {'model': 'test', 'dimensions': 2}, FakeEmbedding(), Path(directory) / 'query',
                rerank_config=load_rerank_config(ROOT), rerank_client=reranker,
                rerank_cache_dir=Path(directory) / 'rerank')
        self.assertEqual(len(report['vector_candidates']), 10)
        self.assertEqual(len(report['rerank_candidates']), 11)
        self.assertEqual(len(report['hits']), 5)
        self.assertEqual(report['hits'][0]['chunk_id'], 'lexical')
        self.assertIsNone(report['hits'][0]['cosine_rank'])
        self.assertEqual(len(reranker.calls[0][1]), 11)


class CragTests(unittest.TestCase):
    def test_sufficient_skips_web(self):
        web = Web()
        context, report = apply_crag('问题', CONTEXT, Evaluator('sufficient'), web, 'system')
        self.assertEqual(context, CONTEXT)
        self.assertEqual(web.attempts, 0)
        self.assertEqual(report['route'], 'local')

    def test_partial_keeps_local_and_adds_one_web_block(self):
        context, report = apply_crag('问题', CONTEXT, Evaluator('partial'), Web(), 'system')
        self.assertEqual([c['chunk_id'] for c in context['chunks']], ['a', 'web-1'])
        self.assertEqual(report['web_http_attempts'], 1)

    def test_insufficient_drops_unrelated_hits_and_web_failure_leaves_no_evidence(self):
        for fail in (False, True):
            context, report = apply_crag('问题', CONTEXT, Evaluator('insufficient'), Web(fail), 'system')
            self.assertEqual([c['chunk_id'] for c in context['chunks']], [] if fail else ['web-1'])
            self.assertEqual(report['web_http_attempts'], 1)

    def test_failed_evaluation_cannot_promote_local_evidence(self):
        context, report = apply_crag('问题', CONTEXT, Evaluator('partial', error=True), Web(True), 'system')
        self.assertTrue(report['evaluation_failed'])
        self.assertEqual(context['chunks'], [])

    def test_web_disabled_and_total_budget_are_respected_without_cutting_local_text(self):
        web = Web()
        context, report = apply_crag('问题', CONTEXT, Evaluator('partial'), web, 'system', allow_web=False)
        self.assertEqual(web.attempts, 0)
        self.assertEqual(context['chunks'], CONTEXT['chunks'])
        context, report = apply_crag('问题', CONTEXT, Evaluator('partial'), web, 'system', max_chars=10)
        self.assertEqual([c['chunk_id'] for c in context['chunks']], ['web-1'])
        self.assertEqual(report['omitted_local_ids'], ['a'])
        self.assertFalse(context['complete'])

    def test_invalid_or_truncated_assessment_is_rejected(self):
        for change in ({'support_ids': []}, {'support_ids': ['invented']}, {'decision': 'high'}, {'reason': ''}):
            result = {'complete': True, 'answer': json.dumps({**assessment('sufficient'), **change})}
            with self.assertRaises(ValueError):
                parse_assessment(result, {'a'})
        with self.assertRaises(ValueError):
            parse_assessment({'complete': False}, {'a'})


class WebTests(unittest.TestCase):
    def test_zero_based_provider_source_indices_are_valid(self):
        body = web_response()
        body['output']['search_info']['search_results'][0]['index'] = 0
        body['output']['choices'][0]['message']['content'] = '更名已经批准。[ref_0]'
        result = parse_web_response(body, CONFIG)
        self.assertEqual(result['status'], 'supplemented')
        self.assertIn('更名已经批准', result['chunk']['text'])

    def test_accept_only_official_cited_paragraphs_and_return_real_urls(self):
        result = parse_web_response(web_response(), CONFIG)
        self.assertEqual(result['provider_search_count'], 1)
        self.assertEqual(len(result['accepted_sources']), 1)
        text = result['chunk']['text']
        self.assertIn('https://www.usx.edu.cn/info/1.htm', text)
        self.assertNotIn('#top', text)
        self.assertNotIn('没有引用', text)
        self.assertNotIn('其他说法', text)
        self.assertEqual(result['chunk']['kind'], 'web_search_summary')

    def test_no_sources_or_only_untrusted_sources_cannot_become_evidence(self):
        body = web_response()
        body['output']['search_info']['search_results'] = []
        result = parse_web_response(body, CONFIG)
        self.assertIsNone(result['chunk'])
        self.assertEqual(result['status'], 'no_reliable_evidence')
        for value in ('https://usx.edu.cn.evil.com', 'https://evil-usx.edu.cn', 'file:///a',
                      'https://user:password@usx.edu.cn/a', 'https://usx.edu.cn:9000'):
            self.assertIsNone(approved_url(value, CONFIG['allowed_domains']))

    def test_repeated_paragraphs_are_deduplicated_and_budget_does_not_cut_facts(self):
        body = web_response()
        body['output']['choices'][0]['message']['content'] = '更名已经批准。[ref_1]\n更名已经批准。[ref_1]'
        result = parse_web_response(body, CONFIG)
        self.assertEqual(result['chunk']['text'].count('更名已经批准'), 1)
        result = parse_web_response(body, {**CONFIG, 'max_web_chars': 5})
        self.assertIsNone(result['chunk'])

    def test_payload_forces_native_search_and_client_does_not_retry(self):
        class Opener:
            requests = []
            def open(self, request, timeout):
                self.requests.append(request)
                return io.BytesIO(json.dumps(web_response()).encode())
        opener = Opener()
        client = WebSearchClient('https://ws-test.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
                                 'fake-key', CONFIG, opener=opener)
        client.search('绍兴大学 更名')
        request = opener.requests[0]
        self.assertTrue(request.full_url.endswith('/api/v1/services/aigc/text-generation/generation'))
        self.assertTrue(json.loads(request.data)['parameters']['search_options']['forced_search'])
        with self.assertRaises(ApiFailure):
            client.search('问题')
        self.assertEqual(len(opener.requests), 1)


if __name__ == '__main__':
    unittest.main()
