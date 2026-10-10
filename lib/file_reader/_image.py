"""lib/file_reader/_image.py — Image reading, inspection, and compression.

Handles the native VLM image path (``_read_image`` → ``__screenshot__``
protocol dict), the multi-resolution ``inspect_image_file`` re-render tool
(crop / rotate / zoom / grid), and the ``_compress_image`` JPEG helper.

Depends on the shared extension/limit constants declared in ``_router``.
"""

import base64
import os

from lib.log import get_logger

from ._router import IMAGE_EXTENSIONS, MAX_IMAGE_BYTES, _EXT_MIME

logger = get_logger(__name__)

# MIME type detection from magic bytes
_IMAGE_MAGICS = {
    b'\x89PNG':   'image/png',
    b'\xff\xd8':  'image/jpeg',
    b'GIF8':      'image/gif',
    b'RIFF':      'image/webp',
    b'BM':        'image/bmp',
}


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
# bytes at full source resolution (EXIF orientation → explicit rotate →
# crop-or-centre-zoom → fit), recovering the detail the initial downscale
# discarded. The result rides the same
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
    """Re-render a region of a local image file at full source resolution.

    Reads the ORIGINAL file on disk and delegates to
    :func:`inspect_image_bytes`. Kept for callers that already have a
    filesystem path (project-relative reads, absolute paths). For a
    chat-uploaded image referenced by ``/api/images/<f>`` (which has no path
    the model can name), route through
    :func:`lib.project_mod.read_tools.tool_inspect_image` → the centralized
    attachment resolver instead.

    Args:
        path: Absolute or user-expandable image file path.
        crop / rotate / zoom / grid: See :func:`inspect_image_bytes`.

    Returns:
        ``__screenshot__`` dict on success, or an ``Error: …`` string the
        caller surfaces verbatim to the model.
    """
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
        with open(path, 'rb') as f:
            raw = f.read()
    except Exception as e:
        logger.error('[FileReader] inspect_image failed to read %s: %s', path, e, exc_info=True)
        return f'Error: Failed to open image: {e}'

    return inspect_image_bytes(raw, source_name=os.path.basename(path),
                               crop=crop, rotate=rotate, zoom=zoom, grid=grid)


