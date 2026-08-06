"""lib/slides/render_png.py — deck pages → PNG previews via headless Chrome.

One browser boot per deck (pages are loaded sequentially into ONE page),
2× device scale for crisp previews. The outputs feed three consumers: the
chat preview grid, the visual-QA stage, and the exporter's chart/icon
rasterisation. Never raises per-page: a page that fails to screenshot is
logged and skipped (the export still ships; the QA sees what it can).
"""

from __future__ import annotations

import os

from lib.log import get_logger
from lib.slides.pptd import Deck
from lib.slides.render_html import render_page_html

logger = get_logger(__name__)

__all__ = ['render_previews', 'render_page_png']


def render_previews(deck: Deck, out_dir: str, *, scale: float = 2.0,
                    keep_html: bool = False, timeout_ms: int = 20000) -> dict:
    """Render every page to ``{out_dir}/pages/NN.png``. Returns a manifest:
    ``{'ok', 'pages': [{'index', 'png', 'html'?}], 'failed': [...]}``.
    """
    from playwright.sync_api import sync_playwright
    try:
        import chromium_env
        chromium_env.ensure_chromium_env(os.environ)
    except Exception as e:
        logger.debug('[Slides] chromium_env shim unavailable: %s', e)

    pages_dir = os.path.join(out_dir, 'pages')
    os.makedirs(pages_dir, exist_ok=True)
    manifest = {'ok': True, 'pages': [], 'failed': []}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(
                viewport={'width': deck.width, 'height': deck.height},
                device_scale_factor=scale)
            for i, pg in enumerate(deck.pages):
                name = f'{i + 1:02d}.png'
                png_path = os.path.join(pages_dir, name)
                try:
                    html = render_page_html(deck, pg, page_index=i)
                    html_path = os.path.join(pages_dir, f'{i + 1:02d}.html')
                    with open(html_path, 'w', encoding='utf-8') as fh:
                        fh.write(html)
                    page.goto('file://' + html_path, wait_until='load',
                              timeout=timeout_ms)
                    page.wait_for_timeout(400)   # fonts/images settle
                    page.screenshot(path=png_path)
                    entry = {'index': i, 'png': png_path}
                    if keep_html:
                        entry['html'] = html_path
                    else:
                        os.unlink(html_path)
                    manifest['pages'].append(entry)
                except Exception as e:
                    logger.warning('[Slides] page %d preview failed: %s',
                                   i + 1, e)
                    manifest['failed'].append({'index': i, 'error': str(e)})
        finally:
            browser.close()
    if manifest['failed']:
        manifest['ok'] = False
    logger.info('[Slides] previews: %d ok, %d failed → %s',
                len(manifest['pages']), len(manifest['failed']), pages_dir)
    return manifest


def render_page_png(deck: Deck, page_index: int, out_path: str, *,
                    scale: float = 2.0, timeout_ms: int = 20000) -> str:
    """Render ONE page (used by per-page re-render after a chat edit)."""
    from playwright.sync_api import sync_playwright
    try:
        import chromium_env
        chromium_env.ensure_chromium_env(os.environ)
    except Exception as e:
        logger.debug('[Slides] chromium_env shim unavailable: %s', e)
    pg = deck.pages[page_index]
    html = render_page_html(deck, pg, page_index=page_index)
    os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(
                viewport={'width': deck.width, 'height': deck.height},
                device_scale_factor=scale)
            tmp = out_path + '.html'
            with open(tmp, 'w', encoding='utf-8') as fh:
                fh.write(html)
            page.goto('file://' + tmp, wait_until='load', timeout=timeout_ms)
            page.wait_for_timeout(400)
            page.screenshot(path=out_path)
            os.unlink(tmp)
        finally:
            browser.close()
    return out_path
