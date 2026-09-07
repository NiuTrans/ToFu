"""lib/pdf_parser/images/ — Image extraction and figure/table detection from PDF.

Façade package — split from the former single-module ``images.py`` into
cohesive submodules while preserving the public import path::

    from lib.pdf_parser.images import (
        detect_and_clip_figures, resize_image_bytes, render_pdf_pages,
    )
"""

from lib.log import get_logger

from lib.pdf_parser.images._resize import (
    _auto_crop_whitespace,
    resize_image_bytes,
)
from lib.pdf_parser.images._detect import (
    _FIGURE_CAP_RE,
    _TABLE_CAP_RE,
    _SECTION_HEAD_RE,
    _merge_nearby_rects,
    _parse_page_blocks,
    _is_body_text,
    detect_and_clip_figures,
    _try_stitch_next_page_table,
)
from lib.pdf_parser.images._render import PdfPageLimitExceeded, render_pdf_pages

logger = get_logger(__name__)

__all__ = [
    'PdfPageLimitExceeded',
    'detect_and_clip_figures',
    'resize_image_bytes',
    'render_pdf_pages',
]
