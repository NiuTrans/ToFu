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

import time
import uuid

from flask import Blueprint, Response

from lib.api_response import (
    api_bad_request, api_internal_error, api_not_found,
)
from lib.byo_resolve import dispose_after_terminal, resolve_model_and_provider
from lib.compat.anthropic import (
    build_anthropic_response, stream_anthropic_chunks,
    translate_anthropic_request,
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

compat_anthropic_bp = Blueprint('compat_anthropic', __name__)


def _wait_terminal(task, timeout_s: float):
    deadline = time.time() + timeout_s
    poll = 0.1
    while task.get('status') not in ('done', 'error', 'aborted'):
        if time.time() >= deadline:
            raise RuntimeError('completion timed out')
        time.sleep(poll)
        poll = min(poll * 1.2, 1.5)


@compat_anthropic_bp.route('/v1/messages', methods=['POST'])
@require_scope('chat')
@idempotent_post()
@api_meta(summary='Anthropic-compatible Messages API',
          description='Drop-in /v1/messages endpoint. Use the Anthropic '
                       'SDK with `base_url` set to this server and a '
                       'Tofu API key.',
          tags=['compat:anthropic'], scope='chat')
def messages():
    body = parse_body()
    try:
        msgs, cfg, options = translate_anthropic_request(body)
    except ValueError as e:
        return api_bad_request(str(e))
    if not msgs:
        return api_bad_request('messages is empty', field='messages')

    auth = current_auth()
    owner = (auth.key_id if auth else '') or 'anonymous'

    # ── BYO model resolution ──
    # Resolve ``model="name@prov_xxx"`` (+ optional inline ``provider``
    # block) against the caller's registered BYO providers, mirroring
    # /api/v1/chat so a BYO model advertised by /v1/models is actually
    # invokable through the Anthropic-compat adapter too.
    _byo_handle = None
    _model_in = cfg.get('model') or ''
    if _model_in:
        _model_id, _byo_handle, _byo_prov, _err, _status = (
            resolve_model_and_provider(_model_in, body.get('provider'), owner))
        if _err:
            return (api_not_found(_err) if _status == 404
                    else api_bad_request(_err, field='model'))
        cfg['model'] = _model_id  # strip the @suffix

    audit_log('compat_anthropic_messages',
              key_id=(auth.key_id if auth else ''),
              model=cfg.get('model', '?'),
              n_messages=len(msgs), stream=options['stream'])

    from lib.tasks_pkg import create_task, spawn_task
    conv_id = f'compat-anthropic-{uuid.uuid4().hex[:12]}'
    task = create_task(conv_id, msgs, cfg)
    task['_inline_messages'] = True
    task['_compat_anthropic'] = True
    if auth and auth.key_id:
        task['_api_key_id'] = auth.key_id

    try:
        spawn_task(task)
    except Exception as e:
        if _byo_handle is not None:
            dispose_ephemeral_slot(_byo_handle)
        logger.exception('[compat:anthropic] spawn_task failed')
        return api_internal_error(e, context='compat:anthropic',
                                   source='routes.compat_anthropic')

    if _byo_handle is not None:
        import threading
        threading.Thread(
            target=dispose_after_terminal, args=(task, _byo_handle),
            name=f'byo-dispose-{_byo_handle.handle_id}', daemon=True,
        ).start()

    model = cfg.get('model', '?')

    if options['stream']:
        return Response(
            stream_anthropic_chunks(task, model=model),
            mimetype='text/event-stream',
            headers={
                'Content-Type': 'text/event-stream; charset=utf-8',
                'Cache-Control': 'no-cache, no-transform',
                'X-Accel-Buffering': 'no',
                'Connection': 'keep-alive',
                'X-Tofu-Task-Id': task['id'],
            })

    try:
        _wait_terminal(task, options['timeout_s'])
    except RuntimeError as e:
        return api_internal_error(str(e), context='compat:anthropic')

    out = build_anthropic_response(task, model=model)
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

    from flask import jsonify
    return jsonify(out)


@compat_anthropic_bp.route('/v1/messages/count_tokens', methods=['POST'])
@require_scope('chat')
@api_meta(summary='Anthropic-compatible token counter',
          tags=['compat:anthropic'], scope='chat')
def count_tokens():
    body = parse_body()
    msgs, _cfg, _opts = translate_anthropic_request(body)
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
    from flask import jsonify
    return jsonify({'input_tokens': int(n)})


__all__ = ['compat_anthropic_bp']
