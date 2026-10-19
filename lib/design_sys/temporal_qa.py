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
import math
import os
import re
import tempfile
import time

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['DEFAULT_PROGRESS_POINTS', 'screenshot_timeline_contact_sheet']

DEFAULT_PROGRESS_POINTS = (0.08, 0.5, 0.8, 0.94)
_MAX_SAMPLES = 12
_MAX_FRAME_PIXELS = 9_000_000
_MAX_FRAME_BYTES = 16 * 1024 * 1024

_SEEK_JS = """async ({seconds, duration, timeout}) => {
  const roots = [...document.querySelectorAll('body > [data-composition-id]')];
  if (roots.length !== 1) throw new Error('Expected exactly one composition root');
  const root = roots[0];
  const declared = Number(root.dataset.duration);
  if (!Number.isFinite(declared) || declared <= 0)
    throw new Error('Invalid composition duration');
  if (Math.abs(declared - duration) > 0.001)
    throw new Error('QA duration differs from composition render duration');
  const timeline = (window.__timelines || {})[root.dataset.compositionId];
  if (!timeline || typeof timeline.totalTime !== 'function')
    throw new Error('Composition timeline is not registered or seekable');
  // GSAP progress is relative to animation length, not rendered scene length.
  // totalTime also handles finite repeats; callbacks must run for counters.
  timeline.pause();
  timeline.totalTime(seconds, false);
  if (root.querySelectorAll('video').length > 8)
    throw new Error('Composition exceeds the QA video asset limit');
  const deadline = Date.now() + timeout;
  for (const media of root.querySelectorAll('audio, video')) media.pause();
  const waitEvent = (media, event, action) => new Promise((resolve, reject) => {
    const finish = error => {
      clearTimeout(timer);
      media.removeEventListener(event, ready);
      media.removeEventListener('error', failed);
      error ? reject(error) : resolve();
    };
    const ready = () => finish();
    const failed = () => finish(new Error('Video asset could not be decoded'));
    const timer = setTimeout(() => finish(new Error('Video seek timed out')),
                             Math.max(1, deadline - Date.now()));
    media.addEventListener(event, ready, {once: true});
    media.addEventListener('error', failed, {once: true});
    try { action(); } catch (error) { finish(error); }
  });
  // Background footage must be sampled at the same time as its overlays.
  // Waiting for metadata/seeked prevents QA from repeatedly seeing frame 0.
  for (const video of root.querySelectorAll('video')) {
    if (video.readyState < 1) await waitEvent(video, 'loadedmetadata', () => video.load());
    if (!Number.isFinite(video.duration) || video.duration <= 0)
      throw new Error('Video asset has no finite duration');
    const clip = video.closest('[data-start]');
    const start = Number(video.dataset.start ?? clip?.dataset.start ?? 0);
    const offset = Number(video.dataset.mediaStart ?? video.dataset.playbackStart ?? 0);
    if (!Number.isFinite(start) || !Number.isFinite(offset))
      throw new Error('Video clip has invalid timing');
    const target = Math.max(0, Math.min(video.duration - 0.001, offset + seconds - start));
    if (Math.abs(video.currentTime - target) > 0.001)
      await waitEvent(video, 'seeked', () => { video.currentTime = target; });
  }
}"""

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
        scene_dir: str, out_path: str | None, *, width: int = 0, height: int = 0,
        progresses=DEFAULT_PROGRESS_POINTS, settle_ms: int = 120,
        timeout_ms: int = 20000, duration_s: float | None = None,
        abort_check=None, sample_observer=None, deadline: float | None = None) -> str | bytes:
    """Capture fractions of the declared render duration, with finite bounds.

    Entry point for motion production QA; dependencies are lazy Playwright/PIL.
    Missing timelines, asset errors and failed seeks invalidate the evidence.
    Cancellation preserves any previous contact sheet.

    ``sample_observer(page, seconds, png_bytes)`` optionally records domain
    evidence after each capture. It must not mutate the page; exceptions abort
    capture and use the same cleanup path. Browser ownership stays here.

    ``out_path=None`` returns bounded PNG bytes without publishing a file, for
    callers with their own authorization-aware output owner.
    """
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
    if os.path.getsize(index) > 1024 * 1024:
        raise ValueError('timeline QA composition exceeds HTML byte limit')
    with open(index, encoding='utf-8') as fh:
        head = fh.read(64 * 1024)
    if not width or not height:
        mw = re.search(r'data-width="(\d+)"', head)
        mh = re.search(r'data-height="(\d+)"', head)
        width = width or (int(mw.group(1)) if mw else 1080)
        height = height or (int(mh.group(1)) if mh else 1440)

    if duration_s is None:
        match = re.search(r'data-duration=[\"\']([^\"\']+)[\"\']', head)
        if match is None:
            raise ValueError('composition has no render duration')
        duration_s = float(match.group(1))
    duration_s = float(duration_s)
    points = []
    for point in progresses:
        if len(points) >= _MAX_SAMPLES:
            raise ValueError(f'timeline QA exceeds {_MAX_SAMPLES} samples')
        point = float(point)
        if not math.isfinite(point) or not 0 <= point <= 1:
            raise ValueError('timeline progress must be finite and within 0..1')
        points.append(point)
    if (not points or not math.isfinite(duration_s)
            or not 0 < duration_s <= 3600):
        raise ValueError('timeline QA requires samples and a finite render duration')
    if (not 0 < width <= 4096 or not 0 < height <= 4096
            or width * height > _MAX_FRAME_PIXELS):
        raise ValueError('timeline QA frame exceeds the finite geometry limit')
    timeout_ms = max(1000, min(120_000, int(timeout_ms)))
    settle_ms = max(0, min(1000, int(settle_ms)))
    tile_w = min(540, width)
    tile_h = max(1, round(height * tile_w / width))
    sheet_pixels = (len(points) * tile_w + (len(points) - 1) * 12) * (tile_h + 42)
    if sheet_pixels > _MAX_FRAME_PIXELS:
        tile_w = max(1, int(tile_w * math.sqrt(_MAX_FRAME_PIXELS / sheet_pixels) * 0.95))
        tile_h = max(1, round(height * tile_w / width))

    def check_abort():
        if abort_check is not None and abort_check():
            raise InterruptedError('timeline QA aborted')

        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError('timeline QA deadline exhausted')

    def remaining_ms():
        check_abort()
        if deadline is None:
            return timeout_ms
        return max(1, min(timeout_ms, int((deadline - time.monotonic()) * 1000)))
    check_abort()
    frames = []
    with sync_playwright() as p:
        browser = p.chromium.launch(timeout=remaining_ms())
        try:
            page = browser.new_page(viewport={'width': width, 'height': height})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)[:500]))
            page.goto('file://' + index, wait_until='load', timeout=remaining_ms())
            readiness = page.evaluate(
                'limit => Promise.race([(' + _READINESS_JS + ')(), '
                'new Promise((_, reject) => setTimeout(() => '
                'reject(new Error("timeline assets readiness timed out")), limit))])',
                remaining_ms())
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
            for point in points:
                check_abort()
                page.evaluate(_SEEK_JS, {'seconds': point * duration_s,
                                         'duration': duration_s, 'timeout': remaining_ms()})
                page.wait_for_timeout(settle_ms)
                if errors:
                    raise RuntimeError('timeline runtime failed: ' + '; '.join(errors[:3]))
                raw = page.screenshot(timeout=remaining_ms())
                if len(raw) > _MAX_FRAME_BYTES:
                    raise ValueError('timeline QA screenshot exceeds byte limit')
                check_abort()
                if sample_observer is not None:
                    sample_observer(page, point * duration_s, raw)
                    check_abort()
                with Image.open(io.BytesIO(raw)) as frame:
                    frames.append(frame.convert('RGB').resize(
                        (tile_w, tile_h), Image.Resampling.LANCZOS))
        finally:
            browser.close()

    # Keep the multimodal payload bounded while retaining enough resolution to
    # judge text and chart labels.  The original frame is never modified.
    label_h = 42
    gutter = 12
    sheet = Image.new('RGB',
                      (len(frames) * tile_w + (len(frames) - 1) * gutter,
                       tile_h + label_h), '#151515')
    draw = ImageDraw.Draw(sheet)
    for i, (frame, point) in enumerate(zip(frames, points)):
        x = i * (tile_w + gutter)
        sheet.paste(frame, (x, label_h))
        draw.text((x + 12, 13), f'{point * duration_s:.2f}s / {duration_s:g}s', fill='white')
    if out_path is None:
        check_abort()
        buffer = io.BytesIO()
        sheet.save(buffer, format='PNG', optimize=True)
        check_abort()
        return buffer.getvalue()
    os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
    check_abort()
    with tempfile.NamedTemporaryFile(dir=os.path.dirname(out_path) or '.',
                                     suffix='.png', delete=False) as handle:
        temporary = handle.name
    try:
        sheet.save(temporary, format='PNG', optimize=True)
        check_abort()
        os.replace(temporary, out_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return out_path
