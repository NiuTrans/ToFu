"""routes/api_v1/chat.py — POST /api/v1/chat/completions.

A clean, headless-first wrapper over the existing chat task pipeline.
Reuses ``create_task`` + ``spawn_task`` from ``lib.tasks_pkg`` so every
feature available to the UI (tool use, thinking, fallback chain, MCP,
etc.) is reachable from the API without re-implementation.

Two response modes:
  * ``stream: false`` (default) — block until the task is terminal,
    return the assistant turn (content + tool_calls + usage).
  * ``stream: true``            — Server-Sent Events. Each event mirrors
    the Tofu task event format. The final ``[DONE]`` line matches
    OpenAI's convention so generic SSE consumers terminate cleanly.

Because every response carries the underlying ``task_id``, the caller
can switch to ``/api/v1/tasks/{id}/...`` for fine-grained control
(abort, replay, late polling) on the same job.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

from flask import Blueprint, Response

from lib.agent_core.admission import (
    await_terminal, controller, on_terminal, register_waiter,
    unregister_waiter, wait_for_event,
)
from lib.api_response import (
    api_bad_request, api_error, api_internal_error, api_not_found, api_ok,
)
from lib.billing.request_flow import (
    estimate_prompt_tokens, release_reservation, reserve_for_task, settle_task,
)
from lib.idempotency import idempotent_post
from lib.log import audit_log, get_logger
from lib.openapi import api_meta
from lib.rate_limit_api import record_tokens
from lib.usage_tracker import record as record_usage
from lib.request_parser import (
    async_parse_body, optional_bool, optional_dict, optional_str, require_list,
)

from .auth import current_auth, guard_model_relay_or_dispose, require_scope

logger = get_logger(__name__)

api_v1_chat_bp = Blueprint('api_v1_chat', __name__)


# ── Helpers ─────────────────────────────────────────────────────────

def _validate_messages(messages: list) -> list[dict]:
    """Sanity-check the OpenAI-style messages array.

    The full transformation pipeline (image normalisation, gateway-term
    sanitisation, role merging) lives in
    ``lib.tasks_pkg.conv_message_builder._transform_messages`` and runs
    server-side later. Here we just reject obviously malformed input.
    """
    out = []
    for i, m in enumerate(messages):
        if not isinstance(m, dict):
            raise ValueError(f'messages[{i}] must be an object')
        role = m.get('role')
        if role not in ('system', 'user', 'assistant', 'tool'):
            raise ValueError(f'messages[{i}].role invalid: {role!r}')
        if 'content' not in m and 'tool_calls' not in m:
            raise ValueError(f'messages[{i}] missing content/tool_calls')
        out.append(m)
    return out


async def _wait_for_terminal(task, *, timeout_s: float):
    """Await the task reaching done/error/aborted (event-driven).

    Times out by raising RuntimeError (logged by the caller).
    """
    ok = await await_terminal(task, timeout_s=timeout_s)
    if not ok:
        raise RuntimeError('chat completion timed out')


def _assemble_assistant_message(task) -> dict:
    """Turn the in-memory task state into an OpenAI-shaped message."""
    msg = {'role': 'assistant', 'content': task.get('content') or ''}
    # Pick up tool_calls if any survived the orchestrator.
    rounds = task.get('toolRounds') or []
    if rounds:
        # The legacy task dict's toolRounds is a UI-shaped array; for
        # the API we re-emit only the most recent assistant tool_calls
        # batch (a real conversation continuation should poll
        # /api/v1/tasks for the full structured event log).
        last = rounds[-1] if rounds else None
        if isinstance(last, dict) and last.get('tool_calls'):
            msg['tool_calls'] = last['tool_calls']
    if task.get('thinking'):
        # Tofu-specific: surfaces reasoning in a non-standard field.
        # Compatible clients can ignore it; native callers consume it.
        msg['reasoning_content'] = task['thinking']
    return msg


def _completion_response(task, *, model: str, requested_id: str = '') -> dict:
    """Build an OpenAI-shaped chat.completion response from a finished task.

    ``finish_reason`` is normalised to OpenAI's enum so SDK clients
    don't choke on unknown values. Tofu-internal reasons go in the
    Tofu-specific ``error`` / ``finish_reason_internal`` fields.
    """
    raw_finish = task.get('finishReason') or 'stop'
    # OpenAI spec: stop | length | tool_calls | content_filter | function_call
    _OPENAI_FINISH = {'stop', 'length', 'tool_calls', 'content_filter',
                       'function_call'}
    finish = raw_finish if raw_finish in _OPENAI_FINISH else 'stop'
    if task.get('status') == 'error':
        finish = 'stop'
    elif task.get('status') == 'aborted':
        # Closest OpenAI mapping: caller cancelled = no completion.
        finish = 'stop'
    usage = task.get('usage') or {}
    _agent_in = int(usage.get('input_tokens') or usage.get('prompt_tokens') or 0)
    _agent_out = int(usage.get('output_tokens') or usage.get('completion_tokens') or 0)
    # Fold the input-translation round's tokens into the request totals so the
    # translate cost is reported (and billed) rather than hidden. Surface the
    # breakdown separately under ``translation_usage`` for transparency.
    _tu = task.get('_translate_usage') or {}
    _tr_in = int(_tu.get('input_tokens') or _tu.get('prompt_tokens') or 0)
    _tr_out = int(_tu.get('output_tokens') or _tu.get('completion_tokens') or 0)
    body = {
        'id': requested_id or f'chatcmpl-{uuid.uuid4().hex[:24]}',
        'object': 'chat.completion',
        'created': int(time.time()),
        'model': model,
        'choices': [{
            'index': 0,
            'message': _assemble_assistant_message(task),
            'finish_reason': finish,
        }],
        'usage': {
            'prompt_tokens': _agent_in + _tr_in,
            'completion_tokens': _agent_out + _tr_out,
            'total_tokens': _agent_in + _tr_in + _agent_out + _tr_out,
        },
        # Tofu-specific:
        'task_id': task.get('id'),
    }
    if _tr_in or _tr_out:
        body['translation_usage'] = {
            'prompt_tokens': _tr_in,
            'completion_tokens': _tr_out,
            'total_tokens': _tr_in + _tr_out,
            'model': (_tu.get('_dispatch') or {}).get('model') or _tu.get('model') or '',
        }
    if task.get('error'):
        body['error'] = task['error']
    if raw_finish != finish:
        body['finish_reason_internal'] = raw_finish
    return body


def _sse_event(payload) -> str:
    """Format a single SSE message in OpenAI's wire shape."""
    return f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'


