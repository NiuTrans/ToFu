"""lib/file_reader/_docs.py — PDF, Office, and plain-text extraction.

Non-image readers dispatched to by ``read_local_file``:
  - ``_read_pdf``    → ``lib.pdf_parser.extract_pdf_text`` (lazy import).
  - ``_read_office`` → ``lib.doc_parser.extract_document_text`` (lazy import).
  - ``_read_text``   → direct read with encoding detection + binary guard.

The ``lib.pdf_parser`` / ``lib.doc_parser`` imports stay LAZY (inside the
functions) — both are façade packages and importing them eagerly would drag
heavy optional deps into every ``import lib.file_reader``.
"""

import os

from lib.log import get_logger

from ._router import (
    IMAGE_EXTENSIONS,
    MAX_FILE_BYTES,
    MAX_TEXT_CHARS,
    OFFICE_EXTENSIONS,
)

logger = get_logger(__name__)


def _read_pdf(path: str, file_size: int) -> str:
    """Read a PDF file and extract text."""
    if file_size > MAX_FILE_BYTES:
        return (f'Error: PDF too large: {file_size:,} bytes '
                f'(max {MAX_FILE_BYTES // (1024*1024)} MB)')

    try:
        with open(path, 'rb') as f:
            pdf_bytes = f.read()
    except Exception as e:
        logger.error('[FileReader] Failed to read PDF %s: %s', path, e, exc_info=True)
        return f'Error: Failed to read PDF: {e}'

    try:
        from lib.pdf_parser.text import extract_pdf_text
        text = extract_pdf_text(pdf_bytes, MAX_TEXT_CHARS)
        if not text:
            return f'Error: PDF appears to be scanned/image-only — no text could be extracted from: {os.path.basename(path)}'

        filename = os.path.basename(path)
        logger.info('[FileReader] PDF extracted: %s → %s chars', filename, f'{len(text):,}')
        return (f'PDF: {filename} ({file_size:,} bytes)\n\n'
                f'{text}')
    except Exception as e:
        logger.error('[FileReader] PDF parsing failed for %s: %s', path, e, exc_info=True)
        return f'Error: PDF parsing failed: {e}'


def _read_office(path: str, filename: str, file_size: int) -> str:
    """Read an Office document and extract text."""
    if file_size > MAX_FILE_BYTES:
        return (f'Error: Document too large: {file_size:,} bytes '
                f'(max {MAX_FILE_BYTES // (1024*1024)} MB)')

    try:
        with open(path, 'rb') as f:
            file_bytes = f.read()
    except Exception as e:
        logger.error('[FileReader] Failed to read document %s: %s', path, e, exc_info=True)
        return f'Error: Failed to read document: {e}'

    try:
        from lib.doc_parser import extract_document_text
        result = extract_document_text(file_bytes, filename, max_chars=MAX_TEXT_CHARS)
        text = result.get('text', '')
        if not text:
            return f'Error: No text could be extracted from: {filename}'

        method = result.get('method', '?')
        warnings = result.get('warnings', [])
        header = f'Document: {filename} ({file_size:,} bytes, method={method})'
        if warnings:
            header += f'\nWarnings: {"; ".join(warnings)}'

        logger.info('[FileReader] Document extracted: %s → %s chars (method=%s)',
                    filename, f'{len(text):,}', method)
        return f'{header}\n\n{text}'
    except Exception as e:
        logger.error('[FileReader] Document parsing failed for %s: %s', path, e, exc_info=True)
        return f'Error: Document parsing failed: {e}'


def _read_text(path: str, filename: str, file_size: int) -> str:
    """Read a text file with encoding detection."""
    if file_size > MAX_FILE_BYTES:
        return (f'Error: File too large: {file_size:,} bytes '
                f'(max {MAX_FILE_BYTES // (1024*1024)} MB)')

    # Quick binary check — read first 8KB to detect binary
    try:
        with open(path, 'rb') as f:
            header = f.read(8192)
    except Exception as e:
        logger.error('[FileReader] Failed to read %s: %s', path, e, exc_info=True)
        return f'Error: Failed to read file: {e}'

    # If more than 30% non-printable bytes, it's likely binary
    if header:
        non_text = sum(1 for b in header if b < 8 or (b > 13 and b < 32 and b != 27))
        if non_text > len(header) * 0.3:
            return (f'Error: File appears to be binary: {filename} ({file_size:,} bytes). '
                    f'Cannot read as text. Supported binary formats: '
                    f'images ({", ".join(sorted(IMAGE_EXTENSIONS))}), '
                    f'PDF (.pdf), Office ({", ".join(sorted(OFFICE_EXTENSIONS))})')

    # Read as text with encoding detection
    text = None
    for encoding in ('utf-8', 'utf-8-sig', 'gbk', 'gb18030', 'latin-1'):
        try:
            with open(path, encoding=encoding) as f:
                text = f.read(MAX_TEXT_CHARS + 100)
            break
        except (UnicodeDecodeError, LookupError) as _e_audit:
            logger.debug('[file_reader] _read_text caught %s: %s', type(_e_audit).__name__, _e_audit)
            continue

    if text is None:
        # Last resort
        with open(path, encoding='utf-8', errors='replace') as f:
            text = f.read(MAX_TEXT_CHARS + 100)

    truncated = len(text) > MAX_TEXT_CHARS
    if truncated:
        text = text[:MAX_TEXT_CHARS]

    ext = os.path.splitext(filename)[1].lower()
    logger.info('[FileReader] Text file read: %s (%s, %s chars%s)',
                filename, ext, f'{len(text):,}',
                ', truncated' if truncated else '')

    header = f'File: {filename} ({file_size:,} bytes)'
    if truncated:
        header += f' [truncated at {MAX_TEXT_CHARS:,} chars]'
    return f'{header}\n\n{text}'
