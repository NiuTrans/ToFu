"""routes/compat_anthropic.py — Anthropic-compatible adapter routes.

Mounted at:
  POST /v1/messages
  POST /v1/messages/count_tokens

A drop-in for the Anthropic Python/JS SDK, Cline, Continue.dev, etc.
Auth: ``Authorization: Bearer tofu_…`` (validated by global middleware).
Anthropic also accepts ``x-api-key``; we honour that header too for full
SDK compatibility.
"""

from __future__ import annotations

from quart import Blueprint

from lib.agent_core.admission import (
    await_terminal, controller, register_waiter,
    unregister_waiter,
)
from lib.agent_core.execution_session import (
    ExecutionPhase,
    bind_admission_lease,
    bind_model_route,
    execution_session_for_task,
)
from lib.api_response import (
    api_bad_request, api_error, api_internal_error,
    sse_response,
)
from lib.model_routing import ModelRoutingError
from lib.compat.anthropic import (
    build_anthropic_response, stream_anthropic_chunks,
    translate_anthropic_request,
)
from lib.compat._common import CompatTerminalFailure
from lib.idempotency import idempotent_post
from lib.ids import short_id
from lib.log import audit_log, get_logger
from lib.openapi import api_meta
from lib.rate_limit_api import record_tokens
from lib.usage_tracker import record as record_usage
from lib.request_parser import async_parse_body, parse_body

from routes.api_v1.auth import (
    current_auth,
    guard_model_relay_or_dispose,
    request_user_id,
    require_scope,
)
from routes.model_routing_adapter import (
    dispose_routed_slot_group,
    mint_compatible_request_route,
    routing_error_fields,
)

logger = get_logger(__name__)

compat_anthropic_bp = Blueprint('compat_anthropic', __name__)


async def _wait_terminal(task, timeout_s: float):
    """Await terminal state without busy-waiting (event-driven)."""
    ok = await await_terminal(task, timeout_s=timeout_s)
    if not ok:
        raise RuntimeError('completion timed out')


@compat_anthropic_bp.route('/v1/messages', methods=['POST'])
@require_scope('chat')
@idempotent_post()
@api_meta(summary='Anthropic-compatible Messages API',
          description='Drop-in /v1/messages endpoint. Use the Anthropic '
                       'SDK with `base_url` set to this server and a '
                       'Tofu API key.',
          tags=['compat:anthropic'], scope='chat')
