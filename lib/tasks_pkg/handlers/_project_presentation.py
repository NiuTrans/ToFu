# HOT_PATH
"""Project image projection and optional write-artifact presentation.

Entry points: prepare_project_image_result, maybe_promote_write_to_artifact.
Image projection is pure presentation; artifact promotion uses existing guarded
project reads and the artifact service. Neither function settles a tool round
or admits file execution. The project handler owns their order and settlement.
"""
from __future__ import annotations

import os

from lib.log import get_logger

logger = get_logger(__name__)


def _screenshot_to_descriptor(img, filename=''):
    """Turn a ``__screenshot__`` dict into a frontend-render descriptor.

    Returns a dict ``{uri, format, filename}`` where ``uri`` is the full
    ``data:<mime>;base64,...`` string the browser can drop straight into an
    ``<img src>``. ``uri`` is empty when the dict lacks a usable data URL.
    """
    if not isinstance(img, dict):
        return {'uri': '', 'format': '', 'filename': filename}
    return {
        'uri': img.get('dataUrl', '') or '',
        'format': img.get('format', 'png'),
        'filename': filename,
    }



def prepare_project_image_result(fn_name, fn_args, tool_content, *,
                                 svg_renders, response_assets):
    """Return image content/metadata, or None for ordinary text results."""
    # read_files with absolute image paths returns a batch dict with __batch_images__
    _img_descriptors = None  # frontend-render image list (all images in a batch)
    _video = None  # inspect_video facts for the round header
    is_batch_image = isinstance(tool_content, dict) and tool_content.get('__batch_images__')
    if is_batch_image:
        # Extract the first image for VLM upload, keep text content
        _images = tool_content['__batch_images__']
        _text = tool_content.get('_text_content', '')
        # A batch may carry its own text for a text-only model (inspect_video
        # keeps probe facts and transcript instead of a bare placeholder).
        _no_vision_text = tool_content.get('_no_vision_fallback', '')
        _video = tool_content.get('videoInspect')
        # Capture every image's data URI for inline rendering. Each image dict
        # already carries its own filename (set in read_tools).
        _img_list = [img for img in _images.values()
                     if isinstance(img, dict) and img.get('__screenshot__')]
        _img_descriptors = [_screenshot_to_descriptor(img, img.get('filename', ''))
                            for img in _img_list]
        # Use the first image as the primary screenshot result, but attach the
        # full list so EVERY image rides the wire to the VLM (one image_url
        # block each — see _append_screenshot_message).
        first_img = _img_list[0] if _img_list else next(iter(_images.values()))
        tool_content = first_img
        if len(_img_list) > 1:
            tool_content['images'] = _img_list
        # Store the text content as fallback
        if _text:
            tool_content['_text_fallback'] = _text
        if _no_vision_text:
            tool_content['_no_vision_fallback'] = _no_vision_text

    # read_files may return a __screenshot__ dict for images (single absolute image path)
    is_image_result = isinstance(tool_content, dict) and tool_content.get('__screenshot__')
    if is_image_result:
        file_path = fn_args.get('path', '?')
        filename = os.path.basename(file_path)
        fmt = tool_content.get('format', 'png')
        orig_size = tool_content.get('originalSize', 0)
        comp_size = tool_content.get('compressedSize', 0)
        size_info = f'{comp_size:,} bytes'
        if tool_content.get('compressionApplied') and orig_size:
            size_info = f'{orig_size:,} → {comp_size:,} bytes (compressed)'
        if _img_descriptors is None:
            _img_descriptors = [_screenshot_to_descriptor(tool_content, filename)]
        meta = {
            'toolName': fn_name, 'title': filename,
            'snippet': f'{filename} ({fmt}, {size_info})',
            'source': 'Project', 'fetched': True,
            'fetchedChars': comp_size, 'url': '',
            'badge': fmt,
            # Inline-render payload — frontend (tool_rounds.js) draws an
            # <img> per descriptor. Each carries a full data: URL.
            'imageDataUris': ([d for d in _img_descriptors if d.get('uri')]
                              + [s for s in svg_renders if s.get('uri')]),
        }
        if response_assets:
            meta['responseAssets'] = response_assets
            tool_content['responseAssets'] = response_assets
        # ── inspect_image: surface the transform + source/view dimensions ──
        if fn_name == 'inspect_image':
            _ops = tool_content.get('inspectOps', '') or ''
            _view = tool_content.get('viewSize') or []
            _src = tool_content.get('sourceSize') or []
            meta['inspectOps'] = _ops
            if len(_view) == 2 and len(_src) == 2:
                meta['snippet'] = (f'{filename}: {_src[0]}×{_src[1]} → '
                                   f'{_view[0]}×{_view[1]}px ({_ops})')
            if _ops:
                meta['badge'] = _ops
        # ── inspect_video: name the clip, its geometry and frame count ──
        if fn_name == 'inspect_video' and isinstance(_video, dict):
            _facts = [str(_video.get('name') or filename),
                      f'{float(_video.get("durationS") or 0):.2f}s']
            if _video.get('width') and _video.get('height'):
                _facts.append(f'{_video["width"]}x{_video["height"]}')
            _window = _video.get('window') or []
            if (len(_window) == 2 and (_window[0] > 0
                                       or _window[1] < float(_video.get('durationS') or 0))):
                _facts.append(f'window {_window[0]:.2f}–{_window[1]:.2f}s')
            _facts.append(f'{_video.get("frames", len(_img_descriptors))} frames')
            _facts.append(f'transcript {_video.get("transcript") or "unavailable"}')
            meta['title'] = _facts[0]
            meta['snippet'] = ' · '.join(_facts)
            meta['badge'] = f'{_video.get("frames", len(_img_descriptors))} frames'
        return tool_content, meta

    return None


