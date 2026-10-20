"""Offline tests: API validation/retries, atomic checkpoints and ordered resume."""

from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import urllib.error

from scripts.embed_chunks import identity_for, load_config, load_source, run_batches
from scripts.embedding_api import ApiFailure, EmbeddingClient, validate_base_url, validate_response
from scripts.embedding_store import output_lock, persist_batch, saved_batches, sha256


ROOT = Path(__file__).resolve().parents[1]
MODEL = 'qwen3.7-text-embedding'
CONFIG = {**load_config(ROOT), 'dimensions': 3, 'batch_size': 2, 'pause_seconds': 0}
BASE_URL = 'https://dashscope.aliyuncs.com/compatible-mode/v1'


def response(texts):
    # Reverse response order deliberately: clients must restore input order by index.
    return {'model': MODEL, 'data': [{'index': i, 'embedding': [float(i + 1), 0.25, -0.5]}
                                    for i in reversed(range(len(texts)))],
            'usage': {'prompt_tokens': len(texts) * 3, 'total_tokens': len(texts) * 3}}


class FakeOpener:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return BytesIO(json.dumps(reply).encode('utf-8'))


class FakeClient:
    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on

    def embed(self, texts):
        self.calls.append(texts)
        if len(self.calls) == self.fail_on:
            raise ApiFailure('Simulated failure')
        vectors, usage = validate_response(response(texts), len(texts), 3, MODEL)
        return {'model': MODEL, 'vectors': vectors, 'usage': usage}


