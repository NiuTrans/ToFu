"""lib/file_reader.py — Read arbitrary local files: images, PDFs, Office docs, text.

Provides a unified ``read_local_file(path)`` function that handles:
  - **Images** (.png, .jpg, .gif, .webp, .bmp): returns structured dict with
    base64 data for native VLM upload (``__screenshot__`` protocol).
  - **PDFs** (.pdf): text extraction via ``lib.pdf_parser``.
  - **Office docs** (.docx, .xlsx, .pptx, .doc, .xls, .ppt): text extraction
    via ``lib.doc_parser``.
  - **Plain text** (any other text-decodable file): direct read with encoding
    detection.

This module is called by ``read_files`` (via ``_read_absolute_file`` in
``lib/project_mod/read_tools.py``) when the path is absolute.
"""

import base64
import os

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['read_local_file', 'inspect_image_file', 'IMAGE_EXTENSIONS', 'SUPPORTED_EXTENSIONS']

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
MAX_TEXT_CHARS = 50 * 1024 * 1024    # ★ char cap lifted to the byte bound; MAX_FILE_BYTES is the real limit

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
    # Strip file:// URI prefix if present (e.g. file:///home/user/doc.pdf → /home/user/doc.pdf)
    if path.startswith('file://'):
        path = path[7:]  # len('file://') == 7
        logger.debug('[FileReader] Stripped file:// prefix → %s', path)
    # Expand ~ and resolve
    path = os.path.expanduser(path)
    path = os.path.abspath(path)

    if not os.path.isfile(path):
        return f'Error: File not found: {path}'

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
        return (f'Error: Image too large: {file_size:,} bytes '
                f'(max {MAX_IMAGE_BYTES // (1024*1024)} MB)')

    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except Exception as e:
        logger.error('[FileReader] Failed to read image %s: %s', path, e, exc_info=True)
        return f'Error: Failed to read image: {e}'

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
            f'Image file: {filename} ({fmt}, {len(raw):,} bytes). '
            f'The image is displayed above — analyze it visually.'
        ),
    }


# ── Multi-resolution image inspection (zoom / rotate / crop) ──────────
# The native read path (``_read_image``) compresses a large image down to
# ~1 MB and the LLM body-builder further downscales it to Claude's pixel
# ceiling, so fine detail in a big schematic/diagram is unreadable. The
# ``inspect_image`` tool re-renders a *region* of the ORIGINAL on-disk
# bytes at full source resolution (crop → rotate → fit), recovering the
# detail the initial downscale discarded. The result rides the same
# ``__screenshot__`` protocol as a normal image read, so dispatch /
# compaction / rendering / billing all handle it unchanged.

# Longest-side ceiling for an inspected view. Sits under Claude's
# single-image 8000px hard limit (see the claude-image-dimension-limits
# skill) so build_body never has to re-downscale a fresh crop. The many-
# image 2000px regime is handled downstream by _downscale_oversized_images;
# we stay generous here so a single deliberate crop keeps its resolution.
_INSPECT_MAX_PX = 4000
_INSPECT_JPEG_QUALITY = 88


