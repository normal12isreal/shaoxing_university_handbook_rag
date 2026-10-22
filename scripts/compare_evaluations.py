"""Compare the fixed hybrid baseline against its single-keyword ablation, offline."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

if __package__:
    from .evaluate_questions import ROOT
    from .embedding_store import sha256, write_json
else:
    from evaluate_questions import ROOT
    from embedding_store import sha256, write_json


def load_run(path):
    manifest = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
    report = json.loads((path / 'report.json').read_text(encoding='utf-8'))
    if report['run_identity'] != manifest['identity'] or report['summary']['reviewed_answers'] != 10:
        raise ValueError('Both runs must have ten reviewed answers with matching identities')
    return manifest['basis'], report


def compare(baseline, keyword):
    left, a = load_run(baseline)
    right, b = load_run(keyword)
    if left.get('retrieval_mode', 'hybrid') != 'hybrid' or right['retrieval_mode'] != 'keyword_only':
        raise ValueError('Expected a hybrid baseline and keyword-only ablation')
    for key in ('questions_sha256', 'rules_sha256', 'corpus_identity', 'vectors_sha256',
                'embedding', 'rerank', 'generation', 'expansion', 'system_prompt_sha256',
                'crag_enabled', 'web_enabled'):
        if left[key] != right[key]:
            raise ValueError('Comparison settings differ: ' + key)
    for name, value in left['source_hashes'].items():
        if name != 'evaluate_questions.py' and right['source_hashes'].get(name) != value:
            raise ValueError('Production component changed: ' + name)
    if a['annotation_supplements'] != b['annotation_supplements']:
        raise ValueError('Both runs must use the same reviewed evidence alternatives')
    rows = []
    for x, y in zip(a['rows'], b['rows'], strict=True):
        if x['question_id'] != y['question_id'] or x['question'] != y['question']:
            raise ValueError('Question order or content differs')
        rows.append({'question_id': x['question_id'], 'question': x['question'],
            'answerability': x['answerability'], 'hybrid_score': x['answer']['content_score'],
            'keyword_score': y['answer']['content_score'],
            'keyword_missing_context_groups': (y['retrieval']['stages']['answer_context']['missing_group_ids']
                                               if y['retrieval']['eligible'] else None)})
    result = {'experiment': 'hybrid_dense10_keyword1_vs_keyword1',
        'baseline_run_identity': a['run_identity'], 'keyword_run_identity': b['run_identity'],
        'report_hashes': {'baseline': sha256((baseline / 'report.json').read_bytes()),
                          'keyword': sha256((keyword / 'report.json').read_bytes())},
        'scope': '同一固定10题、语料、模型、提示词、评分规则；CRAG和联网关闭。仅移除向量召回，关键词仍取1条。',
        'limits': '候选预算随向量路线移除而减少，并非同预算算法比较。每种配置只生成一次，未测随机波动；8道可回答题不能外推整体准确率。回答由助手逐项复核，无独立盲评。',
        'hybrid_summary': a['summary'], 'keyword_summary': b['summary'], 'rows': rows}
    write_json(keyword / 'comparison.json', result)
    sa, sb = a['summary'], b['summary']
    by_id = {r['question_id']: r for r in rows}
    percent = lambda value: f'{value * 100:.2f}%'
    lines = ['学生手册RAG：混合召回与纯关键词消融对比', result['scope'], result['limits'], '',
        '指标                              混合召回          关键词Top-1',
        f"重排后证据组宏平均召回率          {percent(sa['retrieval']['rerank_top5']['macro_evidence_recall'])}          {percent(sb['retrieval']['rerank_top5']['macro_evidence_recall'])}",
        f"补读后证据组宏平均召回率          {percent(sa['retrieval']['answer_context']['macro_evidence_recall'])}          {percent(sb['retrieval']['answer_context']['macro_evidence_recall'])}",
        f"补读后全部依据齐全                {sa['retrieval']['answer_context']['all_groups_present_count']}/8             {sb['retrieval']['answer_context']['all_groups_present_count']}/8",
        f"MRR@5                            {sa['retrieval']['rerank_top5']['mrr_at_5']:.2f}             {sb['retrieval']['rerank_top5']['mrr_at_5']:.2f}",
        f"8道可回答题内容平均分             {sa['answerable_content_mean']:.2f}           {sb['answerable_content_mean']:.2f}",
        f"内容及引用完整通过                {sa['grounded_pass']}/8             {sb['grounded_pass']}/8", '',
        '逐题内容分（边界题单列，不混入8题平均）']
    lines.extend(f"{r['question_id']}：{r['hybrid_score']:g} → {r['keyword_score']:g}；{r['question']}" for r in rows)
    lines.extend(['', '结果解释',
        f"q008内容分{by_id['q008']['hybrid_score']:g} → {by_id['q008']['keyword_score']:g}，关键词最终缺少证据组{by_id['q008']['keyword_missing_context_groups']}：本题申请程序位于chunk-0158，资格条件位于chunk-0157。具体遗漏按report.txt逐项复核。",
        f"q007内容分{by_id['q007']['hybrid_score']:g} → {by_id['q007']['keyword_score']:g}：表格与续借正文分属不同块，可对照report.txt中的召回和补读阶段检查关联补全的作用。",
        'MRR@5衡量首个相关结果的位置；即使为1，也不能证明多条件问题的依据齐全。',
        f"边界题q009：{by_id['q009']['hybrid_score']:g} → {by_id['q009']['keyword_score']:g}；q010：{by_id['q010']['hybrid_score']:g} → {by_id['q010']['keyword_score']:g}。拒答指引和澄清行为的逐项扣分见report.txt。",
        '', '实际调用与用量（不换算费用，不据单次耗时判断性能）',
        '混合组HTTP尝试：' + json.dumps(sa['http_attempts_in_saved_records'], ensure_ascii=False),
        '关键词组HTTP尝试：' + json.dumps(sb['http_attempts_in_saved_records'], ensure_ascii=False),
        '混合组成功调用Token：' + json.dumps(sa['usage_reported_by_successful_calls'], ensure_ascii=False),
        '关键词组成功调用Token：' + json.dumps(sb['usage_reported_by_successful_calls'], ensure_ascii=False),
        '关键词组重排只有1个候选，无法通过排序找回未召回资料；保留调用用于维持既有链路。',
        '', '详细实际答案、来源及逐项扣分理由：同目录report.txt；逐题原始输入输出：records/。'])
    (keyword / 'comparison.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8', newline='\n')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, default=ROOT / 'output/evaluation-v1')
    parser.add_argument('--keyword', type=Path, default=ROOT / 'output/evaluation-keyword-v1')
    args = parser.parse_args()
    compare(args.baseline, args.keyword)
    print(args.keyword / 'comparison.txt')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
