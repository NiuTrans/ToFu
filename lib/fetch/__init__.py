"""lib/fetch/ — Content fetching, extraction, and caching package.

Sub-modules:
  utils           — HTTP session, user-agent rotation, proxy config
  http            — Raw HTTP fetching with retry logic
  html_extract    — HTML content extraction & readability fallback
  pdf_extract     — PDF text extraction (pymupdf / pdfplumber)
  playwright_pool — Lazy-loaded singleton Playwright browser pool
  core            — Main fetch_page_content entry point and URL routing
  content_filter  — LLM-based web content noise removal (ads, nav, sidebars)
"""

import logging

from lib._pkg_utils import build_facade

_logger = logging.getLogger(__name__)

__all__: list[str] = []

# ── Core modules (required) ─────────────────────────────────────────
from . import core, html_extract, http, pdf_extract, utils  # noqa: E402
from .core import *  # noqa: F401,F403
from .html_extract import *  # noqa: F401,F403
from .http import *  # noqa: F401,F403
from .pdf_extract import *  # noqa: F401,F403
from .utils import *  # noqa: F401,F403

build_facade(__all__, utils, http, html_extract, pdf_extract, core)

# ── Playwright browser pool ──────────────────────────────────────────
from . import playwright_pool  # noqa: E402
from .playwright_pool import *  # noqa: F401,F403

build_facade(__all__, playwright_pool)
