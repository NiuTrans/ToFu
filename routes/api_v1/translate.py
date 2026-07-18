"""routes/api_v1/translate.py — Translation REST surface.

Routes:
  POST /api/v1/translate                — synchronous translate
  POST /api/v1/translate/start          — start async task
  GET  /api/v1/translate/poll/<id>      — poll single task
  POST /api/v1/translate/poll-batch     — poll multiple tasks
  POST /api/v1/translate/mt-test        — test MT provider config

The PPTX multipart upload + binary download carve-outs stay in
``routes/translate.py`` (``POST /api/translate/pptx``,
``GET /api/translate/pptx/download/<file>``).

All routes require ``@require_auth``.
"""

from __future__ import annotations

import uuid

from flask import Blueprint, jsonify

from lib.api_response import api_bad_request, api_error, api_internal_error, api_ok
from lib.log import get_logger
from lib.openapi import api_meta
from lib.request_parser import parse_body
from lib.translate import (
    _build_translate_prompt,
    _cleanup_translate_tasks,
    _do_translate,
    _extract_notranslate_blocks,
    _reattach_notranslate_blocks,
    _strip_notranslate_tags,
    _SYNC_TRANSLATE_MAX_CHARS,
    _translate_freetext,
    _translate_runtime,
    _translate_tasks,
    _translate_tasks_lock,
)

from .auth import require_auth

logger = get_logger(__name__)

api_v1_translate_bp = Blueprint('api_v1_translate', __name__)


def _lang_params(data: dict) -> tuple[str, str]:
    """Extract (target, source) accepting both camelCase and snake_case.

    The UI sends ``targetLang``/``sourceLang``; the v1 OpenAPI schema (and
    the ``/api/v1/agents/translate`` façade) advertise ``target_lang``/
    ``source_lang``. Honour both so the documented snake_case keys are not
    silently dropped. camelCase wins when both are present (UI precedence).
    """
    target = data.get('targetLang') or data.get('target_lang') or 'English'
    source = data.get('sourceLang') or data.get('source_lang') or ''
    return target, source


def _build_poll_payload(task):
    """Shape one task's poll response."""
    r = {'taskId': task['id'], 'status': task['status']}
    if task.get('progress'):
        r['progress'] = task['progress']
    if task['status'] == 'done':
        r['translated'] = task['result']
        r['model'] = task.get('model')
    elif task['status'] == 'error':
        r['error'] = task['error']
    elif task['status'] == 'running':
        if task.get('statusMessage'):
            r['statusMessage'] = task['statusMessage']
            r['statusKind'] = task.get('statusKind', '')
        if task.get('partial'):
            r['partial'] = task['partial']
    return r


@api_v1_translate_bp.route('/api/v1/translate', methods=['POST'])
@require_auth
@api_meta(
    summary='Translate text synchronously',
    description=(
        'Returns ``{translated, model}`` for texts up to '
        f'{_SYNC_TRANSLATE_MAX_CHARS} chars. For longer inputs, use the '
        'async ``/api/v1/translate/start`` + ``poll`` flow.'
    ),
    tags=['translate'],
)
def translate_text_v1():
    data = parse_body()
    text = (data.get('text') or '').strip()
    if not text:
        return api_bad_request('No text')
    if len(text) > _SYNC_TRANSLATE_MAX_CHARS:
        return jsonify({
            'error': f'Text too long for sync ({len(text)} > {_SYNC_TRANSLATE_MAX_CHARS}). '
                     f'Use /api/v1/translate/start.',
            'useAsync': True,
        }), 413
    target, source = _lang_params(data)
    system_prompt = _build_translate_prompt(target, source)
    input_len = len(text)

    try:
        text, nt_blocks = _extract_notranslate_blocks(text)
        if nt_blocks:
            logger.info('[Translate.v1] sync: %d notranslate blocks', len(nt_blocks))
            if not text.strip():
                return jsonify({
                    'translated': _strip_notranslate_tags(
                        data.get('text', '').strip())
                })

        content, _usage = _translate_freetext(text, system_prompt,
                                              source=source, target=target)

        if not content or not content.strip():
            logger.error('[Translate.v1] empty result (%d chars, target=%s)',
                         input_len, target)
            return api_error('Empty translation result', status=502)

        if nt_blocks:
            content = _reattach_notranslate_blocks(content, nt_blocks)
        content = content.strip()

        _model = 'unknown'
        _truncated = False
        if isinstance(_usage, dict):
            _disp = _usage.get('_dispatch', {})
            _model = _disp.get('model', _usage.get('model', 'unknown'))
            # Surface the engine's completeness verdict so a display overlay
            # (e.g. the Project Brain content translation) can refuse to
            # replace a COMPLETE original with a known-incomplete translation.
            _truncated = ((_usage.get('_translate_trace') or {}).get('verdict')
                          == 'truncated')
        return jsonify({'translated': content, 'model': _model,
                        'truncated': _truncated})
    except Exception as e:
        logger.error('[Translate.v1] sync error (%d chars, %s): %s',
                     input_len, target, e, exc_info=True)
        return api_internal_error(e, source='api_v1.translate.sync')


