"""lib/doc_parser — Document text extraction for non-PDF formats.

Supported formats:
  - .docx  (Word 2007+, via python-docx)
  - .pptx  (PowerPoint 2007+, via python-pptx — optional)
  - .xlsx  (Excel 2007+, via openpyxl — optional)
  - .doc / .ppt / .xls  (legacy binary Office 97-2003)
  - Plain text (.txt, .md, .csv, .json, .xml, .html, .log, .yaml, .yml, etc.)

All extractors return a dict with:
    text, textLength, totalPages, isScanned, method, warnings

This is a pure re-export facade.  The implementation lives in the sub-modules:
    _dispatch  — is_supported_document, extract_document_text (routing)
    _office    — _extract_docx / _extract_pptx / _extract_xlsx (OOXML)
    _legacy    — _extract_doc_legacy / _extract_xls_legacy / _extract_ppt_legacy
    _plain     — _binary_text_extract / _extract_plaintext

``from lib.doc_parser import X`` continues to work byte-identically for every
public symbol that existed on the original module.
"""

from lib.log import get_logger

logger = get_logger(__name__)

# ── Public API — the only two symbols consumers import ──
from lib.doc_parser._dispatch import (  # noqa: E402,F401
    is_supported_document,
    extract_document_text,
)

# ── Extension tables + constants (re-exported for backwards-compat) ──
from lib.doc_parser._dispatch import (  # noqa: E402,F401
    _ALL_SUPPORTED,
    _DOCX_EXTS,
    _PPTX_EXTS,
    _XLSX_EXTS,
    _PLAIN_TEXT_EXTS,
    _MAX_CHARS,
)

# ── OOXML extractors + their scan-bound constants ──
from lib.doc_parser._office import (  # noqa: E402,F401
    _extract_docx,
    _extract_pptx,
    _extract_xlsx,
    _XLSX_MAX_ROWS,
    _XLSX_MAX_COLS,
    _XLSX_MAX_EMPTY_RUN,
)

# ── Legacy binary Office extractors ──
from lib.doc_parser._legacy import (  # noqa: E402,F401
    _extract_doc_legacy,
    _extract_xls_legacy,
    _extract_ppt_legacy,
)

# ── Plain-text + binary fallback extractors ──
from lib.doc_parser._plain import (  # noqa: E402,F401
    _binary_text_extract,
    _extract_plaintext,
)

__all__ = ['extract_document_text', 'is_supported_document']
