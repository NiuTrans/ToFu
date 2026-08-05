"""lib/browser/preview.py — Server-side rendered page preview.

Renders an HTML page (a project file or a URL) in the shared Playwright
pool's headless Chromium and returns a ``__screenshot__``-protocol dict:
the rendered screenshot PLUS a console / page-error / failed-request
report in ``_text_fallback`` (which doubles as the degradation text for
text-only models). This is the chrome-devtools-mcp use case — "let the
agent see what the page it wrote looks like when it runs" — riding our
existing pool instead of a raw ``--remote-debugging-port=9222`` port
(which Chrome 136+ refuses on the user's real profile anyway).

Two input modes:

* ``path`` — a project-relative HTML file. The page is served to Chromium
  over a virtual host (``http://tofu-preview.invalid/...``) fulfilled
  straight from the project root, so relative assets AND ES modules work
  (plain ``file://`` blocks module scripts via CORS). No network request
  leaves the pool in this mode: external http(s) subresources are aborted
  and counted, which also closes the SSRF surface of model-written JS.
* ``url`` — a real http(s) URL (e.g. the dev server the user just
  started). Rendered directly; failed requests are reported.

The pool-side work runs in :func:`_do_page_preview`, registered once as a
task kind on the shared pool (tofu_search >= 0.6.1). All feature code
lives here; the pool only gained the generic registry.
"""

from __future__ import annotations

import base64
import io
import mimetypes
import os
import threading
import time
from urllib.parse import unquote, urlsplit

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['render_page_preview']

_TASK_KIND = 'page_preview'
# RFC 2606 .invalid — guaranteed to never resolve, so a fulfil miss can
# never turn into a real network lookup.
_VIRTUAL_ORIGIN = 'http://tofu-preview.invalid'

_CALL_TIMEOUT_S = 45          # caller-side wait for the pool worker
_NAV_TIMEOUT_MS = 15_000      # goto budget (domcontentloaded)
_MAX_WAIT_MS = 15_000         # settle-wait ceiling
_MAX_FILE_BYTES = 32 * 1024 * 1024  # virtual-host fulfilment cap
_MAX_CONSOLE = 50
_MAX_ERRORS = 20
_MAX_FAILED = 20
_TEXT_ITEM_CHARS = 300
_SHOT_MAX_BYTES = 500 * 1024  # JPEG recompress threshold (mirrors extension path)

# Explicit MIME map for the extensions where a wrong Content-Type breaks
# the preview (ES modules are refused outright on a non-JS mime).
_MIME_OVERRIDES = {
    '.js': 'text/javascript', '.mjs': 'text/javascript',
    '.css': 'text/css', '.html': 'text/html', '.htm': 'text/html',
    '.json': 'application/json', '.svg': 'image/svg+xml',
    '.wasm': 'application/wasm', '.map': 'application/json',
}


# ══════════════════════════════════════════════════════════════════════
#  Pool worker side (runs on the dedicated Playwright thread)
# ══════════════════════════════════════════════════════════════════════

def _capped(lst, item, cap):
    if len(lst) < cap:
        lst.append(item)


def _resolve_virtual_root(web_root, url):
    """Map a virtual-host URL to a file under ``web_root`` (or None).

    Pure and Playwright-free so the traversal guard is unit-testable.
    ``realpath`` on BOTH sides collapses symlink escapes: a symlink inside
    the project that points outside resolves outside and is rejected.
    """
    path = unquote(urlsplit(url).path).lstrip('/')
    if not path:
        path = 'index.html'
    root = os.path.realpath(web_root)
    cand = os.path.realpath(os.path.join(root, path))
    if cand != root and not cand.startswith(root + os.sep):
        return None
    if not os.path.isfile(cand):
        return None
    return cand


