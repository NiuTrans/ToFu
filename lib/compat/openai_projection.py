"""Pure OpenAI wire projections shared by task and direct execution.

Entry points: openai_finish_reason, project_completion_usage, openai_completion_chunk.
Callers validate terminal evidence and choose content/accounting policy before
projection. This module owns no auth, execution, clock, event or billing state.
Dependencies: standard-library types only.
"""

from __future__ import annotations


def openai_finish_reason(raw_finish: str, *, aborted: bool = False) -> str:
    """Map one validated Tofu reason to OpenAI's closed finish enum."""
    if aborted:
        return 'length'
    mapping = {
        'stop': 'stop',
        'done': 'stop',
        'completed': 'stop',
        'end_turn': 'stop',
        'length': 'length',
        'max_tokens': 'length',
        'context_length': 'length',
        'budget_exceeded': 'length',
        'incomplete': 'length',
        'max_turns': 'length',
        'aborted': 'length',
        'interrupted': 'length',
        'tool_calls': 'tool_calls',
        'tool_use': 'tool_calls',
        'function_call': 'function_call',
        'content_filter': 'content_filter',
    }
    try:
        return mapping[raw_finish]
    except KeyError as exc:
        raise ValueError(
            f'unsupported verified finish reason: {raw_finish!r}') from exc


def project_completion_usage(usage: dict) -> dict:
    """Preserve provider counters without exporting private dispatch metadata."""
    prompt = int(usage.get('prompt_tokens') or usage.get('input_tokens') or 0)
    completion = int(usage.get('completion_tokens') or usage.get('output_tokens') or 0)
    public_usage = {key: value for key, value in usage.items() if not key.startswith('_')}
    public_usage.update(prompt_tokens=prompt, completion_tokens=completion,
                        total_tokens=int(usage.get('total_tokens') or prompt + completion))
    return public_usage


def openai_completion_chunk(completion_id: str, model: str, *, created: int,
                            delta: dict | None = None, finish_reason: str | None = None) -> dict:
    """Build one fresh wire value; callers own content, clocks and settlement."""
    return {
        'id': completion_id,
        'object': 'chat.completion.chunk',
        'created': created,
        'model': model,
        'choices': [{'index': 0, 'delta': {} if delta is None else delta,
                     'finish_reason': finish_reason}],
    }
