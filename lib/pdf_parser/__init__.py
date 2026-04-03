"""lib/pdf_parser/ — Unified PDF parsing: text, images, math, VLM.

Façade package — all public API is re-exported here::

    from lib.pdf_parser import parse_pdf, extract_pdf_text
    from lib.pdf_parser import vlm_parse_pdf, start_vlm_task, get_vlm_task
    from lib.pdf_parser import render_pdf_pages
"""

from lib._pkg_utils import build_facade, safe_import

__all__: list[str] = []
_import = safe_import(__name__, globals(), __all__)

# ── Core (must load) ──
from . import core, text
from .core import *  # noqa: F401,F403
from .text import *  # noqa: F401,F403

build_facade(__all__, text, core)

# ── Optional sub-modules (degrade gracefully) ──
_import('images', 'image extraction')
_import('vlm', 'VLM-based parsing')
_import('math', 'math formula detection')
_import('postprocess', 'text postprocessing')