def _guess_mime(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in _MIME_OVERRIDES:
        return _MIME_OVERRIDES[ext]
    return mimetypes.guess_type(path)[0] or 'application/octet-stream'


def _virtual_host_fulfiller(web_root, blocked_external, missing):
    """Build a Playwright route handler serving ``web_root`` offline."""
    def _fulfill(route):
        request = route.request
        url = request.url or ''
        if not url.lower().startswith(_VIRTUAL_ORIGIN + '/'):
            # External subresource — offline preview means it never loads.
            _capped(blocked_external, url[:_TEXT_ITEM_CHARS], _MAX_FAILED)
            try:
                route.abort()
            except Exception as e:
                logger.debug('[Preview] external abort failed (%s): %s', url[:80], e)
            return
        if request.method not in ('GET', 'HEAD'):
            route.fulfill(status=405, body='method not allowed',
                          content_type='text/plain')
            return
        target = _resolve_virtual_root(web_root, url)
        if target is None:
            _capped(missing, url[:_TEXT_ITEM_CHARS], _MAX_FAILED)
            route.fulfill(status=404, body='not found', content_type='text/plain')
            return
        try:
            size = os.path.getsize(target)
            if size > _MAX_FILE_BYTES:
                route.fulfill(status=413, body='file too large for preview',
                              content_type='text/plain')
                return
            with open(target, 'rb') as fh:
                body = fh.read()
        except OSError as e:
            logger.debug('[Preview] read failed for %s: %s', target, e)
            route.fulfill(status=404, body='read error', content_type='text/plain')
            return
        route.fulfill(status=200, body=body, content_type=_guess_mime(target),
                      headers={'Cache-Control': 'no-store',
                               'X-Content-Type-Options': 'nosniff'})
    return _fulfill


def _do_page_preview(browser, payload):
    """Render one page and harvest screenshot + console + errors.

    Runs on the pool worker thread (registered as task kind
    ``page_preview``). Never raises — the caller-side timeout is the only
    guard against a hung page, and the pool loop wraps us anyway.
    """
    mode = payload.get('mode')
    width = int(payload.get('width') or 1280)
    height = int(payload.get('height') or 800)
    full_page = bool(payload.get('full_page'))
    wait_ms = max(0, min(int(payload.get('wait_ms', 1500)), _MAX_WAIT_MS))
    timeout_ms = int(payload.get('timeout_ms', _NAV_TIMEOUT_MS))

    console_msgs, page_errors, failed_reqs = [], [], []
    blocked_external, missing = [], []
    context = None
    t0 = time.time()
    try:
        context = browser.new_context(
            viewport={'width': width, 'height': height},
            ignore_https_errors=True,
            java_script_enabled=True,   # the point: watch the page RUN
        )
        page = context.new_page()
        page.on('console', lambda m: _capped(
            console_msgs, {'type': m.type, 'text': m.text[:_TEXT_ITEM_CHARS]},
            _MAX_CONSOLE))
        page.on('pageerror', lambda e: _capped(
            page_errors, str(e)[:_TEXT_ITEM_CHARS], _MAX_ERRORS))

        if mode == 'file':
            page.route('**/*', _virtual_host_fulfiller(
                payload['web_root'], blocked_external, missing))
            target = _VIRTUAL_ORIGIN + '/' + payload['entry']
        else:
            target = payload['url']
            page.on('requestfailed', lambda r: _capped(
                failed_reqs,
                {'url': (r.url or '')[:_TEXT_ITEM_CHARS],
                 'error': ((r.failure or '') if isinstance(r.failure, str)
                           else str(r.failure))[:_TEXT_ITEM_CHARS]},
                _MAX_FAILED))

        nav_error = None
        try:
            page.goto(target, timeout=timeout_ms, wait_until='domcontentloaded')
        except Exception as e:
            # A dead dev server / missing file still yields a screenshot of
            # the error page — exactly what the agent needs to see.
            nav_error = str(e)[:_TEXT_ITEM_CHARS]
            logger.info('[Preview] navigation issue for %s: %s', target[:100], nav_error)
        if wait_ms:
            try:
                page.wait_for_timeout(wait_ms)
            except Exception as e:
                logger.debug('[Preview] settle wait failed: %s', e)
        try:
            title = page.title()
        except Exception as e:
            logger.debug('[Preview] title read failed: %s', e)
            title = ''
        shot = page.screenshot(type='jpeg', quality=80, full_page=full_page)
        result = {
            'ok': True,
            'screenshot': shot,
            'title': title,
            'url': page.url,
            'console': console_msgs,
            'page_errors': page_errors,
            'failed_requests': failed_reqs,
            'missing_files': missing,
            'blocked_external': blocked_external,
            'nav_error': nav_error,
            'viewport': [width, height],
            'elapsed_s': round(time.time() - t0, 2),
        }
        logger.info('[Preview] rendered %s in %.1fs — %d console, %d errors, '
                    '%d missing, %d blocked-external, shot=%d bytes',
                    target[:100], time.time() - t0, len(console_msgs),
                    len(page_errors), len(missing), len(blocked_external),
                    len(shot))
        return result
    except Exception as e:
        logger.warning('[Preview] render failed: %s', e, exc_info=True)
        return {'ok': False, 'error': str(e)}
    finally:
        if context:
            try:
                context.close()
            except Exception as e:
                logger.debug('[Preview] context close failed: %s', e)


# ══════════════════════════════════════════════════════════════════════
#  Caller side (any thread)
# ══════════════════════════════════════════════════════════════════════

_registered = False
_register_lock = threading.Lock()


def _register_once():
    """Register the task kind on the shared pool exactly once.

    Raises on tofu_search < 0.6.1 (no ``register_task_kind``) — the caller
    converts that into a model-visible error string, so an older library
    degrades the FEATURE instead of crashing the task.
    """
    global _registered
    if _registered:
        return
    with _register_lock:
        if _registered:
            return
        from tofu_search.fetch.playwright_pool import _pw_pool
        _pw_pool.register_task_kind(_TASK_KIND, _do_page_preview)
        _registered = True


def _compress_shot(jpeg_bytes, full_page):
    """Mirror the extension screenshot budget: >500KB → downscale + q70."""
    if len(jpeg_bytes) <= _SHOT_MAX_BYTES:
        return jpeg_bytes, False
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(jpeg_bytes))
        max_height = 12000 if full_page else 3000
        width, height = img.size
        if height > max_height:
            scale = max_height / height
            img = img.resize((int(width * scale), max_height), Image.LANCZOS)
        out = io.BytesIO()
        img.convert('RGB').save(out, format='JPEG', quality=70)
        return out.getvalue(), True
    except Exception as e:
        logger.warning('[Preview] recompress failed, using original: %s', e)
        return jpeg_bytes, False


