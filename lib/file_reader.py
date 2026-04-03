"""lib/file_reader.py — Read arbitrary local files: images, PDFs, Office docs, text.

Provides a unified ``read_local_file(path)`` function that handles:
  - **Images** (.png, .jpg, .gif, .webp, .bmp): returns structured dict with
    base64 data for native VLM upload (``__screenshot__`` protocol).
  - **PDFs** (.pdf): text extraction via ``lib.pdf_parser``.
  - **Office docs** (.docx, .xlsx, .pptx, .doc, .xls, .ppt): text extraction
    via ``lib.doc_parser``.
  - **Plain text** (any other text-decodable file): direct read with encoding
    detection.

This module is called by the ``read_local_file`` tool handler in the executor.
"""

import base64
import os

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['read_local_file', 'IMAGE_EXTENSIONS', 'SUPPORTED_EXTENSIONS']

# ── Extension categories ──────────────────────────────────────────────

IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}

PDF_EXTENSIONS = {'.pdf'}

OFFICE_EXTENSIONS = {
    '.docx', '.doc', '.xlsx', '.xls', '.pptx', '.ppt',
}

# Text extensions — we'll also try any unknown extension as text
TEXT_EXTENSIONS = {
    '.txt', '.md', '.markdown', '.csv', '.tsv',
    '.json', '.jsonl', '.xml', '.html', '.htm',
    '.log', '.yaml', '.yml', '.toml', '.ini', '.cfg',
    '.rst', '.tex', '.bib', '.srt', '.vtt',
    '.py', '.js', '.ts', '.java', '.c', '.cpp', '.h', '.hpp',
    '.go', '.rs', '.rb', '.php', '.sh', '.bash', '.zsh',
    '.css', '.scss', '.less', '.sql', '.r', '.m', '.swift',
    '.jsx', '.tsx', '.vue', '.svelte',
}

SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | PDF_EXTENSIONS | OFFICE_EXTENSIONS | TEXT_EXTENSIONS

# ── Limits ──────────────────────────────────────────────────────────

MAX_IMAGE_BYTES = 20 * 1024 * 1024   # 20 MB max for images
MAX_FILE_BYTES = 50 * 1024 * 1024    # 50 MB max for documents
MAX_TEXT_CHARS = 200_000             # max chars for text output

# MIME type detection from magic bytes
_IMAGE_MAGICS = {
    b'\x89PNG':   'image/png',
    b'\xff\xd8':  'image/jpeg',
    b'GIF8':      'image/gif',
    b'RIFF':      'image/webp',
    b'BM':        'image/bmp',
}

_EXT_MIME = {
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
}


def read_local_file(path: str) -> dict | str:
    """Read a local file and return its content.

    Args:
        path: Absolute or user-expandable file path.

    Returns:
        For images: dict with ``__screenshot__`` protocol (sent as image_url
        to VLM).
        For all other files: str with extracted text content.

    Raises:
        FileNotFoundError: If the file doesn't exist.
        ValueError: If the file is too large or unsupported.
    """
    # Expand ~ and resolve
    path = os.path.expanduser(path)
    path = os.path.abspath(path)

    if not os.path.isfile(path):
        return f'❌ File not found: {path}'

    file_size = os.path.getsize(path)
    filename = os.path.basename(path)
    ext = os.path.splitext(filename)[1].lower()

    logger.info('[FileReader] Reading file: %s (ext=%s, size=%s)',
                path, ext, f'{file_size:,}')

    # ── Images → native VLM upload ────────────────────────────────
    if ext in IMAGE_EXTENSIONS:
        return _read_image(path, ext, file_size)

    # ── PDFs → text extraction ────────────────────────────────────
    if ext in PDF_EXTENSIONS:
        return _read_pdf(path, file_size)

    # ── Office documents → text extraction ────────────────────────
    if ext in OFFICE_EXTENSIONS:
        return _read_office(path, filename, file_size)

    # ── Everything else → try as text ─────────────────────────────
    return _read_text(path, filename, file_size)


def _read_image(path: str, ext: str, file_size: int) -> dict | str:
    """Read an image file and return a VLM-compatible dict."""
    if file_size > MAX_IMAGE_BYTES:
        return (f'❌ Image too large: {file_size:,} bytes '
                f'(max {MAX_IMAGE_BYTES // (1024*1024)} MB)')

    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except Exception as e:
        logger.error('[FileReader] Failed to read image %s: %s', path, e, exc_info=True)
        return f'❌ Failed to read image: {e}'

    # Detect MIME from magic bytes, fall back to extension
    mime = None
    for magic, mtype in _IMAGE_MAGICS.items():
        if raw.startswith(magic):
            mime = mtype
            break
    if not mime:
        mime = _EXT_MIME.get(ext, 'image/png')

    # Compress large images to JPEG for efficiency
    compressed = False
    original_size = len(raw)
    if original_size > 1024 * 1024:  # > 1 MB
        try:
            raw, mime, compressed = _compress_image(raw, max_kb=1024)
        except Exception as e:
            logger.warning('[FileReader] Image compression failed, using original: %s', e)

    b64 = base64.b64encode(raw).decode('ascii')
    data_url = f'data:{mime};base64,{b64}'

    filename = os.path.basename(path)
    fmt = mime.split('/')[-1]

    logger.info('[FileReader] Image loaded: %s (%s, %s bytes%s)',
                filename, mime, f'{len(raw):,}',
                f', compressed from {original_size:,}' if compressed else '')

    # Return __screenshot__ protocol dict — executor will convert to image_url
    return {
        '__screenshot__': True,
        'dataUrl': data_url,
        'format': fmt,
        'originalSize': original_size,
        'compressedSize': len(raw),
        'compressionApplied': compressed,
        '_text_fallback': (
            f'📄 Image file: {filename} ({fmt}, {len(raw):,} bytes). '
            f'The image is displayed above — analyze it visually.'
        ),
    }


