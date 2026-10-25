"""Offline cosine, query cache, source integrity and bounded-context checks."""

import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.embed_chunks import ROOT, identity_for, load_config, run_batches
from scripts.embedding_store import sha256
from scripts.retrieve_chunks import cosine_top_k, load_corpus, query_vector, run_query, unit_vector


def chunk(cid):
    return {'chunk_id': cid, 'text': f'正文 {cid}', 'source_file': 'test.pdf', 'pdf_pages': [1],
            'related_chunk_ids': []}


class FakeClient:
    def __init__(self):
        self.attempts = 0
        self.calls = []

    def embed(self, texts):
        self.attempts += 1
        self.calls.append(texts)
        return {'vectors': [[1.0, 0.0]], 'usage': {'prompt_tokens': 5, 'total_tokens': 5}}


class CosineTests(unittest.TestCase):
    def test_ranking_uses_angle_instead_of_raw_magnitude(self):
        # Raw dot product would incorrectly rank [100,100] above [1,0].
        chunks = [chunk('large'), chunk('aligned'), chunk('opposite')]
        documents = [unit_vector(v, 2) for v in ([100, 100], [1, 0], [-1, 0])]
        hits = cosine_top_k([1, 0], documents, chunks, 3)
        self.assertEqual([h['chunk_id'] for h in hits], ['aligned', 'large', 'opposite'])
        self.assertAlmostEqual(hits[0]['cosine_similarity'], 1)
        self.assertAlmostEqual(hits[1]['cosine_similarity'], math.sqrt(0.5))
        self.assertAlmostEqual(hits[2]['cosine_similarity'], -1)

    def test_exact_top10_and_stable_source_order_for_ties(self):
        chunks = [chunk(str(i)) for i in range(15)]
        hits = cosine_top_k([1, 0], [[1, 0] for _ in chunks], chunks, 10)
        self.assertEqual([h['chunk_id'] for h in hits], [str(i) for i in range(10)])
        self.assertEqual([h['rank'] for h in hits], list(range(1, 11)))

    def test_invalid_vectors_and_top_k_are_rejected(self):
        for vector in ([0, 0], [float('nan'), 1], [True, 0], [1], [float('inf'), 0]):
            with self.subTest(vector=vector), self.assertRaises(ValueError):
                unit_vector(vector, 2)
        with self.assertRaises(ValueError):
            cosine_top_k([1, 0], [[1, 0]], [chunk('a')], 21)
        with self.assertRaises(ValueError):
            cosine_top_k([1, 0], [[1, 0, 0]], [chunk('a')], 1)


class QueryCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name)
        self.client = FakeClient()
        self.config = {'model': 'test-model', 'dimensions': 2}

    def test_repeat_question_reuses_cache_with_no_api_usage(self):
        first, metadata = query_vector(' 借期？ ', 'source-identity', self.config, self.cache, self.client)
        second, cached = query_vector('借期？', 'source-identity', self.config, self.cache, self.client, offline=True)
        self.assertEqual(first, second)
        self.assertFalse(metadata['cache_hit'])
        self.assertTrue(cached['cache_hit'])
        self.assertEqual(cached['http_attempts'], 0)
        self.assertEqual(cached['usage_this_run'], {})
        self.assertEqual(self.client.calls, [['借期？']])

    def test_changed_corpus_and_new_offline_question_do_not_reuse_cache(self):
        query_vector('借期？', 'source-one', self.config, self.cache, self.client)
        with self.assertRaises(ValueError):
            query_vector('借期？', 'source-two', self.config, self.cache, self.client, offline=True)
        with self.assertRaises(ValueError):
            query_vector('新问题', 'source-one', self.config, self.cache, self.client, offline=True)
        self.assertEqual(self.client.attempts, 1)

    def test_corrupt_cached_vector_is_rejected_without_new_charge(self):
        query_vector('借期？', 'identity', self.config, self.cache, self.client)
        path = next(self.cache.glob('*.json'))
        record = json.loads(path.read_text(encoding='utf-8'))
        record['vector'][0] = 0.5
        path.write_text(json.dumps(record), encoding='utf-8')
        with self.assertRaises(ValueError):
            query_vector('借期？', 'identity', self.config, self.cache, self.client)
        self.assertEqual(self.client.attempts, 1)

    def test_top10_is_distinct_from_limited_answer_context(self):
        chunks = [chunk(str(i)) for i in range(12)]
        chunks[0]['related_chunk_ids'] = [str(i) for i in range(1, 12)]
        limits = {'max_seed_chunks': 8, 'max_added_chunks': 4, 'max_total_chars': 6000, 'max_candidates': 64}
        report = run_query('问题', chunks, [[1, 0] for _ in chunks], 'identity', self.config,
                           self.client, self.cache, top_k=10, expand_top=1, expansion_limits=limits)
        self.assertEqual(len(report['hits']), 10)
        self.assertEqual(len(report['answer_context']['chunks']), 5)
        self.assertFalse(report['answer_context']['complete'])
        self.assertFalse(report['reranker_used'])
        self.assertFalse(report['answer_model_used'])

    def test_invalid_queries_do_not_call_api(self):
        for question in ('', ' ', '字' * 501):
            with self.subTest(question_length=len(question)), self.assertRaises(ValueError):
                query_vector(question, 'identity', self.config, self.cache, self.client)
        self.assertEqual(self.client.calls, [])


