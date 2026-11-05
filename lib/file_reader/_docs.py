"""lib/file_reader/_docs.py — PDF, Office, and plain-text extraction.

Non-image readers dispatched to by ``read_local_file``:
  - ``_read_pdf``    → ``lib.pdf_parser.extract_pdf_text`` (lazy import).
  - ``_read_office`` → ``lib.doc_parser.extract_document_text`` (lazy import).
  - ``_read_text``   → direct read with encoding detection + binary guard,
    rendered by ``render_text_bytes`` (also used for already-pinned bytes).

The ``lib.pdf_parser`` / ``lib.doc_parser`` imports stay LAZY (inside the
functions) — both are façade packages and importing them eagerly would drag
heavy optional deps into every ``import lib.file_reader``.
"""

import io
import os

from lib.log import get_logger

from ._router import (
    IMAGE_EXTENSIONS,
    OFFICE_EXTENSIONS,
)

logger = get_logger(__name__)


def _read_pdf(path: str, file_size: int) -> str:
    """Read a PDF file and extract text."""

    try:
        with open(path, 'rb') as f:
            pdf_bytes = f.read()
    except Exception as e:
        logger.error('[FileReader] Failed to read PDF %s: %s', path, e, exc_info=True)
        return f'Error: Failed to read PDF: {e}'

    try:
        from lib.pdf_parser.text import extract_pdf_text
        text = extract_pdf_text(pdf_bytes)
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

    try:
        with open(path, 'rb') as f:
            file_bytes = f.read()
    except Exception as e:
        logger.error('[FileReader] Failed to read document %s: %s', path, e, exc_info=True)
        return f'Error: Failed to read document: {e}'

    try:
        from lib.doc_parser import extract_document_text
        result = extract_document_text(file_bytes, filename)
        text = result.get('text', '')
        if not text or result.get('textLength') == 0:
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


def video_reader_hint(head: bytes, filename: str) -> str:
    """Name inspect_video for binary bytes that are a video or audio file, else ''."""
    from lib.video_analysis import (
        AUDIO_EXTS, VIDEO_EXTS, sniff_audio_container, sniff_video_container,
    )
    ext = os.path.splitext(filename)[1].lower()
    if ext not in AUDIO_EXTS and (ext in VIDEO_EXTS or sniff_video_container(head)):
        return (' It is a video: watch it with inspect_video on the same path '
                '(timestamped key frames, probe facts and transcript).')
    if ext in AUDIO_EXTS or sniff_audio_container(head):
        return (' It is an audio file: listen to it with inspect_video on the same '
                'path (probe facts and transcript).')
    return ''


def _binary_refusal(head: bytes, filename: str, file_size: int) -> str:
    """The binary-file error when the first 8KB look binary, else ''.

    The project reader's rule: a NUL byte, or more than 30% control bytes.
    Compressed media and archives spread their bytes over the whole range,
    so only the NUL test recognizes them.
    """
    if head:
        non_text = sum(1 for b in head if b < 8 or (b > 13 and b < 32 and b != 27))
        if b'\x00' in head or non_text > len(head) * 0.3:
            return (f'Error: File appears to be binary: {filename} ({file_size:,} bytes). '
                    f'Cannot read as text.{video_reader_hint(head, filename)} '
                    f'Supported binary formats: '
                    f'images ({", ".join(sorted(IMAGE_EXTENSIONS))}), '
                    f'PDF (.pdf), Office ({", ".join(sorted(OFFICE_EXTENSIONS))})')
    return ''


def _read_text(path: str, filename: str, file_size: int) -> str:
    """Read a text file with encoding detection."""
    # Quick binary check — read first 8KB to detect binary
    try:
        with open(path, 'rb') as f:
            refusal = _binary_refusal(f.read(8192), filename, file_size)
            if refusal:
                return refusal
            f.seek(0)
            data = f.read()
    except Exception as e:
        logger.error('[FileReader] Failed to read %s: %s', path, e, exc_info=True)
        return f'Error: Failed to read file: {e}'
    return render_text_bytes(data, filename, file_size)


def render_text_bytes(data: bytes, filename: str, file_size: int) -> str:
    """Render file bytes exactly as ``_read_text`` renders the file.

    Callers holding descriptor-pinned bytes render what they observed instead
    of reopening a path that may have changed since.
    """
    refusal = _binary_refusal(data[:8192], filename, file_size)
    if refusal:
        return refusal

    # Read as text with encoding detection
    text = None
    # TextIOWrapper keeps open(path, encoding=...) semantics, including
    # universal-newline translation.
    for encoding in ('utf-8', 'utf-8-sig', 'gbk', 'gb18030', 'latin-1'):
        try:
            text = io.TextIOWrapper(io.BytesIO(data), encoding=encoding).read()
            break
        except (UnicodeDecodeError, LookupError) as _e_audit:
            logger.debug('[file_reader] _read_text caught %s: %s', type(_e_audit).__name__, _e_audit)
            continue

    if text is None:
        # Last resort
        text = io.TextIOWrapper(io.BytesIO(data), encoding='utf-8', errors='replace').read()

    ext = os.path.splitext(filename)[1].lower()
    logger.info('[FileReader] Text file read: %s (%s, %s chars)',
                filename, ext, f'{len(text):,}')

    header = f'File: {filename} ({file_size:,} bytes)'
    return f'{header}\n\n{text}'
