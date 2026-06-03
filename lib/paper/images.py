"""Figure extraction + report-time image injection.

Per ``paper-report-image-injection`` memory: cheap LLMs ignore the
"please embed ``![caption](url)``" instruction in the manifest, so we do
it deterministically post-stream — match each manifest entry to the
paragraph that discusses it most thoroughly (longest paragraph mentioning
the figure number) and insert the image there. Unmatched figures land in
an appendix gallery so they're never silently lost.
"""

import base64 as _b64
import json
import os
import re
import time

from lib.database import get_db, get_thread_db
from lib.log import get_logger

from .hashing import PAPER_DIR, PAPER_IMG_DIR, _safe_hash_dir

logger = get_logger(__name__)


# Bump this when changing figure-extraction params (resolution, layout, …)
# so old manifests regenerate at the new quality on next access.
_FIG_EXTRACT_VERSION = 7


def _load_image_manifest(phash):
    """Load an extracted image manifest by paper_hash, or [] if unknown.

    The manifest lives at ``uploads/papers/images/<phash>/manifest.json`` and
    is written by ``extract_images()`` / ``_ensure_paper_images()``. The
    server is the source of truth for image URLs — clients should never
    forward images they previously received.
    """
    phash_safe = _safe_hash_dir(phash)
    if not phash_safe:
        return []
    manifest_path = os.path.join(PAPER_IMG_DIR, phash_safe, 'manifest.json')
    if not os.path.isfile(manifest_path):
        return []
    try:
        with open(manifest_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # New versioned format only — legacy bare-list manifests point at
        # low-resolution PNGs, so we ignore them here and let the caller's
        # fallback (_ensure_paper_images) regenerate at the current quality.
        if isinstance(data, dict) and data.get('version') == _FIG_EXTRACT_VERSION:
            imgs = data.get('images') or []
            if isinstance(imgs, list):
                return [im for im in imgs if isinstance(im, dict) and im.get('url')]
    except Exception as e:
        logger.warning('[Paper:Images] Manifest read failed for %s: %s', phash_safe, e)
    return []


def _extract_paper_figures(filepath, phash, *, max_images=30, max_image_width=1800):
    """Run pymupdf figure/table extraction and persist a manifest.

    Returns the manifest list. Cached: re-uses the on-disk manifest if it
    already exists AND was produced by the current extractor version.
    Caller is responsible for ensuring ``filepath`` exists.
    """
    out_dir = os.path.join(PAPER_IMG_DIR, phash)
    manifest_path = os.path.join(out_dir, 'manifest.json')
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                cached = json.load(f)
            # New format: {'version': N, 'images': [...]}
            # Legacy format: bare list — predates the resolution bump,
            # regenerate so figures aren't blurry on retina displays.
            if isinstance(cached, dict) and cached.get('version') == _FIG_EXTRACT_VERSION:
                imgs = cached.get('images')
                if isinstance(imgs, list):
                    return imgs
            elif isinstance(cached, list):
                logger.info('[Paper:Images] Legacy manifest hash=%s — regenerating at higher resolution', phash)
        except Exception as e:
            logger.warning('[Paper:Images] Cached manifest unreadable, regenerating: %s', e)

    try:
        import pymupdf
    except ImportError as e:
        logger.error('[Paper:Images] pymupdf not available: %s', e)
        return []
    from lib.pdf_parser._common import PYMUPDF_LOCK
    from lib.pdf_parser.images import detect_and_clip_figures

    try:
        with open(filepath, 'rb') as f:
            pdf_bytes = f.read()
    except Exception as e:
        logger.error('[Paper:Images] Read failed: %s', e, exc_info=True)
        return []

    os.makedirs(out_dir, exist_ok=True)
    images_out = []
    t0 = time.time()
    try:
        with PYMUPDF_LOCK:
            doc = pymupdf.open(stream=pdf_bytes, filetype='pdf')
            try:
                total_pages = len(doc)
                for pi in range(total_pages):
                    if len(images_out) >= max_images:
                        break
                    try:
                        page_imgs = detect_and_clip_figures(
                            doc[pi], pi, total_pages,
                            max_image_width=max_image_width,
                            doc=doc,
                        )
                    except Exception as pe:
                        logger.warning('[Paper:Images] detect failed on page %d: %s', pi, pe)
                        continue
                    for img in page_imgs:
                        if len(images_out) >= max_images:
                            break
                        try:
                            raw = _b64.b64decode(img['base64'])
                            idx = len(images_out) + 1
                            ext = '.jpg' if 'jpeg' in img.get('mediaType', '') else '.png'
                            fname = f'fig_{idx:02d}_p{img.get("page", pi+1)}{ext}'
                            fpath = os.path.join(out_dir, fname)
                            with open(fpath, 'wb') as f:
                                f.write(raw)
                            images_out.append({
                                'url': f'/api/paper/images/{phash}/{fname}',
                                'caption': img.get('caption', ''),
                                'page': img.get('page'),
                                'source': img.get('source', ''),
                                'width': img.get('width'),
                                'height': img.get('height'),
                            })
                        except Exception as se:
                            logger.warning('[Paper:Images] Failed to save figure %d: %s',
                                           len(images_out)+1, se)
            finally:
                doc.close()
    except Exception as e:
        logger.error('[Paper:Images] Extraction failed: %s', e, exc_info=True)
        return []

    try:
        with open(manifest_path, 'w', encoding='utf-8') as f:
            json.dump({'version': _FIG_EXTRACT_VERSION, 'images': images_out},
                      f, ensure_ascii=False)
    except Exception as e:
        logger.warning('[Paper:Images] Failed to write manifest: %s', e)

    elapsed = time.time() - t0
    logger.info('[Paper:Images] Extracted %d images from %s in %.1fs (hash=%s)',
                len(images_out), os.path.basename(filepath), elapsed, phash)
    return images_out


def _ensure_paper_images(filename, phash):
    """Ensure a manifest exists for (filename, phash); extract if missing.

    Returns the manifest list. Used by upload / arxiv-fetch flows so figure
    extraction happens BEFORE the user clicks the Report tab — no race.
    """
    if not filename or not phash:
        return []
    filepath = os.path.join(PAPER_DIR, os.path.basename(filename))
    if not os.path.isfile(filepath):
        logger.debug('[Paper:Images] Skip ensure — %s not on disk', filename)
        return []
    return _extract_paper_figures(filepath, phash)


def _inject_images_into_report(report_md, images, lang='en'):
    """Auto-insert extracted figures/tables into the report markdown.

    LLMs frequently ignore "please embed ``![caption](url)``" instructions in
    the manifest, so we do it deterministically: for each image whose caption
    begins with a figure/table number (e.g. ``Figure 3: …`` / ``Table 1 …`` /
    ``图 3 …``), find the first paragraph in the report that mentions that
    number and insert the image right after it. Any images that can't be
    matched to a mention are appended as an appendix at the end.

    If the model *did* embed images correctly (unlikely but possible) we
    bail out to avoid duplicates.

    Args:
        report_md: The generated report Markdown.
        images: Manifest entries ``[{url, caption, page, source, ...}]``.
        lang: 'zh' or 'en' — controls the appendix heading.

    Returns:
        Enriched report Markdown, or the original string on failure / no-op.
    """
    if not report_md:
        return report_md
    try:
        # Strip "fake" image references the model invents when we ask it to
        # embed figures but no matching manifest entry exists.  E.g. for a
        # Table 5 that wasn't extracted, models like to write
        #   ![表 5 — 设计选择消融](表 5 数据见正文)
        # marked.js refuses to render the bracketed text as a URL (it
        # contains spaces) and falls back to the literal `![...](...)` —
        # which the user sees as a broken/un-rendered placeholder.  Detect
        # any `![alt](url)` whose URL is not http(s)://, /, or data: and
        # downgrade it to its alt text in italics.
        def _strip_fake_img(m):
            alt, url = m.group(1), m.group(2).strip()
            if re.match(r'^(?:https?://|/|data:|#)', url):
                return m.group(0)
            return f'*{alt}*' if alt.strip() else ''
        report_md = re.sub(r'!\[([^\]]*)\]\(([^)]+)\)', _strip_fake_img, report_md)

        if not images:
            return report_md

        # If the model already embedded any paper image, trust it and skip.
        if re.search(r'!\[[^\]]*\]\(/api/paper/images/', report_md):
            return report_md

        # Parse each caption for kind + number so we can find textual mentions.
        fig_re = re.compile(r'^\s*(?:Figure|Fig\.?|图)\s*\.?\s*(\d+)', re.IGNORECASE)
        tab_re = re.compile(r'^\s*(?:Table|Tab\.?|表)\s*\.?\s*(\d+)', re.IGNORECASE)
        parsed = []
        for img in images:
            url = (img.get('url') or '').strip()
            cap = (img.get('caption') or '').strip()
            if not url:
                continue
            kind, num = None, None
            m = fig_re.match(cap)
            if m:
                kind, num = 'figure', int(m.group(1))
            else:
                m = tab_re.match(cap)
                if m:
                    kind, num = 'table', int(m.group(1))
            # Alt text must not contain newlines or ] that would break syntax.
            alt = (cap.replace('\n', ' ')
                      .replace(']', ')')
                      .replace('[', '(')).strip()[:200] or (
                      ('Figure' if kind == 'figure' else 'Table' if kind == 'table' else 'Figure')
                      + (f' {num}' if num else ''))
            parsed.append({'url': url, 'caption': cap, 'alt': alt,
                           'kind': kind, 'num': num})

        # Split report into paragraphs preserving separators.
        # paras = [p0, sep0, p1, sep1, ...]
        paras = re.split(r'(\n\n+)', report_md)

        # Pick the paragraph that DISCUSSES each figure most thoroughly,
        # not just the first one that mentions it.  Models often write a
        # one-line drive-by mention near the top ("...as shown in Figure 4")
        # and then return to the figure with substantive analysis several
        # paragraphs later.  Inserting next to the drive-by mention puts the
        # image far from its discussion, which the user explicitly flagged.
        # Heuristic: among paragraphs that mention the figure, pick the
        # longest one (proxy for "most detailed discussion").
        placed = set()
        by_para: dict[int, list[str]] = {}
        # First, pick best paragraph for each parsed image
        candidate_paras: list[tuple[int, int]] = []  # (para_index, length)
        for i in range(0, len(paras), 2):
            p = paras[i]
            stripped = p.strip()
            if not stripped:
                continue
            if stripped.startswith('```') or stripped.startswith('|'):
                continue
            candidate_paras.append((i, len(stripped)))
        for pi, img in enumerate(parsed):
            if img['kind'] is None or img['num'] is None:
                continue
            if img['kind'] == 'figure':
                pat = rf'(?:Figure|Fig\.?|图)\s*\.?\s*{img["num"]}\b'
            else:
                pat = rf'(?:Table|Tab\.?|表)\s*\.?\s*{img["num"]}\b'
            best_idx, best_len = -1, -1
            for idx, plen in candidate_paras:
                if re.search(pat, paras[idx], re.IGNORECASE) and plen > best_len:
                    best_idx, best_len = idx, plen
            if best_idx >= 0:
                by_para.setdefault(best_idx, []).append(
                    f'\n\n![{img["alt"]}]({img["url"]})\n\n')
                placed.add(pi)

        # Insert from the end so earlier indices stay valid.
        for i in sorted(by_para.keys(), reverse=True):
            paras.insert(i + 1, ''.join(by_para[i]))
        out = ''.join(paras)

        # Append any unreferenced images as an appendix gallery.
        unplaced = [p for pi, p in enumerate(parsed) if pi not in placed]
        if unplaced:
            title = '图表附录' if lang == 'zh' else 'Figures & Tables (Appendix)'
            blurb = ('论文中未在报告正文中显式引用的图表：'
                     if lang == 'zh'
                     else 'Figures and tables from the paper not referenced above:')
            out = out.rstrip() + f'\n\n---\n\n## 📎 {title}\n\n{blurb}\n\n'
            for img in unplaced:
                out += f'![{img["alt"]}]({img["url"]})\n\n'
                if img['caption']:
                    cap_clean = img['caption'].replace('\n', ' ').strip()
                    out += f'*{cap_clean}*\n\n'

        logger.info('[Paper:Report] Image inject — %d placed inline, %d in appendix '
                    '(%d total)', len(placed), len(unplaced), len(parsed))
        return out
    except Exception as e:
        logger.warning('[Paper:Report] Image injection failed (returning original): %s',
                       e, exc_info=True)
        return report_md


def _lookup_paper_title(phash: str) -> str:
    """Best-effort title lookup for a paper hash.

    Pulls the most recently-updated `paper_library` row matching the hash
    (across all users — paper_hash is content-addressable, not user-scoped).
    Returns '' if no row exists or the lookup fails.

    Uses ``get_thread_db()`` so it's safe to call from background worker
    threads (where Flask's request-scoped ``g`` is not available).
    """
    if not _safe_hash_dir(phash):
        return ''
    try:
        # Background task path: no Flask request context → can't use get_db()
        # which relies on flask.g. Fall back to a thread-local connection.
        try:
            db = get_db()
        except RuntimeError as e:
            # Working outside of application context — expected from worker threads.
            logger.debug('[Paper:Report] No Flask context, using thread-local DB: %s', e)
            db = get_thread_db()
        row = db.execute(
            'SELECT title, arxiv_id FROM paper_library '
            'WHERE paper_hash=? ORDER BY updated_at DESC LIMIT 1',
            (phash,),
        ).fetchone()
    except Exception as e:
        logger.warning('[Paper:Report] Title lookup failed for hash=%s: %s', phash, e)
        return ''
    if not row:
        logger.info('[Paper:Report] No paper_library row for hash=%s — title prepend skipped', phash)
        return ''
    title = (row['title'] or '').strip()
    if title:
        return title
    arxiv = (row['arxiv_id'] or '').strip()
    return f'arXiv:{arxiv}' if arxiv else ''


def _ensure_title_heading(report_md: str, phash: str) -> str:
    """Idempotently prepend a `# Title` heading to a report.

    Older cached reports were persisted before the title-prepend logic
    existed, so they render without a top-level heading. This helper looks
    up the title via _lookup_paper_title and prepends it only when the
    report doesn't already start with `# `.
    """
    if not report_md:
        return report_md
    if re.match(r'^\s*#\s+\S', report_md):
        return report_md
    title = _lookup_paper_title(phash)
    if not title:
        return report_md
    return f'# {title}\n\n' + report_md.lstrip()


def _build_image_manifest(images, lang='en'):
    """Build a compact image manifest block for the LLM prompt."""
    if not images:
        return ''
    header = ('Image manifest — figures/tables extracted from the paper.\n'
              'Embed each as `![caption](url)` in Markdown where relevant.\n'
              'URLs must be copied VERBATIM from this list.\n') if lang != 'zh' else (
              '图像清单 —— 从论文中抽取的图/表。\n'
              '如需引用请在正文中用 `![说明](url)` 嵌入，URL 必须原样照抄。\n')
    lines = [header]
    for i, img in enumerate(images, 1):
        cap = (img.get('caption') or '').strip().replace('\n', ' ')[:160]
        page = img.get('page', '?')
        src = img.get('source', '')
        url = img.get('url', '')
        if not url:
            continue
        kind = 'table' if 'table' in src else 'figure'
        lines.append(f'{i}. [{kind} · p.{page}] {url}\n   caption: {cap}')
    return '\n'.join(lines)
