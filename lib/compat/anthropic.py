"""lib/compat/anthropic.py — Anthropic Messages API ↔ Tofu translator.

Implements the request/response shape from Anthropic's official
Messages API (``POST /v1/messages``):
  * ``system`` as a string (or array of typed blocks)
  * ``messages`` with ``role: user|assistant`` only
  * ``content`` as either a plain string OR a list of blocks
    (``text``, ``image``, ``tool_use``, ``tool_result``, ``thinking``)
  * Streaming uses ``message_start`` / ``content_block_start`` /
    ``content_block_delta`` / ``content_block_stop`` /
    ``message_delta`` / ``message_stop`` named events.

Pure functions; no Flask imports.
"""

from __future__ import annotations

import asyncio
import json
from typing import AsyncGenerator

from lib.compat._common import (
    CompatTerminalFailure,
    apply_common_cfg,
    apply_thinking_cfg,
    apply_tools_and_personal_defaults,
    require_deliverable_terminal,
    short_id,
)
from lib.error_envelope import BadRequest
from lib.log import get_logger, task_log_tag
from lib.turn_verdict import terminal_finish_reason

logger = get_logger(__name__)

# Anthropic's own vocabulary: a content-filter finish is the model's
# ``refusal``, never a successful ``end_turn``.
_STOP_REASON_BY_FINISH = {
    'stop': 'end_turn',
    'done': 'end_turn',
    'completed': 'end_turn',
    'end_turn': 'end_turn',
    'length': 'max_tokens',
    'max_tokens': 'max_tokens',
    'budget_exceeded': 'max_tokens',
    'incomplete': 'max_tokens',
    'context_length': 'model_context_window_exceeded',
    'max_turns': 'pause_turn',
    'tool_use': 'tool_use',
    'tool_calls': 'tool_use',
    'function_call': 'tool_use',
    'content_filter': 'refusal',
    'aborted': 'end_turn',
    'interrupted': 'end_turn',
}


def _stop_reason(finish: object) -> str:
    reason = str(finish or '')
    try:
        return _STOP_REASON_BY_FINISH[reason]
    except KeyError as exc:
        raise ValueError(f'unsupported verified finish reason: {reason!r}') from exc


def _terminal_stop_reason(task: dict, terminal_event: dict | None = None) -> str:
    """Project verified task/event evidence without defaulting to natural completion."""
    # Failed verdicts are refused before projection. Interrupted tasks retain
    # this adapter's existing representation, including an interrupted error task.
    if task.get('status') in ('aborted', 'error'):
        return 'end_turn'
    finish = (terminal_event['finishReason']
              if terminal_event is not None and 'finishReason' in terminal_event
              else terminal_finish_reason(task))
    return _stop_reason(finish)


def _system_to_text(system) -> str:
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        out = []
        for block in system:
            if isinstance(block, dict) and block.get('type') == 'text':
                out.append(block.get('text', ''))
            elif isinstance(block, str):
                out.append(block)
        return '\n\n'.join(out)
    return ''


def translate_anthropic_request(body: dict) -> tuple[list[dict], dict, dict]:
    """Anthropic body → (messages, cfg, options).

    Translates ``system`` into a leading ``role:'system'`` message so the
    Tofu pipeline can treat it uniformly.
    """
    raw_messages = body.get('messages') or []
    if not isinstance(raw_messages, list):
        raise BadRequest('messages must be an array', field='messages')
    try:
        timeout_s = float(body.get('timeout_s') or 600)
    except (TypeError, ValueError):
        raise BadRequest('timeout_s must be a number', field='timeout_s') from None

    messages: list[dict] = []
    system_text = _system_to_text(body.get('system'))
    if system_text:
        messages.append({'role': 'system', 'content': system_text})

    for m in raw_messages:
        if not isinstance(m, dict):
            continue
        role = m.get('role')
        content = m.get('content', '')
        # Anthropic only emits user|assistant; tool results come back
        # as user messages with content blocks.
        if role not in ('user', 'assistant'):
            continue
        messages.append({'role': role, 'content': content})

    cfg: dict = {}
    apply_common_cfg(cfg, body)
    # Anthropic-specific cfg fields.
    if 'stop_sequences' in body:
        cfg['stop'] = body['stop_sequences']
    if 'metadata' in body and isinstance(body['metadata'], dict):
        if body['metadata'].get('user_id'):
            cfg['user'] = body['metadata']['user_id']
    apply_thinking_cfg(cfg, body)

    apply_tools_and_personal_defaults(cfg, body)

    options = {
        'stream': bool(body.get('stream')),
        'timeout_s': timeout_s,
    }
    return messages, cfg, options


