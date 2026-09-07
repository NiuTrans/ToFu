"""lib/pdf_parser/images/_resize.py — Image resize/crop utilities."""

import base64
import io

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None  # type: ignore[assignment]
    # Warning already logged by lib/fetch/utils.py — debug-only here

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════
#  Image utilities
# ═══════════════════════════════════════════════════════

def _auto_crop_whitespace(img, threshold=245, min_margin=4):
    """Trim near-white borders from an image.

    Finds the bounding box of non-white content (pixels darker than
    threshold) and crops to that box plus a small margin.  Returns the
    image unchanged if it's already tight or mostly non-white.
    """
    import numpy as np
    arr = np.asarray(img)
    # Mask of "dark enough" pixels (any channel below threshold)
    mask = arr.min(axis=2) < threshold
    coords = np.argwhere(mask)
    if len(coords) < 20:
        return img
    y0, x0 = coords.min(axis=0)
    y1, x1 = coords.max(axis=0)
    # Add small margin
    w, h = img.size
    x0 = max(0, x0 - min_margin)
    y0 = max(0, y0 - min_margin)
    x1 = min(w - 1, x1 + min_margin)
    y1 = min(h - 1, y1 + min_margin)
    # Only crop if we'd remove at least 3% from any side
    trim_pct = 1.0 - ((x1 - x0) * (y1 - y0)) / (w * h)
    if trim_pct < 0.03:
        return img
    return img.crop((x0, y0, x1 + 1, y1 + 1))


def resize_image_bytes(img_bytes: bytes, max_width: int = 1800,
                       fmt: str = 'JPEG', quality: int = 90
                       ) -> tuple[str, str, int | None, int | None]:
    """Resize image bytes, return (base64, mediaType, width, height)."""
    source_buf: io.BytesIO | None = None
    out_buf: io.BytesIO | None = None
    try:
        source_buf = io.BytesIO(img_bytes)
        img = PILImage.open(source_buf)
        w, h = img.size
        if img.mode in ('RGBA', 'P', 'LA'):
            img = img.convert('RGBA')
            bg = PILImage.new('RGB', img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[3])
            img = bg
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        # Auto-crop white borders for cleaner figure extraction
        img = _auto_crop_whitespace(img)
        w, h = img.size
        if w > max_width:
            ratio = max_width / w
            img = img.resize((max_width, int(h * ratio)), PILImage.LANCZOS)
        out_buf = io.BytesIO()
        img.save(out_buf, format=fmt, quality=quality, optimize=True)
        b64 = base64.b64encode(out_buf.getvalue()).decode()
        mt = f'image/{fmt.lower()}'
        return b64, mt, img.width, img.height
    except Exception as e:
        logger.warning('[PDF] image resize/compress failed, using raw bytes: %s', e, exc_info=True)
        b64 = base64.b64encode(img_bytes).decode()
        if img_bytes[:2] == b'\xff\xd8':
            mt = 'image/jpeg'
        elif img_bytes[:4] == b'\x89PNG':
            mt = 'image/png'
        elif img_bytes[:4] in (b'RIFF', b'WEBP'):
            mt = 'image/webp'
        else:
            mt = 'image/png'
        return b64, mt, None, None
    finally:
        if out_buf is not None:
            out_buf.close()
        if source_buf is not None:
            source_buf.close()