def _settle_streaming_billing(task, *, user_id: str, model: str) -> None:
    """Post-stream billing finalize. Idempotent on ``ref_id=task_id``.

    Stream-mode never enters the request handler's ``settle`` block, so
    we run the shared settle path here once the SSE generator
    terminates. Thin wrapper over :func:`lib.billing.request_flow.settle_task`
    so stream and block modes share identical reserve/settle semantics.
    """
    settle_task(task, user_id=user_id, model=model)


async def _stream_generator(task, model: str, requested_id: str,
                            *, billing_user_id: str = ''):
    """SSE generator: emits incremental chunks while the task runs.

    Wire format: each chunk is a ``chat.completion.chunk`` JSON line.
    Tofu's native event types (``phase``, ``tool_call``, ``messages_snapshot``)
    are surfaced under an ``event_type: "tofu.*"`` field on a chunk so
    they reach native clients without breaking OpenAI consumers (which
    ignore unknown fields).

    Event-driven: blocks on the task's wakeup signal instead of polling,
    so it never pins a thread while the model is generating.

    When ``billing_user_id`` is set, the generator runs
    :func:`_settle_streaming_billing` exactly once before yielding the
    terminal ``[DONE]`` line so the customer is debited even when the
    HTTP response is in stream mode.
    """
    completion_id = requested_id or f'chatcmpl-{uuid.uuid4().hex[:24]}'
    emitted_role = False
    cursor = 0
    _billed = False
    task_id = task.get('id') or ''

    try:
      while True:
        # Snapshot events under the task's events_lock.
        with task['events_lock']:
            new_events = list(task['events'][cursor:])
            cursor = len(task['events'])

        for ev in new_events:
            etype = ev.get('type', '')
            chunk = {
                'id': completion_id,
                'object': 'chat.completion.chunk',
                'created': int(time.time()),
                'model': model,
                'choices': [{'index': 0, 'delta': {}, 'finish_reason': None}],
            }
            if not emitted_role:
                chunk['choices'][0]['delta']['role'] = 'assistant'
                emitted_role = True

            if etype == 'delta':
                if ev.get('content'):
                    chunk['choices'][0]['delta']['content'] = ev['content']
                if ev.get('thinking'):
                    chunk['choices'][0]['delta']['reasoning_content'] = ev['thinking']
                yield _sse_event(chunk)
            elif etype in ('phase', 'messages_snapshot', 'memory_prefetch',
                            'tool_call', 'tool_result',
                            'mcp_login_hint', 'aborted'):
                # Native channel: clients that know Tofu's vocabulary use it.
                chunk['tofu'] = ev
                yield _sse_event(chunk)
            elif etype == 'done':
                final = dict(chunk)
                final['choices'][0]['delta'] = {}
                final['choices'][0]['finish_reason'] = (
                    ev.get('finishReason') or task.get('finishReason') or 'stop')
                if ev.get('usage') or task.get('usage'):
                    final['usage'] = ev.get('usage') or task.get('usage')
                if ev.get('error') or task.get('error'):
                    final['tofu_error'] = ev.get('error') or task.get('error')
                yield _sse_event(final)
                if billing_user_id and not _billed:
                    _settle_streaming_billing(
                        task, user_id=billing_user_id, model=model)
                    _billed = True
                yield 'data: [DONE]\n\n'
                return

        if task.get('status') in ('done', 'error', 'aborted') and not new_events:
            # Synthesise a terminal frame if the orchestrator already
            # emitted the 'done' event before we started reading.
            yield _sse_event({
                'id': completion_id, 'object': 'chat.completion.chunk',
                'created': int(time.time()), 'model': model,
                'choices': [{'index': 0, 'delta': {},
                             'finish_reason': task.get('finishReason') or 'stop'}],
            })
            if task.get('usage'):
                yield _sse_event({'usage': task['usage']})
            if billing_user_id and not _billed:
                _settle_streaming_billing(
                    task, user_id=billing_user_id, model=model)
                _billed = True
            yield 'data: [DONE]\n\n'
            return

        # Block until the next event (or 15s heartbeat) — no busy-wait.
        woke = await wait_for_event(task_id, timeout=15.0)
        if not woke:
            yield ': heartbeat\n\n'
    except (GeneratorExit, asyncio.CancelledError):
        logger.info('[api_v1.chat] stream client disconnected task=%s',
                    task_id[:8])
        raise
    finally:
        unregister_waiter(task_id)


