"""lib/pdf_parser/images/_detect.py — PDF figure/table detection and clipping."""

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
from lib.pdf_parser.images._resize import resize_image_bytes

logger = get_logger(__name__)

_FIGURE_CAP_RE = re.compile(
    r'^\s*(?:Figure|Fig\.?|图)\s*\.?\s*\d', re.IGNORECASE)
_TABLE_CAP_RE = re.compile(
    r'^\s*(?:Table|Tab\.?|表)\s*\.?\s*\d', re.IGNORECASE)
# Numbered section headings like "3.2 Universal Self-Decoder" / "4 Experiments".
# Used to stop figure/table clip regions from spilling into the next section.
_SECTION_HEAD_RE = re.compile(
    r'^\s*\d+(?:\.\d+){0,3}\s+[A-Z\u4e00-\u9fff]')


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
                            max_image_width=1800, min_dim=80, min_bytes=2000,
                            *, doc=None):
    """Detect figures/tables on a page and render them as images.

    ``doc`` (optional) is the parent ``pymupdf.Document``. When provided and
    a table extends to the bottom of the page (suggesting a multi-page
    table), the clip is augmented with a continuation image rendered from
    the top of the next page until the next caption / section heading.
    Without ``doc`` the per-page logic stays unchanged.
    """
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
            clip_x0, clip_x1 = 10, pw - 10
            above_imgs = [r for r in merged_imgs
                          if r.y1 <= cap_rect.y0 + 20
                          and r.y0 > cap_rect.y0 - ph * 0.8
                          and abs((r.x0 + r.x1) / 2 - pw / 2) < pw * 0.45]
            if above_imgs:
                nearest = max(above_imgs, key=lambda r: r.y1)
                clip_top = nearest.y0 - 5
                # Tighten horizontal bounds to the union of detected image
                # rects + the caption, instead of spanning the full page width.
                union_x0 = min(r.x0 for r in above_imgs)
                union_x1 = max(r.x1 for r in above_imgs)
                # Include caption width (it may be wider than the figure)
                union_x0 = min(union_x0, cap_rect.x0)
                union_x1 = max(union_x1, cap_rect.x1)
                clip_x0 = max(5, union_x0 - 10)
                clip_x1 = min(pw - 5, union_x1 + 10)

            if clip_top is None:
                clip_top = 0
                for j in range(ei - 1, -1, -1):
                    r, t, bt = entries[j]
                    if bt in ('figure_cap', 'table_cap'):
                        clip_top = r.y1 + 3
                        break
                    if bt == 'text' and _SECTION_HEAD_RE.match(t or ''):
                        clip_top = r.y1 + 3
                        break
                    if bt == 'text' and _is_body_text(r, t, pw):
                        clip_top = r.y1 + 3
                        break

            clip = pymupdf.Rect(clip_x0, max(0, clip_top),
                                clip_x1, min(ph, clip_bottom))
            source = 'figure_clip'

        else:  # table_cap
            # Tables in academic papers may have the caption ABOVE or BELOW
            # the body. Strategy:
            #   1. Look for a structured table detected by find_tables()
            #      near the caption (below OR above).
            #   2. If find_tables() missed the table, walk DOWNWARDS for a
            #      content region (capped at half the page).
            #   3. If the downward walk produces a too-small clip (caption
            #      followed quickly by body text), assume caption-below
            #      convention and walk UPWARDS instead — clip from the
            #      previous caption / section heading down to the caption.
            clip_top = cap_rect.y0 - 5
            clip_bottom = None

            matching_table = None
            # Below caption (most common)
            for tb in table_bboxes:
                if (tb.y0 >= cap_rect.y0 - 15 and
                        tb.y0 <= cap_rect.y1 + 60):
                    clip_bottom = tb.y1 + 5
                    matching_table = tb
                    break
            # Above caption (rarer — caption-below convention)
            if matching_table is None:
                for tb in table_bboxes:
                    if (tb.y1 <= cap_rect.y0 + 10 and
                            tb.y1 >= cap_rect.y0 - 60):
                        clip_top = tb.y0 - 5
                        clip_bottom = cap_rect.y1 + 5
                        matching_table = tb
                        break

            if clip_bottom is None:
                # No structured table detected near the caption. Walk forward
                # to find the natural end of the table region. STOP at:
                #   • another caption (figure/table)
                #   • a numbered section heading (e.g. "3.2 Foo")
                #   • a true body-text paragraph
                #   • a sizable image block (next figure — never part of a
                #     text-typeset table; this is the signal that the
                #     caption-below convention is in play)
                # AND cap the total span at half the page height so a missed
                # table boundary never spills into the next section.
                max_extent = cap_rect.y1 + ph * 0.45
                clip_bottom = min(ph, max_extent)
                hit_image_below = False
                for j in range(ei + 1, len(entries)):
                    r, t, bt = entries[j]
                    if bt in ('figure_cap', 'table_cap'):
                        clip_bottom = r.y0 - 3
                        break
                    if bt == 'text' and _SECTION_HEAD_RE.match(t or ''):
                        clip_bottom = r.y0 - 3
                        break
                    if bt == 'text' and _is_body_text(r, t, pw):
                        clip_bottom = r.y0 - 3
                        break
                    if bt == 'image' and r.height > 50 and r.width > pw * 0.3:
                        # The downward path runs straight into a real image
                        # block — that's the next figure, not the table body.
                        # Stop the downward walk here AND mark so the
                        # caption-below fallback below knows to engage even
                        # if the resulting clip is "tall enough".
                        clip_bottom = r.y0 - 3
                        hit_image_below = True
                        break

                # Caption-below fallback. Engage when EITHER:
                #   • the downward clip is tiny (< 100 px), suggesting the
                #     caption is followed only by body text, OR
                #   • the downward walk hit an image block (the next figure
                #     starts right below this caption — caption-below
                #     convention).
                # Walk UPWARDS to find a real upper bound (previous caption,
                # section heading, OR an image block whose top is the table
                # rendered as graphics above this caption).
                downward_height = clip_bottom - clip_top
                need_upward = (downward_height < 100) or hit_image_below
                if need_upward:
                    up_top = None
                    for j in range(ei - 1, -1, -1):
                        r, t, bt = entries[j]
                        if bt in ('figure_cap', 'table_cap'):
                            up_top = r.y1 + 3
                            break
                        if bt == 'text' and _SECTION_HEAD_RE.match(t or ''):
                            up_top = r.y1 + 3
                            break
                        if bt == 'image' and r.height > 50 and r.width > pw * 0.3:
                            # An image block above the caption — could be
                            # the table rendered as graphics. Don't cross it
                            # going up; use its TOP as the upper bound.
                            up_top = r.y0 - 5
                            break
                    if up_top is None:
                        up_top = max(0, cap_rect.y0 - ph * 0.5)
                    upward_height = cap_rect.y1 + 5 - up_top
                    # Use upward when:
                    #   • we MUST (hit an image below — downward is wrong), OR
                    #   • upward is meaningfully bigger than downward.
                    if hit_image_below or upward_height > downward_height:
                        clip_top = up_top
                        clip_bottom = cap_rect.y1 + 5
                        logger.debug('p%d: table_cap "%s" — using caption-below clip '
                                     '(top=%.0f bottom=%.0f, hit_image=%s)',
                                     page_idx + 1, caption[:40],
                                     clip_top, clip_bottom, hit_image_below)

            # Tighten horizontal bounds using the detected table bbox
            clip_x0, clip_x1 = 10, pw - 10
            if matching_table:
                union_x0 = min(matching_table.x0, cap_rect.x0)
                union_x1 = max(matching_table.x1, cap_rect.x1)
                clip_x0 = max(5, union_x0 - 10)
                clip_x1 = min(pw - 5, union_x1 + 10)

            clip = pymupdf.Rect(clip_x0, max(0, clip_top),
                                clip_x1, min(ph, clip_bottom))
            source = 'table_clip'

        if clip.height < min_dim or clip.width < 100:
            continue
        if clip.height > ph * 0.92:
            continue

        pad = 8
        clip = pymupdf.Rect(
            max(0, clip.x0 - pad), max(0, clip.y0 - pad),
            min(pw, clip.x1 + pad), min(ph, clip.y1 + pad))

        zoom = max(1.0, min(max_image_width / clip.width, 5.0))
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

        # Multi-page table continuation: when this is a table_cap clip whose
        # bottom is near the page edge, the table likely continues on the
        # next page. Render the continuation and stitch the two images
        # vertically into one composite. Bounded to ONE next page (the
        # vast majority of journal-style spillover) — longer tables would
        # need recursion which we deliberately avoid for safety/perf.
        composite_pages = [page_idx + 1]
        if (cap_type == 'table_cap'
                and doc is not None
                and page_idx + 1 < total_pages
                and clip.y1 >= ph - 30):
            try:
                stitched = _try_stitch_next_page_table(
                    doc[page_idx + 1], clip, pix,
                    max_image_width=max_image_width,
                )
                if stitched is not None:
                    raw_png, pix_w, pix_h = stitched
                    composite_pages.append(page_idx + 2)
                    source = 'table_clip_multi'
                    logger.debug('p%d: stitched continuation from p%d for table',
                                 page_idx + 1, page_idx + 2)
                else:
                    pix_w, pix_h = pix.width, pix.height
            except Exception as e:
                logger.warning('[PDF] table continuation stitch failed on p%d: %s',
                               page_idx + 1, e, exc_info=True)
                pix_w, pix_h = pix.width, pix.height
        else:
            pix_w, pix_h = pix.width, pix.height

        b64, mt, w, h = resize_image_bytes(raw_png, max_image_width)

        logger.debug('p%d: %s %dx%d → %dx%d 「%s」',
                     page_idx + 1, source,
                     int(clip.width), int(clip.height),
                     pix_w, pix_h, caption[:60])

        results.append({
            'base64': b64, 'mediaType': mt, 'page': page_idx + 1,
            'width': w or pix_w, 'height': h or pix_h,
            'sizeKB': len(b64) * 3 // 4 // 1024,
            'source': source, 'caption': caption,
            'pages': composite_pages,
        })

    return results


