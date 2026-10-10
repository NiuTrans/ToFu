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
* ``"atif"``            — ATIF v1.3 (Agent Trajectory Interchange Format,
                          harbor RFC 0001); the shape harbor/terminal-bench
                          trajectory viewers and RL pipelines consume

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


AVAILABLE_FORMATS = ('sharegpt', 'openai-finetune', 'anthropic', 'tofu-native',
                     'atif')


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


def _atif_messages(task: dict) -> list[dict]:
    """Prefer a committed working transcript; reconstruct input-only snapshots.

    ``run_task`` stamps provenance when writing back receipts and final prose.
    Appending toolRounds to that transcript duplicates every execution. Input
    history can contain old tools; only this run's evidence decides fallback.
    """
    messages = list(_input_messages(task))
    includes_run = task.get('_messages_include_current_run')
    if includes_run is None:
        # Legacy snapshots lack provenance. Historical input tools are not
        # current-run evidence, even if a provider recycles the same call ID.
        last_user = max((i for i, m in enumerate(messages)
                         if m.get('role') == 'user' and not m.get('_isMeta')),
                        default=-1)
        includes_run = any(m.get('role') == 'tool' or m.get('tool_calls')
                           for m in messages[last_user + 1:])
    if not includes_run:
        rounds = task.get('toolRounds') or []
        if any(isinstance(r, dict) and 'toolCallId' in r for r in rounds):
            from lib.tasks_pkg.conv_message_builder._toolcalls import (
                _reconstruct_tool_call_messages,
            )
            messages.extend(_reconstruct_tool_call_messages(rounds) or [])
        else:
            # Historical fixture/plugin format. Keep call/result adjacency,
            # without treating display-only results as execution evidence.
            for rnd in rounds:
                if not isinstance(rnd, dict) or not rnd.get('tool_calls'):
                    continue
                messages.append({
                    'role': 'assistant', 'content': rnd.get('assistantContent') or '',
                    'tool_calls': rnd['tool_calls'],
                })
                messages.extend(_tool_messages({'toolRounds': [rnd]}))
    # The working transcript's final prose is the authority, not task.content
    # (which may aggregate earlier narration). Synthesize only when absent.
    if not messages or messages[-1].get('role') != 'assistant' or messages[-1].get('tool_calls'):
        final = _assistant_turn(task)
        final.pop('tool_calls', None)  # these are historical, not pending calls
        messages.append(final)
    return messages


def _atif_steps(messages: list[dict]) -> list[dict]:
    """Map ordered call occurrences and their receipts, never tool text to user."""
    import json

    steps: list[dict] = []
    pending: dict[str, list[dict]] = {}
    for message in messages:
        role = message.get('role', 'user')
        if role == 'tool':
            call_id = str(message.get('tool_call_id') or '')
            candidates = pending.get(call_id) or []
            result = {'content': _coerce_text(message.get('content'))}
            if candidates:
                step = candidates.pop(0)
                result['source_call_id'] = call_id
                step.setdefault('observation', {'results': []})['results'].append(result)
            else:
                # Keep the observation as an explicit orphan, not a fabricated
                # user request or a link to a call that never appeared.
                steps.append({
                    'step_id': len(steps) + 1, 'source': 'agent', 'message': '',
                    'observation': {'results': [result]},
                    'extra': {'original_role': 'tool', 'orphan_call_id': call_id},
                })
            continue
        step = {
            'step_id': len(steps) + 1,
            'source': ('agent' if role == 'assistant' else
                       'system' if role in ('system', 'developer') else 'user'),
            'message': _coerce_text(message.get('content')),
        }
        if role == 'assistant':
            if message.get('reasoning_content'):
                step['reasoning_content'] = _coerce_text(message['reasoning_content'])
            calls = []
            for tc in message.get('tool_calls') or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get('function') or {}
                if not isinstance(fn, dict):
                    continue
                raw_args = fn.get('arguments')
                try:
                    args = json.loads(raw_args or '{}') if isinstance(raw_args, str) else raw_args
                except (ValueError, TypeError):
                    args = {'_raw': raw_args}
                call_id = str(tc.get('id') or '')
                calls.append({
                    'tool_call_id': call_id,
                    'function_name': str(fn.get('name') or ''),
                    'arguments': args if isinstance(args, dict) else {'_raw': args},
                })
                if call_id:
                    pending.setdefault(call_id, []).append(step)
            if calls:
                step['tool_calls'] = calls
        steps.append(step)
    return steps


def _to_atif(task: dict) -> dict:
    """ATIF v1.3 — Agent Trajectory Interchange Format (harbor RFC 0001).

    Working transcript roles/order are retained; each tool receipt is attached
    to its matching call occurrence. Native input-only snapshots reuse causal
    replay. Token usage is task-level, so per-step metrics stay absent.
    """
    steps = _atif_steps(_atif_messages(task))

    try:
        from lib.version import __version__ as _ver
    except ImportError:
        _ver = 'unknown'
    agent: dict = {'name': 'tofu-agent', 'version': _ver}
    if task.get('model'):
        agent['model_name'] = task['model']

    usage = task.get('usage') or {}
    final_metrics: dict = {'total_steps': len(steps)}
    prompt_tokens = usage.get('prompt_tokens', usage.get('input_tokens'))
    completion_tokens = usage.get('completion_tokens', usage.get('output_tokens'))
    if prompt_tokens is not None:
        final_metrics['total_prompt_tokens'] = prompt_tokens
    if completion_tokens is not None:
        final_metrics['total_completion_tokens'] = completion_tokens

    return {
        'schema_version': 'ATIF-v1.3',
        'session_id': str(task.get('id') or ''),
        'agent': agent,
        'steps': steps,
        'final_metrics': final_metrics,
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
    elif fmt == 'atif':
        body = _to_atif(task)
    else:
        raise ValueError(
            f'unknown trajectory format: {fmt!r}; '
            f'must be one of {AVAILABLE_FORMATS}')
    return {'format': fmt, 'trajectory': body}
