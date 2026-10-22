"""Retrieve ten, rerank five, expand direct context and generate one cited answer."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import re
import sys

if __package__:
    from .adaptive_routing import apply_adaptive_routing
    from .answer_reflection import reflect_answer
    from .context_expansion import expand_context
    from .crag import apply_crag, load_crag_config
    from .embed_chunks import ROOT, load_config, load_local_credentials
    from .embedding_api import ApiFailure, EmbeddingClient
    from .embedding_store import write_json
    from .generation_api import GenerationClient, load_generation_config
    from .rerank_api import RerankClient, load_rerank_config
    from .retrieve_chunks import load_corpus, run_query
    from .web_search import WebSearchClient
else:
    from adaptive_routing import apply_adaptive_routing
    from answer_reflection import reflect_answer
    from context_expansion import expand_context
    from crag import apply_crag, load_crag_config
    from embed_chunks import ROOT, load_config, load_local_credentials
    from embedding_api import ApiFailure, EmbeddingClient
    from embedding_store import write_json
    from generation_api import GenerationClient, load_generation_config
    from rerank_api import RerankClient, load_rerank_config
    from retrieve_chunks import load_corpus, run_query
    from web_search import WebSearchClient


def build_messages(question: str, context: dict, system_prompt: str) -> tuple[list[dict], list[dict]]:
    if not context['chunks']:
        raise ValueError('Cannot generate an answer without evidence')
    sources = [{'label': f'资料{i}', **chunk} for i, chunk in enumerate(context['chunks'], 1)]
    # JSON escaping keeps source text inside explicit fields instead of prompt-template delimiters.
    data = {'用户问题': question.strip(), '上下文补读是否完整': context['complete'],
            '检索资料': sources}
    if 'evidence_assessment' in context:
        data['证据评估'] = context['evidence_assessment']
    return [{'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': json.dumps(data, ensure_ascii=False, indent=2)}], sources


def citation_audit(answer: str, source_count: int) -> dict:
    cited = sorted({int(number) for number in re.findall(r'\[资料(\d+)\]', answer)})
    return {'cited_numbers': cited, 'unknown_numbers': [n for n in cited if not 1 <= n <= source_count],
            'has_citations': bool(cited),
            'note': 'Only citation numbering is checked; support and factual correctness require review.'}


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
    if not question.strip() or len(question) > 500:
        raise ValueError('Question must contain 1..500 characters')
    question = question.strip()
    generation = load_generation_config(ROOT)
    system_prompt = (ROOT / 'config/answer-system-v1.txt').read_text(encoding='utf-8')
    config = {**load_config(ROOT), 'max_requests_per_run': 3}
    reranking = {**load_rerank_config(ROOT), 'max_requests_per_run': 3}
    key, base_url = load_local_credentials(ROOT)
    if not args.dry_run and (not key or any(c.isspace() for c in key)):
        raise ValueError('Configure the local DashScope API key before answering')
    chunks, normalized, identity, receipt = load_corpus(ROOT, config, base_url)
    retrieval = run_query(question, chunks, normalized, identity, config,
                EmbeddingClient(base_url, key, config), ROOT / '.runtime/query-embeddings-v1',
                top_k=10, offline=args.retrieval_offline or args.dry_run,
                rerank_config=reranking, rerank_client=RerankClient(base_url, key, reranking),
                rerank_cache_dir=ROOT / '.runtime/reranking-v1')
    limits = json.loads((ROOT / 'config/table-context-v1.json').read_text(encoding='utf-8'))['expansion']
    context = expand_context([h['chunk_id'] for h in retrieval['hits']],
                             {c['chunk_id']: c for c in chunks}, limits)
    local_context = context
    crag_report = {'route': 'disabled' if args.no_crag else 'skipped_dry_run'}
    if not args.no_crag and not args.dry_run:
        crag_config = load_crag_config(ROOT)
        def evaluator_factory():
            return GenerationClient(base_url, key, {**generation, 'model': crag_config['evaluator_model'],
                                                    'max_completion_tokens': 500 if args.legacy_crag else 900})

        def local_retry(query):
            retried = run_query(query, chunks, normalized, identity, config,
                EmbeddingClient(base_url, key, config), ROOT / '.runtime/query-embeddings-v1',
                top_k=10, offline=args.retrieval_offline,
                rerank_config=reranking, rerank_client=RerankClient(base_url, key, reranking),
                rerank_cache_dir=ROOT / '.runtime/reranking-v1')
            expanded = expand_context([h['chunk_id'] for h in retried['hits']],
                                      {c['chunk_id']: c for c in chunks}, limits)
            return expanded, retried

        web = WebSearchClient(base_url, key, crag_config)
        if args.legacy_crag:
            context, crag_report = apply_crag(question, context, evaluator_factory(), web,
                (ROOT / 'config/crag-evaluator-v1.txt').read_text(encoding='utf-8'),
                allow_web=not args.no_web, max_chars=limits['max_total_chars'])
        else:
            context, crag_report = apply_adaptive_routing(question, context, evaluator_factory,
                (ROOT / 'config/crag-evaluator-v2.txt').read_text(encoding='utf-8'), local_retry, web,
                allow_web=not args.no_web, max_chars=limits['max_total_chars'])
    messages, sources = build_messages(question, context, system_prompt) if context['chunks'] else ([], [])
    terminal = crag_report.get('terminal_response')
    if terminal:
        messages = []
    if sum(len(m['content']) for m in messages) > generation['max_prompt_chars']:
        raise ValueError('Answer prompt exceeds the configured character limit')
    output = ROOT / 'output/answers-v1'
    output.mkdir(parents=True, exist_ok=True)
    record = {'schema_version': 3, 'run_at_utc': datetime.now(timezone.utc).isoformat(),
              'question': question, 'vectors_sha256': receipt['vectors_sha256'],
              'retrieval': retrieval, 'answer_context': context, 'sources': sources,
              'messages': messages, 'generation_config': generation, 'answer_model_used': False,
              'local_context': local_context, 'crag': crag_report,
              'reflection': {'status': 'skipped_dry_run' if args.dry_run else 'not_started'},
              'status': 'prepared' if args.dry_run else 'pending'}
    # Save the exact evidence and prompt before sending; failures retain an inspectable record.
    write_json(output / 'last_run.json', record)
    if args.dry_run:
        print(f'未调用 API；待发送提示词：{output / "last_run.json"}')
        return
    client = GenerationClient(base_url, key, generation)
    if messages:
        try:
            result = client.generate(messages)
        except ApiFailure:
            record.update(status='generation_failed', generation_http_attempts=client.attempts)
            write_json(output / 'last_run.json', record)
            raise
    else:
        result = {'answer': terminal or '现有资料没有提供足够依据，暂时无法确定这个问题的答案。',
                  'complete': True, 'usage': {}, 'response_model': None,
                  'finish_reason': crag_report['route'] if terminal else 'no_evidence'}
    record.update(draft_generation=result, generation_http_attempts=client.attempts, status='draft_ready')
    write_json(output / 'last_run.json', record)
    if messages and result['complete'] and not args.no_reflection:
        result, reflection = reflect_answer(question, context, result,
            GenerationClient(base_url, key, {**generation, 'max_completion_tokens': 1000}),
            lambda: GenerationClient(base_url, key, generation),
            (ROOT / 'config/answer-reflection-v1.txt').read_text(encoding='utf-8'), messages, citation_audit)
        record['reflection'] = reflection
    else:
        record['reflection'] = {'status': 'disabled' if args.no_reflection else 'skipped_no_complete_draft'}
    record.update(status='completed' if result['complete'] else 'truncated', answer_model_used=bool(messages),
                  generation_http_attempts=client.attempts, generation=result,
                  citation_audit=citation_audit(result['answer'], len(sources)))
    write_json(output / 'last_run.json', record)
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
    print(f'完整记录：{output / "last_run.json"}')


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    try:
        main()
    except KeyboardInterrupt:
        print('Stopped.', file=sys.stderr)
        sys.exit(130)
    except (ApiFailure, ValueError, OSError) as error:
        print(f'Error: {error}' if isinstance(error, (ApiFailure, ValueError)) else
              f'Local file operation failed ({type(error).__name__}).', file=sys.stderr)
        sys.exit(1)
