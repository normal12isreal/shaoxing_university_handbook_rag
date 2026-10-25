"""Reusable, stateful resource loader; each question gets fresh API clients."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import threading
from time import perf_counter
from uuid import uuid4

from scripts.adaptive_routing import apply_adaptive_routing
from scripts.answer_reflection import reflect_answer
from scripts.context_expansion import expand_context
from scripts.crag import apply_crag, load_crag_config
from scripts.embed_chunks import load_config, load_local_credentials
from scripts.embedding_api import ApiFailure, EmbeddingClient
from scripts.embedding_store import write_json
from scripts.generation_api import GenerationClient, load_generation_config
from scripts.rerank_api import RerankClient, load_rerank_config
from scripts.retrieve_chunks import load_corpus, run_query
from scripts.web_search import WebSearchClient
from .prompts import build_messages, citation_audit
from .results import answer_result


class HandbookRAG:
    """Load an existing project once and reuse it for sequential questions.

    Initialization reads local configuration/index files only. `ask` may call
    paid model APIs. Supply the data project explicitly, independent of cwd.
    Calls on the same instance are serialized to protect shared cache files.
    """

    def __init__(self, project_dir: str | Path, *, api_key: str | None = None,
                 base_url: str | None = None, save_records: bool = True):
        if type(save_records) is not bool:
            raise ValueError('save_records must be bool')
        self.project_dir = Path(project_dir).expanduser().resolve()
        self.generation = load_generation_config(self.project_dir)
        self.embedding = {**load_config(self.project_dir), 'max_requests_per_run': 3}
        self.reranking = {**load_rerank_config(self.project_dir), 'max_requests_per_run': 3}
        self._key, self._base_url = load_local_credentials(
            self.project_dir, api_key=api_key, base_url=base_url)
        if not self._key or any(character.isspace() for character in self._key):
            raise ValueError(
                'Missing or invalid DashScope API key. Fill in api_key in '
                '.runtime/dashscope.local.json or set DASHSCOPE_API_KEY. See README.md.')
        self.chunks, self.normalized, self.identity, self.receipt = load_corpus(
            self.project_dir, self.embedding, self._base_url)
        self.limits = json.loads((self.project_dir / 'config/table-context-v1.json').read_text(
            encoding='utf-8'))['expansion']
        self.crag_config = load_crag_config(self.project_dir)
        self.prompts = {name: (self.project_dir / f'config/{name}.txt').read_text(encoding='utf-8')
                       for name in ('answer-system-v1', 'crag-evaluator-v1',
                                    'crag-evaluator-v2', 'answer-reflection-v1')}
        self.save_records = save_records
        self._lock = threading.Lock()

    def ask(self, question: str, *, allow_web: bool = True, crag: bool = True,
            reflection: bool = True, retrieval_offline: bool = False) -> dict:
        """Return JSON-serializable answer, cited sources, status and stage usage.

        Raises ValueError for invalid input/configuration, ModelAPIError for
        upstream request failures, and OSError for local file failures.
        Clarification/refusal are normal results, not exceptions.
        """
        record = self._run_record(question, allow_web=allow_web, crag=crag,
            reflection=reflection, retrieval_offline=retrieval_offline)
        return answer_result(record)

    def _run_record(self, question, *, allow_web=True, crag=True, reflection=True,
                    retrieval_offline=False, legacy_crag=False, dry_run=False):
        if not isinstance(question, str) or not question.strip() or len(question) > 500:
            raise ValueError('Question must contain 1..500 characters')
        if any(type(flag) is not bool for flag in
               (allow_web, crag, reflection, retrieval_offline, legacy_crag, dry_run)):
            raise ValueError('Pipeline options must be bool')
        question = question.strip()
        if not dry_run and (not self._key or any(c.isspace() for c in self._key)):
            raise ValueError('Configure the local DashScope API key before answering')
        with self._lock:
            start = perf_counter()
            request_id = uuid4().hex
            output = self.project_dir / 'output/answers-v1'
            path = output / 'runs' / f'{request_id}.json'
            record = {'request_id': request_id, 'run_at_utc': datetime.now(timezone.utc).isoformat(),
                      'question': question, 'status': 'started',
                      'record_path': str(path) if self.save_records else None}

            def save():
                if self.save_records:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    write_json(path, record)
                    write_json(output / 'last_run.json', record)

            save()
            try:
                self._execute(question, record, save, allow_web, crag, reflection,
                              retrieval_offline, legacy_crag, dry_run)
            except (ApiFailure, ValueError, OSError) as exc:
                if not record['status'].endswith('_failed'):
                    record['status'] = 'failed'
                record['error_type'] = type(exc).__name__
                raise
            finally:
                record['elapsed_seconds'] = round(perf_counter() - start, 3)
                save()
            return record

    def _execute(self, question, record, save, allow_web, crag, reflection_enabled,
                 retrieval_offline, legacy_crag, dry_run):
        generation, config, reranking = self.generation, self.embedding, self.reranking
        key, base_url = self._key, self._base_url
        chunks, normalized, identity, receipt = self.chunks, self.normalized, self.identity, self.receipt
        system_prompt = self.prompts['answer-system-v1']
        retrieval = run_query(question, chunks, normalized, identity, config,
                    EmbeddingClient(base_url, key, config), self.project_dir / '.runtime/query-embeddings-v1',
                    top_k=10, offline=retrieval_offline or dry_run,
                    rerank_config=reranking, rerank_client=RerankClient(base_url, key, reranking),
                    rerank_cache_dir=self.project_dir / '.runtime/reranking-v1')
        limits = self.limits
        context = expand_context([h['chunk_id'] for h in retrieval['hits']],
                                 {c['chunk_id']: c for c in chunks}, limits)
        local_context = context
        crag_report = {'route': 'disabled' if not crag else 'skipped_dry_run'}
        if crag and not dry_run:
            crag_config = self.crag_config
            def evaluator_factory():
                return GenerationClient(base_url, key, {**generation, 'model': crag_config['evaluator_model'],
                                                        'max_completion_tokens': 500 if legacy_crag else 900})

            def local_retry(query):
                retried = run_query(query, chunks, normalized, identity, config,
                    EmbeddingClient(base_url, key, config), self.project_dir / '.runtime/query-embeddings-v1',
                    top_k=10, offline=retrieval_offline,
                    rerank_config=reranking, rerank_client=RerankClient(base_url, key, reranking),
                    rerank_cache_dir=self.project_dir / '.runtime/reranking-v1')
                expanded = expand_context([h['chunk_id'] for h in retried['hits']],
                                          {c['chunk_id']: c for c in chunks}, limits)
                return expanded, retried

            web = WebSearchClient(base_url, key, crag_config)
            if legacy_crag:
                context, crag_report = apply_crag(question, context, evaluator_factory(), web,
                    self.prompts['crag-evaluator-v1'],
                    allow_web=allow_web, max_chars=limits['max_total_chars'])
            else:
                context, crag_report = apply_adaptive_routing(question, context, evaluator_factory,
                    self.prompts['crag-evaluator-v2'], local_retry, web,
                    allow_web=allow_web, max_chars=limits['max_total_chars'])
        messages, sources = build_messages(question, context, system_prompt) if context['chunks'] else ([], [])
        terminal = crag_report.get('terminal_response')
        if terminal:
            messages = []
        if sum(len(m['content']) for m in messages) > generation['max_prompt_chars']:
            raise ValueError('Answer prompt exceeds the configured character limit')
        record.update({'schema_version': 3,
                  'question': question, 'vectors_sha256': receipt['vectors_sha256'],
                  'retrieval': retrieval, 'answer_context': context, 'sources': sources,
                  'messages': messages, 'generation_config': generation, 'answer_model_used': False,
                  'local_context': local_context, 'crag': crag_report,
                  'reflection': {'status': 'skipped_dry_run' if dry_run else 'not_started'},
                  'status': 'prepared' if dry_run else 'pending'})
        # Save the exact evidence and prompt before sending; failures retain an inspectable record.
        save()
        if dry_run:
            return
        client = GenerationClient(base_url, key, generation)
        if messages:
            try:
                result = client.generate(messages)
            except ApiFailure:
                record.update(status='generation_failed', generation_http_attempts=client.attempts)
                save()
                raise
        else:
            result = {'answer': terminal or '现有资料没有提供足够依据，暂时无法确定这个问题的答案。',
                      'complete': True, 'usage': {}, 'response_model': None,
                      'finish_reason': crag_report['route'] if terminal else 'no_evidence'}
        record.update(draft_generation=result, generation_http_attempts=client.attempts, status='draft_ready')
        save()
        if messages and result['complete'] and reflection_enabled:
            result, reflection = reflect_answer(question, context, result,
                GenerationClient(base_url, key, {**generation, 'max_completion_tokens': 1000}),
                lambda: GenerationClient(base_url, key, generation),
                self.prompts['answer-reflection-v1'], messages, citation_audit)
            record['reflection'] = reflection
        else:
            record['reflection'] = {'status': 'disabled' if not reflection_enabled else 'skipped_no_complete_draft'}
        record.update(status='completed' if result['complete'] else 'truncated', answer_model_used=bool(messages),
                      generation_http_attempts=client.attempts, generation=result,
                      citation_audit=citation_audit(result['answer'], len(sources)))
        save()
        return record