def _content_blocks_from_task(task: dict) -> list[dict]:
    """Build Anthropic content[] from the assembled task content."""
    from lib.tasks_pkg.segments import deliverable_text
    blocks: list[dict] = []
    if task.get('thinking'):
        blocks.append({'type': 'thinking', 'thinking': task['thinking']})
    # Deliverable = narration-free answer from the segment model (epic
    # , step 3) — one source of truth with the streaming path.
    text = deliverable_text(task)
    if text:
        blocks.append({'type': 'text', 'text': text})
    rounds = task.get('toolRounds') or []
    if rounds:
        last = rounds[-1] if isinstance(rounds[-1], dict) else None
        if last and last.get('tool_calls'):
            for tc in last['tool_calls']:
                fn = tc.get('function', {})
                try:
                    args = json.loads(fn.get('arguments') or '{}')
                except (json.JSONDecodeError, TypeError) as e:
                    logger.debug('[compat:anthropic] tool_call args JSON decode failed: %s', e)
                    args = {}
                blocks.append({
                    'type': 'tool_use',
                    'id': tc.get('id') or short_id('toolu_', 16),
                    'name': fn.get('name', ''),
                    'input': args,
                })
    return blocks


def build_anthropic_response(task: dict, model: str) -> dict:
    """Tofu finished task → Anthropic Messages response."""
    require_deliverable_terminal(task)
    stop_reason = _terminal_stop_reason(task)
    usage = task.get('usage') or {}
    return {
        'id': 'msg_' + (task.get('id') or short_id(n=16)),
        'type': 'message',
        'role': 'assistant',
        'model': model,
        'content': _content_blocks_from_task(task),
        'stop_reason': stop_reason,
        'stop_sequence': None,
        'usage': {
            'input_tokens': int(usage.get('input_tokens') or
                                 usage.get('prompt_tokens') or 0),
            'output_tokens': int(usage.get('output_tokens') or
                                  usage.get('completion_tokens') or 0),
        },
    }


def _evt(name: str, data: dict) -> str:
    return f'event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'


def _terminal_frames(task, model, msg_id, *, started, block_index,
                     thinking_block_open, terminal_event=None):
    """Project one verified terminal observation, including late connections."""
    event = terminal_event or {}
    try:
        require_deliverable_terminal(task, terminal_event)
    except CompatTerminalFailure as exc:
        yield _evt('error', {
            'type': 'error',
            'error': {
                'type': 'api_error',
                'message': str(exc),
            },
        })
        return
    if not started:
        yield _evt('message_start', {
            'type': 'message_start',
            'message': {
                'id': msg_id, 'type': 'message', 'role': 'assistant',
                'model': model,
                'content': [], 'stop_reason': None,
                'stop_sequence': None,
                'usage': {'input_tokens': 0, 'output_tokens': 0},
            },
        })
    # Close the live thinking block (if any) before the answer block.
    if thinking_block_open:
        yield _evt('content_block_stop', {
            'type': 'content_block_stop', 'index': block_index,
        })
        block_index += 1
    # Emit the narration-free deliverable as one text block NOW.
    from lib.tasks_pkg.segments import deliverable_text
    answer = deliverable_text(task)
    if answer:
        yield _evt('content_block_start', {
            'type': 'content_block_start',
            'index': block_index,
            'content_block': {'type': 'text', 'text': ''},
        })
        yield _evt('content_block_delta', {
            'type': 'content_block_delta',
            'index': block_index,
            'delta': {'type': 'text_delta', 'text': answer},
        })
        yield _evt('content_block_stop', {
            'type': 'content_block_stop', 'index': block_index,
        })
    # Tool blocks follow the answer, using the sync response's complete call batch.
    _rounds = task.get('toolRounds') or []
    _last = _rounds[-1] if (_rounds and isinstance(_rounds[-1], dict)) else None
    _tcs = _last.get('tool_calls') if _last else None
    if _tcs:
        if answer:
            block_index += 1
        for tc in _tcs:
            fn = tc.get('function', {})
            try:
                args = json.loads(fn.get('arguments') or '{}')
            except (json.JSONDecodeError, TypeError) as _e:
                logger.debug('[compat:anthropic] stream tool_call args '
                             'JSON decode failed: %s', _e)
                args = {}
            yield _evt('content_block_start', {
                'type': 'content_block_start',
                'index': block_index,
                'content_block': {
                    'type': 'tool_use',
                    'id': tc.get('id') or short_id('toolu_', 16),
                    'name': fn.get('name', ''),
                    'input': {},
                },
            })
            yield _evt('content_block_delta', {
                'type': 'content_block_delta',
                'index': block_index,
                'delta': {'type': 'input_json_delta',
                          'partial_json': json.dumps(args, ensure_ascii=False)},
            })
            yield _evt('content_block_stop', {
                'type': 'content_block_stop', 'index': block_index,
            })
            block_index += 1
    stop_reason = _terminal_stop_reason(task, terminal_event)
    usage = event.get('usage') or task.get('usage') or {}
    yield _evt('message_delta', {
        'type': 'message_delta',
        'delta': {'stop_reason': stop_reason,
                   'stop_sequence': None},
        'usage': {
            'input_tokens': int(usage.get('input_tokens') or 0),
            'output_tokens': int(usage.get('output_tokens') or 0),
        },
    })
    yield _evt('message_stop', {'type': 'message_stop'})
    return


