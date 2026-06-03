"""lib/trajectory.py — Flatten a finished task into trajectory formats.

Tofu's task event log is rich (every tool call, phase transition, and
delta is captured). For trajectory generation / fine-tuning workflows
the consumer usually wants a flat, well-known shape. This module turns
``task['events'] + task['messages']`` into one of:

* ``"sharegpt"``        — list of ``{from, value}`` rows
* ``"openai-finetune"`` — JSONL-shaped ``{messages: [{role, content, tool_calls?}]}``
* ``"anthropic"``       — Claude-style ``{messages: [{role, content: [...]}]}``
* ``"tofu-native"``     — full event log (no transformation), for callers
                          that want every phase / delta / tool boundary

The ``"tofu-native"`` shape is the most lossless — use it when the
downstream system understands the rich event vocabulary. The other
three are lossy by design (delta merging, tool-call inlining) but
match what existing fine-tune pipelines expect.

Public API
----------
  flatten(task, fmt)     → dict (always ``{format, trajectory}``)
  AVAILABLE_FORMATS      → tuple[str, ...]
"""

from __future__ import annotations

from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['flatten', 'AVAILABLE_FORMATS']


AVAILABLE_FORMATS = ('sharegpt', 'openai-finetune', 'anthropic', 'tofu-native')


