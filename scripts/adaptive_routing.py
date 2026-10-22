"""One adaptive action, at most one local retry and one post-retry assessment."""
import re

if __package__:
    from .evidence_assessment import assess_evidence
    from .embedding_api import ApiFailure
else:
    from evidence_assessment import assess_evidence
    from embedding_api import ApiFailure


def merge_context(initial, extra, max_chars=6000, max_chunks=12):
    """Prioritize newly retrieved evidence, deduplicate and never cut source blocks."""
    chunks, seen_ids, seen_text, omitted = [], set(), set(), []
    used = 0
    for chunk in extra['chunks'] + initial['chunks']:
        cid, text = chunk['chunk_id'], re.sub(r'\s+', '', chunk['text'])
        if cid in seen_ids or text in seen_text:
            continue
        seen_ids.add(cid)
        seen_text.add(text)
        if used + len(chunk['text']) > max_chars or len(chunks) >= max_chunks:
            omitted.append(cid)
            continue
        chunks.append(chunk)
        used += len(chunk['text'])
    return {**initial, 'chunks': chunks, 'total_chars': used,
        'complete': initial['complete'] and extra['complete'] and not omitted,
        'routing_merge': {'omitted_chunk_ids': omitted, 'max_chars': max_chars, 'max_chunks': max_chunks}}


def apply_adaptive_routing(question, context, evaluator_factory, prompt, local_retry, web_client,
                           *, allow_web=True, max_chars=6000):
    check = assess_evidence(question, context, evaluator_factory(), prompt)
    report = {'assessments': [check], 'local_retry': None, 'web_search': {'status': 'not_needed'},
              'web_http_attempts': 0, 'terminal_response': None}
    current = context
    assessment = check['assessment']
    if assessment['next_action'] == 'retrieve_local':
        try:
            extra, retrieval = local_retry(assessment['retrieval_query'])
            report['local_retry'] = {'status': 'completed', 'query': assessment['retrieval_query'],
                                     'retrieval': retrieval, 'context': extra}
            current = merge_context(current, extra, max_chars)
            check = assess_evidence(question, current, evaluator_factory(), prompt)
            report['assessments'].append(check)
            assessment = check['assessment']
        except (ApiFailure, ValueError, OSError) as exc:
            report['local_retry'] = {'status': 'failed', 'error': str(exc) if isinstance(exc, ApiFailure) else type(exc).__name__}
            report.update(route='abstain', terminal_response='本地资料补查未完成，暂时无法可靠回答。')
            return current, report
    action = assessment['next_action']
    if action == 'ask_user':
        report.update(route='ask_user', terminal_response=assessment['followup_question'])
    elif action == 'answer_local':
        report['route'] = 'local'
    elif action == 'search_web':
        report['web_search'] = {'status': 'disabled'}
        if allow_web:
            try:
                report['web_search'] = web_client.search(assessment['search_query'])
            except (ApiFailure, ValueError):
                report['web_search'] = {'status': 'failed'}
            report['web_http_attempts'] = web_client.attempts
        web = report['web_search'].get('chunk')
        if web:
            local = current if assessment['decision'] == 'partial' else {**current, 'chunks': [], 'total_chars': 0}
            current = merge_context(local, {'chunks': [web], 'complete': True}, max_chars)
            report['route'] = 'web_supplement'
        elif assessment['decision'] == 'partial' and current['chunks']:
            report['route'] = 'limited_evidence'
        else:
            report.update(route='abstain', terminal_response='现有资料不足以确定所问事项；需要对应年份或事项的官方通知，目前未取得可用补充依据。')
    elif action == 'retrieve_local' and current['chunks'] and assessment['decision'] == 'partial':
        report['route'] = 'limited_evidence'
        report['budget_exhausted'] = True
    else:
        report.update(route='abstain', terminal_response='现有资料不足以可靠回答这个问题，暂时无法确定。')
    current = {**current, 'evidence_assessment': {**assessment, 'web_status': report['web_search']['status']}}
    return current, report
