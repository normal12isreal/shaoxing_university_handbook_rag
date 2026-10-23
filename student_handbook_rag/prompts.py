"""Evidence prompts and citation numbering shared by CLI and Python callers."""
import json
import re


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