async def messages():
    body = await async_parse_body()
    try:
        msgs, cfg, options = translate_anthropic_request(body)
    except ValueError as e:
        return api_bad_request(str(e))
    if not msgs:
        return api_bad_request('messages is empty', field='messages')

    auth = current_auth()
    if auth is None or auth.owner_user_id is None:
        return api_bad_request('caller has no repository owner identity')

    # String model IDs remain compatible; Tofu creator/provider preferences
    # are explicit extensions and ambiguity is never guessed.
    _route_group = None
    _model_in = cfg.get('model') or ''
    try:
        _model_id, _selection, _route_group = mint_compatible_request_route(
            body,
            model_id=_model_in,
            owner_user_id=auth.owner_user_id,
            tenant_id=auth.tenant_id,
            owner_tag=f'compat-anthropic:{auth.owner_user_id}',
            protocol='',
        )
        cfg['model'] = _model_id
    except ModelRoutingError as exc:
        return api_bad_request(str(exc), **routing_error_fields(exc))
    relay_denial = guard_model_relay_or_dispose(_route_group)
    if relay_denial is not None:
        return relay_denial

    audit_log('compat_anthropic_messages',
              key_id=(auth.key_id if auth else ''),
              model=cfg.get('model', '?'),
              n_messages=len(msgs), stream=options['stream'])

    from lib.tasks_pkg.manager import create_task
    from lib.tasks_pkg.spawn import spawn_task
    conv_id = short_id('compat-anthropic-', 12)
    task = create_task(
        conv_id, msgs, cfg, user_id=int(request_user_id())
    )
    task['_inline_messages'] = True
    task['_compat_anthropic'] = True
    if auth and auth.key_id:
        task['_api_key_id'] = auth.key_id
    # Hard provider isolation — see lib/llm_dispatch/provider_pin.py.
    task['_pinned_provider_id'] = _route_group.pin_id
    task['_requested_model_ref'] = (
        _selection.model.public_dict()
        if _selection.model is not None
        else _selection.provider_offering.public_dict()
    )
    execution_session = execution_session_for_task(task)
    bind_model_route(
        execution_session,
        lambda: dispose_routed_slot_group(_route_group),
    )

    # ── Admission control: refuse with 503 when at capacity ───────
    admission_lease = controller.acquire()
    if admission_lease is None:
        execution_session.settle(
            ExecutionPhase.FAILED, cause='task_admission_refused')
        logger.warning('[compat:anthropic] admission refused '
                       '(in_flight=%d/%d) key=%s model=%s',
                       controller.in_flight, controller.capacity,
                       auth.key_id, cfg.get('model', '?'))
        return api_error('Server at capacity; retry shortly.', status=503,
                         error_kind='overloaded', retry_after=5)
    bind_admission_lease(
        execution_session,
        lambda: controller.release(admission_lease),
    )
    register_waiter(task['id'])

    try:
        spawn_task(task)
    except Exception as e:
        execution_session.settle(
            ExecutionPhase.FAILED, cause='task_spawn_failed')
        unregister_waiter(task['id'])
        logger.exception('[compat:anthropic] spawn_task failed task=%s',
                         task['id'][:8])
        return api_internal_error(e, context='compat:anthropic',
                                   source='routes.compat_anthropic')

    model = cfg.get('model', '?')

    if options['stream']:
        return sse_response(
            stream_anthropic_chunks(task, model=model),
            extra_headers={'X-Tofu-Task-Id': task['id']})

    try:
        await _wait_terminal(task, options['timeout_s'])
    except RuntimeError as e:
        logger.warning('[compat:anthropic] task=%s timed out model=%s '
                       'elapsed=%.0fs', task['id'][:8], model,
                       options['timeout_s'])
        return api_internal_error(str(e), context='compat:anthropic')
    finally:
        unregister_waiter(task['id'])

    try:
        out = build_anthropic_response(task, model=model)
    except CompatTerminalFailure as exc:
        logger.warning(
            '[compat:anthropic] refusing false-success task=%s cause=%s',
            task['id'][:8], exc.verdict.cause)
        return api_internal_error(
            str(exc), context='compat:anthropic', log_traceback=False,
            error_kind=exc.verdict.cause,
        )
    out['task_id'] = task['id']
    try:
        if auth and auth.key_id:
            usage = out.get('usage', {})
            total = (int(usage.get('input_tokens', 0))
                      + int(usage.get('output_tokens', 0)))
            record_tokens(auth.key_id, total)
            record_usage(auth.key_id, n_tokens=total,
                          model=cfg.get('model', '') or '',
                          request_count=0)
    except Exception as e:
        logger.debug('[compat:anthropic] record_tokens failed: %s', e)

    from quart import jsonify
    return jsonify(out)


@compat_anthropic_bp.route('/v1/messages/count_tokens', methods=['POST'])
@require_scope('chat')
@api_meta(summary='Anthropic-compatible token counter',
          tags=['compat:anthropic'], scope='chat')
def count_tokens():
    body = parse_body()
    try:
        msgs, _cfg, _opts = translate_anthropic_request(body)
    except ValueError as e:
        # Mirror the /v1/messages handler: a malformed body is a 400, not an
        # uncaught 500 (translate_anthropic_request raises ValueError on bad
        # input, and this call was the one place it wasn't guarded).
        return api_bad_request(str(e))
    # Reuse Tofu's token counter if available.
    n = 0
    try:
        from lib.token_counter import count_tokens
        result = count_tokens(msgs, model=body.get('model') or '')
        n = int(result.get('tokens') if isinstance(result, dict) else result)
    except Exception as e:
        logger.debug('[compat:anthropic] count_tokens fallback: %s', e)
        text = '\n'.join(
            (m.get('content') if isinstance(m.get('content'), str)
             else str(m.get('content')))
            for m in msgs
        )
        n = max(1, len(text) // 4)
    from quart import jsonify
    return jsonify({'input_tokens': int(n)})


__all__ = ['compat_anthropic_bp']
