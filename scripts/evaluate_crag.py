"""Evaluate only evidence decisions/routes on 20 saved contexts from the fixed ten questions."""
import argparse
import json
from pathlib import Path
import sys
from time import perf_counter

if __package__:
    from .crag import parse_assessment
    from .embed_chunks import ROOT, load_local_credentials
    from .embedding_api import ApiFailure
    from .embedding_store import output_lock, sha256, write_json
    from .evaluate_questions import DATASET, digest, load_dataset, read_record, save_record
    from .evidence_assessment import assessment_messages, parse_adaptive_assessment
    from .generation_api import GenerationClient, load_generation_config
else:
    from crag import parse_assessment
    from embed_chunks import ROOT, load_local_credentials
    from embedding_api import ApiFailure
    from embedding_store import output_lock, sha256, write_json
    from evaluate_questions import DATASET, digest, load_dataset, read_record, save_record
    from evidence_assessment import assessment_messages, parse_adaptive_assessment
    from generation_api import GenerationClient, load_generation_config


def execute(version, output, limit=20):
    questions, manifest, _ = load_dataset()
    cases_path = DATASET / 'crag_cases.json'
    labels = json.loads(cases_path.read_text(encoding='utf-8'))
    questions = {q['question_id']: q for q in questions}
    if labels['questions_sha256'] != manifest['questions_sha256'] or len(labels['cases']) != 20:
        raise ValueError('Expected the same ten questions with two saved contexts each')
    filename = 'crag-evaluator-v1.txt' if version == 'legacy' else 'crag-evaluator-v2.txt'
    prompt = (ROOT / 'config' / filename).read_text(encoding='utf-8')
    config = {**load_generation_config(ROOT), 'max_completion_tokens': 500 if version == 'legacy' else 900}
    key, url = load_local_credentials(ROOT)
    if not key:
        raise ValueError('Configure local credentials first')
    basis = {'version': version, 'labels_sha256': sha256(cases_path.read_bytes()),
        'questions_sha256': manifest['questions_sha256'], 'prompt_sha256': sha256(prompt.encode('utf-8')),
        'config': config, 'source_hashes': {name: sha256((ROOT / 'scripts' / name).read_bytes())
            for name in ['crag.py', 'evidence_assessment.py', 'evaluate_crag.py', 'generation_api.py']},
        'endpoint': url + '/chat/completions'}
    identity = digest(basis)
    prior = output / 'manifest.json'
    if prior.exists() and json.loads(prior.read_text(encoding='utf-8'))['identity'] != identity:
        raise ValueError('Evaluator version changed; choose a new output directory')
    write_json(prior, {'identity': identity, 'basis': basis})
    records = output / 'records'
    records.mkdir(exist_ok=True)
    for case in labels['cases'][:limit]:
        path = records / (case['case_id'] + '.json')
        if path.exists():
            saved = read_record(path)
            if saved['identity'] != identity:
                raise ValueError('Saved assessment belongs to another version')
            if saved['status'] == 'completed':
                print(case['case_id'] + ': reuse completed', flush=True)
                continue
        source = ROOT / case['record_path']
        if sha256(source.read_bytes()) != case['record_sha256']:
            raise ValueError('Saved evidence record changed')
        original = read_record(source)
        question = questions[case['question_id']]['question']
        if original['question'] != question:
            raise ValueError('Question text differs')
        context = original['answer_context']
        messages = assessment_messages(question, context, prompt)
        record = {'case_id': case['case_id'], 'identity': identity, 'status': 'prepared',
                  'messages': messages, 'source_record_sha256': case['record_sha256']}
        save_record(path, record)
        client = GenerationClient(url, key, config)
        start = perf_counter()
        try:
            result = client.generate(messages)
            record['response'] = result
            parser = parse_assessment if version == 'legacy' else parse_adaptive_assessment
            assessment = parser(result, {c['chunk_id'] for c in context['chunks']})
            record.update(status='completed', assessment=assessment,
                proposed_action=(assessment.get('next_action') or
                                 ('answer_local' if assessment['decision'] == 'sufficient' else 'search_web')))
        except (ApiFailure, ValueError, KeyError, TypeError) as exc:
            record.update(status='failed', error=str(exc) if isinstance(exc, ApiFailure) else type(exc).__name__)
        record.update(http_attempts=client.attempts, elapsed_seconds=perf_counter() - start)
        save_record(path, record)
        print(case['case_id'] + ': ' + record['status'] + ' ' + record.get('proposed_action', ''), flush=True)
        if record.get('error', '').startswith(('HTTP 401', 'HTTP 402', 'HTTP 403')):
            break
    return score(output, labels, identity)