class LocalCorpusTests(unittest.TestCase):
    @unittest.skipUnless((ROOT / 'output/embeddings-v1/vectors.jsonl').exists(), 'Local embeddings have not been generated')
    def test_completed_local_corpus_matches_ordered_source_and_unit_norms(self):
        config = load_config(ROOT)
        chunks, normalized, _, receipt = load_corpus(
            ROOT, config, 'https://dashscope.aliyuncs.com/compatible-mode/v1')
        self.assertEqual(len(chunks), 266)
        self.assertEqual(len(normalized), 266)
        self.assertEqual(receipt['status'], 'completed')
        for vector in normalized:
            self.assertEqual(len(vector), 1024)
            self.assertAlmostEqual(math.hypot(*vector), 1.0)


class PortableCorpusTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {**load_config(ROOT), 'dimensions': 2, 'pause_seconds': 0}
        self.source = [{**chunk('a'), 'retrieval_text': '正文 a'}]
        linked = self.root / 'output/linked-v1'
        linked.mkdir(parents=True)
        raw = (json.dumps(self.source[0], ensure_ascii=False) + '\n').encode('utf-8')
        (linked / 'chunks.jsonl').write_bytes(raw)
        self.source_sha = sha256(raw)
        (linked / 'run.json').write_text(json.dumps({
            'artifact_sha256': {'chunks.jsonl': self.source_sha}}), encoding='utf-8')
        self.original_url = 'https://ws-example.cn-beijing.maas.aliyuncs.com/compatible-mode/v1'
        basis, self.original_identity = identity_for(self.source_sha, self.config, self.original_url)

        class Client(FakeClient):
            def embed(client, texts):
                return {**super().embed(texts), 'model': self.config['model']}

        run_batches(self.source, self.config, self.root / 'output/embeddings-v1',
                    basis, self.original_identity, Client(), progress=lambda _: None)

    def test_other_supported_endpoint_reuses_vectors_with_separate_cache_identity(self):
        alternate = 'https://dashscope.aliyuncs.com/compatible-mode/v1'
        chunks, vectors, identity, receipt = load_corpus(self.root, self.config, alternate)
        self.assertEqual(chunks, self.source)
        self.assertEqual(vectors, [[1.0, 0.0]])
        self.assertEqual(receipt['identity'], self.original_identity)
        self.assertNotEqual(identity, self.original_identity)
        self.assertEqual(identity, identity_for(self.source_sha, self.config, alternate)[1])
        self.assertEqual(load_corpus(self.root, self.config, self.original_url)[2],
                         self.original_identity)

    def test_portability_does_not_accept_other_providers_or_changed_model(self):
        with self.assertRaises(ValueError):
            load_corpus(self.root, self.config, 'https://example.com/compatible-mode/v1')
        with self.assertRaises(ValueError):
            load_corpus(self.root, {**self.config, 'model': 'different-model'}, self.original_url)
        with self.assertRaises(ValueError):
            load_corpus(self.root, {**self.config, 'dimensions': 3}, self.original_url)

    def test_changed_manifest_or_vectors_still_fail_integrity_checks(self):
        output = self.root / 'output/embeddings-v1'
        manifest_path = output / 'manifest.json'
        original = manifest_path.read_bytes()
        manifest = json.loads(original)
        manifest['basis']['base_url'] = 'https://dashscope.aliyuncs.com/compatible-mode/v1'
        manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
        with self.assertRaises(ValueError):
            load_corpus(self.root, self.config, self.original_url)
        manifest_path.write_bytes(original)
        with (output / 'vectors.jsonl').open('ab') as stream:
            stream.write(b'\n')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            load_corpus(self.root, self.config, self.original_url)


if __name__ == '__main__':
    unittest.main()