def _build_report(payload_desc, res):
    """Assemble the model-facing text report (rides ``_text_fallback``)."""
    lines = [f'Page preview — {payload_desc}']
    lines.append(f'Final URL: {res.get("url", "")}    Title: {res.get("title", "")}')
    vw = res.get('viewport') or [0, 0]
    lines.append(f'Viewport: {vw[0]}x{vw[1]}'
                 + (', full page' if res.get('full_page') else '')
                 + f' (rendered in {res.get("elapsed_s", "?")}s)')
    if res.get('nav_error'):
        lines.append(f'NAVIGATION WARNING: {res["nav_error"]}')
    console = res.get('console') or []
    lines.append(f'Console messages: {len(console)}')
    for m in console[:8]:
        lines.append(f'  [{m.get("type", "log")}] {m.get("text", "")}')
    if len(console) > 8:
        lines.append(f'  … {len(console) - 8} more')
    errors = res.get('page_errors') or []
    lines.append(f'Uncaught page errors: {len(errors)}')
    for e in errors[:8]:
        lines.append(f'  {e}')
    if len(errors) > 8:
        lines.append(f'  … {len(errors) - 8} more')
    failed = res.get('failed_requests') or []
    missing = res.get('missing_files') or []
    blocked = res.get('blocked_external') or []
    if failed:
        lines.append(f'Failed requests: {len(failed)}')
        for r in failed[:8]:
            lines.append(f'  {r.get("url", "")} — {r.get("error", "")}')
    if missing:
        lines.append(f'Files the page asked for but the project does not have: {len(missing)}')
        for u in missing[:8]:
            lines.append(f'  {u}')
    if blocked:
        lines.append(f'External requests blocked (offline preview): {len(blocked)}')
        for u in blocked[:5]:
            lines.append(f'  {u}')
    lines.append('The rendered page is displayed above — analyze the screenshot visually.')
    return '\n'.join(lines)