def _coerce_text(content: Any) -> str:
    """Turn a string-or-list content into a flat string.

    Multi-modal content blocks (``{type:'text',text:...}`` /
    ``{type:'image_url',...}``) collapse to their text part; image
    parts become ``[image]`` placeholders so the trajectory still
    reflects "an image was here" without bloating with base64.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get('type') == 'text':
                parts.append(b.get('text') or '')
            elif b.get('type') in ('image', 'image_url'):
                parts.append('[image]')
            elif 'text' in b:
                parts.append(str(b.get('text') or ''))
        return ''.join(parts)
    if content is None:
        return ''
    return str(content)


def _input_messages(task: dict) -> list[dict]:
    """Return the user-supplied prompt messages, oldest first."""
    msgs = task.get('messages') or []
    if not isinstance(msgs, list):
        return []
    return [m for m in msgs if isinstance(m, dict)]


def _assistant_turn(task: dict) -> dict:
    """Synthesise the final assistant message from the task's terminal state.

    Includes tool_calls (last round only) and reasoning_content when
    the underlying model emitted thinking.
    """
    out: dict = {'role': 'assistant', 'content': task.get('content') or ''}
    rounds = task.get('toolRounds') or []
    if rounds:
        last = rounds[-1] if rounds else None
        if isinstance(last, dict) and last.get('tool_calls'):
            out['tool_calls'] = last['tool_calls']
    if task.get('thinking'):
        out['reasoning_content'] = task['thinking']
    return out


def _tool_messages(task: dict) -> list[dict]:
    """Synthesise OpenAI-style ``role:tool`` messages from toolRounds."""
    rounds = task.get('toolRounds') or []
    out = []
    for rnd in rounds:
        if not isinstance(rnd, dict):
            continue
        for r in (rnd.get('results') or []):
            if not isinstance(r, dict):
                continue
            tcid = r.get('tool_call_id') or r.get('id') or ''
            out.append({
                'role': 'tool',
                'tool_call_id': tcid,
                'name': r.get('name') or '',
                'content': _coerce_text(r.get('result') or r.get('content') or ''),
            })
    return out


def _to_sharegpt(task: dict) -> list[dict]:
    out = []
    role_map = {'system': 'system', 'user': 'human', 'assistant': 'gpt',
                'tool': 'tool'}
    for m in _input_messages(task):
        role = role_map.get(m.get('role', 'user'), 'human')
        out.append({'from': role, 'value': _coerce_text(m.get('content'))})
    # Interleave tool messages between assistant rounds. For ShareGPT we
    # simplify: dump tool results in order, then the final assistant
    # message. Consumers that need multi-turn tool fidelity should use
    # ``openai-finetune`` or ``tofu-native``.
    for tm in _tool_messages(task):
        out.append({'from': 'tool', 'value': tm.get('content', '')})
    a = _assistant_turn(task)
    if a.get('tool_calls'):
        # Encode tool_calls as a JSON string so downstream tooling can
        # round-trip if it cares; ShareGPT has no native field for them.
        import json as _json
        out.append({'from': 'gpt',
                    'value': a.get('content', ''),
                    'tool_calls': _json.dumps(a['tool_calls'],
                                               ensure_ascii=False)})
    else:
        out.append({'from': 'gpt', 'value': a.get('content', '')})
    return out


def _to_openai_finetune(task: dict) -> dict:
    msgs: list[dict] = []
    for m in _input_messages(task):
        msgs.append({
            'role': m.get('role', 'user'),
            'content': _coerce_text(m.get('content')),
        })
    msgs.extend(_tool_messages(task))
    a = _assistant_turn(task)
    msg = {'role': 'assistant', 'content': a.get('content', '')}
    if a.get('tool_calls'):
        msg['tool_calls'] = a['tool_calls']
    msgs.append(msg)
    return {'messages': msgs}


def _to_anthropic(task: dict) -> dict:
    """Claude Messages-API shape.

    System message goes into a top-level ``system`` field; the rest
    becomes ``messages`` with content blocks.
    """
    sys_text = ''
    msgs: list[dict] = []
    for m in _input_messages(task):
        role = m.get('role', 'user')
        text = _coerce_text(m.get('content'))
        if role == 'system':
            sys_text = (sys_text + '\n' + text).strip() if sys_text else text
            continue
        msgs.append({'role': role,
                     'content': [{'type': 'text', 'text': text}]})
    a = _assistant_turn(task)
    blocks: list[dict] = []
    if a.get('reasoning_content'):
        blocks.append({'type': 'thinking', 'thinking': a['reasoning_content']})
    if a.get('content'):
        blocks.append({'type': 'text', 'text': a['content']})
    for tc in (a.get('tool_calls') or []):
        try:
            import json as _json
            fn = tc.get('function') or {}
            blocks.append({
                'type': 'tool_use',
                'id': tc.get('id') or '',
                'name': fn.get('name') or '',
                'input': _json.loads(fn.get('arguments') or '{}'),
            })
        except (ValueError, TypeError) as e:
            logger.debug('[Trajectory] tool_call parse failed: %s', e)
    msgs.append({'role': 'assistant', 'content': blocks or [{'type': 'text', 'text': ''}]})
    out = {'messages': msgs}
    if sys_text:
        out['system'] = sys_text
    return out


def _to_tofu_native(task: dict) -> dict:
    """Full event log + final state — lossless."""
    SKIP = {'events_lock', 'abort_event', 'content_lock'}
    state = {k: v for k, v in task.items() if k not in SKIP}
    # The events list itself is fine — it's all JSON-shaped already.
    return {
        'task_id': task.get('id'),
        'kind': task.get('kind') or 'chat',
        'status': task.get('status'),
        'finish_reason': task.get('finishReason'),
        'usage': task.get('usage') or {},
        'messages': state.get('messages') or [],
        'events': list(task.get('events') or []),
        'tool_rounds': task.get('toolRounds') or [],
        'final_assistant': _assistant_turn(task),
    }


def flatten(task: dict, fmt: str) -> dict:
    """Convert a finished (or in-flight) task into ``fmt``.

    Args:
        task: The task dict from a :class:`TaskRuntime` registry.
        fmt: One of :data:`AVAILABLE_FORMATS`.

    Returns:
        ``{'format': fmt, 'trajectory': <shape>}``.

    Raises:
        ValueError: ``fmt`` is unknown.
    """
    if fmt == 'sharegpt':
        body = _to_sharegpt(task)
    elif fmt == 'openai-finetune':
        body = _to_openai_finetune(task)
    elif fmt == 'anthropic':
        body = _to_anthropic(task)
    elif fmt == 'tofu-native':
        body = _to_tofu_native(task)
    else:
        raise ValueError(
            f'unknown trajectory format: {fmt!r}; '
            f'must be one of {AVAILABLE_FORMATS}')
    return {'format': fmt, 'trajectory': body}
