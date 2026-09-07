"""lib/artifacts/pdf_export.py — Server-side PDF rendering.

Routes call ``render_artifact_pdf(artifact_id) -> bytes`` to get a PDF
of an artifact ready to ship as ``application/pdf``.  Implementation
strategy:

  1. Markdown artifacts → ``markdown_it`` (already a project dep) →
     wrapped in a self-contained HTML template with print-friendly CSS.
  2. HTML artifacts → used as-is (after stripping <script> for safety,
     since the PDF runs in our browser pool).
  3. SVG artifacts → embedded in a minimal HTML wrapper.

  4. The wrapped HTML is sent to a Playwright Chromium instance via the
     existing ``tofu_search.fetch.playwright_pool`` worker.  We add a new task
     kind ``'pdf_render'`` so we don't need a second pool / worker
     thread.

Failure modes:

  * Playwright not installed / launch failed → raises ``PdfRenderError``;
    the route returns 503.
  * markdown_it not available → markdown fallback uses a minimal
    plaintext-in-<pre> wrapper (still produces a PDF, just unstyled).
"""

from __future__ import annotations

import re
import time
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


class PdfRenderError(RuntimeError):
    """Raised when PDF rendering fails (Playwright down, oversize, etc.)."""


# ── Constants ─────────────────────────────────────────────────────────
_PRINT_CSS = """
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI',
                 'Helvetica Neue', Arial, 'Noto Sans CJK SC', sans-serif;
    color: #111;
    line-height: 1.55;
    max-width: 780px;
    margin: 0 auto;
    padding: 28px 32px;
  }
  h1, h2, h3, h4 { page-break-after: avoid; line-height: 1.25; }
  h1 { font-size: 26px; margin-top: 0; border-bottom: 1px solid #ddd;
       padding-bottom: 8px; }
  h2 { font-size: 21px; margin-top: 28px; }
  h3 { font-size: 17px; margin-top: 22px; }
  p { margin: 0.7em 0; }
  pre, code {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
  }
  pre {
    background: #f5f5f5;
    padding: 10px 12px;
    border-radius: 4px;
    overflow: auto;
    page-break-inside: avoid;
  }
  code:not(pre code) {
    background: #f0f0f0;
    padding: 1px 4px;
    border-radius: 3px;
  }
  table {
    border-collapse: collapse;
    margin: 14px 0;
    page-break-inside: avoid;
  }
  table th, table td {
    border: 1px solid #ccc;
    padding: 6px 10px;
  }
  table th { background: #f5f5f5; }
  blockquote {
    border-left: 3px solid #ccc;
    margin: 1em 0;
    padding: 0.4em 1em;
    color: #555;
  }
  img, svg { max-width: 100%; height: auto; }
  a { color: #0366d6; text-decoration: none; }
  a:hover { text-decoration: underline; }
"""


def _wrap_html(body: str, title: str = '') -> str:
    """Wrap rendered HTML body in a self-contained doc with print CSS."""
    safe_title = re.sub(r'<[^>]+>', '', title)[:200] or 'Artifact'
    return (
        '<!doctype html><html><head>'
        '<meta charset="utf-8">'
        f'<title>{safe_title}</title>'
        f'<style>{_PRINT_CSS}</style>'
        '</head><body>'
        f'{body}'
        '</body></html>'
    )


def _render_markdown_to_html(md: str) -> str:
    """Best-effort Markdown → HTML.  Falls back to <pre>plaintext."""
    try:
        from markdown_it import MarkdownIt
    except ImportError:
        logger.info('[ArtifactsPDF] markdown_it not available, using plaintext fallback')
        from html import escape as _escape
        return '<pre>' + _escape(md) + '</pre>'
    md_engine = MarkdownIt('commonmark', {
        'breaks': True,
        'html': False,  # do NOT pass through raw HTML in markdown source —
                        # PDF renders go to our browser pool, and this is
                        # one of the few defenses against XSS-via-PDF.
        'linkify': True,
        'typographer': False,
    })
    return md_engine.render(md or '')


