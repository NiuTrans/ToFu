# HOT_PATH
"""Persist generated images to the uploads folder and to the active project.

Depends on the base ``_resolve`` submodule for the ``_images_dir()`` authority.
"""

from __future__ import annotations

import os

from lib.log import get_logger
from lib.tasks_pkg.executor_image._resolve import _images_dir

logger = get_logger(__name__)


def _save_image_to_disk(image_b64, mime_type='image/png'):
    """Save base64 image to uploads/images/ and return the local URL path."""
    import base64 as _b64
    import time as _time

    ext_map = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/webp': '.webp'}
    ext = ext_map.get(mime_type, '.png')
    filename = f'gen_{int(_time.time() * 1000)}{ext}'

    upload_dir = _images_dir()
    os.makedirs(upload_dir, exist_ok=True)
    filepath = os.path.join(upload_dir, filename)

    try:
        raw_bytes = _b64.b64decode(image_b64)
        with open(filepath, 'wb') as f:
            f.write(raw_bytes)
        logger.info('[Tool:generate_image] Saved image to %s (%d KB)',
                    filename, len(raw_bytes) // 1024)
        return f'/api/images/{filename}'
    except Exception as e:
        logger.warning('[Tool:generate_image] Failed to save image to disk: %s', e)
        return ''


def _save_image_to_project(image_b64, mime_type, output_path, project_path,
                           conv_id=None, task_id=None):
    """Save base64 image to a path inside the active project directory.

    ``output_path`` may be a project-relative path OR a multi-root namespaced
    path like ``rootname:rel/path.png``.  Without prefix resolution, the
    literal colon is treated as part of the filename and silently creates
    a top-level directory whose NAME is ``rootname:rel`` under the primary
    root — see the ``tofu:static/posters/`` artifact from 2026-05-05.

    Returns:
        Tuple ``(display_path, eff_base, eff_rel)`` on success, ``('', '', '')``
        on failure. ``display_path`` is what the LLM sees (preserves the
        ``rootname:`` prefix if used). ``eff_base`` / ``eff_rel`` are the
        resolved root + path-under-root, needed by callers that perform
        further work (e.g. SVG conversion) on the same file.
    """
    import base64 as _b64

    from lib.project_mod.modifications import _record_modification
    from lib.project_mod.scanner import _safe_path
    from lib.project_mod.tools import _resolve_base, _touch_for_vscode

    try:
        eff_base, eff_rel = _resolve_base(project_path, output_path, conv_id=conv_id)
    except ValueError as e:
        logger.warning('[Tool:generate_image] Project save path namespace rejected %s: %s',
                       output_path, e)
        return '', '', ''
    try:
        target = _safe_path(eff_base, eff_rel)
    except ValueError as e:
        logger.warning('[Tool:generate_image] Project save path rejected %s: %s',
                       output_path, e)
        return '', '', ''

    parent = os.path.dirname(target)
    if parent and not os.path.isdir(parent):
        try:
            os.makedirs(parent, exist_ok=True)
        except Exception as e:
            logger.warning('[Tool:generate_image] makedirs failed for %s: %s',
                           parent, e, exc_info=True)
            return '', '', ''

    existed = os.path.isfile(target)
    original_content = None
    if existed:
        try:
            with open(target, 'rb') as f:
                original_content = f.read()
        except Exception as e:
            logger.debug('[Tool:generate_image] Could not read original %s: %s',
                         output_path, e)

    try:
        raw_bytes = _b64.b64decode(image_b64)
        with open(target, 'wb') as f:
            f.write(raw_bytes)
            f.flush()
            os.fsync(f.fileno())
        _touch_for_vscode(target)

        logger.info('[Tool:generate_image] Saved image to project path %s (%d KB)',
                    output_path, len(raw_bytes) // 1024)

        _record_modification(
            eff_base, 'write_file', eff_rel,
            original_content=original_content if existed else None,
            conv_id=conv_id, task_id=task_id,
        )

        return output_path, eff_base, eff_rel
    except Exception as e:
        logger.error('[Tool:generate_image] Failed to save image to project path %s: %s',
                     output_path, e, exc_info=True)
        return '', '', ''
