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
    await_terminal, controller, on_terminal, register_waiter,
    unregister_waiter,
)
from lib.api_response import (
    api_bad_request, api_error, api_internal_error, api_not_found,
    sse_response,
)
from lib.byo_resolve import resolve_model_and_provider
from lib.compat.openai import (
    build_openai_response, models_payload, stream_openai_chunks,
    translate_openai_request,
)
from lib.idempotency import idempotent_post
from lib.ids import short_id
from lib.llm_dispatch.ephemeral import dispose_ephemeral_slot
from lib.log import audit_log, get_logger
from lib.openapi import api_meta
from lib.rate_limit_api import record_tokens
from lib.usage_tracker import record as record_usage
from lib.request_parser import async_parse_body, parse_body

from routes.api_v1.auth import current_auth, guard_model_relay_or_dispose, require_scope

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
    owner = (auth.key_id if auth else '') or 'anonymous'

    # ── BYO model resolution ──
    # Resolve ``model="name@prov_xxx"`` (and an inline ``provider``
    # block, if any) against the caller's registered BYO providers,
    # mirroring /api/v1/chat. Without this, a model that /v1/models
    # advertised with the @prov suffix could not actually be invoked
    # through the OpenAI-compat adapter.
    _byo_handle = None
    _model_in = cfg.get('model') or ''
    if _model_in:
        _model_id, _byo_handle, _byo_prov, _err, _status = (
            resolve_model_and_provider(_model_in, body.get('provider'), owner))
        if _err:
            return (api_not_found(_err) if _status == 404
                    else api_bad_request(_err, field='model'))
        cfg['model'] = _model_id  # strip the @suffix

    # BYO-only relay backstop (model_relay_enabled=false): refuse
    # operator-pool requests; BYO + admin pass. See routes/api_v1/auth.py.
    _relay_denied = guard_model_relay_or_dispose(_byo_handle)
    if _relay_denied is not None:
        return _relay_denied

    audit_log('compat_openai_chat',
              key_id=(auth.key_id if auth else ''),
              name=(auth.name if auth else ''),
              model=cfg.get('model', '?'),
              n_messages=len(messages), stream=options['stream'])

    from lib.tasks_pkg import create_task, spawn_task
    conv_id = short_id('compat-openai-', 12)
    task = create_task(conv_id, messages, cfg)
    task['_inline_messages'] = True
    task['_compat_openai'] = True
    if auth and auth.key_id:
        task['_api_key_id'] = auth.key_id
    # Hard provider isolation — see lib/llm_dispatch/provider_pin.py.
    if _byo_handle is not None:
        task['_pinned_provider_id'] = _byo_handle.slot.provider_id

    # ── Admission control: refuse with 503 when at capacity ───────
    if not controller.try_acquire():
        if _byo_handle is not None:
            dispose_ephemeral_slot(_byo_handle)
        logger.warning('[compat:openai] admission refused (in_flight=%d/%d) '
                       'key=%s model=%s', controller.in_flight,
                       controller.capacity, owner, cfg.get('model', '?'))
        return api_error('Server at capacity; retry shortly.', status=503,
                         error_kind='overloaded', retry_after=5)

    # Release slot + dispose BYO ephemeral slot once, on terminal state.
    _released = {'done': False}

    def _on_done(_tid, _handle=_byo_handle):
        if _released['done']:
            return
        _released['done'] = True
        controller.release()
        if _handle is not None:
            try:
                dispose_ephemeral_slot(_handle)
            except Exception as ex:
                logger.error('[compat:openai] ephemeral dispose failed '
                             'handle=%s task=%s: %s', _handle.handle_id,
                             _tid[:8], ex, exc_info=True)

    on_terminal(task['id'], _on_done)
    register_waiter(task['id'])

    try:
        spawn_task(task)
    except Exception as e:
        _on_done(task['id'])
        unregister_waiter(task['id'])
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

    out = build_openai_response(task, model=model, requested_id=requested_id)
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
          description=('Returns operator-curated models plus this '
                        'caller\'s registered BYO providers (each '
                        'served model surfaced as `id="<name>@<prov_id>"` '
                        'so OpenAI SDKs can pin without custom code).'),
          tags=['compat:openai'], scope='chat')
def models():
    from quart import jsonify
    auth = current_auth()
    owner = (auth.key_id if auth else '') or ''
    return jsonify(models_payload(owner_key_id=owner))


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
    if not model:
        try:
            from lib import EMBEDDING_MODELS
            model = (EMBEDDING_MODELS or [''])[0]
        except ImportError as e:
            logger.debug('[compat:openai] EMBEDDING_MODELS unavailable: %s', e)
            model = ''
    if not model:
        return api_bad_request('No embedding model configured', field='model')

    try:
        # Some providers route embeddings through a dedicated client; we
        # delegate to the dispatcher's pick_key flow and call the
        # provider's /embeddings directly.
        from lib.llm_dispatch import pick_key_for_model
        api_key, _key_name, slot = pick_key_for_model(model)
    except ImportError as e:
        return api_internal_error(e, context='Dispatcher unavailable',
                                  source='routes.compat_openai.embeddings')

    from lib.http_client import http_post
    base_url = ''
    try:
        if slot:
            base_url = getattr(slot, 'base_url', '') or ''
    except AttributeError as e:
        logger.debug('[compat:openai] slot.base_url unavailable: %s', e)
        base_url = ''
    if not base_url:
        try:
            from lib import LLM_BASE_URL as base_url  # type: ignore
        except ImportError as e:
            logger.debug('[compat:openai] LLM_BASE_URL unavailable, using default: %s', e)
            base_url = 'https://api.openai.com/v1'

    url = base_url.rstrip('/') + '/embeddings'
    try:
        resp = http_post(url, json={'model': model, 'input': inputs},
                         headers={'Authorization': f'Bearer {api_key}'},
                         timeout=60)
    except Exception as e:
        logger.warning('[compat:openai] embeddings fetch failed url=%s: %s',
                       url, e, exc_info=True)
        return api_internal_error(e, context='compat:openai',
                                  source='routes.compat_openai.embeddings',
                                  log_traceback=False)
    if not resp.ok:
        return api_bad_request(
            f'Upstream embedding failed: {resp.status_code}',
            upstream_status=resp.status_code,
            upstream_body=resp.text[:500])
    from quart import jsonify
    return jsonify(resp.json())


__all__ = ['compat_openai_bp']