async def stream_anthropic_chunks(task, model: str
                                  ) -> AsyncGenerator[str, None]:
    """Yield Anthropic-style SSE frames (named events).

    Sequence: message_start → content_block_start → content_block_delta*
              → content_block_stop → message_delta → message_stop.

    Event-driven: blocks on the task's wakeup signal instead of polling,
    so it never pins a thread while the model is generating.
    """
    from lib.agent_core.admission import unregister_waiter, wait_for_event

    msg_id = short_id('msg_', 16)
    cursor = 0
    started = False
    thinking_block_open = False
    block_index = 0
    task_id = task.get('id') or ''
    from lib.task_replay.task_replay import task_memory_replay_page


    try:
        while True:
            page = task_memory_replay_page(task, cursor)
            new_events = page.events
            cursor = page.next_cursor

            for ev in new_events:
                etype = ev.get('type', '')
                if not started:
                    yield _evt('message_start', {
                        'type': 'message_start',
                        'message': {
                            'id': msg_id, 'type': 'message', 'role': 'assistant',
                            'model': model,
                            'content': [], 'stop_reason': None,
                            'stop_sequence': None,
                            'usage': {'input_tokens': 0, 'output_tokens': 0},
                        },
                    })
                    started = True
                if etype == 'delta':
                    # Narrator-leak root fix (, step 3):
                    #   content deltas are unclassifiable mid-stream and a wire
                    #   client can't retract, so we do NOT stream raw content as the
                    #   answer text block. The narration-free deliverable is emitted
                    #   from the segment model at `done`. Thinking DOES stream live
                    #   as a thinking block (real-time reasoning without polluting
                    #   the answer). Retires the compat surface's DELTA_RESET need.
                    if ev.get('thinking'):
                        if not thinking_block_open:
                            yield _evt('content_block_start', {
                                'type': 'content_block_start',
                                'index': block_index,
                                'content_block': {'type': 'thinking', 'thinking': ''},
                            })
                            thinking_block_open = True
                        yield _evt('content_block_delta', {
                            'type': 'content_block_delta',
                            'index': block_index,
                            'delta': {'type': 'thinking_delta',
                                      'thinking': ev['thinking']},
                        })
                elif etype == 'done':
                    for frame in _terminal_frames(
                        task, model, msg_id, started=started, block_index=block_index,
                        thinking_block_open=thinking_block_open, terminal_event=ev):
                        yield frame
                    return

            if task.get('status') in ('done', 'error', 'aborted') and not new_events:
                for frame in _terminal_frames(
                    task, model, msg_id, started=started, block_index=block_index,
                    thinking_block_open=thinking_block_open):
                    yield frame
                return

            woke = await wait_for_event(task_id, timeout=15.0)
            if not woke:
                yield ': heartbeat\n\n'
    except (GeneratorExit, asyncio.CancelledError):
        logger.info('[compat:anthropic] stream client disconnected task=%s',
                    task_log_tag(task_id))
        raise
    finally:
        unregister_waiter(task_id)


__all__ = [
    'translate_anthropic_request',
    'build_anthropic_response',
    'stream_anthropic_chunks',
]
