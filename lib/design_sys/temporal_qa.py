"""Multi-timepoint screenshots for motion QA.

A settled-frame screenshot cannot reveal a blank opening, a broken midpoint,
an incorrect animated number, or elements that collide only during entrance.
This module samples one real composition at recipe-selected timeline positions
in a single browser session and builds a labelled contact sheet for the
existing vision-review channel.  Fonts and images are awaited explicitly:
capturing an unloaded fallback font is a false QA result, not a harmless race.
"""

from __future__ import annotations

import io
import os
import re

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['DEFAULT_PROGRESS_POINTS', 'screenshot_timeline_contact_sheet']

DEFAULT_PROGRESS_POINTS = (0.08, 0.5, 0.8, 0.94)

_READINESS_JS = """async () => {
  await document.fonts.ready;
  const fontFailures = Array.from(document.fonts)
    .filter((font) => font.status === 'error')
    .map((font) => font.family || 'unknown-font');
  const imageFailures = [];
  await Promise.all(Array.from(document.images).map(async (img) => {
    try {
      if (typeof img.decode === 'function') await img.decode();
      if (!img.complete || img.naturalWidth <= 0 || img.naturalHeight <= 0) {
        imageFailures.push(img.currentSrc || img.src || 'unknown-image');
      }
    } catch (error) {
      imageFailures.push(img.currentSrc || img.src || 'unknown-image');
    }
  }));
  return {fontFailures, imageFailures, images: document.images.length};
}"""


def screenshot_timeline_contact_sheet(
        scene_dir: str, out_path: str, *, width: int = 0, height: int = 0,
        progresses=DEFAULT_PROGRESS_POINTS, settle_ms: int = 120,
        timeout_ms: int = 20000) -> str:
    """Capture ``progresses`` and write one left-to-right labelled PNG."""
    from PIL import Image, ImageDraw
    from playwright.sync_api import sync_playwright

    try:
        import chromium_env
        chromium_env.ensure_chromium_env(os.environ)
    except Exception as e:
        logger.debug('[TemporalQA] Chromium environment bootstrap skipped: %s', e)

    index = os.path.abspath(os.path.join(scene_dir, 'index.html'))
    if not os.path.isfile(index):
        raise FileNotFoundError(f'no composition at {index}')
    with open(index, encoding='utf-8') as fh:
        head = fh.read(4096)
    if not width or not height:
        mw = re.search(r'data-width="(\d+)"', head)
        mh = re.search(r'data-height="(\d+)"', head)
        width = width or (int(mw.group(1)) if mw else 1080)
        height = height or (int(mh.group(1)) if mh else 1440)

    points = tuple(max(0.0, min(1.0, float(p))) for p in progresses)
    if not points:
        raise ValueError('at least one timeline progress point is required')
    frames = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={'width': width, 'height': height})
            page.goto('file://' + index, wait_until='load', timeout=timeout_ms)
            readiness = page.evaluate(_READINESS_JS)
            font_failures = list((readiness or {}).get('fontFailures') or [])
            image_failures = list((readiness or {}).get('imageFailures') or [])
            if font_failures or image_failures:
                details = []
                if font_failures:
                    details.append('fonts=' + ', '.join(font_failures[:4]))
                if image_failures:
                    details.append('images=' + ', '.join(image_failures[:4]))
                raise RuntimeError('timeline assets not render-ready: '
                                   + '; '.join(details))
            logger.debug('[TemporalQA] render-ready: %d image(s), fonts loaded',
                         int((readiness or {}).get('images') or 0))
            page.wait_for_timeout(120)
            for point in points:
                page.evaluate(
                    '(progress) => { const t = window.__timelines || {};'
                    ' for (const k in t) { try { t[k].progress(progress).pause(); }'
                    ' catch (e) {} } }', point)
                page.wait_for_timeout(settle_ms)
                frames.append(Image.open(io.BytesIO(page.screenshot())).convert('RGB'))
        finally:
            browser.close()

    # Keep the multimodal payload bounded while retaining enough resolution to
    # judge text and chart labels.  The original frame is never modified.
    tile_w = min(540, width)
    scale = tile_w / width
    tile_h = max(1, round(height * scale))
    label_h = 42
    gutter = 12
    sheet = Image.new('RGB',
                      (len(frames) * tile_w + (len(frames) - 1) * gutter,
                       tile_h + label_h), '#151515')
    draw = ImageDraw.Draw(sheet)
    for i, (frame, point) in enumerate(zip(frames, points)):
        x = i * (tile_w + gutter)
        frame = frame.resize((tile_w, tile_h), Image.Resampling.LANCZOS)
        sheet.paste(frame, (x, label_h))
        draw.text((x + 12, 13), f'{round(point * 100)}% timeline', fill='white')
    os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
    sheet.save(out_path, format='PNG', optimize=True)
    return out_path
