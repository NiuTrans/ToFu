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

    # BOM-aware Unicode first. UTF-16/32 files otherwise contain many NULs and
    # latin-1 would "successfully" turn them into mojibake before a better
    # decoder had a chance.
    if file_bytes.startswith((b'\xff\xfe\x00\x00', b'\x00\x00\xfe\xff')):
        candidates = ('utf-32', 'utf-8', 'gb18030')
    elif file_bytes.startswith((b'\xff\xfe', b'\xfe\xff')):
        candidates = ('utf-16', 'utf-8', 'gb18030')
    else:
        candidates = ('utf-8-sig', 'utf-8', 'gb18030', 'gbk')

    for encoding in candidates:
        try:
            text = file_bytes.decode(encoding)
            if encoding not in ('utf-8', 'utf-8-sig'):
                logger.debug('[DocParser] Decoded %s with %s', filename, encoding)
            break
        except (UnicodeDecodeError, LookupError) as _e_audit:
            logger.debug('[doc_parser] _extract_plaintext caught %s: %s', type(_e_audit).__name__, _e_audit)
            continue

    # charset-normalizer is already a transitive dependency on most installs
    # and handles Shift-JIS/Big5/Windows code pages better than guessing. It is
    # optional so a minimal offline install still works.
    if text is None:
        try:
            from charset_normalizer import from_bytes
            best = from_bytes(file_bytes).best()
            if best is not None:
                text = str(best)
                logger.debug('[DocParser] Decoded %s with charset-normalizer (%s)',
                             filename, getattr(best, 'encoding', '?'))
        except Exception as exc:
            logger.debug('[DocParser] charset-normalizer unavailable/failed: %s', exc)

    if text is None:
        try:
            text = file_bytes.decode('latin-1')
            warnings.append('Encoding was uncertain; decoded as latin-1')
        except UnicodeDecodeError as exc:
            logger.debug('[DocParser] latin-1 fallback decode failed: %s', exc)
            text = None

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
