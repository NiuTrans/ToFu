"""routes/api_v1/translate.py — Translation REST surface.

Routes:
  POST /api/v1/translate                — synchronous translate
  POST /api/v1/translate/start          — start async task
  GET  /api/v1/translate/poll/<id>      — poll single task
  POST /api/v1/translate/poll-batch     — poll multiple tasks
  POST /api/v1/translate/abort/<id>     — cancel queued/running task
  POST /api/v1/translate/mt-test        — test MT provider config

The PPTX multipart upload + binary download carve-outs stay in
``routes/translate.py`` (``POST /api/translate/pptx``,
``GET /api/translate/pptx/download/<file>``).

All routes require ``@require_auth``.
"""

from __future__ import annotations

import uuid

from quart import Blueprint

from lib.api_response import (
    api_bad_request, api_error, api_internal_error, api_not_found, api_ok,
    api_payload,
)
from lib.error_envelope import from_exception
from lib.log import get_logger
from lib.openapi import api_meta
from lib.request_parser import parse_body
from lib.translate.constants import _SYNC_TRANSLATE_MAX_CHARS
from lib.translate.errors import (
    TranslationContentRefused,
    TranslationNoAdmissibleProvider,
    TranslationProviderQueueFull,
)
from lib.translate.execution import (
    abort_translation_task,
    submit_translation_task,
)
from lib.translate.notranslate import (
    _extract_notranslate_blocks, _reattach_notranslate_blocks,
)
from lib.translate.prompt import _build_translate_prompt, _strip_notranslate_tags
from lib.translate.runtime._state import (
    _cleanup_translate_tasks, _translate_runtime,
)
from lib.turn_lifecycle import LifecycleNotFound, get_turn
from routes._task_routes import register_task_routes

from .auth import request_user_id, require_auth

logger = get_logger(__name__)

api_v1_translate_bp = Blueprint('api_v1_translate', __name__)


def _translate_freetext(*args, **kwargs):
    """Load the LLM/MT engine for an explicit synchronous translation."""
    from lib.translate.engine import _translate_freetext as implementation

    return implementation(*args, **kwargs)


