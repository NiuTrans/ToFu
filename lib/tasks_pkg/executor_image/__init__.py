# HOT_PATH
"""Image generation tool handler — extracted from executor.py for modularity.

──────────────────────────────────────────────────────────────────────────
This module is a **facade-preserving package** (split from the original
~802-line ``executor_image.py``). Every public and private symbol is
re-exported here so all existing ``from lib.tasks_pkg.executor_image import X``
call sites keep working byte-identically — the import path is UNCHANGED.

Implementations live in:

  * ``._resolve``   — ``_APP_ROOT`` / ``_images_dir`` /
    ``_resolve_source_image`` / ``_extract_image_gen_history``
  * ``._thumbnail`` — ``_LLM_THUMB_MAX_PX`` / ``_LLM_THUMB_JPEG_QUALITY`` /
    ``_downsize_for_llm``
  * ``._save``      — ``_save_image_to_disk`` / ``_save_image_to_project``
  * ``._svg``       — ``_PNG_TO_SVG_MOD`` / ``_PNG_TO_SVG_LOCK`` /
    ``_load_png_to_svg`` / ``_convert_to_svg`` (the ``global``-rebound cache
    stays co-located with its loader)
  * ``._register``  — ``register_image_gen_handler`` (the public entry point)
"""

from lib.log import get_logger

logger = get_logger(__name__)

# ── Source-image resolve + history + shared base (._resolve) ──────────
from lib.tasks_pkg.executor_image._resolve import (  # noqa: E402,F401
    _APP_ROOT,
    _extract_image_gen_history,
    _images_dir,
    _resolve_source_image,
)

# ── LLM-wire thumbnail (._thumbnail) ──────────────────────────────────
from lib.tasks_pkg.executor_image._thumbnail import (  # noqa: E402,F401
    _LLM_THUMB_JPEG_QUALITY,
    _LLM_THUMB_MAX_PX,
    _downsize_for_llm,
)

# ── Save-to-disk / save-to-project (._save) ───────────────────────────
from lib.tasks_pkg.executor_image._save import (  # noqa: E402,F401
    _save_image_to_disk,
    _save_image_to_project,
)

# ── PNG→SVG convert + cached loader (._svg) ───────────────────────────
# NOTE: import the module too, so ``ei._PNG_TO_SVG_MOD`` reflects the live
# cache after ``_load_png_to_svg`` rebinds it in its defining module.
from lib.tasks_pkg.executor_image import _svg  # noqa: E402,F401
from lib.tasks_pkg.executor_image._svg import (  # noqa: E402,F401
    _PNG_TO_SVG_LOCK,
    _PNG_TO_SVG_MOD,
    _convert_to_svg,
    _load_png_to_svg,
)

# ── Public entry point (._register) ───────────────────────────────────
from lib.tasks_pkg.executor_image._register import (  # noqa: E402,F401
    register_image_gen_handler,
)

__all__ = [
    # public entry point
    'register_image_gen_handler',
    # shared base / resolve
    '_APP_ROOT',
    '_images_dir',
    '_resolve_source_image',
    '_extract_image_gen_history',
    # thumbnail
    '_LLM_THUMB_MAX_PX',
    '_LLM_THUMB_JPEG_QUALITY',
    '_downsize_for_llm',
    # save
    '_save_image_to_disk',
    '_save_image_to_project',
    # svg
    '_PNG_TO_SVG_MOD',
    '_PNG_TO_SVG_LOCK',
    '_load_png_to_svg',
    '_convert_to_svg',
]