def _try_stitch_next_page_table(next_page, base_clip, base_pix,
                                *, max_image_width=1800,
                                min_continuation_height=40):
    """Detect and render a table continuation on the next page.

    Heuristic: the continuation begins at the top of the next page and
    ends at the first of:
      • a structured table bbox detected by find_tables() that starts
        near the top of the page (its bottom is the cut),
      • the first text block whose y0 > 60 px from the page top — this
        usually marks the beginning of the next paragraph/section,
      • the bottom of the page if neither of the above fires (rare, but
        handles tables that fill the whole next page).

    Returns (composite_png_bytes, total_width_px, total_height_px) or None
    if no plausible continuation was found.
    """
    npw, nph = next_page.rect.width, next_page.rect.height
    n_entries = _parse_page_blocks(next_page)
    if not n_entries:
        return None

    # If the very first text block on the next page is a caption, a section
    # heading, or sits below ~80 px from the page top, there's no spillover.
    top_text = next((e for e in n_entries
                     if e[2] in ('text', 'figure_cap', 'table_cap')), None)
    if top_text is None:
        return None
    top_rect, top_text_str, top_type = top_text
    if top_type in ('figure_cap', 'table_cap'):
        return None
    if _SECTION_HEAD_RE.match(top_text_str or ''):
        return None
    if top_rect.y0 > 100:  # top of page is empty → no continuation
        return None

    # Find an end y for the continuation.
    end_y = None
    try:
        n_tabs = next_page.find_tables()
        for tab in n_tabs.tables:
            tb = pymupdf.Rect(tab.bbox)
            if tb.y0 < 80 and tb.y1 > 40:  # starts near top
                end_y = tb.y1 + 5
                break
    except Exception as e:
        logger.debug('[PDF] continuation table detection failed: %s', e)

    if end_y is None:
        for r, t, bt in n_entries:
            if r.y0 < 40:
                continue
            if bt == 'text' and (_SECTION_HEAD_RE.match(t or '')
                                 or _is_body_text(r, t, npw)):
                end_y = r.y0 - 3
                break
            if bt in ('figure_cap', 'table_cap'):
                end_y = r.y0 - 3
                break
    if end_y is None:
        end_y = nph - 5

    if end_y < min_continuation_height:
        return None

    # Use the same horizontal bounds + zoom as the base clip so the two
    # pieces line up visually.
    cont_clip = pymupdf.Rect(
        max(0, base_clip.x0), 5,
        min(npw, base_clip.x1), min(nph, end_y),
    )
    if cont_clip.height < min_continuation_height or cont_clip.width < 100:
        return None

    base_w = base_pix.width
    zoom = max(1.0, min(base_w / cont_clip.width, 5.0))
    try:
        cont_pix = next_page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom),
                                        clip=cont_clip, alpha=False)
    except Exception as e:
        logger.warning('[PDF] continuation render failed: %s', e, exc_info=True)
        return None

    # Stitch via PIL: same width (resize continuation to match base width).
    if PILImage is None:
        return None
    try:
        base_img = PILImage.open(io.BytesIO(base_pix.tobytes('png')))
        cont_img = PILImage.open(io.BytesIO(cont_pix.tobytes('png')))
        if cont_img.width != base_img.width:
            ratio = base_img.width / cont_img.width
            cont_img = cont_img.resize(
                (base_img.width, int(cont_img.height * ratio)),
                PILImage.LANCZOS,
            )
        gap = 6  # thin separator between page slices
        total_h = base_img.height + gap + cont_img.height
        composite = PILImage.new('RGB', (base_img.width, total_h),
                                 (255, 255, 255))
        composite.paste(base_img.convert('RGB'), (0, 0))
        composite.paste(cont_img.convert('RGB'),
                        (0, base_img.height + gap))
        out = io.BytesIO()
        composite.save(out, format='PNG', optimize=True)
        return out.getvalue(), composite.width, composite.height
    except Exception as e:
        logger.warning('[PDF] continuation PIL stitch failed: %s', e, exc_info=True)
        return None
