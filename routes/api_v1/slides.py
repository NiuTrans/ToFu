"""routes/api_v1/slides.py — Slide-deck delivery API.

Poll/stream/abort/start are all GENERIC (``/api/v1/tasks/*`` — the runtime
registers in ``_registries()`` and the starter in ``_starters()`` there).
This blueprint carries only what is genuinely slides-specific: serving the
finished FILES.

Routes:
  GET /api/v1/slides/<task_id>/file                — the PPTX (Range)
  GET /api/v1/slides/<task_id>/pages/<n>.png       — page preview (Range)
"""

from __future__ import annotations

import os
import re

from quart import Blueprint

from lib.api_response import api_not_found
from lib.log import get_logger

from .auth import request_user_id, require_auth

logger = get_logger(__name__)

api_v1_slides_bp = Blueprint('api_v1_slides', __name__)

_PPTX_MIME = ('application/vnd.openxmlformats-officedocument.'
              'presentationml.presentation')


def _task_workdir(task_id: str, *, user_id: int) -> str:
    """Live task → workdir; finished/restarted → disk manifest anchor.

    Path safety: the served path is assembled from the recorded job workdir
    and a validated integer page number — never client path material.
    """
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', task_id or ''):
        return ''
    from lib.slides.runtime import _slides_runtime
    task = _slides_runtime.get_owned(task_id, user_id=user_id)
    if task:
        result = task.get('result') or {}
        return result.get('workdir') or task.get('workdir') or ''
    from lib.production.jobs import read_manifest
    from lib.slides.engine import slides_root
    workdir = os.path.join(slides_root(), 'jobs', task_id)
    manifest = read_manifest(workdir)
    if (not manifest
            or int(manifest.get('user_id') or 0) != int(user_id)):
        return ''
    return workdir


@api_v1_slides_bp.route('/api/v1/slides/<task_id>/file', methods=['GET'])
@require_auth
def serve_deck_file(task_id):
    """Serve the finished PPTX. SYNC: pure file serving through the
    sync-safe shim (same carve-out as serve_motion_file)."""
    owner_user_id = int(request_user_id())
    workdir = _task_workdir(task_id, user_id=owner_user_id)
    if not workdir:
        return api_not_found('not_found')
    path = ''
    result = {}
    from lib.slides.runtime import _slides_runtime
    task = _slides_runtime.get_owned(task_id, user_id=owner_user_id)
    if task:
        result = task.get('result') or {}
        path = result.get('pptx_path') or ''
    if not path:
        # Disk fallback: the deck dir holds exactly one .pptx.
        deck_dir = os.path.join(workdir, 'deck')
        try:
            found = [f for f in os.listdir(deck_dir) if f.endswith('.pptx')]
        except OSError as e:
            logger.debug('[Slides.v1] deck dir listing failed for %s: %s',
                         deck_dir, e)
            found = []
        if found:
            path = os.path.join(deck_dir, found[0])
    if not path or not os.path.isfile(path):
        return api_not_found('file_not_ready')
    from lib.file_serving import send_file_conditional
    return send_file_conditional(path, mimetype=_PPTX_MIME)


@api_v1_slides_bp.route('/api/v1/slides/<task_id>/pages/<int:n>.png',
                        methods=['GET'])
@require_auth
def serve_page_preview(task_id, n):
    """Serve one page's preview PNG (1-based)."""
    if n < 1 or n > 99:
        return api_not_found('not_found')
    workdir = _task_workdir(task_id, user_id=int(request_user_id()))
    if not workdir:
        return api_not_found('not_found')
    path = os.path.join(workdir, 'deck', 'preview', 'pages', f'{n:02d}.png')
    if not os.path.isfile(path):
        return api_not_found('file_not_ready')
    from lib.file_serving import send_file_conditional
    return send_file_conditional(path, mimetype='image/png')


__all__ = ['api_v1_slides_bp']
