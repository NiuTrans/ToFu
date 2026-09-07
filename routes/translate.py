"""routes/translate.py — Translation endpoints (sync + async + PPTX).

This module is the thin PPTX route layer. Every piece of business logic
(prompt building, notranslate handling, chunking, dedup, the LLM/MT
retry engine, the async TaskRuntime, DB commit, PPTX worker) lives in
``lib.translate.*``.

Text translation lives at ``routes/api_v1/translate.py``; this module owns
only formatting-preserving PPTX upload and download.
"""

import os
import uuid

from quart import Blueprint, request

from lib.quart_sync import request_files, request_form, send_file

from lib.api_response import (
    api_bad_request, api_error, api_internal_error, api_not_found, api_ok,
)
from lib.log import get_logger
from lib.translate.pptx import (
    _MAX_PPTX_BYTES, _PPTX_UPLOAD_DIR, _ensure_pptx_upload_dir,
)
from lib.translate.execution import submit_translation_task
from lib.translate.runtime._state import (
    _cleanup_translate_tasks, _translate_runtime,
)
from routes.api_v1.auth import request_user_id, require_scope

logger = get_logger(__name__)

translate_bp = Blueprint('translate', __name__)


def _do_translate_pptx(*args, **kwargs):
    """Load the formatting-preserving PPTX worker inside its task thread."""
    from lib.translate.pptx import _do_translate_pptx as implementation

    return implementation(*args, **kwargs)


# ── Non-PPTX routes (sync, start, poll, poll_batch) moved to
# routes/api_v1/translate.py. mt-test moved from translate_mt_test.py
# into the same v1 module. PPTX carve-outs stay below.


# ══════════════════════════════════════════════════════
#  PPTX File Translation (formatting-preserving, carve-out)
# ══════════════════════════════════════════════════════


@translate_bp.route('/api/translate/pptx', methods=['POST'])
@require_scope('agents:translate')
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

    files = request_files()
    if 'file' not in files:
        return api_bad_request('No file provided')
    file = files['file']
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

    form = request_form()
    target = form.get('targetLang', 'English')
    source = form.get('sourceLang', '')

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

    owner_user_id = int(request_user_id())
    _translate_runtime.create(
        user_id=owner_user_id,
        task_id=task_id,
        meta={'type': 'pptx', 'filename': filename, 'targetLang': target,
              'fileSize': len(file_bytes)},
    )
    accepted = submit_translation_task(
        _translate_runtime,
        task_id, _do_translate_pptx,
        task_id, input_path, filename, target, source,
        running_fields={'model': None, 'progress': None},
    )
    if not accepted:
        try:
            os.remove(input_path)
        except OSError as cleanup_error:
            logger.debug(
                '[PPTX-Translate] rejected input cleanup failed: %s',
                cleanup_error,
            )
        return api_error(
            'Translation worker capacity is unavailable; retry shortly',
            status=503,
        )

    logger.info('[PPTX-Translate] Started task %s: %s (%d KB) → %s',
                task_id, filename, len(file_bytes) // 1024, target)
    return api_ok({'taskId': task_id})


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