def render_page_preview(*, project_path=None, path=None, url=None,
                        width=1280, height=800, full_page=False, wait_ms=1500):
    """Render a page preview. Returns a ``__screenshot__`` dict or an
    ``Error: …`` string (the tool-handler contract).

    Args:
        project_path: Workspace root used to resolve ``path``.
        path: Project-relative HTML file (mode 'file').
        url: http(s) URL to render directly (mode 'url').
        width/height: Viewport size.
        full_page: Capture the entire scrollable page.
        wait_ms: Extra settle time after domcontentloaded.
    """
    if bool(path) == bool(url):
        return 'Error: exactly one of "path" or "url" is required.'

    if path:
        if not project_path:
            return 'Error: "path" mode requires an attached project.'
        from lib.project_mod.scanner import _safe_path
        try:
            abs_path = _safe_path(project_path, path)
        except ValueError as e:
            logger.debug('[Preview] path rejected by _safe_path: %s', e)
            return f'Error: {e}'
        # _safe_path guards the lexical path; the realpath pass below guards
        # the symlink case (root itself, or a symlink inside it, resolving
        # elsewhere) so the virtual host can never serve outside the root.
        root = os.path.realpath(project_path)
        abs_path = os.path.realpath(abs_path)
        if abs_path != root and not abs_path.startswith(root + os.sep):
            return f'Error: {path} resolves outside the project root.'
        if not os.path.isfile(abs_path):
            return f'Error: file not found: {path}'
        if os.path.splitext(abs_path)[1].lower() not in ('.html', '.htm'):
            return f'Error: {path} is not an HTML file — preview renders .html/.htm pages.'
        entry = os.path.relpath(abs_path, root).replace(os.sep, '/')
        payload = {
            'mode': 'file',
            'web_root': root,
            'entry': entry,
        }
        desc = path
    else:
        scheme = urlsplit(url).scheme.lower()
        if scheme not in ('http', 'https'):
            return f'Error: only http(s) URLs can be previewed, got: {url[:120]}'
        payload = {'mode': 'url', 'url': url}
        desc = url

    payload.update({
        'width': max(320, min(int(width or 1280), 3840)),
        'height': max(240, min(int(height or 800), 2160)),
        'full_page': bool(full_page),
        'wait_ms': max(0, min(int(wait_ms or 0), _MAX_WAIT_MS)),
        'timeout_ms': _NAV_TIMEOUT_MS,
    })

    try:
        _register_once()
    except Exception as e:
        logger.warning('[Preview] pool registration failed: %s', e)
        return (f'Error: page preview requires tofu-search >= 0.6.1 '
                f'(PlaywrightPool.register_task_kind): {e}')

    try:
        from tofu_search.fetch.playwright_pool import _pw_pool
    except Exception as e:
        logger.warning('[Preview] Playwright pool import failed: %s', e)
        return f'Error: Playwright pool unavailable: {e}'

    if not _pw_pool._ensure_thread():
        return ('Error: headless Chromium is not available on this server '
                '(binary missing or launch failed — see logs).')

    import queue as _queue_mod
    result_q: _queue_mod.Queue = _queue_mod.Queue()
    _pw_pool._task_q.put(((_TASK_KIND, payload), result_q))
    try:
        result = result_q.get(timeout=_CALL_TIMEOUT_S)
    except _queue_mod.Empty:
        logger.warning('[Preview] worker timeout for %s', desc[:100])
        return f'Error: preview render timed out ({_CALL_TIMEOUT_S}s): {desc}'

    if not isinstance(result, dict) or not result.get('ok'):
        msg = (result or {}).get('error', 'render failed') if isinstance(result, dict) else 'render failed'
        return f'Error: preview failed for {desc}: {msg}'

    shot, compressed = _compress_shot(result['screenshot'], bool(full_page))
    b64 = base64.b64encode(shot).decode('ascii')
    result['full_page'] = bool(full_page)
    return {
        '__screenshot__': True,
        'dataUrl': f'data:image/jpeg;base64,{b64}',
        'format': 'jpeg',
        'originalSize': len(result['screenshot']),
        'compressedSize': len(shot),
        'compressionApplied': compressed,
        'fullPage': bool(full_page),
        'width': payload['width'],
        'height': payload['height'],
        '_text_fallback': _build_report(desc, result),
    }
