"""routes/compat_openai.py — OpenAI-compatible adapter routes.

Mounted at:
  POST /v1/chat/completions
  GET  /v1/models
  POST /v1/embeddings    (delegates to the dispatcher's embedding path)

A drop-in for the OpenAI Python/JS SDKs, OpenWebUI, LangChain, Aider,
Cline, etc. — point ``base_url`` at this server and use a Tofu API
key as the OpenAI ``api_key``.

Auth: standard ``Authorization: Bearer tofu_…`` (validated by the
``bearer_auth_before_request`` middleware).
"""

from __future__ import annotations

from quart import Blueprint

from lib.agent_core.admission import (
    await_terminal, controller, register_waiter,
    unregister_waiter,
)
from lib.agent_core.execution_session import (
    acquire_and_bind_admission,
    bind_model_route,
    execution_session_for_task,
)
from lib.api_response import (
    api_bad_request, api_error, api_internal_error,
    sse_response,
)
from lib.model_routing import (
    ModelRoutingError,
)
from lib.compat.openai import (
    build_openai_response, models_payload, stream_openai_chunks,
    translate_openai_request,
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

compat_openai_bp = Blueprint('compat_openai', __name__)


# ── Helpers ────────────────────────────────────────────────────────

async def _wait_terminal(task, timeout_s: float):
    """Await terminal state without busy-waiting (event-driven)."""
    ok = await await_terminal(task, timeout_s=timeout_s)
    if not ok:
        raise RuntimeError('completion timed out')


# ── Routes ─────────────────────────────────────────────────────────

@compat_openai_bp.route('/v1/chat/completions', methods=['POST'])
@require_scope('chat')
@idempotent_post()
@api_meta(summary='OpenAI-compatible chat completion',
          description=(
              'Drop-in /v1/chat/completions endpoint. Set `base_url` to '
              'this server in the OpenAI SDK and use a Tofu API key.\n\n'
              'Streaming, tool_calls, vision content, response_format, '
              'and reasoning_effort are all supported. The underlying '
              'task gets a `task_id` you can poll via /api/v1/tasks/.'),
          tags=['compat:openai'], scope='chat')
async def chat_completions():
    body = await async_parse_body()
    try:
        messages, cfg, options = translate_openai_request(body)
    except ValueError as e:
        return api_bad_request(str(e))

    if not messages:
        return api_bad_request('messages is empty', field='messages')

    auth = current_auth()
    if auth is None or auth.owner_user_id is None:
        return api_bad_request('caller has no repository owner identity')

    # String model IDs remain compatible; Tofu creator/provider preferences
    # are orthogonal extension fields. Ambiguity is returned, never guessed.
    _route_group = None
    _model_in = cfg.get('model') or ''
    try:
        _model_id, _selection, _route_group = mint_compatible_request_route(
            body,
            model_id=_model_in,
            owner_user_id=auth.owner_user_id,
            tenant_id=auth.tenant_id,
            owner_tag=f'compat-openai:{auth.owner_user_id}',
            protocol='',
        )
        cfg['model'] = _model_id
    except ModelRoutingError as exc:
        return api_bad_request(str(exc), **routing_error_fields(exc))
    relay_denial = guard_model_relay_or_dispose(_route_group)
    if relay_denial is not None:
        return relay_denial

    audit_log('compat_openai_chat',
              key_id=(auth.key_id if auth else ''),
              name=(auth.name if auth else ''),
              model=cfg.get('model', '?'),
              n_messages=len(messages), stream=options['stream'])

    from lib.tasks_pkg.manager import create_task, reject_unstarted_chat_task
    from lib.tasks_pkg.spawn import spawn_task
    conv_id = short_id('compat-openai-', 12)
    task = create_task(
        conv_id, messages, cfg, user_id=int(request_user_id())
    )
    task['_inline_messages'] = True
    task['_compat_openai'] = True
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
    try:
        bind_model_route(
            execution_session,
            lambda: dispose_routed_slot_group(_route_group),
        )
    except Exception as exc:
        reject_unstarted_chat_task(
            task, exc, cause='model_route_bind_failed', conv_id=conv_id)
        raise

    # ── Admission control: refuse with 503 when at capacity ───────
    try:
        admission_lease = acquire_and_bind_admission(
            execution_session, controller)
    except Exception as exc:
        reject_unstarted_chat_task(
            task, exc, cause='admission_acquire_failed', conv_id=conv_id)
        raise
    if admission_lease is None:
        reject_unstarted_chat_task(
            task, RuntimeError('Task admission refused'),
            cause='task_admission_refused', conv_id=conv_id)
        logger.warning('[compat:openai] admission refused (in_flight=%d/%d) '
                       'key=%s model=%s', controller.in_flight,
                       controller.capacity, auth.key_id, cfg.get('model', '?'))
        return api_error('Server at capacity; retry shortly.', status=503,
                         error_kind='overloaded', retry_after=5)
    register_waiter(task['id'])

    try:
        spawn_task(task)
    except Exception as e:
        unregister_waiter(task['id'])
        reject_unstarted_chat_task(
            task, e, cause='task_spawn_failed', conv_id=conv_id)
        logger.exception('[compat:openai] spawn_task failed task=%s',
                         task['id'][:8])
        return api_internal_error(e, context='compat:openai',
                                   source='routes.compat_openai')

    model = cfg.get('model', '?')
    requested_id = options.get('id') or ''

    if options['stream']:
        gen = stream_openai_chunks(
            task, model=model, requested_id=requested_id,
            include_tofu_native=False,
        )
        return sse_response(
            gen, extra_headers={'X-Tofu-Task-Id': task['id']})

    try:
        await _wait_terminal(task, options['timeout_s'])
    except RuntimeError as e:
        logger.warning('[compat:openai] task=%s timed out model=%s elapsed=%.0fs',
                       task['id'][:8], model, options['timeout_s'])
        return api_internal_error(str(e), context='compat:openai')
    finally:
        unregister_waiter(task['id'])

    try:
        out = build_openai_response(
            task, model=model, requested_id=requested_id)
    except CompatTerminalFailure as exc:
        logger.warning('[compat:openai] refusing false-success task=%s cause=%s',
                       task['id'][:8], exc.verdict.cause)
        return api_internal_error(
            str(exc), context='compat:openai', log_traceback=False,
            error_kind=exc.verdict.cause,
        )
    out['task_id'] = task['id']  # extension; OpenAI SDKs ignore unknown fields
    try:
        if auth and auth.key_id:
            total = int(out.get('usage', {}).get('total_tokens') or 0)
            record_tokens(auth.key_id, total)
            record_usage(auth.key_id, n_tokens=total,
                          model=cfg.get('model', '') or '',
                          request_count=0)
    except Exception as e:
        logger.debug('[compat:openai] record_tokens failed: %s', e)
    # Return raw dict (no 'ok' envelope) — OpenAI SDKs expect the unwrapped
    # body. We intentionally bypass api_ok here.
    from quart import jsonify
    return jsonify(out)


@compat_openai_bp.route('/v1/models', methods=['GET'])
@require_scope('chat')
@api_meta(summary='OpenAI-compatible /v1/models',
          description=('Returns the owner\'s v2 official models and '
                       'provider-scoped pending deployments. Creator and '
                       'provider preferences are exposed as Tofu metadata.'),
          tags=['compat:openai'], scope='chat')
def models():
    from quart import jsonify
    auth = current_auth()
    if auth is None or auth.owner_user_id is None:
        return jsonify({'object': 'list', 'data': []})
    return jsonify(models_payload(
        owner_user_id=auth.owner_user_id,
        tenant_id=auth.tenant_id,
    ))


@compat_openai_bp.route('/v1/embeddings', methods=['POST'])
@require_scope('chat')
@api_meta(summary='OpenAI-compatible /v1/embeddings',
          tags=['compat:openai'], scope='chat')
def embeddings():
    body = parse_body()
    inp = body.get('input')
    model = (body.get('model') or '').strip()
    if not inp:
        return api_bad_request('input is required', field='input')
    if isinstance(inp, str):
        inputs = [inp]
    elif isinstance(inp, list) and all(isinstance(x, str) for x in inp):
        inputs = inp
    else:
        return api_bad_request('input must be string or string[]',
                                field='input')
    auth = current_auth()
    if auth is None or auth.owner_user_id is None:
        return api_bad_request('Authenticated owner is required')
    tofu = body.get('tofu') if isinstance(body.get('tofu'), dict) else {}
    preferred_provider_id = str(
        tofu.get('preferred_provider_id')
        or body.get('tofu_preferred_provider_id') or '').strip()
    try:
        from lib.model_routing.embedding_execution import (
            EmbeddingCapacityError,
            EmbeddingUnavailableError,
            EmbeddingUpstreamError,
            execute_embeddings,
        )
        payload = execute_embeddings(
            inputs,
            model=model,
            owner_user_id=auth.owner_user_id,
            tenant_id=auth.tenant_id,
            preferred_provider_id=preferred_provider_id,
        )
        from quart import jsonify
        return jsonify(payload)
    except ValueError as exc:
        return api_bad_request(str(exc), field='input')
    except EmbeddingCapacityError:
        return api_error(
            'Server at capacity; retry shortly.', status=503,
            error_kind='overloaded', retry_after=5,
        )
    except EmbeddingUnavailableError as exc:
        return api_error(str(exc), status=503)
    except EmbeddingUpstreamError as exc:
        return api_bad_request(
            f'Upstream embedding failed: {exc.status_code}',
            upstream_status=exc.status_code,
            upstream_body=exc.body_excerpt,
        )
    except ModelRoutingError as exc:
        return api_bad_request(str(exc), **routing_error_fields(exc))
    except Exception as exc:
        logger.warning(
            '[compat:openai] embeddings execution failed type=%s',
            type(exc).__name__, exc_info=True)
        return api_internal_error(
            exc,
            context='compat:openai',
            source='routes.compat_openai.embeddings',
            log_traceback=False,
        )


__all__ = ['compat_openai_bp']