def _strip_scripts(html: str) -> str:
    """Remove <script> blocks from raw HTML before sending to Chromium.

    PDF rendering runs in our browser pool, so model-supplied JS would
    execute in the same Chromium process used for web fetches.  We
    deliberately strip rather than rely on CSP because the pool browser
    is shared infrastructure.
    """
    if not html:
        return ''
    return re.sub(r'<script\b[^>]*>.*?</script>', '',
                  html, flags=re.IGNORECASE | re.DOTALL)


def _build_print_html(artifact: dict[str, Any]) -> str:
    """Translate any artifact to a self-contained print-ready HTML document."""
    fmt = artifact.get('format')
    content = artifact.get('content') or ''
    title = artifact.get('title') or ''

    if fmt == 'markdown':
        body = _render_markdown_to_html(content)
        return _wrap_html(body, title)
    if fmt == 'html':
        # Already a full document?  Strip scripts and trust the structure.
        if re.search(r'<html[\s>]', content, re.IGNORECASE):
            return _strip_scripts(content)
        return _wrap_html(_strip_scripts(content), title)
    if fmt == 'svg':
        return _wrap_html(content, title)
    # Unknown format — preserve plaintext.
    from html import escape as _escape
    return _wrap_html('<pre>' + _escape(content) + '</pre>', title)


# ── Playwright bridge ─────────────────────────────────────────────────

# We register a ``pdf_render`` task kind on the existing playwright_pool
# worker thread.  Each task: ((kind, payload), result_q).  The pool's
# worker loop hands non-fetch kinds to ``_do_pdf_render``.  See
# lib/fetch/playwright_pool.py for the dispatch wiring.

_PDF_RENDER_TIMEOUT_SECS = 30


def render_artifact_pdf(artifact_id: str) -> bytes:
    """Return PDF bytes for the given artifact id.

    Raises:
        ArtifactNotFoundError: artifact doesn't exist.
        PdfRenderError: Playwright not available or render failed.
    """
    from lib.artifacts import get_artifact  # late import (avoids cycles)
    artifact = get_artifact(artifact_id)

    t0 = time.monotonic()
    print_html = _build_print_html(artifact)
    logger.info(
        '[ArtifactsPDF] render start id=%s format=%s wrapped_size=%d',
        artifact_id[:8], artifact['format'], len(print_html),
    )

    try:
        from lib.search_runtime import prepare_search_dependency_import
        prepare_search_dependency_import()
        from tofu_search.fetch.playwright_pool import _pw_pool
    except Exception as e:
        raise PdfRenderError(f'Playwright pool unavailable: {e}') from e

    if not _pw_pool._ensure_thread():
        raise PdfRenderError(
            'Playwright thread not ready '
            '(chromium binary missing or launch failed — see logs).'
        )

    import queue as _queue_mod

    result_q: _queue_mod.Queue = _queue_mod.Queue()
    payload = {'html': print_html, 'title': artifact.get('title') or ''}
    _pw_pool._task_q.put((('pdf_render', payload), result_q))
    try:
        result = result_q.get(timeout=_PDF_RENDER_TIMEOUT_SECS + 5)
    except _queue_mod.Empty as e:
        logger.warning('[ArtifactsPDF] worker timeout id=%s', artifact_id[:8])
        raise PdfRenderError('PDF render worker timed out') from e

    if not isinstance(result, (bytes, bytearray)) or not result:
        msg = 'PDF render returned empty result'
        if isinstance(result, dict) and result.get('error'):
            msg = f'PDF render failed: {result["error"]}'
        logger.warning('[ArtifactsPDF] %s id=%s', msg, artifact_id[:8])
        raise PdfRenderError(msg)

    elapsed = time.monotonic() - t0
    logger.info(
        '[ArtifactsPDF] render ok id=%s bytes=%d elapsed=%.2fs',
        artifact_id[:8], len(result), elapsed,
    )
    return bytes(result)
