"""Offline evidence coverage and reviewed answer scoring for the fixed evaluation."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import statistics
import sys

if __package__:
    from .evaluate_questions import DATASET, ROOT, load_dataset, read_record
    from .embedding_store import sha256, write_json
else:
    from evaluate_questions import DATASET, ROOT, load_dataset, read_record
    from embedding_store import sha256, write_json


def compact(text):
    return re.sub(r'\s+', '', text)


def evidence_parts(evidence, pages):
    locator = evidence['locator']
    if locator['kind'] == 'text_spans':
        parts = [pages[s['pdf_page'] - 1]['blocks'][s['block_index']]['content'][s['char_start']:s['char_end']]
                 for s in locator['spans']]
        if ''.join(parts) != evidence['quote']:
            raise ValueError('Gold evidence text differs from its source locator')
        return [compact(p) for p in parts]
    table = pages[locator['pdf_page'] - 1]['blocks'][locator['block_index']]['content']
    row = table['subtables'][locator['subtable_index']]['rows'][locator['row_index']]
    if row != locator['row_values'] or row != json.loads(evidence['quote']):
        raise ValueError('Gold evidence table row differs')
    return [compact(f'{column}：{value}') for column, value in row.items()]


def evidence_match(evidence, selected, pages, *, partial=False):
    texts = [compact(c['text']) for c in selected if c['chunk_id'] in evidence['chunk_ids']]
    matches = [any(part in text for text in texts) for part in evidence_parts(evidence, pages)]
    return any(matches) if partial else all(matches)


def stage_score(question, selected, chunks, pages, *, ranked=False):
    unique = {c['chunk_id']: c for c in selected}
    for cid, c in unique.items():
        if cid not in chunks or any(c.get(k) != chunks[cid][k] for k in ('text', 'source_file', 'pdf_pages')):
            raise ValueError('Actual evidence payload differs from the fixed source chunks')
    covered = [g['group_id'] for g in question['evidence_groups']
               if any(evidence_match(e, list(unique.values()), pages) for e in g['alternatives'])]
    groups = [g['group_id'] for g in question['evidence_groups']]
    result = {'status': 'completed', 'chunk_ids': list(unique), 'covered_group_ids': covered,
              'missing_group_ids': [g for g in groups if g not in covered],
              'evidence_recall': len(covered) / len(groups), 'hit': int(bool(covered)),
              'all_required_groups_present': len(covered) == len(groups), 'candidate_count': len(unique)}
    if ranked:
        first = next((rank for rank, c in enumerate(selected[:5], 1)
                      if any(evidence_match(e, [c], pages, partial=True)
                             for g in question['evidence_groups'] for e in g['alternatives'])), None)
        result.update(first_relevant_rank=first, reciprocal_rank=1 / first if first else 0)
    return result


def retrieval_score(q, record, chunks, pages):
    if q['answerability'] != 'answerable':
        return {'eligible': False, 'status': 'not_applicable', 'stages': None}
    stages = {}
    if record and record.get('retrieval'):
        r = record['retrieval']
        candidate_stages = ([('keyword_top1', r['keyword_candidates'])]
                            if r.get('retrieval_mode') == 'keyword_only' else
                            [('dense_top10', r['vector_candidates']), ('merged_candidates', r['rerank_candidates'])])
        for name, values in candidate_stages + [('rerank_top5', r['hits'])]:
            stages[name] = stage_score(q, values, chunks, pages, ranked=name == 'rerank_top5')
    if record and record.get('answer_context'):
        stages['answer_context'] = stage_score(q, record['answer_context']['chunks'], chunks, pages)
        stages['answer_context']['context_complete_flag'] = record['answer_context']['complete']
    expected = 3 if record and (record.get('retrieval') or {}).get('retrieval_mode') == 'keyword_only' else 4
    return {'eligible': True, 'status': 'completed' if len(stages) == expected else 'incomplete', 'stages': stages}


def answer_score(q, record, review, chunks, pages):
    if not record or not record.get('generation'):
        return {'status': 'not_generated', 'content_score': None}
    if not review:
        return {'status': 'awaiting_review', 'content_score': None}
    answer = record['generation']['answer']
    if review['answer_sha256'] != sha256(answer.encode('utf-8')):
        raise ValueError('Answer changed after review')
    point_ids = [p['point_id'] for p in q['required_points']]
    if [p['point_id'] for p in review['points']] != point_ids:
        raise ValueError('Reviewed point IDs differ')
    if len(review['forbidden_claim_asserted']) != len(q['forbidden_claims']) or any(
            type(v) is not bool for v in review['forbidden_claim_asserted']):
        raise ValueError('Every forbidden claim requires an explicit assertion-polarity judgment')
    sources = {s['label']: s for s in record['sources']}
    values = {'correct': 1, 'partial': 0.5, 'missing': 0, 'contradicted': 0}
    weighted_sum = 0
    count = 0
    cited_count = 0
    hard_error = any(review['forbidden_claim_asserted']) or bool(review.get('extra_critical_contradictions'))
    points = []
    for gold, judged in zip(q['required_points'], review['points']):
        if judged['judgment'] not in values or not judged['reason']:
            raise ValueError('Each point needs a valid judgment and reason')
        excerpt = judged.get('answer_excerpt')
        if judged['judgment'] != 'missing' and (not excerpt or compact(excerpt) not in compact(answer)):
            raise ValueError('Review excerpt is not present in the actual answer')
        labels = judged.get('cited_source_labels', [])
        citation_support = False
        if judged.get('citation_support'):
            if not labels or any(label not in sources for label in labels):
                raise ValueError('Point citation references a missing source label')
            citation_excerpt = judged.get('citation_excerpt', '')
            if compact(citation_excerpt) not in compact(answer) or any(f'[{label}]' not in citation_excerpt for label in labels):
                raise ValueError('Citation attachment excerpt is invalid')
            groups = [g for g in q['evidence_groups'] if g['group_id'] in gold['evidence_group_ids']]
            citation_support = bool(groups) and all(any(evidence_match(e, [sources[label] for label in labels], pages)
                for e in g['alternatives']) for g in groups)
            if not citation_support:
                raise ValueError('Point citation does not contain its gold support')
        if gold['critical']:
            count += 1
            weighted_sum += values[judged['judgment']]
            hard_error |= judged['judgment'] == 'contradicted'
            cited_count += int(judged['judgment'] == 'correct' and citation_support)
        points.append({**judged, 'weight': int(gold['critical']), 'value': values[judged['judgment']],
                       'validated_citation_support': citation_support})
    base_score = 100 * weighted_sum / count
    content_score = 0 if hard_error else base_score
    content_pass = weighted_sum == count and not hard_error and not review['extra_unsupported_claims']
    eligible = q['answerability'] == 'answerable'
    coverage = cited_count / count if eligible else None
    invalid = record['citation_audit']['unknown_numbers']
    return {'status': 'reviewed', 'reviewer': review['reviewer'], 'base_score': base_score,
            'content_score': content_score, 'hard_error': hard_error, 'content_pass': content_pass,
            'critical_point_citation_coverage': coverage, 'invalid_citation_numbers': invalid,
            'grounded_pass': content_pass and coverage == 1 and not invalid if eligible else None,
            'boundary_pass': content_pass if not eligible else None,
            'extra_unsupported_claims': review['extra_unsupported_claims'], 'points': points,
            'review_reason': review['reason'], 'forbidden_claim_asserted': review['forbidden_claim_asserted']}


def mean(values):
    return statistics.mean(values) if values else None


def build_report(run, reviews_path=None):
    questions, manifest, rules = load_dataset()
    pages = json.loads((ROOT / 'output/reviewed-v1/document.json').read_text(encoding='utf-8'))['pages']
    chunks = {c['chunk_id']: c for c in map(json.loads, (ROOT / 'output/linked-v1/chunks.jsonl').read_text(encoding='utf-8').splitlines())}
    run_manifest = json.loads((run / 'manifest.json').read_text(encoding='utf-8'))
    basis = run_manifest['basis']
    mode = basis.get('retrieval_mode', 'hybrid')
    if mode not in ('hybrid', 'keyword_only'):
        raise ValueError('Unsupported experiment mode')
    if basis['questions_sha256'] != manifest['questions_sha256'] or basis['rules_sha256'] != sha256((DATASET / 'scoring_rules.json').read_bytes()):
        raise ValueError('Dataset or rules changed since the run')
    records = {p.stem: read_record(p) for p in (run / 'records').glob('q*.json')}
    if any(r['identity'] != run_manifest['identity'] for r in records.values()):
        raise ValueError('Record belongs to a different run')
    if any((r.get('retrieval') or {}).get('retrieval_mode', 'hybrid') != mode
           for r in records.values() if r.get('retrieval')):
        raise ValueError('Saved retrieval mode differs from the run manifest')
    reviews = {}
    additions = []
    if reviews_path:
        loaded = json.loads(reviews_path.read_text(encoding='utf-8'))
        if loaded['run_identity'] != run_manifest['identity']:
            raise ValueError('Review set belongs to a different run')
        reviews = {r['question_id']: r for r in loaded['answers']}
        if len(reviews) != len(loaded['answers']) or not set(reviews) <= {q['question_id'] for q in questions}:
            raise ValueError('Duplicate or unknown review question IDs')
        additions = loaded.get('evidence_additions', [])
        questions = deepcopy(questions)
        by_id = {q['question_id']: q for q in questions}
        for addition in additions:
            if not addition.get('reason'):
                raise ValueError('A reviewed alternative needs a source-based reason')
            group = next(g for g in by_id[addition['question_id']]['evidence_groups']
                         if g['group_id'] == addition['group_id'])
            evidence = addition['evidence']
            evidence_parts(evidence, pages)
            if not evidence_match(evidence, [chunks[cid] for cid in evidence['chunk_ids']], pages):
                raise ValueError('Reviewed alternative is not present in its source chunks')
            group['alternatives'].append(evidence)
    rows = []
    for q in questions:
        r = records.get(q['question_id'])
        if r and (r['question_id'] != q['question_id'] or r['question'] != q['question']):
            raise ValueError('Saved record is not the corresponding fixed question')
        rows.append({'question_id': q['question_id'], 'question': q['question'], 'answerability': q['answerability'],
            'execution_status': r['status'] if r else 'not_attempted',
            'retrieval': retrieval_score(q, r, chunks, pages),
            'answer': answer_score(q, r, reviews.get(q['question_id']), chunks, pages),
            'raw_answer': r.get('generation', {}).get('answer') if r else None,
            'sources': [{'label': s['label'], 'chunk_id': s['chunk_id'], 'pdf_pages': s['pdf_pages']}
                        for s in r.get('sources', [])] if r else [],
            'timing_seconds': r.get('timing_seconds') if r else None})
    eligible = [r for r in rows if r['retrieval']['eligible']]
    reviewed = [r for r in eligible if r['answer']['status'] == 'reviewed']
    boundary = [r for r in rows if not r['retrieval']['eligible'] and r['answer']['status'] == 'reviewed']
    stage_summary = {}
    stage_names = (['keyword_top1'] if mode == 'keyword_only' else ['dense_top10', 'merged_candidates'])
    for stage in stage_names + ['rerank_top5', 'answer_context']:
        available = [r['retrieval']['stages'][stage] for r in eligible if stage in r['retrieval']['stages']]
        stage_summary[stage] = {'completed': len(available), 'eligible': 8,
            'macro_evidence_recall': mean([a['evidence_recall'] for a in available]),
            'hit_rate': mean([a['hit'] for a in available]),
            'all_groups_present_count': sum(a['all_required_groups_present'] for a in available) if available else None,
            'mrr_at_5': mean([a['reciprocal_rank'] for a in available]) if stage == 'rerank_top5' else None}
    usage = {'embedding': {}, 'rerank': {}, 'generation': {}}
    attempts = {'embedding': 0, 'rerank': 0, 'generation': 0}
    for r in records.values():
        for name, value in r['attempts'].items():
            attempts[name] += value
        ret = r.get('retrieval') or {}
        for name, part in [('embedding', ret.get('query_embedding', {}).get('usage_this_run', {})),
                           ('rerank', ret.get('reranking', {}).get('usage_this_run', {})),
                           ('generation', r.get('generation', {}).get('usage', {}))]:
            for key, value in part.items():
                usage[name][key] = usage[name].get(key, 0) + value
    summary = {'completed': sum(r['execution_status'] == 'completed' for r in rows), 'expected': 10,
        'pipeline_failed': sum(r['execution_status'] == 'failed' for r in rows),
        'reviewed_answers': sum(r['answer']['status'] == 'reviewed' for r in rows),
        'retrieval': stage_summary, 'answerable_content_mean': mean([r['answer']['content_score'] for r in reviewed]),
        'answerable_content_pass': sum(r['answer']['content_pass'] for r in reviewed) if reviewed else None,
        'answerable_reviewed': len(reviewed), 'answerable_eligible': 8,
        'citation_coverage_macro': mean([r['answer']['critical_point_citation_coverage'] for r in reviewed]),
        'grounded_pass': sum(r['answer']['grounded_pass'] for r in reviewed) if reviewed else None,
        'boundary_pass': sum(r['answer']['boundary_pass'] for r in boundary) if boundary else None, 'boundary_reviewed': len(boundary),
        'usage_reported_by_successful_calls': usage, 'http_attempts_in_saved_records': attempts,
        'usage_note': 'Failed or interrupted calls may be billed without returned usage. No document embeddings were regenerated.'}
    report = {'rules_id': rules['rules_id'], 'run_identity': run_manifest['identity'],
              'retrieval_mode': mode,
              'scorer_sha256': sha256(Path(__file__).read_bytes()),
              'review_file_sha256': sha256(reviews_path.read_bytes()) if reviews_path else None,
              'annotation_supplements': additions,
              'scope': '固定10题；CRAG与联网关闭；检索程序评分，回答由助手逐项核对；无独立测试集。',
              'summary': summary, 'rows': rows}
    write_json(run / 'report.json', report)
    render_report(run / 'report.txt', report)
    return report


def pct(value):
    return 'N/A' if value is None else f'{value * 100:.2f}%'


def render_report(path, report):
    s = report['summary']
    mode_label = '纯关键词 Top-1 消融' if report['retrieval_mode'] == 'keyword_only' else '混合检索基线'
    lines = ['学生手册固定10题：' + mode_label + '评测报告', report['scope'],
             f"执行完成：{s['completed']}/10；失败：{s['pipeline_failed']}/10；已评分：{s['reviewed_answers']}/10。", '', '一、检索（8道可回答题）']
    if report['annotation_supplements']:
        lines.append('共同标注补充：手册附录《普通高等学校学生管理规定》第六十条与q002原标注的校内规定等价，沿用基线复核结果作为有效依据。本轮是否召回见逐题记录；原10题未修改。')
    if report['retrieval_mode'] == 'keyword_only':
        lines.append('向量召回已关闭；关键词只取1条，重排上限仍为5但实际只有1个候选；保留原有关联补读。')
    for stage, score in s['retrieval'].items():
        lines.append(f"{stage}：完成{score['completed']}/8，证据组宏平均召回率{pct(score['macro_evidence_recall'])}，全部依据齐全{score['all_groups_present_count']}/8" +
                     (f"，MRR@5={score['mrr_at_5']:.4f}" if score['mrr_at_5'] is not None else ''))
    lines.extend(['', '二、回答（内容与引用分别统计）',
        f"8题内容平均分：{s['answerable_content_mean'] if s['answerable_content_mean'] is not None else '待评分'}；内容完整通过{s['answerable_content_pass']}/8。",
        f"关键要点引用覆盖率（逐题宏平均）：{pct(s['citation_coverage_macro'])}；有依据完整通过{s['grounded_pass']}/8。",
        f"两道边界题：已评分{s['boundary_reviewed']}/2，行为完整通过{s['boundary_pass']}/2。",
        '没有把检索、内容、引用、边界行为加权成一个总分。', '', '三、逐题结果'])
    for row in report['rows']:
        a = row['answer']
        lines.extend(['-' * 64, row['question_id'] + '：' + row['question'],
            f"执行：{row['execution_status']}；内容分：{a.get('content_score')}；引用覆盖：{pct(a.get('critical_point_citation_coverage'))}"])
        if row['retrieval']['eligible']:
            for name, stage in row['retrieval']['stages'].items():
                lines.append(f"{name}：{pct(stage['evidence_recall'])}；缺少证据组：{stage['missing_group_ids']}")
        else:
            lines.append('检索分：N/A（不计入8题分母）。')
        lines.extend(['实际回答：', row['raw_answer'] or '未生成', '评分记录：'])
        for p in a.get('points', []):
            lines.append(f"{p['point_id']}：{p['judgment']}，权重{p['weight']}，引用支持{p['validated_citation_support']}；{p['reason']}")
        lines.append('复核说明：' + a.get('review_reason', '尚未评分'))
        lines.append('来源映射：' + json.dumps(row['sources'], ensure_ascii=False))
    lines.extend(['', '四、成功响应报告用量与HTTP尝试次数',
        json.dumps(s['usage_reported_by_successful_calls'], ensure_ascii=False, indent=2),
        json.dumps(s['http_attempts_in_saved_records'], ensure_ascii=False), s['usage_note'],
        '', '说明：本轮回答由助手依据事先固定的标注逐项核对，不声称经过独立人工盲评，也未调用付费裁判模型。'])
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8', newline='\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, default=ROOT / 'output/evaluation-v1')
    parser.add_argument('--reviews', type=Path)
    args = parser.parse_args()
    print(json.dumps(build_report(args.run, args.reviews)['summary'], ensure_ascii=False, indent=2))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
