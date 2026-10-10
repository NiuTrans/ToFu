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

from lib.api_v4 import raw_success_response
from lib.openapi import media_responses, api_meta
from lib.api_response import api_bad_request, api_not_found, api_ok
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
@api_meta(
    responses=media_responses('application/vnd.openxmlformats-officedocument.presentationml.presentation', conditional=True),
)
@raw_success_response
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
@api_meta(
    responses=media_responses('image/png', conditional=True),
)
@raw_success_response
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


@api_v1_slides_bp.route('/api/v1/slides/<task_id>/pages/<int:n>.html',
                        methods=['GET'])
@require_auth
def serve_page_html(task_id, n):
    """Serve one page's self-contained HTML (the editor canvas's live view).

    Rendered on demand from the deck's CURRENT PPTD (no browser boot) — an
    edit reloads this URL and the canvas reflects the patch immediately.
    """
    if n < 1 or n > 99:
        return api_not_found('not_found')
    workdir = _task_workdir(task_id, user_id=int(request_user_id()))
    if not workdir:
        return api_not_found('not_found')
    from lib.slides.pptd import parse_deck
    try:
        deck = parse_deck(os.path.join(workdir, 'deck', 'deck.pptd'))
    except Exception as e:
        logger.debug('[Slides.v1] deck parse failed for %s: %s', task_id, e)
        return api_not_found('deck_not_ready')
    if n > len(deck.pages):
        return api_not_found('not_found')
    from lib.slides.render_html import render_page_html
    return (render_page_html(deck, deck.pages[n - 1], page_index=n - 1),
            200, {'Content-Type': 'text/html; charset=utf-8',
                  'Cache-Control': 'no-store'})


@api_v1_slides_bp.route('/api/v1/slides/<task_id>/pages/<int:n>/elements',
                        methods=['GET'])
@require_auth
def list_page_elements(task_id, n):
    """Element inventory for the editor's property bar (ids, types, bounds,
    text excerpt, current style fields)."""
    if n < 1 or n > 99:
        return api_not_found('not_found')
    workdir = _task_workdir(task_id, user_id=int(request_user_id()))
    if not workdir:
        return api_not_found('not_found')
    from lib.slides.pptd import parse_deck
    try:
        deck = parse_deck(os.path.join(workdir, 'deck', 'deck.pptd'))
    except Exception:
        return api_not_found('deck_not_ready')
    if n > len(deck.pages):
        return api_not_found('not_found')
    page = deck.pages[n - 1]
    out = []
    for el in page.elements:
        if not isinstance(el, dict):
            continue
        content = el.get('content') if isinstance(el.get('content'), dict) else {}
        text = str(content.get('text') or '')
        import re as _re
        text = _re.sub(r'<[^>]+>', '', text).strip()[:120]
        out.append({
            'elementId': str(el.get('elementId') or ''),
            'elementType': str(el.get('elementType') or ''),
            'bounds': el.get('bounds'),
            'text': text,
            'style': {k: content.get(k) for k in
                      ('fontSize', 'color', 'bold', 'italic', 'fontFamily',
                       'align') if content.get(k) is not None},
            'src': str(el.get('src') or '')[:200] or None,
        })
    return api_ok({'task_id': task_id, 'page': n,
                   'width': deck.width, 'height': deck.height,
                   'elements': out})


@api_v1_slides_bp.route('/api/v1/slides/<task_id>/pages/<int:n>/patch',
                        methods=['POST'])
@require_auth
async def patch_page_route(task_id, n):
    """Apply deterministic patch ops to one page (the editor's save path —
    the SAME engine as the patch_slides tool, no LLM involved)."""
    if n < 1 or n > 99:
        return api_not_found('not_found')
    from lib.request_parser import async_parse_body
    body = await async_parse_body(exact=('ops',))
    ops = body.get('ops')
    if not isinstance(ops, list) or not ops:
        return api_bad_request('ops must be a non-empty array', field='ops')
    workdir = _task_workdir(task_id, user_id=int(request_user_id()))
    if not workdir:
        return api_not_found('not_found')
    deck_dir = os.path.join(workdir, 'deck')
    from lib.slides.patch import apply_page_patch
    from lib.slides.versions import snapshot_deck
    snapshot_deck(deck_dir, op='patch', summary=f'editor page {n}')
    result = apply_page_patch(deck_dir, n - 1, ops[:16])
    if not result.get('ok'):
        return api_ok(result)
    result['preview_url'] = f'/api/v1/slides/{task_id}/pages/{n}.png'
    result['download'] = f'/api/v1/slides/{task_id}/file'
    return api_ok(result)


@api_v1_slides_bp.route('/api/v1/slides/import-template', methods=['POST'])
@require_auth
def import_template_route():
    """Upload a brand .pptx as a reusable visual template (multipart).

    The importer reverse-engineers the deck's palette/fonts/geometry into
    PPTD theme tokens; fonts outside the licensed registry are substituted
    and reported. Returns {template_id, ...} for produce_slides.
    SYNC: bounded file import (same carve-out as serve_deck_file).
    """
    from lib.quart_sync import request_files, request_form
    files = request_files()
    if 'file' not in files:
        return api_bad_request('No file provided', field='file')
    upload = files['file']
    filename = str(upload.filename or '')
    if not filename.lower().endswith('.pptx'):
        return api_bad_request('Only .pptx files are supported',
                               field='file')
    from lib.slides.templates import MAX_TEMPLATE_BYTES, import_template
    payload = upload.read()
    if not payload:
        return api_bad_request('Empty file', field='file')
    if len(payload) > MAX_TEMPLATE_BYTES:
        return api_bad_request(
            f'File too large (max {MAX_TEMPLATE_BYTES // 1048576}MB)',
            field='file')
    import tempfile
    owner_user_id = int(request_user_id())
    try:
        with tempfile.NamedTemporaryFile(
                suffix='.pptx', delete=False) as tmp:
            tmp.write(payload)
            tmp_path = tmp.name
        try:
            result = import_template(
                tmp_path,
                title=str(request_form().get('title')
                          or filename.rsplit('.', 1)[0])[:160],
                owner_user_id=owner_user_id)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except ValueError as e:
        return api_bad_request(str(e))
    logger.info('[Slides.v1] template %s imported by user %d',
                result['template_id'], owner_user_id)
    return api_ok(result)


__all__ = ['api_v1_slides_bp']
