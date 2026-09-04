"""lib/slides/render_png.py — deck pages → PNG previews via headless Chrome.

One browser boot per deck (pages are loaded sequentially into ONE page),
2× device scale for crisp previews. The outputs feed three consumers: the
chat preview grid, the visual-QA stage, and the exporter's chart/icon
rasterisation. Never raises per-page: a page that fails to screenshot is
logged and skipped (the export still ships; the QA sees what it can).
"""

from __future__ import annotations

import math
import os
import re

from lib.log import get_logger
from lib.slides.pptd import Deck
from lib.slides.render_html import render_page_html

logger = get_logger(__name__)

__all__ = ['render_previews', 'render_page_png']

_MAX_RENDER_EDGE = 4096
_MAX_RENDER_DEVICE_PIXELS = 9_000_000
_MAX_RENDER_PAGES = 64
_MAX_PREVIEW_PNG_BYTES = 32 * 1024 * 1024
_MAX_PREVIEW_TOTAL_BYTES = 192 * 1024 * 1024
_PREVIEW_FILE_RE = re.compile(r'^[0-9]{2}\.(?:png|html)$')


def _render_contract(deck: Deck, scale, timeout_ms) -> tuple[float, int]:
    try:
        width = int(deck.width)
        height = int(deck.height)
        resolved_scale = float(scale)
        resolved_timeout = int(timeout_ms)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('invalid slide render geometry or timeout') from exc
    if (not 0 < width <= _MAX_RENDER_EDGE
            or not 0 < height <= _MAX_RENDER_EDGE
            or not math.isfinite(resolved_scale)
            or not 0.25 <= resolved_scale <= 4.0):
        raise ValueError('slide render geometry/scale exceeds the finite limit')
    device_pixels = width * height * resolved_scale * resolved_scale
    if device_pixels > _MAX_RENDER_DEVICE_PIXELS:
        raise ValueError(
            f'slide render requires {device_pixels:.0f} device pixels; limit '
            f'is {_MAX_RENDER_DEVICE_PIXELS}')
    return resolved_scale, max(1000, min(120_000, resolved_timeout))


def _validate_preview_file(path: str, *, retained_bytes: int = 0) -> int:
    size = os.path.getsize(path)
    if not 0 < size <= _MAX_PREVIEW_PNG_BYTES:
        raise ValueError(
            f'preview PNG size {size} is outside 1..{_MAX_PREVIEW_PNG_BYTES}')
    if retained_bytes + size > _MAX_PREVIEW_TOTAL_BYTES:
        raise ValueError(
            f'preview PNG batch exceeds {_MAX_PREVIEW_TOTAL_BYTES} bytes')
    return size


def _raise_if_aborted(abort_check) -> None:
    if abort_check is not None and abort_check():
        raise InterruptedError('slide rendering aborted')


