# HOT_PATH
"""PNG→SVG conversion + the cached ``png_to_svg`` module loader.

The module-level ``_PNG_TO_SVG_MOD`` cache and its ``_PNG_TO_SVG_LOCK`` MUST
stay in the same submodule as ``_load_png_to_svg`` — the loader rebinds the
cache via ``global _PNG_TO_SVG_MOD``, so a re-export cannot substitute for
co-location (a ``global`` only rebinds the name in its defining module).
"""

from __future__ import annotations

import os
import threading

from lib.log import get_logger
from lib.tasks_pkg.executor_image._resolve import _APP_ROOT, _images_dir

logger = get_logger(__name__)

# ── png_to_svg module cache ──
# scripts/png_to_svg.py is loaded via importlib (not a regular package import)
# because it lives outside lib/. We cache the module after first load so we
# don't re-exec_module() on every generate_image call. Beyond performance, the
# previous per-call exec_module triggered SIGBUS / "Bus error (core dumped)"
# crashes when the project's FUSE-mounted filesystem hiccupped during the
# C-extension load chain (xml.etree.ElementTree → _elementtree). See
# faulthandler.log dump anchored at scripts/png_to_svg.py:24 → executor_image:_convert_to_svg.
_PNG_TO_SVG_MOD = None
_PNG_TO_SVG_LOCK = threading.Lock()


def _load_png_to_svg():
    """Load scripts/png_to_svg.py once and cache the module.

    Returns the cached module on subsequent calls. Raises ImportError if
    the script cannot be loaded (caller should treat SVG conversion as
    unavailable for this run).
    """
    global _PNG_TO_SVG_MOD
    if _PNG_TO_SVG_MOD is not None:
        return _PNG_TO_SVG_MOD
    with _PNG_TO_SVG_LOCK:
        if _PNG_TO_SVG_MOD is not None:
            return _PNG_TO_SVG_MOD
        import importlib.util
        _svg_script = os.path.join(_APP_ROOT, 'scripts', 'png_to_svg.py')
        spec = importlib.util.spec_from_file_location('png_to_svg', _svg_script)
        if spec is None or spec.loader is None:
            raise ImportError(f'Cannot create spec for {_svg_script}')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _PNG_TO_SVG_MOD = mod
        logger.info('[Tool:generate_image] png_to_svg loaded once and cached')
        return _PNG_TO_SVG_MOD


def _convert_to_svg(saved_url: str, project_save_path: str,
                    project_path: str | None,
                    conv_id: str | None = None,
                    task_id: str | None = None) -> tuple:
    """Convert a saved PNG to SVG using vtracer (background removal + tracing).

    Converts the PNG file that was already saved to disk/project into an SVG
    placed alongside it (same directory, same basename, .svg extension).

    Args:
        saved_url: Local URL path like ``/api/images/gen_xxx.png`` (uploads folder).
        project_save_path: Relative path inside the project (e.g. ``static/logo.png``).
        project_path: Absolute path to the active project root.
        conv_id: Conversation ID for modification tracking.
        task_id: Task ID for modification tracking.

    Returns:
        Tuple of ``(svg_saved_url, svg_project_path)`` — empty strings on failure.
    """
    try:
        convert_png_to_svg = _load_png_to_svg().convert_png_to_svg
    except Exception as e:
        logger.warning('[Tool:generate_image] SVG conversion unavailable: %s', e)
        return '', ''

    svg_saved_url = ''
    svg_project_path = ''

    # ── Convert the uploads copy ──
    if saved_url:
        filename = os.path.basename(saved_url)
        _img_dir = _images_dir()
        png_path = os.path.join(_img_dir, filename)
        svg_filename = os.path.splitext(filename)[0] + '.svg'
        svg_path = os.path.join(_img_dir, svg_filename)
        try:
            ok = convert_png_to_svg(png_path, svg_path)
            if ok:
                svg_saved_url = f'/api/images/{svg_filename}'
                logger.info('[Tool:generate_image] SVG saved to uploads: %s', svg_filename)
            else:
                logger.warning('[Tool:generate_image] SVG conversion failed for uploads copy')
        except Exception as e:
            logger.error('[Tool:generate_image] SVG conversion error (uploads): %s', e, exc_info=True)

    # ── Convert the project copy ──
    if project_save_path and project_path:
        png_abs = os.path.join(project_path, project_save_path)
        svg_rel = os.path.splitext(project_save_path)[0] + '.svg'
        svg_abs = os.path.join(project_path, svg_rel)
        try:
            ok = convert_png_to_svg(png_abs, svg_abs)
            if ok:
                svg_project_path = svg_rel
                logger.info('[Tool:generate_image] SVG saved to project: %s', svg_rel)

                # Record modification for undo support
                try:
                    from lib.project_mod.modifications import _record_modification
                    from lib.project_mod.write_tools import _touch_for_vscode
                    _record_modification(
                        project_path, 'write_file', svg_rel,
                        original_content=None,
                        conv_id=conv_id, task_id=task_id,
                    )
                    _touch_for_vscode(svg_abs)
                except Exception as e:
                    logger.debug('[Tool:generate_image] SVG mod tracking failed: %s', e)
            else:
                logger.warning('[Tool:generate_image] SVG conversion failed for project copy')
        except Exception as e:
            logger.error('[Tool:generate_image] SVG conversion error (project): %s', e, exc_info=True)

    return svg_saved_url, svg_project_path