# ── Routes ──────────────────────────────────────────────────────────

@api_v1_chat_bp.route('/api/v1/chat/completions', methods=['POST'])
@require_scope('chat')
@idempotent_post()
@api_meta(
    summary='Chat completion (Tofu native)',
    description=(
        'Run an agent turn. Reuses the same orchestrator the UI uses, '
        'so every Tofu capability (tool use, thinking budgets, MCP, '
        'memory, project tools, fallback chain) is available.\n\n'
        'Set `stream=true` for SSE; otherwise the call blocks until the '
        'task is terminal. The response always carries `task_id` so '
        'callers can switch to `/api/v1/tasks/{id}/*` for replay or '
        'abort.'),
    tags=['chat'],
    scope='chat',
    request_body={'required': True, 'content': {'application/json': {
        'schema': {'$ref': '#/components/schemas/ChatCompletionRequest'},
    }}},
    responses={
        '200': {'description': 'OK',
                'content': {
                    'application/json': {
                        'schema': {'$ref': '#/components/schemas/ChatCompletionResponse'}},
                    'text/event-stream': {
                        'schema': {'type': 'string',
                                   'description': 'SSE stream of chat.completion.chunk frames'}},
                }},
    },
)
async def chat_completions():
    body = await async_parse_body()
    try:
        messages_in = require_list(body, 'messages')
        messages = _validate_messages(messages_in)
    except ValueError as e:
        return api_bad_request(str(e), field='messages')
    if not messages:
        return api_bad_request('messages is empty', field='messages')

    stream = optional_bool(body, 'stream', default=False)
    model = optional_str(body, 'model', default='', max_len=200)
    cfg = optional_dict(body, 'config') or {}

    # ── BYO provider suffix resolution ──
    # ``model="<name>@<prov_xxx>"`` pins this single request to a
    # caller-registered endpoint. We mint an ephemeral slot whose
    # lifetime equals the task; any other shape (plain alias, missing
    # suffix) falls through to the normal dispatcher pool.
    _byo_handle = None
    _byo_provider = None
    if model and '@' in model:
        from lib.byo_providers import resolve_model_string, touch_provider
        from lib.llm_dispatch.ephemeral import mint_ephemeral_slot
        _auth_for_byo = current_auth()
        _owner = (_auth_for_byo.key_id if _auth_for_byo else '') or ''
        _resolved = resolve_model_string(model, _owner)
        if _resolved is None:
            return api_not_found(
                f'model {model!r} references an unknown or disabled BYO '
                f'provider; check the @prov_xxx suffix')
        if _resolved.provider is not None:
            _prov = _resolved.provider
            try:
                _byo_handle = mint_ephemeral_slot(
                    base_url=_prov['base_url'],
                    api_key=_prov.get('api_key') or '',
                    model_id=_resolved.model_id,
                    owner=f'{_owner}:{_prov["id"]}',
                    extra_headers=_prov.get('extra_headers') or {},
                    # Carry the persisted dialect so the dispatcher
                    # uses the right body shape for this engine.
                    thinking_format=_prov.get('thinking_format') or '',
                )
                _byo_provider = _prov
                touch_provider(_prov['id'])
                model = _resolved.model_id  # strip the suffix
            except (ValueError, RuntimeError) as e:
                return api_bad_request(str(e), field='model')

    # ── BYO-only relay backstop ──
    # On a model_relay_enabled=false deployment, refuse requests that
    # would consume the OPERATOR's slot pool. BYO requests + admin keys
    # pass; a tenant pool request is rejected even if a stale key still
    # carries ``chat`` (scope strip at mint time is the primary control;
    # this is defense-in-depth). Disposes the slot on rejection.
    _relay_denied = guard_model_relay_or_dispose(_byo_handle)
    if _relay_denied is not None:
        return _relay_denied

    # Field mapping (body → cfg) is the shared kernel — the same code the
    # in-process façade (tofu.chat) uses, so the two surfaces can never
    # drift on how knobs land in cfg. Billing + BYO (above/below) stay
    # HTTP-only and are NOT part of the kernel.
    from lib.tasks_pkg.entry import build_chat_config
    cfg = build_chat_config(
        model, cfg,
        max_tokens=body.get('max_tokens') if 'max_tokens' in body else None,
        temperature=body.get('temperature') if 'temperature' in body else None,
        tools=body.get('tools') if 'tools' in body else None,
        response_format=(body.get('response_format')
                         if 'response_format' in body else None),
        thinking_depth=(body.get('thinking_depth')
                        or body.get('thinkingDepth') or ''),
        user=body.get('user') or '',
    )

    requested_id = optional_str(body, 'id', default='', max_len=200)
    timeout_s = float(body.get('timeout_s') or 600)

    conversation_id = optional_str(body, 'conversation_id',
                                    default='', max_len=200)
    if not conversation_id:
        conversation_id = f'api-{uuid.uuid4().hex[:12]}'

    auth = current_auth()
    audit_log('api_chat_start',
              key_id=(auth.key_id if auth else ''),
              name=(auth.name if auth else ''),
              model=cfg.get('model', '?'),
              n_messages=len(messages), stream=stream)

    # ── Auto-translate the user's input to English (any source language) ──
    # English is the language the model performs best in. When the caller
    # enables ``config.autoTranslate`` (optionally pinning the source language
    # via ``config.translateSourceLang``), translate the LAST user message to
    # English before the agent sees it. The translation's own token usage is
    # captured and folded into the task usage below so the translate cost is
    # billed/reported, not hidden.
    _translate_usage = None
    _translate_original = None
    if cfg.get('autoTranslate'):
        from lib.chat.turn_builder import translate_user_text_to_english
        for _m in reversed(messages):
            if _m.get('role') == 'user' and isinstance(_m.get('content'), str):
                _translated, _orig, _tu, _fail = translate_user_text_to_english(
                    _m['content'], cfg)
                if _orig:
                    _m['content'] = _translated
                    _m['originalContent'] = _orig
                    _translate_usage = _tu
                    _translate_original = _orig
                    audit_log('api_chat_input_translated',
                              key_id=(auth.key_id if auth else ''),
                              src_lang=cfg.get('translateSourceLang', '') or 'auto',
                              orig_chars=len(_orig), en_chars=len(_translated))
                elif _fail:
                    logger.warning('[api_v1.chat] input translate failed (%s); '
                                   'sending original text', _fail)
                break

    from lib.tasks_pkg import create_task, spawn_task
    task = create_task(conversation_id, messages, cfg)
    task['_inline_messages'] = True
    task['_api_v1'] = True
    if _translate_usage is not None:
        # Stash the translate usage so the post-terminal usage-merge folds
        # its tokens into the request total (cost accounting).
        task['_translate_usage'] = _translate_usage
    if auth and auth.key_id:
        task['_api_key_id'] = auth.key_id
    # ── Hard provider isolation ── bind the task to the BYO endpoint's
    # slot so no dispatch can leak onto operator keys. See
    # lib/llm_dispatch/provider_pin.py.
    if _byo_handle is not None:
        task['_pinned_provider_id'] = _byo_handle.slot.provider_id

    # ── Billing: pre-flight reserve (multi-user mode only) ──
    # Estimate the cost of the request based on prompt size + the
    # caller's max_tokens cap. If the wallet can't cover the
    # estimate, return 402 BEFORE dispatching the task. The estimate
    # is held as a ledger entry under ``ref_id=task_id``; settle()
    # refunds the gap post-flight. Personal/private/open installs
    # (no ``user_id``) skip this entirely.
    billing_user_id = (auth.user_id
                       if auth and getattr(auth, 'user_id', '') else '')
    reservation_micro = 0
    if billing_user_id:
        from lib.billing import InsufficientFunds
        try:
            est_completion = int(cfg.get('maxTokens')
                                  or body.get('max_tokens') or 1024)
            reservation_micro = reserve_for_task(
                task, user_id=billing_user_id,
                model=cfg.get('model', '') or '',
                prompt_tokens=estimate_prompt_tokens(messages),
                max_completion_tokens=est_completion)
        except InsufficientFunds as e:
            return api_error(
                f'Insufficient credits. '
                f'Estimated cost {e.needed_micro / 1_000_000:.4f} credits, '
                f'balance {e.balance_micro / 1_000_000:.4f}.',
                status=402,
                error_kind='insufficient_funds',
                balance_micro=e.balance_micro,
                needed_micro=e.needed_micro)

    # ── Admission control: refuse with 503 when at capacity ───────
    if not controller.try_acquire():
        if _byo_handle is not None:
            from lib.llm_dispatch.ephemeral import dispose_ephemeral_slot
            dispose_ephemeral_slot(_byo_handle)
        release_reservation(task, user_id=billing_user_id,
                            reservation_micro=reservation_micro)
        logger.warning('[api_v1.chat] admission refused (in_flight=%d/%d) '
                       'key=%s model=%s', controller.in_flight,
                       controller.capacity,
                       (auth.key_id if auth else '') or 'anonymous',
                       cfg.get('model', '?'))
        return api_error('Server at capacity; retry shortly.', status=503,
                         error_kind='overloaded', retry_after=5)

    # Release the admission slot + dispose the BYO ephemeral slot exactly
    # once, the moment the task reaches a terminal state. Event-driven
    # (fired from manager.append_event) — no per-request polling thread.
    _released = {'done': False}

    def _on_done(_tid, _handle=_byo_handle):
        if _released['done']:
            return
        _released['done'] = True
        controller.release()
        if _handle is not None:
            from lib.llm_dispatch.ephemeral import dispose_ephemeral_slot
            try:
                dispose_ephemeral_slot(_handle)
            except Exception as ex:
                logger.error('[api_v1.chat] ephemeral dispose failed '
                             'handle=%s task=%s: %s', _handle.handle_id,
                             _tid[:8], ex, exc_info=True)

    on_terminal(task['id'], _on_done)
    register_waiter(task['id'])

    try:
        spawn_task(task)
    except Exception as e:
        # spawn failed → release slot + dispose synchronously, drop waiter.
        _on_done(task['id'])
        unregister_waiter(task['id'])
        # Release any credit reservation immediately so the user isn't
        # waiting on the janitor.
        release_reservation(task, user_id=billing_user_id,
                            reservation_micro=reservation_micro)
        logger.exception('[api_v1.chat] spawn_task failed task=%s',
                         task['id'][:8])
        return api_internal_error(e, context='api_v1.chat',
                                   source='routes.api_v1.chat')

    if stream:
        gen = _stream_generator(task, cfg.get('model', model or '?'),
                                 requested_id,
                                 billing_user_id=billing_user_id)
        return Response(gen, mimetype='text/event-stream',
                         headers={
                             'Content-Type': 'text/event-stream; charset=utf-8',
                             'Cache-Control': 'no-cache, no-transform',
                             'X-Accel-Buffering': 'no',
                             'Connection': 'keep-alive',
                             'X-Tofu-Task-Id': task['id'],
                         })

    try:
        await _wait_for_terminal(task, timeout_s=timeout_s)
    except RuntimeError as e:
        logger.warning('[api_v1.chat] task=%s timed out model=%s elapsed=%.0fs',
                       task['id'][:8], cfg.get('model', '?'), timeout_s)
        return api_internal_error(str(e), context='api_v1.chat')
    finally:
        unregister_waiter(task['id'])

    body_out = _completion_response(task, model=cfg.get('model', model or '?'),
                                     requested_id=requested_id)
    # Record TPD usage + analytics post-hoc.
    try:
        if auth and auth.key_id:
            usage = body_out.get('usage') or {}
            total = int(usage.get('total_tokens') or 0)
            record_tokens(auth.key_id, total)
            record_usage(auth.key_id, n_tokens=total,
                          model=cfg.get('model', '') or '',
                          request_count=0)  # request was already counted by middleware
    except Exception as e:
        logger.debug('[api_v1.chat] record_tokens failed: %s', e)

    # ── Billing: settle the reservation (multi-user mode only) ──
    # If a reservation was placed pre-flight, ``settle()`` posts the
    # release + actual debit atomically. If no reservation (e.g.
    # estimator returned 0, or the model is in pricing.json with
    # zero cost), fall back to a direct ``debit()`` so we never
    # double-charge. Personal/private/open installs short-circuit
    # because ``user_id`` is empty.
    # ``settle_task`` reads task['usage'] (input/output OR prompt/completion
    # spellings), settles the reservation or debits directly, and is a
    # no-op when billing_user_id is empty.
    billing = settle_task(task, user_id=billing_user_id,
                          model=cfg.get('model', '') or '')
    if billing:
        body_out['billing'] = billing
    return api_ok(body_out)


# NOTE: ``POST /api/v1/chat/abort/<task_id>`` is intentionally NOT defined here.
# The single authoritative handler lives in ``routes/chat.py::chat_abort``
# (endpoint ``ui_chat_abort``) because it carries the real kill logic —
# SIGTERM to spawned subprocesses + external-backend abort signalling.
# A second registration on this same blueprint used to shadow that handler
# (Flask routes to the first-registered view), silently disabling the
# subprocess/backend kill and the scope gate. Removed 2026-06-01.


__all__ = ['api_v1_chat_bp']
