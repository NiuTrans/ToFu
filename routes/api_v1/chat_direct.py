"""routes/api_v1/chat_direct.py — POST /api/v1/chat/stream-direct.

A NATIVE-ASYNC, ON-LOOP streaming chat endpoint. Unlike
``/api/v1/chat/completions`` (which ``create_task`` + ``spawn_task`` onto an
OFF-loop worker thread and then tails the task's event buffer), this handler
drives ``lib.llm_dispatch.async_dispatch_stream`` **directly on the event
loop** — the httpx streaming call never occupies a thread-pool worker. This is
the production home that finally makes the native-async streaming path live
(see docs/API_CONTRACT.md §9).

How the on-loop bridge works
----------------------------
Native httpx callbacks arrive on the event loop; subscription/desktop adapters
may invoke the same callbacks from a bridge worker. One loop-owned scheduling
seam normalizes both sources before they touch the bounded ``asyncio.Queue``.
The dispatch is a background ``asyncio.Task`` and an async generator drains the
queue into SSE frames; completion flushes the queue, emits the terminal frame,
and closes with ``[DONE]``.

Deliberate scope (NOT a replacement for /chat/completions)
----------------------------------------------------------
This is a single-turn, loop-resident streaming relay: NO tool loop, NO MCP, NO
multi-round orchestration, NO task-replay/abort handle. Those require the full
orchestrator, which is correctly thread-based. Callers needing tools/replay use
``/chat/completions``. This endpoint is for low-latency, pure-text (± thinking)
streaming that benefits from staying on the loop. It shares authenticated v2
model routing, provider isolation, billing settlement, usage accounting, and
admission control with task-backed chat. A disconnected HTTP observer does not
cancel an already-started dispatch; relay chunks are dropped while the provider
response is consumed up to its finite request deadline.
"""

from __future__ import annotations

import asyncio

from quart import Blueprint

from lib.agent_core.admission import controller
from lib.agent_core.execution_session import (
    ExecutionPhase,
    ExecutionSession,
    bind_admission_lease,
    bind_billing_reservation,
    bind_model_route,
)
from lib.agent_core.direct_stream import (
    _DETACHED_DIRECT_DISPATCHES,
    _DIRECT_DEFAULT_TIMEOUT_S,
    _DIRECT_MAX_TIMEOUT_S,
    run_direct_stream,
)
from lib.agent_core.sse_limit import limiter as sse_limiter
from lib.api_response import api_bad_request, api_error, sse_response
from lib.billing.request_flow import (
    estimate_prompt_tokens, release_reservation, reserve_for_task, settle_task,
)
from lib.idempotency import idempotent_post
from lib.ids import short_id
from lib.llm.stream_result import ProviderStreamResult
from lib.log import audit_log, get_logger
from lib.model_routing import ModelRoutingError
from lib.openapi import api_meta
from lib.rate_limit_api import record_tokens
from lib.request_parser import (
    async_parse_body, optional_dict, optional_int, optional_str, require_list,
)
from lib.usage_tracker import record as record_usage

from .auth import (
    current_auth, guard_model_relay_or_dispose, request_principal,
    require_scope,
)
from .chat import _try_acquire_sse_slot, _validate_messages
from routes.model_routing_adapter import (
    dispose_routed_slot_group,
    mint_native_request_route,
    routing_error_fields,
)

logger = get_logger(__name__)

api_v1_chat_direct_bp = Blueprint('api_v1_chat_direct', __name__)

