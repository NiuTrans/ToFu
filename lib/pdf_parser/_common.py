"""lib/pdf_parser/_common.py — Shared constants and initialization for PDF parsing."""

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['MAX_PDF_BYTES', 'HAS_PYMUPDF4LLM', 'HAS_PYMUPDF']

MAX_PDF_BYTES = 200 * 1024 * 1024  # 200 MB safety limit

# ─── PyMuPDF (core PDF engine) ───
try:
    import pymupdf
    HAS_PYMUPDF = True
    # Suppress noisy MuPDF C-library warnings — they are harmless;
    # MuPDF recovers gracefully.
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
except ImportError as e:
    pymupdf = None  # type: ignore[assignment]
    HAS_PYMUPDF = False
    logger.warning('[PDF] pymupdf not installed — PDF parsing disabled: %s', e)

# ─── pymupdf4llm (preferred for table/header-aware extraction) ───
try:
    import pymupdf4llm  # noqa: F401
    HAS_PYMUPDF4LLM = True
except ImportError as e:
    pymupdf4llm = None  # type: ignore[assignment]
    HAS_PYMUPDF4LLM = False
    logger.warning('[PDF] pymupdf4llm not installed — Markdown PDF extraction disabled: %s', e)
