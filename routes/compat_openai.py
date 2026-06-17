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

import time
import uuid

from flask import Blueprint, Response

from lib.api_response import (
    api_bad_request, api_internal_error, api_not_found,
)
from lib.byo_resolve import dispose_after_terminal, resolve_model_and_provider
from lib.compat.openai import (
    build_openai_response, models_payload, stream_openai_chunks,
    translate_openai_request,
)
from lib.idempotency import idempotent_post
from lib.llm_dispatch.ephemeral import dispose_ephemeral_slot
from lib.log import audit_log, get_logger
from lib.openapi import api_meta
from lib.rate_limit_api import record_tokens
from lib.usage_tracker import record as record_usage
from lib.request_parser import parse_body

from routes.api_v1.auth import current_auth, require_scope

logger = get_logger(__name__)

compat_openai_bp = Blueprint('compat_openai', __name__)


# ── Helpers ────────────────────────────────────────────────────────

def _wait_terminal(task, timeout_s: float):
    deadline = time.time() + timeout_s
    poll = 0.1
    while task.get('status') not in ('done', 'error', 'aborted'):
        if time.time() >= deadline:
            raise RuntimeError('completion timed out')
        time.sleep(poll)
        poll = min(poll * 1.2, 1.5)


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
def chat_completions():
    body = parse_body()
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

    audit_log('compat_openai_chat',
              key_id=(auth.key_id if auth else ''),
              name=(auth.name if auth else ''),
              model=cfg.get('model', '?'),
              n_messages=len(messages), stream=options['stream'])

    from lib.tasks_pkg import create_task, spawn_task
    conv_id = f'compat-openai-{uuid.uuid4().hex[:12]}'
    task = create_task(conv_id, messages, cfg)
    task['_inline_messages'] = True
    task['_compat_openai'] = True
    if auth and auth.key_id:
        task['_api_key_id'] = auth.key_id
    # Hard provider isolation — see lib/llm_dispatch/provider_pin.py.
    if _byo_handle is not None:
        task['_pinned_provider_id'] = _byo_handle.slot.provider_id

    try:
        spawn_task(task)
    except Exception as e:
        if _byo_handle is not None:
            dispose_ephemeral_slot(_byo_handle)
        logger.exception('[compat:openai] spawn_task failed')
        return api_internal_error(e, context='compat:openai',
                                   source='routes.compat_openai')

    if _byo_handle is not None:
        import threading
        threading.Thread(
            target=dispose_after_terminal, args=(task, _byo_handle),
            name=f'byo-dispose-{_byo_handle.handle_id}', daemon=True,
        ).start()

    model = cfg.get('model', '?')
    requested_id = options.get('id') or ''

    if options['stream']:
        gen = stream_openai_chunks(
            task, model=model, requested_id=requested_id,
            include_tofu_native=False,
        )
        resp = Response(gen, mimetype='text/event-stream', headers={
            'Content-Type': 'text/event-stream; charset=utf-8',
            'Cache-Control': 'no-cache, no-transform',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
            'X-Tofu-Task-Id': task['id'],
        })
        return resp

    try:
        _wait_terminal(task, options['timeout_s'])
    except RuntimeError as e:
        return api_internal_error(str(e), context='compat:openai')

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
    from flask import jsonify
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
    from flask import jsonify
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
    from flask import jsonify
    return jsonify(resp.json())


__all__ = ['compat_openai_bp']
