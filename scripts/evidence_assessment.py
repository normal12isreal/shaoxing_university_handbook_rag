"""Bounded evidence assessment and explicit action contract; no searches here."""
import json

if __package__:
    from .crag import parse_assessment
    from .embedding_api import ApiFailure
else:
    from crag import parse_assessment
    from embedding_api import ApiFailure

ACTIONS = {'answer_local', 'retrieve_local', 'search_web', 'ask_user', 'abstain'}


def assessment_messages(question, context, prompt):
    return [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': json.dumps(
        {'问题': question, '资料': context['chunks'], '关联补读完整': context['complete']}, ensure_ascii=False)}]


def parse_adaptive_assessment(result, chunk_ids):
    base = parse_assessment(result, chunk_ids, require_search_query=False)
    data = json.loads(result['answer'])
    action, status = data.get('next_action'), data.get('question_status')
    if action not in ACTIONS or status not in ('clear', 'needs_clarification'):
        raise ValueError('Invalid adaptive routing action')
    missing = data.get('missing_aspects')
    if (not isinstance(missing, list) or len(missing) > 5
            or any(not isinstance(s, str) or not 1 <= len(s.strip()) <= 200 for s in missing)):
        raise ValueError('Invalid missing aspects')
    for field, maximum in [('retrieval_query', 200), ('followup_question', 500)]:
        if not isinstance(data.get(field), str) or len(data[field]) > maximum:
            raise ValueError('Invalid routing text')
    if (action == 'ask_user') != (status == 'needs_clarification'):
        raise ValueError('Clarification status and action disagree')
    if action == 'ask_user' and (not data['followup_question'].strip() or base['decision'] == 'sufficient'):
        raise ValueError('Missing clarification or conflicting sufficiency')
    if (base['decision'] == 'sufficient') != (action == 'answer_local'):
        raise ValueError('Evidence sufficiency and answer action disagree')
    if base['decision'] == 'sufficient' and missing:
        raise ValueError('Sufficient evidence cannot have missing aspects')
    if action == 'retrieve_local' and not data['retrieval_query'].strip():
        raise ValueError('Local retry needs an explicit query')
    if action == 'search_web' and not base['search_query'].strip():
        raise ValueError('Web search needs an explicit query')
    if action != 'ask_user' and data['followup_question'].strip():
        raise ValueError('Unexpected followup question')
    return {**base, **{k: data[k] for k in ('question_status', 'missing_aspects', 'next_action',
                                          'retrieval_query', 'followup_question')}}


def assess_evidence(question, context, evaluator, prompt):
    messages = assessment_messages(question, context, prompt)
    report = {'messages': messages, 'status': 'pending'}
    try:
        response = evaluator.generate(messages)
        report['response'] = response
        report['assessment'] = parse_adaptive_assessment(response, {c['chunk_id'] for c in context['chunks']})
        report['status'] = 'completed'
    except (ApiFailure, ValueError, TypeError, KeyError) as exc:
        report.update(status='failed', error=str(exc) if isinstance(exc, ApiFailure) else type(exc).__name__)
        report['assessment'] = {'decision': 'insufficient', 'reason': '证据检查未完成，暂停回答。',
            'support_ids': [], 'search_query': '', 'question_status': 'clear', 'missing_aspects': [],
            'next_action': 'abstain', 'retrieval_query': '', 'followup_question': ''}
    report['http_attempts'] = evaluator.attempts
    return report