def score(output, labels, identity):
    rows, usage = [], {}
    for case in labels['cases']:
        path = output / 'records' / (case['case_id'] + '.json')
        record = read_record(path) if path.exists() else {}
        if record and record['identity'] != identity:
            raise ValueError('Record identity differs')
        valid = record.get('status') == 'completed'
        for k, v in record.get('response', {}).get('usage', {}).items():
            usage[k] = usage.get(k, 0) + v
        expected = case['expected_decision']
        predicted = record.get('assessment', {}).get('decision')
        rows.append({**case, 'status': record.get('status', 'not_attempted'),
            'expected_decision': expected, 'predicted_decision': predicted,
            'decision_correct': valid and predicted == expected if expected is not None else None,
            'predicted_action': record.get('proposed_action'),
            'action_correct': valid and record.get('proposed_action') == case['expected_action'],
            'reason': record.get('assessment', {}).get('reason'),
            'missing_aspects': record.get('assessment', {}).get('missing_aspects'),
            'http_attempts': record.get('http_attempts', 0), 'elapsed_seconds': record.get('elapsed_seconds')})
    eligible = [r for r in rows if r['expected_decision'] is not None]
    insufficient = [r for r in eligible if r['expected_decision'] != 'sufficient']
    sufficient = [r for r in eligible if r['expected_decision'] == 'sufficient']
    summary = {'expected_cases': 20, 'completed': sum(r['status'] == 'completed' for r in rows),
        'decision_eligible': len(eligible), 'decision_correct': sum(r['decision_correct'] for r in eligible),
        'action_correct': sum(r['action_correct'] for r in rows),
        'insufficient_case_count': len(insufficient),
        'false_release_count': sum(r['predicted_decision'] == 'sufficient' for r in insufficient),
        'sufficient_case_count': len(sufficient),
        'overblocking_count': sum(r['predicted_decision'] in ('partial', 'insufficient') for r in sufficient),
        'clarification_correct': sum(r['action_correct'] for r in rows if r['expected_action'] == 'ask_user'),
        'http_attempts': sum(r['http_attempts'] for r in rows), 'usage': usage}
    report = {'identity': identity, 'scope': '固定10题的两组既有上下文；仅评估证据判断和推荐路由，不执行补查、联网、回答或反思。',
        'limitations': '18份有唯一证据充分性标签；2份q010按澄清路由评分，不强行指定充分性标签。15充分、1部分、2不足，类别不均衡。标签由助手事先核对，无独立盲评。旧版动作按其现有充分则本地、否则联网的逻辑推导。',
        'summary': summary, 'rows': rows}
    write_json(output / 'report.json', report)
    lines = ['CRAG独立评估报告', report['scope'], report['limitations'], '',
        f"完成{summary['completed']}/20；充分性判断正确{summary['decision_correct']}/18；推荐动作正确{summary['action_correct']}/20。",
        f"已知不充分的3例中误放行{summary['false_release_count']}例；充分15例中过度拦截{summary['overblocking_count']}例；澄清正确{summary['clarification_correct']}/2。",
        '失败/未执行计入正确率分母，但不冒充语义误判。', '']
    for r in rows:
        lines.extend([r['case_id'] + '：' + r['question'],
            f"证据预期={r['expected_decision']}，实际={r['predicted_decision']}；动作预期={r['expected_action']}，实际={r['predicted_action']}；状态={r['status']}",
            '判断原因：' + str(r['reason']), '标注依据：' + r['label_reason'], ''])
    lines.extend(['实际用量：' + json.dumps(summary['usage'], ensure_ascii=False),
                  'HTTP尝试：' + str(summary['http_attempts'])])
    (output / 'report.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8', newline='\n')
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', choices=['legacy', 'adaptive'], default='adaptive')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--limit', type=int, choices=range(1, 21), default=20)
    args = parser.parse_args()
    output = args.output or ROOT / ('output/evaluation-crag-' + args.version + '-v1')
    with output_lock(output):
        execute(args.version, output, args.limit)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
