"""Two bounded live examples using existing questions: local recovery and draft correction."""
import argparse
import json
from pathlib import Path
import sys

if __package__:
    from .adaptive_routing import apply_adaptive_routing
    from .answer_question import build_messages, citation_audit
    from .answer_reflection import reflect_answer
    from .context_expansion import expand_context
    from .crag import load_crag_config
    from .embed_chunks import ROOT, load_config, load_local_credentials
    from .embedding_api import EmbeddingClient
    from .embedding_store import output_lock, sha256, write_json
    from .evaluate_questions import DATASET, load_dataset, read_record
    from .generation_api import GenerationClient, load_generation_config
    from .rerank_api import RerankClient, load_rerank_config
    from .retrieve_chunks import load_corpus, run_query
    from .web_search import WebSearchClient
else:
    from adaptive_routing import apply_adaptive_routing
    from answer_question import build_messages, citation_audit
    from answer_reflection import reflect_answer
    from context_expansion import expand_context
    from crag import load_crag_config
    from embed_chunks import ROOT, load_config, load_local_credentials
    from embedding_api import EmbeddingClient
    from embedding_store import output_lock, sha256, write_json
    from evaluate_questions import DATASET, load_dataset, read_record
    from generation_api import GenerationClient, load_generation_config
    from rerank_api import RerankClient, load_rerank_config
    from retrieve_chunks import load_corpus, run_query
    from web_search import WebSearchClient


def execute(output, case):
    path = output / (case + '.json')
    if path.exists():
        raise ValueError('Example already recorded; choose a new output directory for new calls')
    questions, _, _ = load_dataset()
    qid = 'q008' if case == 'local_retry' else 'q003'
    question = next(q['question'] for q in questions if q['question_id'] == qid)
    source = ROOT / ('output/evaluation-keyword-v1/records/q008.json' if case == 'local_retry'
                     else 'output/evaluation-v1/records/q003.json')
    original = read_record(source)
    context = original['answer_context']
    key, url = load_local_credentials(ROOT)
    generation = load_generation_config(ROOT)
    factory = lambda: GenerationClient(url, key, generation)
    prompt = (ROOT / 'config/answer-system-v1.txt').read_text(encoding='utf-8')
    record = {'case': case, 'question': question, 'input_record_sha256': sha256(source.read_bytes()),
        'initial_context': context, 'status': 'prepared', 'web_enabled': False,
        'synthetic_wrong_draft': case == 'reflection'}
    write_json(path, record)
    if case == 'local_retry':
        embedding = {**load_config(ROOT), 'max_requests_per_run': 3}
        rerank = {**load_rerank_config(ROOT), 'max_requests_per_run': 3}
        chunks, normalized, identity, _ = load_corpus(ROOT, embedding, url)
        by_id = {c['chunk_id']: c for c in chunks}
        limits = json.loads((ROOT / 'config/table-context-v1.json').read_text(encoding='utf-8'))['expansion']

        def retry(query):
            result = run_query(query, chunks, normalized, identity, embedding,
                EmbeddingClient(url, key, embedding), ROOT / '.runtime/query-embeddings-v1',
                rerank_config=rerank, rerank_client=RerankClient(url, key, rerank),
                rerank_cache_dir=ROOT / '.runtime/reranking-v1')
            return expand_context([h['chunk_id'] for h in result['hits']], by_id, limits), result

        context, record['routing'] = apply_adaptive_routing(question, context,
            lambda: GenerationClient(url, key, {**generation, 'max_completion_tokens': 900}),
            (ROOT / 'config/crag-evaluator-v2.txt').read_text(encoding='utf-8'), retry,
            WebSearchClient(url, key, load_crag_config(ROOT)), allow_web=False)
        record['final_context'] = context
        write_json(path, record)
        if record['routing'].get('terminal_response'):
            record.update(status='terminal', terminal_response=record['routing']['terminal_response'])
            write_json(path, record)
            return record
    messages, record['sources'] = build_messages(question, context, prompt)
    record['messages'] = messages
    draft = (factory().generate(messages) if case == 'local_retry' else
        {'answer': '根据勤工助学规定，每周最多40小时，每月最多8小时。[资料1]',
         'complete': True, 'usage': {}, 'response_model': None, 'finish_reason': 'controlled_wrong_draft'})
    record['draft'] = draft
    write_json(path, record)
    result, report = reflect_answer(question, context, draft,
        GenerationClient(url, key, {**generation, 'max_completion_tokens': 1000}), factory,
        (ROOT / 'config/answer-reflection-v1.txt').read_text(encoding='utf-8'), messages, citation_audit)
    record.update(status='completed', generation=result, reflection=report,
                  citation_audit=citation_audit(result['answer'], len(record['sources'])))
    write_json(path, record)
    print(case + ': ' + report['status'] + '\n' + result['answer'], flush=True)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'output/evaluation-adaptive-examples-v1')
    parser.add_argument('--case', choices=['local_retry', 'reflection', 'all'], default='all')
    args = parser.parse_args()
    with output_lock(args.output):
        for case in (['local_retry', 'reflection'] if args.case == 'all' else [args.case]):
            execute(args.output, case)


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
