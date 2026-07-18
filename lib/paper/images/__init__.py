"""Figure extraction + report-time image injection + title backfill.

Facade package: preserves the historical ``lib.paper.images`` import path.
Every public/private symbol is re-exported here so both
``from lib.paper.images import X`` and lib/paper/__init__.py's multi-symbol
``from .images import (...)`` block resolve byte-identically.

Submodules:
  * ``_extract`` — figure extraction + on-disk manifest management
  * ``_inject``  — report-time deterministic image injection
  * ``_title``   — title lookup / self-healing backfill / heading repair
"""

from lib.log import get_logger

from ._extract import (
    _FIG_EXTRACT_VERSION,
    _build_image_manifest,
    _ensure_paper_images,
    _extract_paper_figures,
    _load_image_manifest,
)
from ._inject import _inject_images_into_report
from ._title import (
    _backfill_library_title,
    _ensure_title_heading,
    _extract_title_from_report,
    _is_placeholder_title,
    _lookup_paper_title,
)

logger = get_logger(__name__)

__all__ = [
    # extract
    '_FIG_EXTRACT_VERSION',
    '_load_image_manifest',
    '_extract_paper_figures',
    '_ensure_paper_images',
    '_build_image_manifest',
    # inject
    '_inject_images_into_report',
    # title
    '_lookup_paper_title',
    '_extract_title_from_report',
    '_backfill_library_title',
    '_is_placeholder_title',
    '_ensure_title_heading',
]
