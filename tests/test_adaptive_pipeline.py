"""Offline behavioral checks for bounded routing and evidence-grounded draft review."""
import json
import unittest
from unittest.mock import Mock

from scripts.adaptive_routing import apply_adaptive_routing, merge_context
from scripts.answer_question import build_messages, citation_audit
from scripts.answer_reflection import reflect_answer
from scripts.embedding_api import ApiFailure
from scripts.evidence_assessment import parse_adaptive_assessment


def chunk(cid, text):
    return {'chunk_id': cid, 'text': text, 'source_file': 'test.pdf', 'pdf_pages': [1]}


def context(*chunks):
    return {'chunks': list(chunks), 'complete': True, 'total_chars': sum(len(c['text']) for c in chunks)}


def response(answer):
    return {'answer': json.dumps(answer, ensure_ascii=False) if isinstance(answer, dict) else answer,
            'complete': True, 'usage': {'total_tokens': 1}}


class Client:
    def __init__(self, data):
        self.data, self.attempts, self.messages = data, 0, None

    def generate(self, messages):
        self.attempts += 1
        self.messages = messages
        if isinstance(self.data, Exception):
            raise self.data
        return response(self.data)


def assessment(action, ids=('a',)):
    return {'decision': 'sufficient' if action == 'answer_local' else 'partial',
        'reason': '检查全部用户所问。', 'support_ids': list(ids), 'search_query': '学校 假期开放',
        'question_status': 'needs_clarification' if action == 'ask_user' else 'clear',
        'next_action': action, 'missing_aspects': [] if action == 'answer_local' else ['待核实事项'],
        'retrieval_query': '奖学金 申请流程', 'followup_question': '你想申请哪一种奖学金？' if action == 'ask_user' else ''}


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.local = context(chunk('a', '资格条件'))
        self.web = Mock(attempts=0)
        self.retry = Mock()

    def route(self, data, **kwargs):
        clients = iter(Client(d) for d in data)
        return apply_adaptive_routing('问题', self.local, lambda: next(clients), 'prompt',
                                      self.retry, self.web, **kwargs)

    def test_ambiguous_question_asks_user_without_search_or_generation(self):
        _, report = self.route([assessment('ask_user')])
        self.assertEqual(report['route'], 'ask_user')
        self.assertIn('哪一种', report['terminal_response'])
        self.retry.assert_not_called()
        self.web.search.assert_not_called()

    def test_local_missing_procedure_is_retrieved_then_reassessed(self):
        self.retry.return_value = (context(chunk('b', '申请流程')), {'hits': [{'chunk_id': 'b'}]})
        result, report = self.route([assessment('retrieve_local'), assessment('answer_local', ('a', 'b'))])
        self.assertEqual(report['route'], 'local')
        self.assertEqual(len(report['assessments']), 2)
        self.assertEqual([c['chunk_id'] for c in result['chunks']], ['b', 'a'])
        self.retry.assert_called_once()
        self.web.search.assert_not_called()

    def test_retry_budget_does_not_loop_even_when_context_flag_is_complete(self):
        self.retry.return_value = (self.local, {})
        _, report = self.route([assessment('retrieve_local'), assessment('retrieve_local')])
        self.assertEqual(report['route'], 'limited_evidence')
        self.assertTrue(report['budget_exhausted'])
        self.retry.assert_called_once()

    def test_retry_failure_stops_without_unplanned_web_escalation(self):
        self.retry.side_effect = ApiFailure('local failure')
        _, report = self.route([assessment('retrieve_local')])
        self.assertEqual(report['route'], 'abstain')
        self.web.search.assert_not_called()

    def test_insufficient_evidence_drops_unrelated_local_and_web_can_be_disabled(self):
        data = {**assessment('search_web', ()), 'decision': 'insufficient'}
        self.web.search.return_value = {'status': 'supplemented', 'chunk': chunk('web-1', '官方检索摘要')}
        result, report = self.route([data])
        self.assertEqual([c['chunk_id'] for c in result['chunks']], ['web-1'])
        self.web.search.assert_called_once()
        self.web.reset_mock()
        _, report = self.route([data], allow_web=False)
        self.assertEqual(report['route'], 'abstain')
        self.web.search.assert_not_called()

    def test_invalid_assessment_fails_closed(self):
        _, report = self.route([{'decision': 'sufficient'}])
        self.assertEqual(report['route'], 'abstain')
        self.assertEqual(report['assessments'][0]['status'], 'failed')

    def test_conflicting_action_or_invented_source_is_rejected(self):
        for change in [{'next_action': 'search_web'}, {'support_ids': ['invented']}, {'missing_aspects': ['missing']}]:
            with self.assertRaises(ValueError):
                parse_adaptive_assessment(response({**assessment('answer_local'), **change}), {'a'})

    def test_merge_deduplicates_and_keeps_new_source_blocks_whole(self):
        result = merge_context(self.local, context(chunk('b', '新材料'), chunk('c', '资格 条件')), max_chars=5)
        self.assertEqual([c['chunk_id'] for c in result['chunks']], ['b'])
        self.assertFalse(result['complete'])


class ReflectionTests(unittest.TestCase):
    def setUp(self):
        self.context = context(chunk('a', '每周不超过8小时。'))
        self.messages, _ = build_messages('每周最多几小时？', self.context, 'system')
        self.draft = response('每周40小时。[资料1]')

    def check(self, critic, reviser):
        return reflect_answer('每周最多几小时？', self.context, self.draft, critic, reviser,
                              'reflection', self.messages, citation_audit)

    def test_pass_preserves_draft_and_passes_explicit_source_labels(self):
        critic = Client({'verdict': 'pass', 'issues': []})
        reviser = Mock()
        result, report = self.check(critic, reviser)
        self.assertEqual(result, self.draft)
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(json.loads(critic.messages[1]['content'])['资料'][0]['label'], '资料1')
        reviser.assert_not_called()

    def test_actual_wrong_draft_is_revised_once_without_claiming_recheck(self):
        critic = Client({'verdict': 'revise', 'issues': [{'kind': 'unsupported',
            'answer_excerpt': '每周40小时。', 'explanation': '原文每周8小时，40小时为每月。'}]})
        reviser = Client('每周8小时。[资料1]')
        factory = Mock(return_value=reviser)
        result, report = self.check(critic, factory)
        self.assertEqual(result['answer'], '每周8小时。[资料1]')
        self.assertEqual(report['status'], 'revised_not_rechecked')
        factory.assert_called_once()
        self.assertEqual(reviser.attempts, 1)

    def test_checker_failure_or_invented_excerpt_cannot_promote_draft(self):
        for data in [ApiFailure('failed'), {'verdict': 'revise', 'issues': [{'kind': 'scope',
                'answer_excerpt': '草稿并没有这句话', 'explanation': 'invalid'}]}]:
            result, report = self.check(Client(data), Mock())
            self.assertEqual(report['status'], 'check_failed')
            self.assertEqual(result['finish_reason'], 'reflection_stopped')

    def test_invalid_revision_citation_is_not_published(self):
        critic = Client({'verdict': 'revise', 'issues': [{'kind': 'unsupported',
            'answer_excerpt': '每周40小时。', 'explanation': '更正数值。'}]})
        result, report = self.check(critic, lambda: Client('每周8小时。[资料99]'))
        self.assertEqual(report['status'], 'revision_invalid')
        self.assertEqual(result['finish_reason'], 'reflection_stopped')


if __name__ == '__main__':
    unittest.main()