def _settle_render_page(page, timeout_ms: int) -> None:
    """Wait for actual fonts/images/layout readiness, with a short hard cap."""
    settle_limit = max(100, min(2000, int(timeout_ms or 20000) // 4))
    page.evaluate(
        """async (limitMs) => {
          const ready = (async () => {
            if (document.fonts && document.fonts.ready) {
              try { await document.fonts.ready; } catch (_) {}
            }
            const images = [...document.images];
            await Promise.all(images.map(async image => {
              if (!image.complete) {
                await new Promise(resolve => {
                  image.addEventListener('load', resolve, {once: true});
                  image.addEventListener('error', resolve, {once: true});
                });
              }
              if (image.decode) {
                try { await image.decode(); } catch (_) {}
              }
            }));
            await new Promise(resolve => requestAnimationFrame(
              () => requestAnimationFrame(resolve)));
          })();
          await Promise.race([
            ready,
            new Promise(resolve => setTimeout(resolve, limitMs)),
          ]);
        }""",
        settle_limit,
    )


def render_previews(deck: Deck, out_dir: str, *, scale: float = 2.0,
                    keep_html: bool = False, timeout_ms: int = 20000,
                    abort_check=None) -> dict:
    """Render every page to ``{out_dir}/pages/NN.png``. Returns a manifest:
    ``{'ok', 'pages': [{'index', 'png', 'html'?}], 'failed': [...]}``.
    """
    scale, timeout_ms = _render_contract(deck, scale, timeout_ms)
    if len(deck.pages) > _MAX_RENDER_PAGES:
        raise ValueError(
            f'deck has {len(deck.pages)} pages; render limit is '
            f'{_MAX_RENDER_PAGES}')
    from playwright.sync_api import sync_playwright
    try:
        import chromium_env
        chromium_env.ensure_chromium_env(os.environ)
    except Exception as e:
        logger.debug('[Slides] chromium_env shim unavailable: %s', e)

    pages_dir = os.path.join(out_dir, 'pages')
    os.makedirs(pages_dir, exist_ok=True)
    manifest = {'ok': True, 'pages': [], 'failed': []}
    retained_bytes = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(
                viewport={'width': deck.width, 'height': deck.height},
                device_scale_factor=scale)
            for i, pg in enumerate(deck.pages):
                _raise_if_aborted(abort_check)
                name = f'{i + 1:02d}.png'
                png_path = os.path.join(pages_dir, name)
                html_path = os.path.join(pages_dir, f'{i + 1:02d}.html')
                try:
                    html = render_page_html(deck, pg, page_index=i)
                    from lib.json_store import (atomic_output_path,
                                                write_text_atomic)
                    write_text_atomic(html_path, html, fsync=False)
                    page.goto('file://' + html_path, wait_until='load',
                              timeout=timeout_ms)
                    _settle_render_page(page, timeout_ms)
                    _raise_if_aborted(abort_check)
                    with atomic_output_path(png_path) as temporary_png:
                        page.screenshot(path=temporary_png, type='png')
                        png_bytes = _validate_preview_file(
                            temporary_png, retained_bytes=retained_bytes)
                    retained_bytes += png_bytes
                    entry = {'index': i, 'png': png_path}
                    if keep_html:
                        entry['html'] = html_path
                    manifest['pages'].append(entry)
                except Exception as e:
                    if abort_check is not None and abort_check():
                        raise InterruptedError('slide rendering aborted') from e
                    try:
                        os.unlink(png_path)
                    except FileNotFoundError:
                        pass
                    logger.warning('[Slides] page %d preview failed: %s',
                                   i + 1, e)
                    manifest['failed'].append({'index': i, 'error': str(e)})
                finally:
                    if not keep_html:
                        try:
                            os.unlink(html_path)
                        except FileNotFoundError:
                            pass
        finally:
            browser.close()
    retained_names = {
        os.path.basename(entry['png']) for entry in manifest['pages']}
    if keep_html:
        retained_names.update(
            os.path.basename(entry['html']) for entry in manifest['pages'])
    for name in os.listdir(pages_dir):
        if _PREVIEW_FILE_RE.fullmatch(name) and name not in retained_names:
            try:
                os.unlink(os.path.join(pages_dir, name))
            except FileNotFoundError:
                pass
    if manifest['failed']:
        manifest['ok'] = False
    logger.info('[Slides] previews: %d ok, %d failed → %s',
                len(manifest['pages']), len(manifest['failed']), pages_dir)
    return manifest


def render_page_png(deck: Deck, page_index: int, out_path: str, *,
                    scale: float = 2.0, timeout_ms: int = 20000,
                    abort_check=None) -> str:
    """Render ONE page (used by per-page re-render after a chat edit)."""
    scale, timeout_ms = _render_contract(deck, scale, timeout_ms)
    from playwright.sync_api import sync_playwright
    try:
        import chromium_env
        chromium_env.ensure_chromium_env(os.environ)
    except Exception as e:
        logger.debug('[Slides] chromium_env shim unavailable: %s', e)
    pg = deck.pages[page_index]
    _raise_if_aborted(abort_check)
    html = render_page_html(deck, pg, page_index=page_index)
    os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
    from lib.json_store import (atomic_output_path, temporary_output_path,
                                write_text_atomic)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(
                viewport={'width': deck.width, 'height': deck.height},
                device_scale_factor=scale)
            with temporary_output_path(out_path, suffix='.html') as html_path:
                write_text_atomic(html_path, html, fsync=False)
                page.goto('file://' + html_path, wait_until='load',
                          timeout=timeout_ms)
                _settle_render_page(page, timeout_ms)
                _raise_if_aborted(abort_check)
                with atomic_output_path(out_path) as temporary_png:
                    page.screenshot(path=temporary_png, type='png')
                    _validate_preview_file(temporary_png)
        finally:
            browser.close()
    return out_path
