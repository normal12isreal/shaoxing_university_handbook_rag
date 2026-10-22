"""One evidence-grounded draft review and at most one revision; no reflection loop."""
import json

if __package__:
    from .embedding_api import ApiFailure
else:
    from embedding_api import ApiFailure


def parse_reflection(result, draft):
    if not result['complete']:
        raise ValueError('Incomplete reflection')
    data = json.loads(result['answer'])
    if not isinstance(data, dict) or data.get('verdict') not in ('pass', 'revise', 'abstain'):
        raise ValueError('Invalid reflection verdict')
    issues = data.get('issues')
    if not isinstance(issues, list) or len(issues) > 6 or (data['verdict'] == 'pass') != (not issues):
        raise ValueError('Invalid reflection issues')
    for issue in issues:
        if not isinstance(issue, dict) or issue.get('kind') not in ('unsupported', 'omission', 'citation', 'scope'):
            raise ValueError('Invalid reflection issue')
        excerpt, explanation = issue.get('answer_excerpt'), issue.get('explanation')
        if (not isinstance(excerpt, str) or len(excerpt) > 500 or (excerpt and excerpt not in draft)
                or not isinstance(explanation, str) or not 1 <= len(explanation.strip()) <= 500):
            raise ValueError('Reflection issue must refer to the actual draft')
    return {'verdict': data['verdict'], 'issues': issues}


def reflect_answer(question, context, draft, critic, reviser_factory, prompt, answer_messages,
                   citation_audit):
    messages = [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': json.dumps(
        {'用户问题': question, '资料': [{'label': f'资料{i}', **c} for i, c in enumerate(context['chunks'], 1)],
         '草稿': draft['answer']}, ensure_ascii=False)}]
    report = {'messages': messages, 'status': 'pending', 'revision_http_attempts': 0}
    try:
        response = critic.generate(messages)
        report['response'] = response
        review = parse_reflection(response, draft['answer'])
        audit = citation_audit(draft['answer'], len(context['chunks']))
        if audit['unknown_numbers']:
            review = {'verdict': 'revise', 'issues': review['issues'] + [{
                'kind': 'citation', 'answer_excerpt': '', 'explanation': '删除不存在的资料编号，并仅引用真实支持的资料。'}]}
        report['review'] = review
        if review['verdict'] == 'pass':
            report['status'] = 'passed'
            return draft, report
        if review['verdict'] == 'revise':
            revision_messages = [answer_messages[0], {'role': 'user', 'content': json.dumps(
                {'原始请求与资料': answer_messages[1]['content'], '待修改草稿': draft['answer'],
                 '复核问题': review['issues'], '修改要求': '仅据所给资料修正。资料缺失则说明无法确定，不添加外部事实。'}, ensure_ascii=False)}]
            report['revision_messages'] = revision_messages
            reviser = reviser_factory()
            try:
                corrected = reviser.generate(revision_messages)
            finally:
                report['revision_http_attempts'] = reviser.attempts
            report['revision'] = corrected
            if corrected['complete'] and not citation_audit(corrected['answer'], len(context['chunks']))['unknown_numbers']:
                report['status'] = 'revised_not_rechecked'
                return corrected, report
            report['status'] = 'revision_invalid'
        else:
            report['status'] = 'abstained'
    except (ApiFailure, ValueError, TypeError, KeyError) as exc:
        report.update(status='check_failed', error=str(exc) if isinstance(exc, ApiFailure) else type(exc).__name__)
    finally:
        report['http_attempts'] = critic.attempts
    return {'answer': '本次回答未能通过依据核对，暂时无法给出可靠结论。', 'complete': True,
            'usage': {}, 'response_model': None, 'finish_reason': 'reflection_stopped'}, report