def inspect_image_file(path, *, crop=None, rotate=0, zoom=None, grid=False):
    """Re-render a region of an image at full source resolution.

    Opens the ORIGINAL file on disk (never the already-downscaled snapshot)
    and applies, in order: rotate → crop → zoom (centre) → optional grid
    overlay → fit-to-budget. Returns a ``__screenshot__`` protocol dict so
    the caller routes it through the native image_url path.

    Args:
        path: Absolute or user-expandable image file path.
        crop: Optional ``[x0, y0, x1, y1]`` box. Values in ``[0, 1]`` are
            treated as fractions of width/height; values > 1 are absolute
            pixels. ``None`` keeps the full frame.
        rotate: Clockwise rotation in degrees — one of 0, 90, 180, 270.
        zoom: Optional float > 1 — centre-crop by this factor (e.g. 2.0
            keeps the middle quarter). Applied after ``crop``.
        grid: When True, overlay a labelled coordinate grid (tenths of the
            view) so the model can pick the next crop precisely.

    Returns:
        ``__screenshot__`` dict on success, or an ``Error: …`` string the
        caller surfaces verbatim to the model.
    """
    import io

    if path.startswith('file://'):
        path = path[7:]
    path = os.path.abspath(os.path.expanduser(path))

    if not os.path.isfile(path):
        return f'Error: File not found: {path}'

    ext = os.path.splitext(path)[1].lower()
    if ext not in IMAGE_EXTENSIONS:
        return (f'Error: inspect_image only supports images '
                f'({", ".join(sorted(IMAGE_EXTENSIONS))}); got {ext or "no extension"}. '
                f'Use read_files for other file types.')

    file_size = os.path.getsize(path)
    if file_size > MAX_IMAGE_BYTES:
        return (f'Error: Image too large: {file_size:,} bytes '
                f'(max {MAX_IMAGE_BYTES // (1024 * 1024)} MB)')

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return ('Error: image inspection requires Pillow (PIL), which is not '
                'installed on the server.')

    filename = os.path.basename(path)
    try:
        img = Image.open(path)
        img.load()
    except Exception as e:
        logger.error('[FileReader] inspect_image failed to open %s: %s',
                     path, e, exc_info=True)
        return f'Error: Failed to open image: {e}'

    src_w, src_h = img.size

    # ── 1. Rotate ──────────────────────────────────────────────────
    try:
        rotate = int(rotate or 0) % 360
    except (TypeError, ValueError):
        rotate = 0
    if rotate not in (0, 90, 180, 270):
        return f'Error: rotate must be one of 0, 90, 180, 270 (got {rotate}).'
    if rotate:
        # PIL rotates counter-clockwise; negate so the arg reads clockwise.
        img = img.rotate(-rotate, expand=True)

    w, h = img.size

    # ── 2. Crop ────────────────────────────────────────────────────
    if crop is not None:
        if not (isinstance(crop, (list, tuple)) and len(crop) == 4):
            return ('Error: crop must be a 4-element [x0, y0, x1, y1] list '
                    '(fractions 0-1 or absolute pixels).')
        try:
            x0, y0, x1, y1 = (float(v) for v in crop)
        except (TypeError, ValueError):
            return 'Error: crop values must be numbers.'
        # Fractional (all within 0..1) → scale to pixels.
        if max(x0, y0, x1, y1) <= 1.0:
            x0, x1 = x0 * w, x1 * w
            y0, y1 = y0 * h, y1 * h
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        x0 = max(0, min(int(round(x0)), w - 1))
        y0 = max(0, min(int(round(y0)), h - 1))
        x1 = max(x0 + 1, min(int(round(x1)), w))
        y1 = max(y0 + 1, min(int(round(y1)), h))
        img = img.crop((x0, y0, x1, y1))
        w, h = img.size

    # ── 3. Zoom (centre crop) ──────────────────────────────────────
    if zoom is not None:
        try:
            zoom = float(zoom)
        except (TypeError, ValueError):
            return 'Error: zoom must be a number > 1.'
        if zoom > 1.0:
            cw, ch = max(1, int(w / zoom)), max(1, int(h / zoom))
            left = (w - cw) // 2
            top = (h - ch) // 2
            img = img.crop((left, top, left + cw, top + ch))
            w, h = img.size

    # ── 4. Fit to the per-view pixel budget ────────────────────────
    fitted = False
    if max(w, h) > _INSPECT_MAX_PX:
        scale = _INSPECT_MAX_PX / max(w, h)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                         Image.LANCZOS)
        w, h = img.size
        fitted = True

    # ── 5. Optional coordinate grid overlay ────────────────────────
    if grid:
        try:
            overlay = img.convert('RGBA') if img.mode != 'RGBA' else img.copy()
            draw = ImageDraw.Draw(overlay)
            line_rgba = (255, 64, 64, 180)
            for i in range(1, 10):
                gx = int(w * i / 10)
                gy = int(h * i / 10)
                draw.line([(gx, 0), (gx, h)], fill=line_rgba, width=1)
                draw.line([(0, gy), (w, gy)], fill=line_rgba, width=1)
                draw.text((gx + 2, 2), f'{i / 10:.1f}', fill=line_rgba)
                draw.text((2, gy + 2), f'{i / 10:.1f}', fill=line_rgba)
            img = overlay
        except Exception as e:
            logger.warning('[FileReader] inspect_image grid overlay failed: %s', e)

    # ── 6. Encode ──────────────────────────────────────────────────
    img.info.pop('icc_profile', None)
    img.info.pop('exif', None)
    if img.mode == 'RGBA' or grid:
        out_format, mime, fmt = 'PNG', 'image/png', 'png'
        save_kw = {'optimize': True}
    else:
        if img.mode != 'RGB':
            img = img.convert('RGB')
        out_format, mime, fmt = 'JPEG', 'image/jpeg', 'jpeg'
        save_kw = {'quality': _INSPECT_JPEG_QUALITY, 'optimize': True}
    try:
        buf = io.BytesIO()
        img.save(buf, format=out_format, **save_kw)
    except Exception as e:
        logger.error('[FileReader] inspect_image encode failed for %s: %s',
                     path, e, exc_info=True)
        return f'Error: Failed to encode inspected image: {e}'

    out_bytes = buf.getvalue()
    b64 = base64.b64encode(out_bytes).decode('ascii')

    ops = []
    if rotate:
        ops.append(f'rotated {rotate}°')
    if crop is not None:
        ops.append('cropped')
    if zoom and zoom > 1.0:
        ops.append(f'zoom {zoom:g}×')
    if grid:
        ops.append('grid overlay')
    if fitted:
        ops.append(f'fit to {_INSPECT_MAX_PX}px')
    op_desc = ', '.join(ops) if ops else 'full frame'

    logger.info('[FileReader] inspect_image %s: src=%dx%d → view=%dx%d (%s, %s, %d bytes)',
                filename, src_w, src_h, w, h, op_desc, fmt, len(out_bytes))

    return {
        '__screenshot__': True,
        'dataUrl': f'data:{mime};base64,{b64}',
        'format': fmt,
        'originalSize': file_size,
        'compressedSize': len(out_bytes),
        'compressionApplied': True,
        'inspectOps': op_desc,
        'viewSize': [w, h],
        'sourceSize': [src_w, src_h],
        '_text_fallback': (
            f'Inspected view of {filename} ({op_desc}). '
            f'Source {src_w}×{src_h}px → view {w}×{h}px. '
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

    # ★ Strip ICC profile / EXIF — they bloat the JPEG encoder buffer
    #   and cause "encoder error -2" on small outputs (Pillow#5448).
    #   Not needed for API image uploads.
    img.info.pop('icc_profile', None)
    img.info.pop('exif', None)

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
        return (f'Error: PDF too large: {file_size:,} bytes '
                f'(max {MAX_FILE_BYTES // (1024*1024)} MB)')

    try:
        with open(path, 'rb') as f:
            pdf_bytes = f.read()
    except Exception as e:
        logger.error('[FileReader] Failed to read PDF %s: %s', path, e, exc_info=True)
        return f'Error: Failed to read PDF: {e}'

    try:
        from lib.pdf_parser import extract_pdf_text
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