@api_v1_translate_bp.route('/api/v1/translate/start', methods=['POST'])
@require_auth
@api_meta(
    summary='Start an async translation task',
    description='Returns ``{taskId}``. Poll with ``/api/v1/translate/poll/<id>``.',
    tags=['translate'],
)
def translate_start_v1():
    _cleanup_translate_tasks()
    data = parse_body()
    text = (data.get('text') or '').strip()
    if not text:
        return api_bad_request('No text')
    target, source = _lang_params(data)
    conv_id = data.get('convId', '')
    msg_idx = data.get('msgIdx')
    msg_id = (data.get('msgId') or '').strip() or None
    field = data.get('field', 'translatedContent')

    task = _translate_runtime.create(
        task_id=str(uuid.uuid4())[:12],
        meta={'convId': conv_id, 'msgIdx': msg_idx, 'msgId': msg_id,
              'field': field, 'targetLang': target, 'textLen': len(text)},
    )
    task.update({
        'status': 'running', 'result': None, 'error': None,
        'model': None, 'progress': None,
        'convId': conv_id, 'msgIdx': msg_idx, 'msgId': msg_id,
        'field': field, 'targetLang': target, 'textLen': len(text),
        'completed_at': None,
    })

    _translate_runtime.spawn(
        task['id'], _do_translate,
        task['id'], text, target, source, conv_id, msg_idx, field,
        msg_id=msg_id,
    )

    logger.info('[Translate.v1] started %s: %d chars → %s, conv=%s field=%s',
                task['id'], len(text), target,
                conv_id[:8] if conv_id else '?', field)
    return jsonify({'taskId': task['id']})


@api_v1_translate_bp.route('/api/v1/translate/poll/<task_id>', methods=['GET'])
@require_auth
@api_meta(summary='Poll a translation task', tags=['translate'])
def translate_poll_v1(task_id):
    with _translate_tasks_lock:
        task = _translate_tasks.get(task_id)
    if not task:
        return jsonify({'error': 'Task not found', 'status': 'not_found'}), 404
    return jsonify(_build_poll_payload(task))


@api_v1_translate_bp.route('/api/v1/translate/poll-batch', methods=['POST'])
@require_auth
@api_meta(
    summary='Poll multiple translation tasks at once',
    description='Body: ``{taskIds: [...]}``.',
    tags=['translate'],
)
def translate_poll_batch_v1():
    data = parse_body()
    task_ids = data.get('taskIds', [])
    results = []
    with _translate_tasks_lock:
        for tid in task_ids:
            task = _translate_tasks.get(tid)
            if not task:
                results.append({'taskId': tid, 'status': 'not_found'})
            else:
                results.append(_build_poll_payload(task))
    return jsonify(results)


@api_v1_translate_bp.route('/api/v1/translate/mt-test', methods=['POST'])
@require_auth
@api_meta(
    summary='Test a machine translation provider configuration',
    description='Body: ``{mt_config: {api_key, ...}, text?, source?, target?}``.',
    tags=['translate'],
)
def mt_test_v1():
    data = parse_body()
    mt_config = data.get('mt_config', {})
    text = data.get('text', 'Hello, this is a test.')
    source = data.get('source', 'en')
    target = data.get('target', 'zh')

    if not mt_config.get('api_key'):
        return api_error('API Key 未填写', status=200)

    api_key = mt_config.get('api_key', '')
    app_id = mt_config.get('app_id', '')
    api_url = mt_config.get('api_url', '')

    try:
        from lib.mt_provider import _niutrans_v1, _niutrans_v2, _normalize_lang
        src_lang = _normalize_lang(source)
        tgt_lang = _normalize_lang(target)

        if app_id:
            result = _niutrans_v2(text, src_lang, tgt_lang, api_key, app_id, api_url)
        else:
            result = _niutrans_v1(text, src_lang, tgt_lang, api_key, api_url)

        logger.info('[MT-Test.v1] OK: "%s"→"%s" (%s→%s)',
                    text[:50], result[:50], source, target)
        return api_ok({'translated': result})
    except Exception as e:
        logger.warning('[MT-Test.v1] Failed: %s', e)
        return api_error(str(e), status=200)


__all__ = ['api_v1_translate_bp']
