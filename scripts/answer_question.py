"""Ask one question using the reusable student_handbook_rag package."""
from __future__ import annotations
import argparse
from pathlib import Path
import sys

# Support the historical `python scripts/answer_question.py` entry point.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from student_handbook_rag import HandbookRAG, ModelAPIError
from student_handbook_rag.prompts import build_messages, citation_audit
from scripts.embed_chunks import ROOT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--question', help='One question; omitted = prompt once')
    parser.add_argument('--retrieval-offline', action='store_true',
                        help='Require cached retrieval; CRAG/web search and answer may still call APIs')
    parser.add_argument('--no-crag', action='store_true', help='Disable evidence assessment and web fallback')
    parser.add_argument('--no-web', action='store_true', help='Evaluate evidence but disable web fallback')
    parser.add_argument('--legacy-crag', action='store_true', help='Use the original three-way CRAG without adaptive routing')
    parser.add_argument('--no-reflection', action='store_true', help='Skip draft checking and its possible single revision')
    parser.add_argument('--dry-run', action='store_true',
                        help='Use cached retrieval and save the prompt only; no API calls')
    args = parser.parse_args()
    question = args.question if args.question is not None else input('请输入问题：')
    engine = HandbookRAG(ROOT)
    record = engine._run_record(question, crag=not args.no_crag, allow_web=not args.no_web,
        reflection=not args.no_reflection, legacy_crag=args.legacy_crag,
        retrieval_offline=args.retrieval_offline, dry_run=args.dry_run)
    if args.dry_run:
        print(f'未调用 API；待发送提示词：{record["record_path"]}')
        return
    question, result = record['question'], record['generation']
    sources, messages, crag_report = record['sources'], record['messages'], record['crag']
    print(f'问题：{question}\n\n{result["answer"]}')
    audit = record['citation_audit']
    cited_sources = [source for i, source in enumerate(sources, 1) if i in audit['cited_numbers']]
    if cited_sources:
        print('\n引用来源：')
        for source in cited_sources:
            title = source['text'].splitlines()[0]
            if source.get('kind') == 'web_search_summary':
                print(f'[{source["label"]}] {title}')
                for ref in source['web_sources']:
                    print(f'  {ref["title"]}：{ref["url"]}')
            else:
                pages = '、'.join(map(str, source['pdf_pages']))
                print(f'[{source["label"]}] {title}；PDF 第 {pages} 页')
    model_used = result['response_model'] or record['draft_generation'].get('response_model') or '未调用'
    print(f'\n回答模型：{model_used}；最终生成响应的用量：{result["usage"]}')
    if 'assessment' in crag_report:
        print(f'CRAG：{crag_report["assessment"]["decision"]}；联网：{crag_report["web_search"]["status"]}')
        print(f'评估用量：{crag_report["evaluator_usage"]}；联网用量：{crag_report["web_search"].get("usage", {})}')
    if 'assessments' in crag_report:
        last = crag_report['assessments'][-1]['assessment']
        print(f'证据判断：{last["decision"]}；执行路由：{crag_report["route"]}；联网：{crag_report["web_search"]["status"]}')
    print(f'草稿复核：{record["reflection"]["status"]}')
    if not result['complete']:
        print('回答达到输出上限，结果不完整；未自动继续请求。')
    if audit['unknown_numbers'] or (messages and not audit['has_citations']):
        print('回答的引用编号检查未通过，请检查保存的原始结果。')
    print(f'完整记录：{record["record_path"]}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        main()
    except KeyboardInterrupt:
        print('Stopped.', file=sys.stderr)
        sys.exit(130)
    except (ModelAPIError, ValueError, OSError) as error:
        print(f'Error: {error}' if isinstance(error, (ModelAPIError, ValueError)) else
              f'Local file operation failed ({type(error).__name__}).', file=sys.stderr)
        sys.exit(1)