def _compress_image(raw: bytes, max_kb: int = 1024) -> tuple:
    """Compress image to JPEG, return (bytes, mime, was_compressed)."""
    import io

    from PIL import Image

    img = Image.open(io.BytesIO(raw))
    if img.mode in ('RGBA', 'LA', 'P'):
        bg = Image.new('RGB', img.size, (255, 255, 255))
        if img.mode == 'P':
            img = img.convert('RGBA')
        bg.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
        img = bg

    target_bytes = max_kb * 1024
    for q in (85, 70, 55, 40):
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=q, optimize=True)
        result = buf.getvalue()
        if len(result) <= target_bytes:
            return result, 'image/jpeg', True

    # Resize if still too large
    scale = 0.7
    for _ in range(3):
        new_w = int(img.width * scale)
        new_h = int(img.height * scale)
        resized = img.resize((new_w, new_h), Image.LANCZOS)
        buf = io.BytesIO()
        resized.save(buf, format='JPEG', quality=55, optimize=True)
        result = buf.getvalue()
        if len(result) <= target_bytes:
            return result, 'image/jpeg', True
        scale *= 0.7

    return result, 'image/jpeg', True


def _read_pdf(path: str, file_size: int) -> str:
    """Read a PDF file and extract text."""
    if file_size > MAX_FILE_BYTES:
        return (f'❌ PDF too large: {file_size:,} bytes '
                f'(max {MAX_FILE_BYTES // (1024*1024)} MB)')

    try:
        with open(path, 'rb') as f:
            pdf_bytes = f.read()
    except Exception as e:
        logger.error('[FileReader] Failed to read PDF %s: %s', path, e, exc_info=True)
        return f'❌ Failed to read PDF: {e}'

    try:
        from lib.pdf_parser import extract_pdf_text
        text = extract_pdf_text(pdf_bytes, MAX_TEXT_CHARS)
        if not text:
            return f'❌ PDF appears to be scanned/image-only — no text could be extracted from: {os.path.basename(path)}'

        filename = os.path.basename(path)
        logger.info('[FileReader] PDF extracted: %s → %s chars', filename, f'{len(text):,}')
        return (f'📄 PDF: {filename} ({file_size:,} bytes)\n\n'
                f'{text}')
    except Exception as e:
        logger.error('[FileReader] PDF parsing failed for %s: %s', path, e, exc_info=True)
        return f'❌ PDF parsing failed: {e}'


def _read_office(path: str, filename: str, file_size: int) -> str:
    """Read an Office document and extract text."""
    if file_size > MAX_FILE_BYTES:
        return (f'❌ Document too large: {file_size:,} bytes '
                f'(max {MAX_FILE_BYTES // (1024*1024)} MB)')

    try:
        with open(path, 'rb') as f:
            file_bytes = f.read()
    except Exception as e:
        logger.error('[FileReader] Failed to read document %s: %s', path, e, exc_info=True)
        return f'❌ Failed to read document: {e}'

    try:
        from lib.doc_parser import extract_document_text
        result = extract_document_text(file_bytes, filename, max_chars=MAX_TEXT_CHARS)
        text = result.get('text', '')
        if not text:
            return f'❌ No text could be extracted from: {filename}'

        method = result.get('method', '?')
        warnings = result.get('warnings', [])
        header = f'📄 Document: {filename} ({file_size:,} bytes, method={method})'
        if warnings:
            header += f'\n⚠️ Warnings: {"; ".join(warnings)}'

        logger.info('[FileReader] Document extracted: %s → %s chars (method=%s)',
                    filename, f'{len(text):,}', method)
        return f'{header}\n\n{text}'
    except Exception as e:
        logger.error('[FileReader] Document parsing failed for %s: %s', path, e, exc_info=True)
        return f'❌ Document parsing failed: {e}'


def _read_text(path: str, filename: str, file_size: int) -> str:
    """Read a text file with encoding detection."""
    if file_size > MAX_FILE_BYTES:
        return (f'❌ File too large: {file_size:,} bytes '
                f'(max {MAX_FILE_BYTES // (1024*1024)} MB)')

    # Quick binary check — read first 8KB to detect binary
    try:
        with open(path, 'rb') as f:
            header = f.read(8192)
    except Exception as e:
        logger.error('[FileReader] Failed to read %s: %s', path, e, exc_info=True)
        return f'❌ Failed to read file: {e}'

    # If more than 30% non-printable bytes, it's likely binary
    if header:
        non_text = sum(1 for b in header if b < 8 or (b > 13 and b < 32 and b != 27))
        if non_text > len(header) * 0.3:
            return (f'❌ File appears to be binary: {filename} ({file_size:,} bytes). '
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
        except (UnicodeDecodeError, LookupError):
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

    header = f'📄 File: {filename} ({file_size:,} bytes)'
    if truncated:
        header += f' [truncated at {MAX_TEXT_CHARS:,} chars]'
    return f'{header}\n\n{text}'