def _do_translate(*args, **kwargs):
    """Load the translation worker inside its TaskRuntime thread."""
    from lib.translate.runtime._worker import _do_translate as implementation

    return implementation(*args, **kwargs)


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
        return api_error(
            f'Text too long for sync ({len(text)} > {_SYNC_TRANSLATE_MAX_CHARS}). '
            f'Use /api/v1/translate/start.',
            status=413, useAsync=True)
    target, source = _lang_params(data)
    system_prompt = _build_translate_prompt(target, source)
    input_len = len(text)

    try:
        text, nt_blocks = _extract_notranslate_blocks(text)
        if nt_blocks:
            logger.info('[Translate.v1] sync: %d notranslate blocks', len(nt_blocks))
            if not text.strip():
                return api_ok({
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
        return api_ok({'translated': content, 'model': _model,
                       'truncated': _truncated})
    except TranslationContentRefused as e:
        # Content guards exhausted their retry budget — NOT a server crash.
        # 502 + typed envelope so the frontend shows the real reason
        # ('translation rejected by quality check') instead of a bare
        # 'INTERNAL SERVER ERROR' (, 73× 500/day).
        logger.warning('[Translate.v1] content refused (%d chars, target=%s): %s',
                       input_len, target, e)
        return api_error(
            from_exception(e, kind='content_refused',
                           source='api_v1.translate.sync'),
            status=502)
    except TranslationProviderQueueFull as e:
        logger.info(
            '[Translate.v1] provider capacity full (%d chars, target=%s)',
            input_len,
            target,
        )
        return api_error(
            e,
            kind='server_busy',
            context='translation:provider_queue_saturated',
            source='api_v1.translate.sync',
            status=503,
        )
    except TranslationNoAdmissibleProvider as e:
        logger.info(
            '[Translate.v1] no admissible provider (%d chars, target=%s)',
            input_len,
            target,
        )
        return api_error(
            e,
            kind='no_slot',
            context='translation:no_admissible_slot',
            source='api_v1.translate.sync',
            status=503,
        )
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
    target, source = _lang_params(data)
    conv_id = str(data.get('convId') or '').strip()
    turn_id = str(data.get('turnId') or '').strip()
    msg_id = str(data.get('msgId') or '').strip()
    field = data.get('field', 'translatedContent')
    user_id = request_user_id()

    if bool(conv_id) != bool(turn_id):
        return api_bad_request(
            'Conversation-bound translation requires convId and turnId')
    if field not in {'translatedContent', 'content'}:
        return api_bad_request('Unsupported translation field')

    if conv_id:
        try:
            turn = get_turn(conv_id, turn_id, user_id=user_id)
        except LifecycleNotFound:
            return api_error('Turn not found', status=404)
        if turn.get('status') in {'pending', 'running', 'waiting_for_user'}:
            return api_error('Turn is still running', status=409)
        # The authoritative projection supplies the source. A stale or forged
        # browser payload can never translate different bytes into this turn.
        text = str((turn.get('projection') or {}).get('content') or '').strip()
    else:
        text = str(data.get('text') or '').strip()
    if not text:
        return api_bad_request('No text')

    task = _translate_runtime.create(
        user_id=int(user_id),
        task_id=str(uuid.uuid4())[:12],
        meta={'convId': conv_id, 'turnId': turn_id, 'msgId': msg_id,
              'userId': user_id, 'field': field, 'targetLang': target,
              'textLen': len(text)},
    )
    accepted = submit_translation_task(
        _translate_runtime,
        task['id'], _do_translate,
        task['id'], text, target, source, conv_id, turn_id, field,
        running_fields={'model': None, 'progress': None},
        user_id=user_id, message_id=msg_id,
    )
    if not accepted:
        return api_error(
            'Translation worker capacity is unavailable; retry shortly',
            status=503,
        )

    logger.info('[Translate.v1] started %s: %d chars → %s, conv=%s turn=%s field=%s',
                task['id'], len(text), target,
                conv_id[:8] if conv_id else '-',
                turn_id[:8] if turn_id else '-', field)
    return api_ok({'taskId': task['id']})


@api_v1_translate_bp.route('/api/v1/translate/poll/<task_id>', methods=['GET'])
@require_auth
@api_meta(summary='Poll a translation task', tags=['translate'])
def translate_poll_v1(task_id):
    user_id = int(request_user_id())
    task = _translate_runtime.get_owned(task_id, user_id=user_id)
    if not task:
        return api_payload({'error': 'Task not found',
                            'status': 'not_found'}, 404)
    return api_ok(_build_poll_payload(task))


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
    user_id = int(request_user_id())
    results = []
    for tid in task_ids:
        task = _translate_runtime.get_owned(str(tid), user_id=user_id)
        if not task:
            results.append({'taskId': tid, 'status': 'not_found'})
        else:
            results.append(_build_poll_payload(task))
    # Coordinated bare-array migration (batch 14): the array moves under
    # ``items``; Api.translate.pollBatch unwraps null-preservingly (the
    # caller's !Array.isArray(data) branch is the probe-failure fallback).
    return api_ok({'items': results})


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
            result = _niutrans_v2(
                text, src_lang, tgt_lang, api_key, app_id, api_url)
        else:
            result = _niutrans_v1(
                text, src_lang, tgt_lang, api_key, api_url)

        logger.info('[MT-Test.v1] OK: "%s"→"%s" (%s→%s)',
                    text[:50], result[:50], source, target)
        return api_ok({'translated': result})
    except Exception as e:
        logger.warning('[MT-Test.v1] Failed: %s', e)
        return api_error(str(e), status=200)


def _abort_translate_task(task_id: str, owner_user_id: int):
    task = _translate_runtime.get_owned(task_id, user_id=owner_user_id)
    if task is None:
        return api_not_found()
    if not abort_translation_task(
            _translate_runtime, task_id, user_id=owner_user_id):
        return api_ok(status=task['status'], note='already finished')
    return api_ok(status='aborting')


register_task_routes(
    api_v1_translate_bp,
    _translate_runtime,
    url_prefix='/api/v1/translate',
    enable_poll=False,
    abort_handler=_abort_translate_task,
    route_decorators=(require_auth,),
    tags=('translate',),
)


__all__ = ['api_v1_translate_bp']
