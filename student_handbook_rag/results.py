"""Compact caller results, separate from full local diagnostic records."""
from copy import deepcopy


def stage_usage(record):
    stages = []

    def add(name, usage, attempts):
        stages.append({'stage': name, 'tokens': usage or {}, 'http_attempts': attempts})

    def retrieval(name, data):
        for part in ('query_embedding', 'reranking'):
            meta = data.get(part, {})
            add(f'{name}.{part}', meta.get('usage_this_run', {}), meta.get('http_attempts', 0))

    retrieval('retrieval', record['retrieval'])
    crag = record['crag']
    retry = crag.get('local_retry') or {}
    if retry.get('retrieval'):
        retrieval('local_retry', retry['retrieval'])
    for i, check in enumerate(crag.get('assessments', []), 1):
        add(f'assessment.{i}', check.get('response', {}).get('usage', {}), check.get('http_attempts', 0))
    if 'evaluator_usage' in crag:
        add('assessment.legacy', crag['evaluator_usage'], crag.get('evaluator_http_attempts', 0))
    add('web', crag.get('web_search', {}).get('usage', {}), crag.get('web_http_attempts', 0))
    add('draft', record['draft_generation'].get('usage', {}), record.get('generation_http_attempts', 0))
    reflection = record['reflection']
    add('reflection', reflection.get('response', {}).get('usage', {}), reflection.get('http_attempts', 0))
    add('revision', reflection.get('revision', {}).get('usage', {}), reflection.get('revision_http_attempts', 0))
    # Final answer may be the draft OR revision; never count it a second time.
    return {'stages': stages,
            'reported_total_tokens': sum(s['tokens'].get('total_tokens', 0) for s in stages)}


def answer_result(record):
    result, route = record['generation'], record['crag']['route']
    audit = record['citation_audit']
    warnings = []
    if audit['unknown_numbers']:
        warnings.append('unknown_citation_numbers')
    if record['messages'] and not audit['has_citations']:
        warnings.append('missing_citations')
    if route == 'ask_user':
        status = 'needs_clarification'
    elif (route == 'abstain' or result['finish_reason'] in ('reflection_stopped', 'no_evidence')):
        status = 'abstained'
    elif not result['complete']:
        status = 'incomplete'
    elif warnings:
        status = 'unverified'
    elif route == 'limited_evidence':
        status = 'partial_answer'
    else:
        status = 'answered'
    sources = []
    for i, source in enumerate(record['sources'], 1):
        if i not in audit['cited_numbers']:
            continue
        sources.append({key: source[key] for key in (
            'label', 'chunk_id', 'text', 'source_file', 'pdf_pages', 'kind', 'web_sources') if key in source})
    return deepcopy({
        'request_id': record['request_id'], 'question': record['question'],
        'answer': result['answer'], 'status': status, 'complete': result['complete'],
        'sources': sources, 'route': route, 'reflection': record['reflection']['status'],
        'model': result.get('response_model'), 'warnings': warnings,
        'usage': stage_usage(record), 'elapsed_seconds': record['elapsed_seconds'],
        'record_path': record['record_path'],
    })
