"""routes/translate.py — Translation endpoints (sync + async + PPTX).

This module is the **thin route layer**. Every piece of business logic
(prompt building, notranslate handling, chunking, dedup, the LLM/MT
retry engine, the async TaskRuntime, DB commit, PPTX worker) lives in
``lib.translate.*``.

The legacy private symbols (``_translate_runtime``, ``_translate_tasks``,
``_translate_tasks_lock``, ``_do_translate``, ``_format_status_message``,
``_strip_notranslate_tags``, ``_build_translate_prompt``,
``_translate_one_chunk``, ``_commit_translation_inner``, …) are re-
exported here for back-compat — 22 callers in routes.chat,
lib.message_queue, lib.tasks_pkg.manager, debug/, and the test suite
import them from ``routes.translate``. New code should import from the
``lib.translate`` package facade instead.
"""

import os
import uuid

from flask import Blueprint, jsonify, request, send_file

from lib.api_response import api_bad_request, api_error, api_internal_error, api_not_found
from lib.log import get_logger
from lib.translate import (  # noqa: F401  — back-compat re-exports
    DEFAULT_USER_ID,
    _MAX_PPTX_BYTES,
    _NOTRANSLATE_ALIAS_RE,
    _NOTRANSLATE_RE,
    _NT_PLACEHOLDER_FMT,
    _NT_PLACEHOLDER_LOOSE_RE,
    _NT_PLACEHOLDER_RE,
    _PPTX_UPLOAD_DIR,
    _SYNC_TRANSLATE_MAX_CHARS,
    _TRANSLATE_TASK_TTL,
    _build_translate_prompt,
    _cleanup_translate_tasks,
    _commit_translation_inner,
    _commit_translation_to_db,
    _dedup_inline_loop,
    _dedup_repetition_loop,
    _do_translate,
    _do_translate_pptx,
    _ensure_pptx_upload_dir,
    _extract_notranslate_blocks,
    _format_status_message,
    _get_commit_lock,
    _reattach_notranslate_blocks,
    _strip_notranslate_tags,
    _translate_one_chunk,
    _translate_runtime,
    _translate_tasks,
    _translate_tasks_lock,
    _wrap_for_translation,
)

logger = get_logger(__name__)

translate_bp = Blueprint('translate', __name__)


# ── Non-PPTX routes (sync, start, poll, poll_batch) moved to
# routes/api_v1/translate.py. mt-test moved from translate_mt_test.py
# into the same v1 module. PPTX carve-outs stay below.


# ══════════════════════════════════════════════════════
#  PPTX File Translation (formatting-preserving, carve-out)
# ══════════════════════════════════════════════════════


@translate_bp.route('/api/translate/pptx', methods=['POST'])
def translate_pptx_upload():
    """Upload and translate a PPTX file (async).

    Accepts multipart form upload with:
        file: The .pptx file
        targetLang: Target language (default: 'English')
        sourceLang: Source language (default: '' = auto-detect)

    Returns: {taskId} — poll with /api/translate/poll/<taskId>
    When done, result contains {filename, download_url, slides, segments, ...}
    """
    import lib as _lib_rt
    if not getattr(_lib_rt, 'PPTX_TRANSLATE_ENABLED', False):
        return api_error('PPTX translation is not enabled. '
                        'Enable it in Settings → Feature Modules.', status=403)
    _cleanup_translate_tasks()
    _ensure_pptx_upload_dir()

    if 'file' not in request.files:
        return api_bad_request('No file provided')
    file = request.files['file']
    if not file.filename:
        return api_bad_request('No filename')

    filename = file.filename
    if not filename.lower().endswith('.pptx'):
        return api_bad_request('Only .pptx files are supported')

    if request.content_length and request.content_length > _MAX_PPTX_BYTES:
        return api_bad_request(f'File too large (max {_MAX_PPTX_BYTES // 1048576}MB)')

    file_bytes = file.read()
    if not file_bytes:
        return api_bad_request('Empty file')
    if len(file_bytes) > _MAX_PPTX_BYTES:
        return api_error(f'File too large ({len(file_bytes) // 1048576}MB, '
                        f'max {_MAX_PPTX_BYTES // 1048576}MB)', status=400)

    target = request.form.get('targetLang', 'English')
    source = request.form.get('sourceLang', '')

    # Save uploaded file
    task_id = str(uuid.uuid4())[:12]
    safe_filename = f'input_{task_id}.pptx'
    input_path = os.path.join(_PPTX_UPLOAD_DIR, safe_filename)
    try:
        with open(input_path, 'wb') as f:
            f.write(file_bytes)
    except Exception as e:
        logger.error('[PPTX-Translate] Failed to save upload: %s', e, exc_info=True)
        return api_internal_error(f'Failed to save file: {e}')

    task = _translate_runtime.create(
        task_id=task_id,
        meta={'type': 'pptx', 'filename': filename, 'targetLang': target,
              'fileSize': len(file_bytes)},
    )
    task.update({
        'status': 'running', 'type': 'pptx',
        'result': None, 'error': None, 'model': None, 'progress': None,
        'filename': filename, 'targetLang': target,
        'fileSize': len(file_bytes),
        'completed_at': None,
    })

    _translate_runtime.spawn(
        task_id, _do_translate_pptx,
        task_id, input_path, filename, target, source,
    )

    logger.info('[PPTX-Translate] Started task %s: %s (%d KB) → %s',
                task_id, filename, len(file_bytes) // 1024, target)
    return jsonify({'taskId': task_id})


@translate_bp.route('/api/translate/pptx/download/<filename>')
def translate_pptx_download(filename):
    """Download a translated PPTX file."""
    safe = os.path.basename(filename)
    filepath = os.path.join(_PPTX_UPLOAD_DIR, safe)
    if not os.path.isfile(filepath):
        return api_not_found('File not found')
    return send_file(
        filepath,
        mimetype='application/vnd.openxmlformats-officedocument.presentationml.presentation',
        as_attachment=True,
        download_name=safe,
    )