def inspect_image_bytes(raw, *, source_name='image', crop=None, rotate=0,
                        zoom=None, grid=False):
    """Re-render a region of in-memory image bytes at full source resolution.

    This is the transform half of image inspection — decoupled from where the
    bytes came from (disk file, disk-backed ``/api/images/`` upload, DB base64,
    remote download). Applies, in order: embedded EXIF orientation → explicit
    rotate → one region selector (crop when present, otherwise centre zoom) →
    fit-to-budget → optional grid overlay, then encodes to a ``__screenshot__``
    protocol dict so the caller routes it through the native image_url path.

    Args:
        raw: The original (pre-downscale) image bytes.
        source_name: Display name used in logs and the text fallback.
        crop: Optional ``[x0, y0, x1, y1]`` box. Each value in ``[0, 1]`` is
            interpreted as a fraction of its own axis; each value > 1 is an
            absolute pixel coordinate in the EXIF-normalized frame after
            ``rotate``. Mixed fractional/pixel boxes are supported. When present,
            this exact region takes precedence over ``zoom``.
        rotate: Clockwise rotation in degrees — one of 0, 90, 180, 270. Applied
            after the image's embedded EXIF display orientation.
        zoom: Optional float > 1 — centre-crop by this factor (e.g. 2.0
            keeps the middle quarter). A convenience alternative used only when
            ``crop`` is absent; it never narrows an explicit crop a second time.
        grid: When True, overlay a labelled coordinate grid (tenths of the
            view) so the model can pick the next crop precisely.

    Returns:
        ``__screenshot__`` dict on success, or an ``Error: …`` string the
        caller surfaces verbatim to the model.
    """
    import io

    file_size = len(raw)
    if file_size > MAX_IMAGE_BYTES:
        return (f'Error: Image too large: {file_size:,} bytes '
                f'(max {MAX_IMAGE_BYTES // (1024 * 1024)} MB)')

    try:
        from PIL import Image, ImageDraw, ImageOps
    except ImportError as e:
        logger.debug('[FileReader] Pillow import failed, using fallback: %s', e)
        return ('Error: image inspection requires Pillow (PIL), which is not '
                'installed on the server.')

    filename = source_name
    try:
        encoded_image = Image.open(io.BytesIO(raw))
        encoded_image.load()
        encoded_src_w, encoded_src_h = encoded_image.size
        # Browsers and native image viewers honor EXIF orientation. Normalize
        # those pixels before any model-supplied coordinates so crop/grid refer
        # to the same frame the user and model saw in the original image.
        img = ImageOps.exif_transpose(encoded_image)
        if img is not encoded_image:
            encoded_image.close()
    except Exception as e:
        logger.error('[FileReader] inspect_image failed to open %s: %s',
                     filename, e, exc_info=True)
        return f'Error: Failed to open image: {e}'

    src_w, src_h = img.size

    # ── 1. Explicit rotate, after embedded display orientation ─────
    try:
        rotate = int(rotate or 0) % 360
    except (TypeError, ValueError) as e:
        logger.debug('[FileReader] inspect_image bad rotate %r (%s) — defaulting to 0', rotate, e)
        rotate = 0
    if rotate not in (0, 90, 180, 270):
        return f'Error: rotate must be one of 0, 90, 180, 270 (got {rotate}).'
    if rotate:
        # PIL rotates counter-clockwise; negate so the arg reads clockwise.
        img = img.rotate(-rotate, expand=True)

    w, h = img.size

    applied_crop_box = None

    # ── 2. Crop ────────────────────────────────────────────────────
    if crop is not None:
        if not (isinstance(crop, (list, tuple)) and len(crop) == 4):
            return ('Error: crop must be a 4-element [x0, y0, x1, y1] list '
                    '(fractions 0-1 or absolute pixels).')
        try:
            x0, y0, x1, y1 = (float(v) for v in crop)
        except (TypeError, ValueError) as e:
            logger.debug('[FileReader] inspect_image bad crop %r (%s)', crop, e)
            return 'Error: crop values must be numbers.'
        # Unit selection is per coordinate, exactly as the public schema says.
        # A mixed box such as [0.5, 0, 1, 400] uses fractional x coordinates
        # and a pixel y endpoint; classifying the whole box from its maximum
        # silently collapsed that valid request into a one-pixel-wide strip.
        x0 = x0 * w if 0.0 <= x0 <= 1.0 else x0
        x1 = x1 * w if 0.0 <= x1 <= 1.0 else x1
        y0 = y0 * h if 0.0 <= y0 <= 1.0 else y0
        y1 = y1 * h if 0.0 <= y1 <= 1.0 else y1
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        x0 = max(0, min(int(round(x0)), w - 1))
        y0 = max(0, min(int(round(y0)), h - 1))
        x1 = max(x0 + 1, min(int(round(x1)), w))
        y1 = max(y0 + 1, min(int(round(y1)), h))
        applied_crop_box = [x0, y0, x1, y1]
        img = img.crop(tuple(applied_crop_box))
        w, h = img.size

    # ── 3. Centre zoom — an alternative selector, never a second crop ──
    zoom_applied = False
    if crop is None and zoom is not None:
        try:
            zoom = float(zoom)
        except (TypeError, ValueError) as e:
            logger.debug('[FileReader] inspect_image bad zoom %r (%s)', zoom, e)
            return 'Error: zoom must be a number > 1.'
        if zoom > 1.0:
            cw, ch = max(1, int(w / zoom)), max(1, int(h / zoom))
            left = (w - cw) // 2
            top = (h - ch) // 2
            img = img.crop((left, top, left + cw, top + ch))
            w, h = img.size
            zoom_applied = True

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
                     filename, e, exc_info=True)
        return f'Error: Failed to encode inspected image: {e}'

    out_bytes = buf.getvalue()
    b64 = base64.b64encode(out_bytes).decode('ascii')

    ops = []
    if rotate:
        ops.append(f'rotated {rotate}°')
    if crop is not None:
        ops.append('cropped')
    if zoom_applied:
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

        # Encoded dimensions are diagnostic only; sourceSize is deliberately
        # the EXIF-normalized visible frame used by crop coordinates.
        'encodedSourceSize': [encoded_src_w, encoded_src_h],
        'cropBox': applied_crop_box,
        '_text_fallback': (
            f'Inspected view of {filename} ({op_desc}). '
            f'Source {src_w}×{src_h}px → view {w}×{h}px. '
            f'The image is displayed above — analyze it visually.'
        ),
    }


def _compress_image(raw: bytes, max_kb: int = 1024) -> tuple:
    """Compress image to JPEG, return (bytes, mime, was_compressed)."""
    import io

    from PIL import Image, ImageOps

    encoded_image = Image.open(io.BytesIO(raw))
    encoded_image.load()
    img = ImageOps.exif_transpose(encoded_image)
    if img is not encoded_image:
        encoded_image.close()
    if img.mode in ('RGBA', 'LA', 'P'):
        bg = Image.new('RGB', img.size, (255, 255, 255))
        if img.mode == 'P':
            img = img.convert('RGBA')
        bg.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
        img = bg

    # Strip ICC profile / EXIF — they bloat the JPEG encoder buffer
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