class EmbeddingApiTests(unittest.TestCase):
    def test_request_payload_and_response_index_order(self):
        opener = FakeOpener([response(['第一块', '第二块'])])
        client = EmbeddingClient(BASE_URL, 'dummy-test-key', CONFIG, opener=opener)
        result = client.embed(['第一块', '第二块'])
        self.assertEqual([v[0] for v in result['vectors']], [1.0, 2.0])
        request, timeout = opener.calls[0]
        self.assertEqual(request.full_url, BASE_URL + '/embeddings')
        self.assertEqual(json.loads(request.data), {'model': MODEL, 'input': ['第一块', '第二块'],
                         'dimensions': 3, 'encoding_format': 'float'})
        self.assertEqual(timeout, 30)

    def test_transient_retry_is_bounded_and_bad_credentials_do_not_retry(self):
        transient = urllib.error.HTTPError(BASE_URL, 429, 'Too many', {}, BytesIO(b'{}'))
        opener = FakeOpener([transient, response(['text'])])
        delays = []
        client = EmbeddingClient(BASE_URL, 'dummy', CONFIG, opener=opener, sleep=delays.append)
        client.embed(['text'])
        self.assertEqual(len(opener.calls), 2)
        self.assertEqual(delays, [1])
        bad = urllib.error.HTTPError(BASE_URL, 401, 'invalid', {},
                                    BytesIO(b'{"error":{"code":"InvalidApiKey","message":"dummy-secret"}}'))
        client = EmbeddingClient(BASE_URL, 'dummy-secret', CONFIG, opener=FakeOpener([bad]))
        with self.assertRaises(ApiFailure) as error:
            client.embed(['text'])
        self.assertNotIn('dummy-secret', str(error.exception))
        self.assertEqual(client.attempts, 1)

    def test_network_errors_and_total_request_budget_stop(self):
        opener = FakeOpener([urllib.error.URLError('dummy-secret') for _ in range(3)])
        client = EmbeddingClient(BASE_URL, 'dummy-secret', CONFIG, opener=opener, sleep=lambda _: None)
        with self.assertRaises(ApiFailure) as error:
            client.embed(['text'])
        self.assertEqual(client.attempts, 3)
        self.assertNotIn('dummy-secret', str(error.exception))
        config = {**CONFIG, 'max_requests_per_run': 1}
        client = EmbeddingClient(BASE_URL, 'dummy', config, opener=FakeOpener([response(['text'])]))
        client.embed(['text'])
        with self.assertRaises(ApiFailure):
            client.embed(['text'])

    def test_wrong_dimension_duplicate_index_nan_zero_and_wrong_model_are_rejected(self):
        baseline = response(['a', 'b'])
        cases = []
        wrong_dimension = deepcopy(baseline)
        wrong_dimension['data'][0]['embedding'] = [1.0]
        cases.append(wrong_dimension)
        duplicate = deepcopy(baseline)
        duplicate['data'][0]['index'] = duplicate['data'][1]['index']
        cases.append(duplicate)
        nan = deepcopy(baseline)
        nan['data'][0]['embedding'][0] = float('nan')
        cases.append(nan)
        zero = deepcopy(baseline)
        zero['data'][0]['embedding'] = [0, 0, 0]
        cases.append(zero)
        cases.append({**baseline, 'model': 'different-model'})
        for case in cases:
            with self.subTest(case=cases.index(case)), self.assertRaises(ValueError):
                validate_response(case, 2, 3, MODEL)

    def test_endpoint_does_not_allow_secret_to_be_redirected_to_other_hosts(self):
        for url in ('http://dashscope.aliyuncs.com/compatible-mode/v1',
                    'https://example.com/compatible-mode/v1',
                    BASE_URL + '?api_key=dummy', BASE_URL + '/embeddings'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_base_url(url)


class EmbeddingCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name) / 'vectors'
        self.source = [{'chunk_id': f'chunk-{i:04d}', 'retrieval_text': f'正文{i}'} for i in range(5)]
        self.basis, self.identity = identity_for('source-hash', CONFIG, BASE_URL)

    def run_client(self, client, limit=None):
        return run_batches(self.source, CONFIG, self.output, self.basis, self.identity,
                           client, limit=limit, progress=lambda _: None)

    def test_interrupted_run_resumes_only_missing_ordered_chunks(self):
        client = FakeClient(fail_on=2)
        with self.assertRaises(ApiFailure):
            self.run_client(client)
        batches, cursor = saved_batches(self.output, self.source, CONFIG, self.identity)
        self.assertEqual(cursor, 2)
        self.assertEqual(len(batches), 1)
        resumed = FakeClient()
        receipt = self.run_client(resumed)
        self.assertEqual(resumed.calls, [['正文2', '正文3'], ['正文4']])
        self.assertEqual(receipt['status'], 'completed')
        self.assertEqual(receipt['usage_tokens_from_saved_batches'], 15)
        export = [json.loads(line) for line in (self.output / 'vectors.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([record['chunk_id'] for record in export], [chunk['chunk_id'] for chunk in self.source])
        self.assertEqual(receipt['vectors_sha256'], sha256((self.output / 'vectors.jsonl').read_bytes()))
        no_op = FakeClient()
        self.run_client(no_op)
        self.assertEqual(no_op.calls, [])

    def test_partial_trial_can_resume_without_changing_batch_config(self):
        trial = FakeClient()
        receipt = self.run_client(trial, limit=1)
        self.assertEqual(receipt['completed_chunks'], 1)
        self.assertEqual(receipt['status'], 'partial')
        self.assertFalse((self.output / 'vectors.jsonl').exists())
        resumed = FakeClient()
        self.run_client(resumed)
        self.assertEqual(resumed.calls, [['正文1', '正文2'], ['正文3', '正文4']])

    def test_changed_configuration_stops_before_api(self):
        self.run_client(FakeClient(), limit=2)
        client = FakeClient()
        with self.assertRaises(ValueError):
            run_batches(self.source, {**CONFIG, 'dimensions': 4}, self.output,
                        {**self.basis, 'dimensions': 4}, 'new-identity', client, progress=lambda _: None)
        self.assertEqual(client.calls, [])

    def test_valid_looking_changed_vector_is_detected_by_batch_checksum(self):
        self.run_client(FakeClient(), limit=2)
        path = self.output / 'batches/000000.json'
        batch = json.loads(path.read_text(encoding='utf-8'))
        batch['records'][0]['embedding'][0] += 0.01
        path.write_text(json.dumps(batch), encoding='utf-8')
        client = FakeClient()
        with self.assertRaises(ValueError):
            self.run_client(client)
        self.assertEqual(client.calls, [])

    def test_changed_source_text_stops_before_network(self):
        self.run_client(FakeClient(), limit=2)
        self.source[0]['retrieval_text'] += '（已修改）'
        client = FakeClient()
        with self.assertRaises(ValueError):
            self.run_client(client)
        self.assertEqual(client.calls, [])

    def test_crash_during_atomic_save_leaves_no_completed_batch(self):
        self.output.mkdir()
        result = FakeClient().embed(['正文0', '正文1'])
        with patch('scripts.embedding_store.os.replace', side_effect=OSError('simulated disk failure')):
            with self.assertRaises(OSError):
                persist_batch(self.output, self.source, 0, result, CONFIG, self.identity)
        batches, cursor = saved_batches(self.output, self.source, CONFIG, self.identity)
        self.assertEqual((batches, cursor), ([], 0))
        persist_batch(self.output, self.source, 0, result, CONFIG, self.identity)
        self.assertEqual(saved_batches(self.output, self.source, CONFIG, self.identity)[1], 2)

    def test_concurrent_writer_is_rejected_and_lock_releases(self):
        with output_lock(self.output):
            with self.assertRaises(ValueError):
                with output_lock(self.output):
                    self.fail('Second writer acquired the output lock')
        with output_lock(self.output):
            pass

    def test_real_source_preflight_is_offline_and_within_limits(self):
        source, digest = load_source(ROOT, load_config(ROOT))
        self.assertEqual(len(source), 266)
        self.assertEqual(len(digest), 64)
        self.assertTrue(all(chunk['retrieval_text'] for chunk in source))


if __name__ == '__main__':
    unittest.main()
