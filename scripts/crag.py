"""Small CRAG-style branch: evaluate evidence once, optionally supplement once."""

import json
from pathlib import Path

if __package__:
    from .embedding_api import ApiFailure
else:
    from embedding_api import ApiFailure


def load_crag_config(root: Path):
    config = json.loads((root / 'config/crag-v1.json').read_text(encoding='utf-8'))
    if (config.get('schema_version') != 1 or config.get('evaluator_model') != 'qwen3.7-plus'
            or config.get('web_model') != 'qwen-plus'
            or config.get('allowed_domains') != ['usx.edu.cn', 'moe.gov.cn', 'mof.gov.cn']
            or type(config.get('max_web_chars')) is not int or not 200 <= config['max_web_chars'] <= 2400
            or type(config.get('max_web_sources')) is not int or not 1 <= config['max_web_sources'] <= 3):
        raise ValueError('Unsupported CRAG configuration')
    return config


def parse_assessment(result, chunk_ids):
    if not result['complete']:
        raise ValueError('Incomplete evidence assessment')
    assessment = json.loads(result['answer'])
    if not isinstance(assessment, dict) or assessment.get('decision') not in ('sufficient', 'partial', 'insufficient'):
        raise ValueError('Invalid evidence decision')
    ids = assessment.get('support_ids')
    if (not isinstance(ids, list) or any(not isinstance(i, str) or i not in chunk_ids for i in ids)
            or len(ids) != len(set(ids)) or (assessment['decision'] == 'sufficient' and not ids)):
        raise ValueError('Invalid supporting chunk IDs')
    if not isinstance(assessment.get('reason'), str) or not 1 <= len(assessment['reason']) <= 500:
        raise ValueError('Invalid assessment reason')
    query = assessment.get('search_query')
    if not isinstance(query, str) or len(query) > 200 or (assessment['decision'] != 'sufficient' and not query.strip()):
        raise ValueError('Invalid assessment query')
    return {k: assessment[k] for k in ('decision', 'reason', 'support_ids', 'search_query')}


def apply_crag(question, context, evaluator, web_client, evaluator_prompt, *, allow_web=True, max_chars=6000):
    messages = [{'role': 'system', 'content': evaluator_prompt}, {'role': 'user', 'content': json.dumps(
        {'问题': question, '资料': context['chunks'], '关联补读完整': context['complete']}, ensure_ascii=False)}]
    report = {'evaluator_model': evaluator.config['model'], 'web_search': {'status': 'not_needed'},
              'evaluator_usage': {}, 'evaluator_http_attempts': 0, 'web_http_attempts': 0}
    try:
        result = evaluator.generate(messages)
        report['evaluator_usage'] = result['usage']
        assessment = parse_assessment(result, {c['chunk_id'] for c in context['chunks']})
    except (ApiFailure, ValueError, TypeError, KeyError):
        assessment = {'decision': 'partial', 'reason': '证据评估失败，按不确定处理。',
                      'support_ids': [], 'search_query': '绍兴大学 绍兴文理学院 ' + question[:175]}
        report['evaluation_failed'] = True
    report.update(assessment=assessment, evaluator_http_attempts=evaluator.attempts)
    if assessment['decision'] == 'sufficient':
        report['route'] = 'local'
        return context, report
    report['web_search'] = {'status': 'disabled'}
    if allow_web:
        try:
            report['web_search'] = web_client.search(assessment['search_query'])
        except (ApiFailure, ValueError):
            report['web_search'] = {'status': 'failed', 'error': '联网检索失败，未自动重试。'}
        report['web_http_attempts'] = web_client.attempts
    web = report['web_search'].get('chunk')
    # Unrelated local hits cannot acquire authority merely because they were retrieved.
    local = context['chunks'] if assessment['decision'] == 'partial' else []
    if report.get('evaluation_failed'):
        local = []
    kept, omitted, used = [], [], len(web['text']) if web else 0
    for chunk in local:
        if used + len(chunk['text']) <= max_chars:
            kept.append(chunk)
            used += len(chunk['text'])
        else:
            omitted.append(chunk['chunk_id'])
    report.update(route='web_supplement' if web else 'limited_evidence', omitted_local_ids=omitted)
    updated = {**context, 'chunks': kept + ([web] if web else []), 'total_chars': used,
               'complete': context['complete'] and not omitted,
               'evidence_assessment': {'decision': assessment['decision'], 'reason': assessment['reason'],
                                       'web_status': report['web_search']['status']}}
    return updated, report
