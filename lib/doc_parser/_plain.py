"""lib/doc_parser/_plain.py — Plain-text + binary fallback text extraction.

Provides:
  - _binary_text_extract  (last-resort byte-scan for legacy Office formats)
  - _extract_plaintext    (encoding-detecting plaintext extractor)
"""

import os

from lib.log import get_logger

from lib.doc_parser._truncation import truncation_warning

logger = get_logger(__name__)


def _binary_text_extract(file_bytes: bytes, limit: int) -> str:
    """Last-resort text extraction from binary Office files.

    Scans the raw bytes for UTF-16LE and ASCII text runs,
    filters to printable content, and returns the best result.
    """
    import re

    # Extract UTF-16LE strings (≥6 chars / 12 bytes)
    utf16_pattern = re.compile(rb'(?:[\x20-\x7e]\x00){6,}')
    matches = utf16_pattern.findall(file_bytes[:limit * 3])
    utf16_text = ''.join(
        m.decode('utf-16-le', errors='ignore') for m in matches
    )

    # Extract ASCII strings (≥8 chars)
    ascii_pattern = re.compile(rb'[\x20-\x7e]{8,}')
    matches = ascii_pattern.findall(file_bytes[:limit * 2])
    ascii_text = ''.join(
        m.decode('ascii', errors='ignore') + '\n' for m in matches
    )

    # Pick the longer, more useful result
    text = utf16_text if len(utf16_text) > len(ascii_text) else ascii_text
    text = re.sub(r'[^\S\n]+', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()

    if len(text) > limit:
        text = text[:limit]

    return text if len(text) > 50 else ''


def _extract_plaintext(file_bytes: bytes, filename: str, limit: int) -> dict:
    """Extract text from plain-text files with encoding detection."""
    warnings = []
    text = None

    # Try UTF-8 first, then common fallbacks
    for encoding in ('utf-8', 'utf-8-sig', 'gbk', 'gb18030', 'latin-1'):
        try:
            text = file_bytes.decode(encoding)
            if encoding not in ('utf-8', 'utf-8-sig'):
                logger.debug('[DocParser] Decoded %s with %s', filename, encoding)
            break
        except (UnicodeDecodeError, LookupError) as _e_audit:
            logger.debug('[doc_parser] _extract_plaintext caught %s: %s', type(_e_audit).__name__, _e_audit)
            continue

    if text is None:
        # Last resort: lossy decode
        text = file_bytes.decode('utf-8', errors='replace')
        warnings.append('File contains non-UTF-8 characters (lossy decode)')

    if len(text) > limit:
        full_len = len(text)
        text = text[:limit]
        warnings.append(truncation_warning(
            kept=len(text), total=full_len, unit='chars',
            detail=f'char limit {limit:,}'))

    ext = os.path.splitext(filename)[1].lower()
    logger.info('[DocParser] Extracted plaintext %s (%s): %s chars',
                filename, ext, f'{len(text):,}')

    return {
        'text': text,
        'textLength': len(text),
        'totalPages': 1,
        'isScanned': False,
        'method': f'plaintext ({ext})',
        'warnings': warnings,
    }