def maybe_promote_write_to_artifact(task, fn_name, fn_args, project_path, meta):
    """Detect renderable writes (md/html/svg) and persist + emit artifact event.

    Mutates ``meta`` in place to attach ``artifactId`` so the chat-side
    tool round can render an "Open in panel" chip.

    This is best-effort: any failure (DB unavailable, read race, oversize)
    logs and degrades to "no artifact, just the normal tool round".
    """
    if fn_name not in ('write_file', 'edit_file', 'apply_diff', 'apply_diffs', 'insert_content', 'insert_contents'):
        return
    rel_path = (fn_args.get('path') or '').strip()
    if not rel_path:
        return

    # Feature-flag gate — read live from lib at call time so a hot
    # config reload disables / re-enables without a restart.
    try:
        import lib as _lib_mod
        if not getattr(_lib_mod, 'ARTIFACTS_ENABLED', True):
            return
    except Exception as e:
        logger.debug('[Artifacts] feature flag check failed (non-fatal): %s', e)

    try:
        from lib.artifacts import (
            ArtifactSizeError,
            create_artifact,
            detect_format,
            emit_artifact_event,
            is_renderable_path,
        )
    except Exception as e:  # pragma: no cover — defensive import guard
        logger.debug('[Artifacts] subsystem unavailable: %s', e)
        return

    if not is_renderable_path(rel_path):
        return

    fmt = detect_format(rel_path)
    if fmt is None:
        return

    # Routine project doc edits (README.md, CHANGELOG.md, etc.) shouldn't
    # spawn artifact chips — only HTML/SVG outputs are "report-shaped"
    # enough to deserve the side panel.  Inline ```markdown fences are
    # still promoted by Producer B (lib/artifacts/scanner.py).
    if fmt == 'markdown':
        return

    # Resolve the on-disk target so we capture the canonical bytes (post
    # write).  ``apply_diff`` / ``insert_content`` lacks a `content` arg —
    # we always read from disk.
    try:
        from lib.project_mod.write_tools import _resolve_write_path
        target = _resolve_write_path(project_path, rel_path)
    except Exception as e:
        # _resolve_write_path raises ValueError when an absolute path
        # falls outside any registered root.  Either way, we can't safely
        # promote it.
        logger.debug('[Artifacts] cannot resolve write path %s: %s', rel_path, e)
        return

    try:
        from lib.artifacts.response_files import read_project_file
        from lib.project_mod.tool_admission import Cancellation
        # The write-path authority already validated this exact target. Reuse
        # the no-follow snapshot reader; its parent is a derived verified root.
        raw = read_project_file(target, [os.path.dirname(target)],
                                cancelled=Cancellation(task).is_set)
        content = raw.decode('utf-8', errors='replace')
        size = len(raw)
    except (OSError, ValueError, RuntimeError) as error:
        logger.warning('[Artifacts] read-back failed: %s', error)
        return

    conv_id = task.get('convId') or ''
    if not conv_id:
        logger.debug('[Artifacts] task without convId, skipping')
        return

    msg_id = task.get('_assistantMsgId') or task.get('msgId') or ''
    title = os.path.basename(rel_path) or rel_path
    src_ref = {'path': rel_path, 'tool': fn_name}

    try:
        artifact_meta = create_artifact(
            conv_id=conv_id,
            content=content,
            format=fmt,
            source='write_file',
            source_ref=src_ref,
            task_id=task.get('id', ''),
            msg_id=msg_id,
            title=title,
        )
    except ArtifactSizeError as e:
        logger.info('[Artifacts] write_file size cap rejection: %s', e)
        return
    except Exception as e:
        logger.warning('[Artifacts] create_artifact failed for %s: %s',
                       rel_path, e, exc_info=True)
        return

    try:
        emit_artifact_event(task, artifact_meta)
    except Exception as e:
        logger.warning('[Artifacts] emit_artifact_event failed for id=%s: %s',
                       artifact_meta.get('id', '?')[:8], e, exc_info=True)

    # Annotate the chat-side meta so the tool round can render the chip.
    meta['artifactId']     = artifact_meta['id']
    meta['artifactFormat'] = artifact_meta['format']
    meta['artifactTitle']  = artifact_meta['title']
    meta['artifactSize']   = artifact_meta['size_bytes']
    logger.debug(
        '[Artifacts] promoted write_file path=%s id=%s fmt=%s size=%d',
        rel_path, artifact_meta['id'][:8], artifact_meta['format'], size,
    )
