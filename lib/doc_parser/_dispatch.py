"""lib/doc_parser/_dispatch.py — Format-routing entrypoints.

Public API:
  - is_supported_document(filename) -> bool
  - extract_document_text(file_bytes, filename, max_chars=0) -> dict

This module owns the supported-extension table and routes each format to the
appropriate extractor.  Dependency direction is acyclic: dispatch imports the
extractor modules (_office, _legacy, _plain), never the reverse.
"""

import os

from lib.log import get_logger

from lib.doc_parser._legacy import (
    _extract_doc_legacy,
    _extract_ppt_legacy,
    _extract_xls_legacy,
)
from lib.doc_parser._office import _extract_docx, _extract_pptx, _extract_xlsx
from lib.doc_parser._plain import _extract_plaintext

logger = get_logger(__name__)

# ── Supported extensions by category ──
_DOCX_EXTS = {'.docx', '.doc'}
_PPTX_EXTS = {'.pptx', '.ppt'}
_XLSX_EXTS = {'.xlsx', '.xls'}
_PLAIN_TEXT_EXTS = {
    '.txt', '.md', '.markdown', '.csv', '.tsv',
    '.json', '.jsonl', '.xml', '.html', '.htm',
    '.log', '.yaml', '.yml', '.toml', '.ini', '.cfg',
    '.rst', '.tex', '.bib', '.srt', '.vtt',
    '.py', '.js', '.ts', '.java', '.c', '.cpp', '.h', '.hpp',
    '.go', '.rs', '.rb', '.php', '.sh', '.bash', '.zsh',
    '.css', '.scss', '.less', '.sql', '.r', '.m', '.swift',
}

_ALL_SUPPORTED = _DOCX_EXTS | _PPTX_EXTS | _XLSX_EXTS | _PLAIN_TEXT_EXTS

# Max chars to extract
_MAX_CHARS = 2_000_000


def is_supported_document(filename: str) -> bool:
    """Check if a filename has a supported document extension."""
    ext = os.path.splitext(filename)[1].lower()
    return ext in _ALL_SUPPORTED


def extract_document_text(file_bytes: bytes, filename: str, max_chars: int = 0,
                          *, robust: bool = False) -> dict:
    """Extract text from a document file.

    Args:
        file_bytes: Raw file bytes.
        filename: Original filename (used to determine format).
        max_chars: Max chars to extract (0 = the package default).
        robust: Use the slower sparse-spreadsheet path. This scans across
            long blank gaps and keeps multiple table blocks instead of
            assuming a sheet is one dense table. Intended for persistent
            knowledge ingestion; interactive attachment parsing keeps the
            lower-latency default.

    Returns:
        Dict with text, textLength, totalPages, isScanned, method, warnings.
    """
    ext = os.path.splitext(filename)[1].lower()
    limit = max_chars if max_chars > 0 else _MAX_CHARS

    if ext == '.docx':
        return _extract_docx(file_bytes, limit)
    elif ext == '.doc':
        return _extract_doc_legacy(file_bytes, limit)
    elif ext == '.pptx':
        return _extract_pptx(file_bytes, limit)
    elif ext == '.ppt':
        return _extract_ppt_legacy(file_bytes, limit)
    elif ext == '.xlsx':
        return _extract_xlsx(file_bytes, limit, robust=robust)
    elif ext == '.xls':
        return _extract_xls_legacy(file_bytes, limit)
    elif ext in _PLAIN_TEXT_EXTS:
        return _extract_plaintext(file_bytes, filename, limit)
    else:
        return {
            'text': f'[Unsupported format: {ext}]',
            'textLength': 0,
            'totalPages': 0,
            'isScanned': False,
            'method': 'unsupported',
            'warnings': [f'Unsupported format: {ext}'],
        }
