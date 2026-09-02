"""Extract PDF figures and own their versioned on-disk manifest."""

import base64 as _b64
import json
import os
import time

from lib.log import get_logger

from lib.paper_identity import PAPER_DIR, PAPER_IMG_DIR, _safe_hash_dir

logger = get_logger(__name__)


# Bump this when changing figure-extraction params (resolution, layout, …)
# so old manifests regenerate at the new quality on next access.
_FIG_EXTRACT_VERSION = 7


def load_image_manifest(phash):
    """Load an extracted image manifest by paper_hash, or [] if unknown.

    The manifest lives at ``uploads/papers/images/<phash>/manifest.json`` and
    is written by ``extract_images()`` / ``ensure_paper_images()``. The
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
        # fallback (ensure_paper_images) regenerate at the current quality.
        if isinstance(data, dict) and data.get('version') == _FIG_EXTRACT_VERSION:
            imgs = data.get('images') or []
            if isinstance(imgs, list):
                return [im for im in imgs if isinstance(im, dict) and im.get('url')]
    except Exception as e:
        logger.warning('[Paper:Images] Manifest read failed for %s: %s', phash_safe, e)
    return []


def extract_paper_figures(filepath, phash, *, max_images=30, max_image_width=1800):
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


def ensure_paper_images(filename, phash):
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
    return extract_paper_figures(filepath, phash)


def build_image_manifest(images, lang='en'):
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
