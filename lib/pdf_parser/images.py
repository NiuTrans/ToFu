"""lib/pdf_parser/images.py — Image extraction and figure/table detection from PDF."""

import base64
import io
import re

try:
    import pymupdf
except ImportError:
    pymupdf = None  # type: ignore[assignment]
    # Warning already logged by _common.py — debug-only here to avoid noise
try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None  # type: ignore[assignment]
    # Warning already logged by lib/fetch/utils.py — debug-only here

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['detect_and_clip_figures', 'resize_image_bytes', 'render_pdf_pages']

_FIGURE_CAP_RE = re.compile(
    r'^\s*(?:Figure|Fig\.?|图)\s*\.?\s*\d', re.IGNORECASE)
_TABLE_CAP_RE = re.compile(
    r'^\s*(?:Table|Tab\.?|表)\s*\.?\s*\d', re.IGNORECASE)


# ═══════════════════════════════════════════════════════
#  Image utilities
# ═══════════════════════════════════════════════════════

def resize_image_bytes(img_bytes: bytes, max_width: int = 1024,
                       fmt: str = 'JPEG', quality: int = 82
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


# ═══════════════════════════════════════════════════════
#  Figure / table image extraction
# ═══════════════════════════════════════════════════════

def _merge_nearby_rects(rects, gap=25):
    """Union-Find merge of nearby rectangles on a page."""
    if not rects:
        return []
    rects = [pymupdf.Rect(r) for r in rects]
    n = len(rects)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(n):
        for j in range(i + 1, n):
            ri, rj = rects[i], rects[j]
            if (max(0, max(ri.x0, rj.x0) - min(ri.x1, rj.x1)) <= gap and
                    max(0, max(ri.y0, rj.y0) - min(ri.y1, rj.y1)) <= gap):
                a, b = find(i), find(j)
                if a != b:
                    parent[a] = b
    groups = {}
    for i in range(n):
        r = find(i)
        groups[r] = (groups[r] | rects[i]) if r in groups else pymupdf.Rect(rects[i])
    return list(groups.values())


def _parse_page_blocks(page):
    """Parse a page into typed entries: (rect, text, type)."""
    try:
        page_dict = page.get_text("dict")
    except Exception as e:
        logger.warning('[PDF] page %s text dict extraction failed: %s',
                       getattr(page, 'number', '?'), e, exc_info=True)
        return []

    entries = []
    for b in page_dict.get("blocks", []):
        bbox = pymupdf.Rect(b["bbox"])
        if bbox.is_empty or bbox.is_infinite:
            continue
        if b.get("type") == 1:
            entries.append((bbox, '', 'image'))
        elif b.get("type") == 0:
            text = ""
            for line in b.get("lines", []):
                for span in line.get("spans", []):
                    text += span.get("text", "")
            text = text.strip()
            if not text:
                continue
            if _FIGURE_CAP_RE.match(text):
                entries.append((bbox, text, 'figure_cap'))
            elif _TABLE_CAP_RE.match(text):
                entries.append((bbox, text, 'table_cap'))
            else:
                entries.append((bbox, text, 'text'))

    entries.sort(key=lambda x: x[0].y0)
    return entries


def _is_body_text(rect, text, page_width):
    return len(text) >= 40 and rect.width >= page_width * 0.45


def detect_and_clip_figures(page, page_idx, total_pages,
                            max_image_width=1024, min_dim=80, min_bytes=2000):
    """Detect figures/tables on a page and render them as images."""
    pw, ph = page.rect.width, page.rect.height
    entries = _parse_page_blocks(page)
    if not entries:
        return []

    img_rects = [r for r, _, t in entries if t == 'image']
    merged_imgs = _merge_nearby_rects(img_rects, gap=25)

    table_bboxes = []
    try:
        tabs = page.find_tables()
        for tab in tabs.tables:
            table_bboxes.append(pymupdf.Rect(tab.bbox))
    except Exception as e:
        logger.warning('[PDF] table detection failed on page %d: %s', page_idx, e, exc_info=True)

    results = []

    for ei, (cap_rect, cap_text, cap_type) in enumerate(entries):
        if cap_type not in ('figure_cap', 'table_cap'):
            continue

        caption = cap_text.split('\n')[0].strip()[:300]

        if cap_type == 'figure_cap':
            clip_bottom = cap_rect.y1 + 5
            clip_top = None
            above_imgs = [r for r in merged_imgs
                          if r.y1 <= cap_rect.y0 + 20
                          and r.y0 > cap_rect.y0 - ph * 0.8
                          and abs((r.x0 + r.x1) / 2 - pw / 2) < pw * 0.45]
            if above_imgs:
                nearest = max(above_imgs, key=lambda r: r.y1)
                clip_top = nearest.y0 - 5

            if clip_top is None:
                clip_top = 0
                for j in range(ei - 1, -1, -1):
                    r, t, bt = entries[j]
                    if bt in ('figure_cap', 'table_cap'):
                        clip_top = r.y1 + 3
                        break
                    if bt == 'text' and _is_body_text(r, t, pw):
                        clip_top = r.y1 + 3
                        break

            clip = pymupdf.Rect(10, max(0, clip_top),
                                pw - 10, min(ph, clip_bottom))
            source = 'figure_clip'

        else:  # table_cap
            clip_top = cap_rect.y0 - 5
            clip_bottom = None

            for tb in table_bboxes:
                if (tb.y0 >= cap_rect.y0 - 15 and
                        tb.y0 <= cap_rect.y1 + 60):
                    clip_bottom = tb.y1 + 5
                    break

            if clip_bottom is None:
                clip_bottom = ph
                for j in range(ei + 1, len(entries)):
                    r, t, bt = entries[j]
                    if bt in ('figure_cap', 'table_cap'):
                        clip_bottom = r.y0 - 3
                        break
                    if bt == 'text' and _is_body_text(r, t, pw):
                        clip_bottom = r.y0 - 3
                        break

            clip = pymupdf.Rect(10, max(0, clip_top),
                                pw - 10, min(ph, clip_bottom))
            source = 'table_clip'

        if clip.height < min_dim or clip.width < 100:
            continue
        if clip.height > ph * 0.92:
            continue

        pad = 8
        clip = pymupdf.Rect(
            max(0, clip.x0 - pad), max(0, clip.y0 - pad),
            min(pw, clip.x1 + pad), min(ph, clip.y1 + pad))

        zoom = max(1.0, min(max_image_width / clip.width, 3.0))
        try:
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom),
                                  clip=clip, alpha=False)
        except Exception as e:
            logger.error('p%d: render error: %s', page_idx + 1, e, exc_info=True)
            continue

        if pix.width < min_dim or pix.height < 40:
            continue

        raw_png = pix.tobytes("png")
        if len(raw_png) < min_bytes:
            continue

        b64, mt, w, h = resize_image_bytes(raw_png, max_image_width)

        logger.debug('p%d: %s %dx%d → %dx%d 「%s」',
                     page_idx + 1, source,
                     int(clip.width), int(clip.height),
                     pix.width, pix.height, caption[:60])

        results.append({
            'base64': b64, 'mediaType': mt, 'page': page_idx + 1,
            'width': w or pix.width, 'height': h or pix.height,
            'sizeKB': len(b64) * 3 // 4 // 1024,
            'source': source, 'caption': caption,
        })

    return results


def render_pdf_pages(pdf_bytes: bytes, *, dpi: int = 150) -> list[bytes]:
    """Render each PDF page to JPEG bytes.

    Returns list of JPEG byte strings, one per page.
    """
    doc = pymupdf.open(stream=pdf_bytes, filetype='pdf')
    try:
        pages = []
        n = len(doc)
        for i in range(n):
            pix = doc[i].get_pixmap(dpi=dpi)
            pages.append(pix.tobytes('jpeg'))
    finally:
        doc.close()
    return pages
