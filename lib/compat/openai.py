"""lib/compat/openai.py — OpenAI Chat Completions ↔ Tofu translator.

Pure functions, no Flask imports. ``routes/compat_openai.py`` wires
them into HTTP.

Provides:
  * ``translate_openai_request(body)``   — OpenAI body → Tofu cfg + messages
  * ``build_openai_response(task, model, requested_id)``
                                          — Tofu task → OpenAI completion
  * ``stream_openai_chunks(task, ...)``  — generator of SSE wire frames
  * ``models_payload()``                 — /v1/models response
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncGenerator

from lib.compat._common import (
    apply_common_cfg,
    apply_tools_and_personal_defaults,
    short_id,
)
from lib.log import get_logger

logger = get_logger(__name__)


# ── Request translation ────────────────────────────────────────────

def translate_openai_request(body: dict) -> tuple[list[dict], dict, dict]:
    """Split an OpenAI body into (messages, cfg, options).

    ``options`` carries fields that don't map onto Tofu cfg but the
    caller (route handler) needs:
        - id              : optional client-supplied completion id
        - stream          : bool
        - timeout_s       : float (for sync mode)
    """
    messages = body.get('messages') or []
    if not isinstance(messages, list):
        raise ValueError('messages must be an array')

    cfg: dict = {}
    apply_common_cfg(cfg, body)
    # OpenAI-specific cfg fields.
    if 'stop' in body:
        cfg['stop'] = body['stop']
    if 'seed' in body:
        cfg['seed'] = body['seed']
    if 'response_format' in body:
        cfg['responseFormat'] = body['response_format']
    if 'user' in body:
        cfg['user'] = body['user']

    apply_tools_and_personal_defaults(cfg, body)

    # Reasoning / thinking — OpenAI's `reasoning_effort` (o-series) maps
    # to our `thinkingDepth`.
    eff = body.get('reasoning_effort') or body.get('reasoning', {}).get('effort')
    if eff:
        depth_map = {'low': 'medium', 'medium': 'high',
                      'high': 'max', 'minimal': 'medium'}
        cfg['thinkingDepth'] = depth_map.get(eff, eff)
        cfg['thinkingEnabled'] = True

    options = {
        'id': body.get('id') or '',
        'stream': bool(body.get('stream')),
        'timeout_s': float(body.get('timeout_s') or 600),
    }
    return list(messages), cfg, options


# ── Response translation (sync) ────────────────────────────────────

def _assistant_message(task: dict) -> dict:
    # Deliverable = narration-free answer from the segment model (epic
    # pt_cb8f98b0cb9b47fb, step 3). Single source of truth across sync +
    # streaming so a headless caller never sees inter-round scaffolding prose.
    from lib.tasks_pkg.segments import deliverable_text
    msg: dict = {'role': 'assistant', 'content': deliverable_text(task)}
    rounds = task.get('toolRounds') or []
    if rounds:
        last = rounds[-1] if isinstance(rounds[-1], dict) else None
        if last and last.get('tool_calls'):
            msg['tool_calls'] = last['tool_calls']
    return msg


def build_openai_response(task: dict, model: str,
                            requested_id: str = '') -> dict:
    """Turn a finished task into an OpenAI ``chat.completion`` body."""
    finish = task.get('finishReason') or 'stop'
    if task.get('status') == 'error':
        # 'error' is NOT a valid OpenAI finish_reason enum value
        # (stop|length|tool_calls|content_filter|function_call). A task that
        # reached build_openai_response already produced a completion body, so
        # report the neutral 'stop' — real error surfacing is the route's
        # api_internal_error path, not a bogus enum an SDK would reject.
        finish = 'stop'
    elif task.get('status') == 'aborted':
        finish = 'length'  # OpenAI doesn't have an 'aborted' code
    # Normalize our internal 'tool_use' to OpenAI's 'tool_calls' enum too.
    elif finish == 'tool_use':
        finish = 'tool_calls'
    usage = task.get('usage') or {}
    return {
        'id': requested_id or short_id('chatcmpl-'),
        'object': 'chat.completion',
        'created': int(time.time()),
        'model': model,
        'choices': [{
            'index': 0,
            'message': _assistant_message(task),
            'finish_reason': finish,
        }],
        'usage': {
            'prompt_tokens': int(usage.get('input_tokens') or
                                  usage.get('prompt_tokens') or 0),
            'completion_tokens': int(usage.get('output_tokens') or
                                      usage.get('completion_tokens') or 0),
            'total_tokens': int(usage.get('total_tokens') or 0),
        },
    }


# ── Streaming ──────────────────────────────────────────────────────

async def stream_openai_chunks(task, model: str, requested_id: str = '',
                               *, include_tofu_native: bool = False
                               ) -> AsyncGenerator[str, None]:
    """Yield SSE wire frames in OpenAI's ``chat.completion.chunk`` shape.

    ``include_tofu_native=True`` adds a `tofu` envelope to chunks for
    non-delta events (phase/tool_call/etc.). Vanilla OpenAI clients
    ignore unknown fields, so this is safe to leave on.

    Event-driven: blocks on the task's wakeup signal instead of polling,
    so it never pins a thread while the model is generating.
    """
    from lib.agent_core.admission import unregister_waiter, wait_for_event

    completion_id = requested_id or short_id('chatcmpl-')
    emitted_role = False
    cursor = 0
    task_id = task.get('id') or ''

    try:
      while True:
        with task['events_lock']:
            new_events = list(task['events'][cursor:])
            cursor = len(task['events'])

        for ev in new_events:
            etype = ev.get('type', '')
            if etype == 'delta':
                # ★ Narrator-leak root fix (epic pt_cb8f98b0cb9b47fb, step 3):
                #   a content delta is UNCLASSIFIABLE mid-stream (narration vs
                #   answer is only known at round close), and a wire client
                #   cannot retract bytes already sent. So we do NOT forward raw
                #   content deltas into the answer channel — the narration-free
                #   deliverable is emitted from the segment model at `done`.
                #   Thinking deltas DO stream live (reasoning_content), giving a
                #   real-time experience without polluting the answer. This
                #   retires the compat surface's dependence on DELTA_RESET.
                if not ev.get('thinking'):
                    continue
                chunk = {
                    'id': completion_id, 'object': 'chat.completion.chunk',
                    'created': int(time.time()), 'model': model,
                    'choices': [{'index': 0, 'delta': {}, 'finish_reason': None}],
                }
                if not emitted_role:
                    chunk['choices'][0]['delta']['role'] = 'assistant'
                    emitted_role = True
                chunk['choices'][0]['delta']['reasoning_content'] = ev['thinking']
                yield f'data: {json.dumps(chunk, ensure_ascii=False)}\n\n'
            elif etype == 'done':
                # ★ Emit the narration-free deliverable as content NOW, from the
                #   segment model (falls back to task['content']). One clean
                #   answer chunk — no inter-round scaffolding prose ever leaked.
                from lib.tasks_pkg.segments import deliverable_text
                answer = deliverable_text(task)
                if answer:
                    ans_chunk = {
                        'id': completion_id, 'object': 'chat.completion.chunk',
                        'created': int(time.time()), 'model': model,
                        'choices': [{'index': 0, 'delta': {}, 'finish_reason': None}],
                    }
                    if not emitted_role:
                        ans_chunk['choices'][0]['delta']['role'] = 'assistant'
                        emitted_role = True
                    ans_chunk['choices'][0]['delta']['content'] = answer
                    yield f'data: {json.dumps(ans_chunk, ensure_ascii=False)}\n\n'
                # ★ Tool-calls parity with the sync path (_assistant_message):
                #   the model may have finished on a tool call. The sync
                #   completion emits message.tool_calls, but the stream `done`
                #   branch previously emitted only content+finish_reason — so a
                #   streaming caller that requested tools saw
                #   finish_reason='tool_calls' with NO tool_calls payload. Emit
                #   them as a delta here (the OpenAI streaming tool-call shape).
                _rounds = task.get('toolRounds') or []
                _last = _rounds[-1] if (_rounds and isinstance(_rounds[-1], dict)) else None
                _tcs = _last.get('tool_calls') if _last else None
                if _tcs:
                    tc_chunk = {
                        'id': completion_id, 'object': 'chat.completion.chunk',
                        'created': int(time.time()), 'model': model,
                        'choices': [{'index': 0, 'delta': {}, 'finish_reason': None}],
                    }
                    if not emitted_role:
                        tc_chunk['choices'][0]['delta']['role'] = 'assistant'
                        emitted_role = True
                    tc_chunk['choices'][0]['delta']['tool_calls'] = [
                        {'index': i, **tc} for i, tc in enumerate(_tcs)]
                    yield f'data: {json.dumps(tc_chunk, ensure_ascii=False)}\n\n'
                final = {
                    'id': completion_id, 'object': 'chat.completion.chunk',
                    'created': int(time.time()), 'model': model,
                    'choices': [{'index': 0, 'delta': {},
                                  'finish_reason': ev.get('finishReason') or
                                                   task.get('finishReason') or 'stop'}],
                }
                if ev.get('usage') or task.get('usage'):
                    final['usage'] = ev.get('usage') or task.get('usage')
                yield f'data: {json.dumps(final, ensure_ascii=False)}\n\n'
                yield 'data: [DONE]\n\n'
                return
            elif include_tofu_native:
                chunk = {
                    'id': completion_id, 'object': 'chat.completion.chunk',
                    'created': int(time.time()), 'model': model,
                    'choices': [{'index': 0, 'delta': {}, 'finish_reason': None}],
                    'tofu': ev,
                }
                yield f'data: {json.dumps(chunk, ensure_ascii=False)}\n\n'

        if task.get('status') in ('done', 'error', 'aborted') and not new_events:
            # Terminal but the `done` event wasn't in the stream (e.g. a late
            # connect after completion) — still emit the deliverable so the
            # client gets the answer, then close.
            from lib.tasks_pkg.segments import deliverable_text
            answer = deliverable_text(task)
            if answer:
                ans_chunk = {
                    'id': completion_id, 'object': 'chat.completion.chunk',
                    'created': int(time.time()), 'model': model,
                    'choices': [{'index': 0, 'delta': {
                        **({'role': 'assistant'} if not emitted_role else {}),
                        'content': answer}, 'finish_reason': None}],
                }
                emitted_role = True
                yield f'data: {json.dumps(ans_chunk, ensure_ascii=False)}\n\n'
            yield 'data: [DONE]\n\n'
            return

        woke = await wait_for_event(task_id, timeout=15.0)
        if not woke:
            yield ': heartbeat\n\n'
    except (GeneratorExit, asyncio.CancelledError):
        logger.info('[compat:openai] stream client disconnected task=%s',
                    task_id[:8])
        raise
    finally:
        unregister_waiter(task_id)


# ── /v1/models ─────────────────────────────────────────────────────

def models_payload(*, owner_key_id: str = '') -> dict:
    """Build the ``/v1/models`` response from the dispatcher's view.

    When ``owner_key_id`` is supplied, the caller's BYO providers
    (registered via :mod:`lib.byo_providers`) also appear, with each
    served model exposed as ``id="<model>@<prov_id>"`` so OpenAI SDKs
    can pin runs to that endpoint without any custom-client code.
    Operator-curated models always come first.
    """
    out_models = []
    seen: set[str] = set()
    try:
        from lib import _SAVED_CONFIG  # type: ignore
    except ImportError as e:
        logger.debug('[compat:openai] _SAVED_CONFIG unavailable: %s', e)
        _SAVED_CONFIG = {}  # noqa: N806 — local fallback only
    for prov in (_SAVED_CONFIG.get('providers', []) or []):
        if not isinstance(prov, dict) or not prov.get('enabled', True):
            continue
        for m in (prov.get('models', []) or []):
            if not isinstance(m, dict):
                continue
            mid = m.get('model_id')
            if not mid or mid in seen:
                continue
            seen.add(mid)
            out_models.append({
                'id': mid,
                'object': 'model',
                'created': 0,
                'owned_by': prov.get('id') or 'tofu',
                'capabilities': m.get('capabilities') or [],
            })
    # ── BYO providers owned by THIS caller ────────────────────────
    if owner_key_id:
        try:
            from lib.byo_providers import list_providers
            for byo in list_providers(owner_key_id):
                if byo.get('disabled'):
                    continue
                prov_id = byo.get('id') or ''
                for m in (byo.get('models') or []):
                    if not isinstance(m, dict):
                        continue
                    mid = m.get('model_id')
                    if not mid:
                        continue
                    suffixed = f'{mid}@{prov_id}'
                    if suffixed in seen:
                        continue
                    seen.add(suffixed)
                    out_models.append({
                        'id': suffixed,
                        'object': 'model',
                        'created': int(byo.get('created_at') or 0),
                        'owned_by': prov_id,
                        'capabilities': m.get('capabilities') or [],
                        # Surface the human-readable name so SDKs that
                        # group-by-owner can show "cluster-A" rather
                        # than the opaque prov_xxx string.
                        'tofu_provider_name': byo.get('name') or '',
                    })
        except Exception as e:
            logger.debug('[compat:openai] BYO models enumeration failed: %s', e)
    return {'object': 'list', 'data': out_models}


__all__ = [
    'translate_openai_request',
    'build_openai_response',
    'stream_openai_chunks',
    'models_payload',
]
