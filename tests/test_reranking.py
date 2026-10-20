"""Offline API contracts, source mapping, cache invalidation and final-hit expansion."""

import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import urllib.error

from scripts.embed_chunks import ROOT
from scripts.embedding_api import ApiFailure
from scripts.rerank_api import RerankClient, load_rerank_config, rerank_endpoint, validate_rerank_response
from scripts.rerank_chunks import rerank_hits
from scripts.retrieve_chunks import run_query


BASE = 'https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1'


def response(count=10):
    return {'output': {'results': [{'index': i, 'relevance_score': (i + 1) / count}
                                   for i in reversed(range(count))]},
            'usage': {'total_tokens': 120}}


def candidates(count=10):
    return [{'rank': i + 1, 'chunk_id': str(i), 'text': f'原文{i}', 'retrieval_text': f'检索上下文{i}',
             'source_file': 'test.pdf', 'pdf_pages': [i + 1], 'cosine_similarity': 1 - i / count}
            for i in range(count)]


class FakeReranker:
    endpoint = rerank_endpoint(BASE)

    def __init__(self):
        self.attempts = 0
        self.calls = []

    def rerank(self, question, documents):
        self.attempts += 1
        self.calls.append((question, documents))
        body = response(len(documents))
        return {'results': body['output']['results'], 'usage': body['usage']}


class FakeEmbedding:
    attempts = 0

    def embed(self, texts):
        self.attempts += 1
        return {'vectors': [[1, 0]], 'usage': {'total_tokens': 3}}


class FakeOpener:
    def __init__(self, *items):
        self.items = iter(items)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        result = next(self.items)
        if isinstance(result, Exception):
            raise result
        return io.BytesIO(json.dumps(result).encode('utf-8'))


class RerankApiTests(unittest.TestCase):
    def setUp(self):
        self.config = load_rerank_config(ROOT)

    def test_native_payload_uses_same_workspace_and_returns_all_indices(self):
        opener = FakeOpener(response(2))
        client = RerankClient(BASE, 'secret-test-value', self.config, opener=opener)
        result = client.rerank('奖学金条件？', ['国籍年级', '学业成绩'])
        request = opener.requests[0]
        self.assertEqual(request.full_url, BASE.replace('/compatible-mode/v1',
                         '/api/v1/services/rerank/text-rerank/text-rerank'))
        payload = json.loads(request.data)
        self.assertEqual(payload['input'], {'query': '奖学金条件？', 'documents': ['国籍年级', '学业成绩']})
        self.assertEqual(payload['parameters']['top_n'], 2)
        self.assertEqual(result['results'][0]['index'], 1)

    def test_invalid_indices_scores_usage_and_count_are_rejected(self):
        bad_bodies = [response(1)]
        for key, value in [('index', 0), ('index', True), ('index', 5),
                           ('relevance_score', float('nan')), ('relevance_score', 1.1),
                           ('relevance_score', True)]:
            body = response(2)
            body['output']['results'][0][key] = value
            bad_bodies.append(body)
        body = response(2)
        body['usage']['total_tokens'] = -1
        bad_bodies.append(body)
        for body in bad_bodies:
            with self.subTest(body=body), self.assertRaises(ValueError):
                validate_rerank_response(body, 2)
        tied, _ = validate_rerank_response({'output': {'results': [
            {'index': 1, 'relevance_score': 0.5}, {'index': 0, 'relevance_score': 0.5}]},
            'usage': {'total_tokens': 1}}, 2)
        self.assertEqual([r['index'] for r in tied], [0, 1])

    def test_retry_transient_error_but_auth_error_is_sanitized_and_not_retried(self):
        def error(status):
            return urllib.error.HTTPError(BASE, status, 'remote secret', {},
                 io.BytesIO(b'{"code":"InvalidApiKey","message":"secret-test-value"}'))
        opener = FakeOpener(error(429), response(2))
        client = RerankClient(BASE, 'secret-test-value', self.config, opener=opener, sleep=lambda _: None)
        client.rerank('问题', ['一', '二'])
        self.assertEqual(client.attempts, 2)
        client = RerankClient(BASE, 'secret-test-value', self.config, opener=FakeOpener(error(401)))
        with self.assertRaises(ApiFailure) as caught:
            client.rerank('问题', ['一', '二'])
        self.assertNotIn('secret-test-value', str(caught.exception))
        self.assertIn('401', str(caught.exception))
        self.assertEqual(client.attempts, 1)

    def test_bad_input_and_attempt_budget_do_not_send_requests(self):
        opener = FakeOpener(response(2))
        client = RerankClient(BASE, '', self.config, opener=opener)
        for docs in ([], ['字' * 2001], ['字'] * 21):
            with self.assertRaises(ValueError):
                client.rerank('问题', docs)
        client.attempts = self.config['max_requests_per_run']
        with self.assertRaises(ApiFailure):
            client.rerank('问题', ['一'])
        self.assertEqual(opener.requests, [])


class RerankPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name)
        self.config = load_rerank_config(ROOT)
        self.client = FakeReranker()
        self.hits = candidates()
        self.chunks = {h['chunk_id']: h for h in self.hits}

    def rerank(self, *, offline=False):
        return rerank_hits('问题', self.hits, self.chunks, 'corpus', self.config,
                           self.client, self.cache, offline=offline)

    def test_final_five_keep_original_text_citations_and_cosine_rank(self):
        hits, meta = self.rerank()
        self.assertEqual([h['chunk_id'] for h in hits], ['9', '8', '7', '6', '5'])
        self.assertEqual([h['rank'] for h in hits], [1, 2, 3, 4, 5])
        self.assertEqual(hits[0]['cosine_rank'], 10)
        self.assertEqual(hits[0]['text'], '原文9')
        self.assertEqual(hits[0]['pdf_pages'], [10])
        self.assertEqual(self.client.calls[0][1][0], '检索上下文0')
        self.assertEqual(meta['usage_this_run'], {'total_tokens': 120})
        cached_hits, cached = self.rerank(offline=True)
        self.assertEqual(cached_hits, hits)
        self.assertEqual(cached['http_attempts'], 0)
        self.assertEqual(cached['usage_this_run'], {})
        self.assertEqual(self.client.attempts, 1)

    def test_candidate_text_order_instruction_endpoint_and_corpus_invalidate_cache(self):
        self.rerank()
        for name in ('text', 'order', 'instruction', 'endpoint', 'corpus'):
            with self.subTest(change=name):
                hits, config, client, identity = candidates(), load_rerank_config(ROOT), FakeReranker(), 'corpus'
                chunks = {h['chunk_id']: h for h in hits}
                if name == 'text':
                    chunks['0']['retrieval_text'] = '修改文字'
                elif name == 'order':
                    hits.reverse()
                elif name == 'instruction':
                    config['instruct'] = 'another task'
                elif name == 'endpoint':
                    client.endpoint = 'another endpoint'
                else:
                    identity = 'another-corpus'
                with self.assertRaises(ValueError):
                    rerank_hits('问题', hits, chunks, identity, config, client, self.cache, offline=True)
                self.assertEqual(client.attempts, 0)
        self.assertEqual(self.client.attempts, 1)

    def test_corrupt_cached_score_is_rejected_without_network_call(self):
        self.rerank()
        path = next(self.cache.glob('*.json'))
        record = json.loads(path.read_text(encoding='utf-8'))
        record['result']['results'][0]['relevance_score'] = 0.2
        path.write_text(json.dumps(record), encoding='utf-8')
        with self.assertRaises(ValueError):
            self.rerank()
        self.assertEqual(self.client.attempts, 1)

    def test_expansion_uses_final_rerank_seeds_instead_of_original_cosine_seeds(self):
        self.hits[9]['related_chunk_ids'] = ['neighbor']
        neighbor = {**candidates(1)[0], 'chunk_id': 'neighbor', 'text': '表格补读'}
        source = self.hits + [neighbor]
        report = run_query('问题', source, [[1, 0]] * len(source), 'corpus',
                           {'model': 'fake', 'dimensions': 2}, FakeEmbedding(), self.cache / 'query',
                           rerank_config=self.config, rerank_client=self.client,
                           rerank_cache_dir=self.cache / 'rerank', expand_top=1,
                           expansion_limits={'max_seed_chunks': 8, 'max_added_chunks': 4,
                                             'max_total_chars': 6000, 'max_candidates': 64})
        self.assertTrue(report['reranker_used'])
        self.assertFalse(report['answer_model_used'])
        self.assertEqual(len(report['hits']), 5)
        self.assertEqual(len(report['vector_candidates']), 10)
        self.assertEqual(report['answer_context']['seed_chunk_ids'], ['9'])
        self.assertEqual(report['answer_context']['added_chunk_ids'], ['neighbor'])


if __name__ == '__main__':
    unittest.main()
