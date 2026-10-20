"""Retrieve ten, rerank five, expand direct context and generate one cited answer."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import re
import sys

if __package__:
    from .context_expansion import expand_context
    from .embed_chunks import ROOT, load_config, load_local_credentials
    from .embedding_api import ApiFailure, EmbeddingClient
    from .embedding_store import write_json
    from .generation_api import GenerationClient, load_generation_config
    from .rerank_api import RerankClient, load_rerank_config
    from .retrieve_chunks import load_corpus, run_query
else:
    from context_expansion import expand_context
    from embed_chunks import ROOT, load_config, load_local_credentials
    from embedding_api import ApiFailure, EmbeddingClient
    from embedding_store import write_json
    from generation_api import GenerationClient, load_generation_config
    from rerank_api import RerankClient, load_rerank_config
    from retrieve_chunks import load_corpus, run_query


def build_messages(question: str, context: dict, system_prompt: str) -> tuple[list[dict], list[dict]]:
    if not context['chunks']:
        raise ValueError('Cannot generate an answer without evidence')
    sources = [{'label': f'资料{i}', **chunk} for i, chunk in enumerate(context['chunks'], 1)]
    # JSON escaping keeps source text inside explicit fields instead of prompt-template delimiters.
    data = {'用户问题': question.strip(), '上下文补读是否完整': context['complete'],
            '检索资料': sources}
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
                        help='Require cached query/reranking; generation still calls the API once')
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
    messages, sources = build_messages(question, context, system_prompt)
    if sum(len(m['content']) for m in messages) > generation['max_prompt_chars']:
        raise ValueError('Answer prompt exceeds the configured character limit')
    output = ROOT / 'output/answers-v1'
    output.mkdir(parents=True, exist_ok=True)
    record = {'schema_version': 1, 'run_at_utc': datetime.now(timezone.utc).isoformat(),
              'question': question, 'vectors_sha256': receipt['vectors_sha256'],
              'retrieval': retrieval, 'answer_context': context, 'sources': sources,
              'messages': messages, 'generation_config': generation, 'answer_model_used': False,
              'status': 'prepared' if args.dry_run else 'pending'}
    # Save the exact evidence and prompt before sending; failures retain an inspectable record.
    write_json(output / 'last_run.json', record)
    if args.dry_run:
        print(f'未调用 API；待发送提示词：{output / "last_run.json"}')
        return
    client = GenerationClient(base_url, key, generation)
    try:
        result = client.generate(messages)
    except ApiFailure:
        record.update(status='generation_failed', generation_http_attempts=client.attempts)
        write_json(output / 'last_run.json', record)
        raise
    record.update(status='completed' if result['complete'] else 'truncated', answer_model_used=True,
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
            pages = '、'.join(map(str, source['pdf_pages']))
            print(f'[{source["label"]}] {title}；PDF 第 {pages} 页')
    print(f'\n模型：{generation["model"]}；本次生成用量：{result["usage"]}')
    if not result['complete']:
        print('回答达到输出上限，结果不完整；未自动继续请求。')
    if audit['unknown_numbers'] or not audit['has_citations']:
        print('回答的引用编号检查未通过，请检查保存的原始结果。')
    print(f'完整记录：{output / "last_run.json"}')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('Stopped.', file=sys.stderr)
        sys.exit(130)
    except (ApiFailure, ValueError, OSError) as error:
        print(f'Error: {error}' if isinstance(error, (ApiFailure, ValueError)) else
              f'Local file operation failed ({type(error).__name__}).', file=sys.stderr)
        sys.exit(1)