@api_v1_chat_direct_bp.route('/api/v1/chat/stream-direct', methods=['POST'])
@require_scope('chat')
@idempotent_post()
@api_meta(
    summary='Native-async streaming chat (on-loop, single-turn)',
    description=(
        'Stream a single-turn chat completion driven directly on the event '
        'loop via the native-async dispatcher — the httpx stream never '
        'occupies a worker thread. SSE only (always streams). NO tool loop / '
        'MCP / multi-round orchestration — use `/api/v1/chat/completions` for '
        'those. Frames are OpenAI `chat.completion.chunk` shape, terminated by '
        '`data: [DONE]`. If the client disconnects after provider dispatch '
        'starts, the server still consumes and validates the upstream response; '
        'this relay-only endpoint has no replay handle, so detached output is '
        'not recoverable by the caller. Dispatch lifetime defaults to 600 '
        'seconds and is capped at 900 seconds.'),
    tags=['chat'],
    scope='chat',
    request_body={'required': True, 'content': {'application/json': {
        'schema': {'$ref': '#/components/schemas/NativeChatCompletionRequest'},
    }}},
    responses={'200': {'description': 'SSE stream of chat.completion.chunk frames',
                       'content': {'text/event-stream': {
                           'schema': {'type': 'string'}}}}},
)
async def chat_stream_direct():
    body = await async_parse_body()
    try:
        messages = _validate_messages(require_list(body, 'messages'))
    except ValueError as e:
        return api_bad_request(str(e), field='messages')
    if not messages:
        return api_bad_request('messages is empty', field='messages')

    cfg_in = optional_dict(body, 'config') or {}
    timeout_s = optional_int(
        body, 'timeout_s', default=_DIRECT_DEFAULT_TIMEOUT_S,
        min=1, max=_DIRECT_MAX_TIMEOUT_S,
    )
    requested_id = optional_str(body, 'id', default='', max_len=200)
    completion_id = requested_id or short_id('chatcmpl-')

    auth = current_auth()
    owner_user_id = request_principal().require_owner(
        context='direct chat stream')
    if auth is None or auth.owner_user_id is None:
        return api_bad_request(
            'caller has no repository owner identity', field='model')

    route_group = None
    try:
        model, model_selection, route_group = await asyncio.to_thread(
            mint_native_request_route,
            body,
            owner_user_id=auth.owner_user_id,
            tenant_id=auth.tenant_id,
            owner_tag=f'direct-chat:{owner_user_id}',
        )
    except ModelRoutingError as exc:
        return api_bad_request(str(exc), **routing_error_fields(exc))
    relay_denial = guard_model_relay_or_dispose(route_group)
    if relay_denial is not None:
        return relay_denial

    from lib.tasks_pkg.entry import build_chat_config
    try:
        cfg = build_chat_config(
            model, cfg_in,
            max_tokens=(body.get('max_tokens')
                        if 'max_tokens' in body else None),
            temperature=(body.get('temperature')
                         if 'temperature' in body else None),
            thinking_depth=(body.get('thinking_depth')
                            or body.get('thinkingDepth') or ''),
        )
    except Exception:
        dispose_routed_slot_group(route_group)
        raise
    audit_log('api_chat_stream_direct',
              key_id=(auth.key_id if auth else ''),
              model=cfg.get('model', '?'), n_messages=len(messages))

    # A direct relay still owns one server-minted request identity so billing
    # idempotency never depends on a caller-supplied completion id.
    request_record = {
        'id': short_id('direct-', 20),
        'usage': {},
        '_requested_model_ref': (
            model_selection.model.public_dict()
            if model_selection.model is not None
            else model_selection.provider_offering.public_dict()
        ),
    }
    execution_session = ExecutionSession(
        execution_id=request_record['id'],
        kind='chat_direct',
        owner_user_id=owner_user_id,
        deadline_seconds=timeout_s,
    )
    bind_model_route(
        execution_session,
        lambda: dispose_routed_slot_group(route_group),
    )
    billing_user_id = auth.account_user_id or ''
    reservation_micro = 0
    if billing_user_id:
        from lib.billing import InsufficientFunds
        try:
            reservation_micro = await asyncio.to_thread(
                reserve_for_task,
                request_record,
                user_id=billing_user_id,
                model=cfg.get('model', '') or '',
                prompt_tokens=estimate_prompt_tokens(messages),
                max_completion_tokens=int(cfg.get('maxTokens') or 1024),
            )
        except InsufficientFunds as exc:
            execution_session.settle(
                ExecutionPhase.FAILED, cause='billing_reservation_refused')
            return api_error(
                'Insufficient credits.', status=402,
                error_kind='insufficient_funds',
                balance_micro=exc.balance_micro,
                needed_micro=exc.needed_micro,
            )
        except Exception:
            execution_session.settle(
                ExecutionPhase.FAILED, cause='billing_reservation_failed')
            raise

    terminal_metadata: dict = {}

    if billing_user_id:
        def _settle_direct_billing():
            billing = settle_task(
                request_record,
                user_id=billing_user_id,
                model=cfg.get('model', '') or '',
                raise_on_error=True,
            )
            if billing:
                terminal_metadata['billing'] = billing
            return billing

        bind_billing_reservation(
            execution_session,
            reservation_micro=reservation_micro,
            settle=_settle_direct_billing,
            release=lambda: release_reservation(
                request_record,
                user_id=billing_user_id,
                reservation_micro=reservation_micro,
                raise_on_error=True,
            ),
        )

    # Admission control: shared backpressure with the rest of the headless
    # surface. The lease follows the upstream dispatch, not the frontend SSE
    # lifetime, so repeated disconnects cannot create uncounted model work.
    sse_slot_token, sse_rejection = _try_acquire_sse_slot(auth)
    if sse_rejection is not None:
        await asyncio.to_thread(
            execution_session.settle,
            ExecutionPhase.FAILED,
            cause='sse_admission_refused',
        )
        return sse_rejection
    admission_lease = controller.acquire()
    if admission_lease is None:
        sse_limiter.release(sse_slot_token)
        await asyncio.to_thread(
            execution_session.settle,
            ExecutionPhase.FAILED,
            cause='task_admission_refused',
        )
        logger.warning('[chat_direct] admission refused (in_flight=%d/%d)',
                       controller.in_flight, controller.capacity)
        return api_error('Server at capacity; retry shortly.', status=503,
                         error_kind='overloaded', retry_after=5)
    bind_admission_lease(
        execution_session,
        lambda: controller.release(admission_lease),
    )
    dispatch_terminal: dict = {}

    def _dispatch_started():
        execution_session.mark_dispatch_started()

    def _settle_dispatch_sync():
        """Run storage/billing adapters off the event-loop thread."""
        result = dispatch_terminal.get('result')
        if isinstance(result, ProviderStreamResult):
            request_record['usage'] = dict(result.usage or {})
            dispatch = request_record['usage'].get('_dispatch') or {}
            if isinstance(dispatch, dict):
                request_record['provider_id'] = str(
                    dispatch.get('provider_id') or '')
        try:
            if auth.key_id:
                usage = request_record.get('usage') or {}
                total = int(usage.get('total_tokens') or (
                    int(usage.get('prompt_tokens') or usage.get('input_tokens') or 0)
                    + int(usage.get('completion_tokens') or usage.get('output_tokens') or 0)
                ))
                record_tokens(auth.key_id, total)
                record_usage(
                    auth.key_id, n_tokens=total,
                    model=cfg.get('model', '') or '', request_count=0)
        except Exception as exc:
            logger.debug(
                '[chat_direct] usage accounting failed request=%s type=%s',
                request_record['id'], type(exc).__name__)
        error = dispatch_terminal.get('error')
        if error is None and isinstance(result, ProviderStreamResult) \
                and result.is_verified_complete:
            outcome = ExecutionPhase.COMPLETED
            cause = ''
        elif isinstance(error, (TimeoutError, asyncio.TimeoutError)) \
                or execution_session.cancel_requested:
            outcome = ExecutionPhase.TIMED_OUT
            cause = 'execution_deadline_exceeded'
        elif isinstance(error, asyncio.CancelledError):
            outcome = ExecutionPhase.CANCELLED
            cause = 'dispatch_cancelled'
        else:
            outcome = ExecutionPhase.FAILED
            cause = 'provider_dispatch_failed'
        dispatch_terminal['execution_receipt'] = execution_session.settle(
            outcome, cause=cause)

    async def _dispatch_settled():
        if execution_session.is_terminal:
            return
        await asyncio.to_thread(_settle_dispatch_sync)

    inner = run_direct_stream(
        messages,
        model=cfg.get('model', model or '?'),
        cfg=cfg,
        completion_id=completion_id,
        owner_user_id=owner_user_id,
        pinned_provider_id=route_group.pin_id,
        dispatch_timeout_s=timeout_s,
        execution_session=execution_session,
        dispatch_terminal=dispatch_terminal,
        terminal_metadata=terminal_metadata,
        on_dispatch_started=_dispatch_started,
        on_dispatch_settled=_dispatch_settled,
    )

    async def _gen():
        try:
            async for frame in inner:
                sse_limiter.refresh(sse_slot_token)
                yield frame
        finally:
            try:
                await inner.aclose()
            except (GeneratorExit, asyncio.CancelledError):
                pass
            except Exception as e:
                logger.debug('[chat_direct] relay close failed type=%s',
                             type(e).__name__)
            # A response rejected/closed before the inner generator starts has
            # no _drive callback to release its lease. Once started, _drive is
            # the sole owner and may outlive this HTTP generator.
            if not execution_session.dispatch_started:
                await asyncio.to_thread(
                    execution_session.settle,
                    ExecutionPhase.FAILED,
                    cause='response_closed_before_dispatch',
                )
            sse_limiter.release(sse_slot_token)

    return sse_response(
        _gen(), extra_headers={'X-Tofu-Request-Id': request_record['id']})


__all__ = [
    '_DETACHED_DIRECT_DISPATCHES',
    'api_v1_chat_direct_bp',
    'run_direct_stream',
]
