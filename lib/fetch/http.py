"""lib/fetch/http.py — HTTP request execution with SSL fallback and browser fallback.

Contains the low-level HTTP request logic, Playwright fallback,
browser extension fallback, and the _HttpError exception.
"""

import time
from urllib.parse import urlparse

import requests as _requests_mod

import lib as _lib  # module ref for hot-reload
from lib.fetch.utils import (
    _HAS_LEGACY_SSL,
    _fetch_cache,
    _session,
    _session_legacy_ssl,
    _session_no_ssl,
)
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'HttpError',
    'do_request',
    'try_playwright_fallback',
    'try_browser_fetch',
]


class HttpError(Exception):
    """携带 HTTP 状态码的异常, 供调用方区分 404 vs 5xx 等."""
    def __init__(self, status_code, url):
        self.status_code = status_code
        self.url = url
        super().__init__(f'HTTP {status_code} for {url[:120]}')


def do_request(url, timeout, verify=True, legacy_ssl=False):
    """执行单次 GET 请求, 返回 (resp, raw_bytes) 或抛异常。
    非 2xx 响应抛出 HttpError 以便调用方根据状态码做不同处理。"""
    if legacy_ssl and _HAS_LEGACY_SSL:
        sess = _session_legacy_ssl
    elif verify:
        sess = _session
    else:
        sess = _session_no_ssl
    domain = urlparse(url).netloc[:40]
    logger.debug('→ GET %s  (timeout=%ds, ssl=%s)', url[:100], timeout, '✓' if verify else '✗')
    t0 = time.time()
    resp = sess.get(url, timeout=(min(timeout, 8), timeout),
                    stream=True, allow_redirects=True, verify=verify)
    conn_ms = int((time.time() - t0) * 1000)
    if not resp.ok:
        status = resp.status_code
        resp.close()
        logger.debug('← %d in %dms — %s', status, conn_ms, domain)
        raise HttpError(status, url)
    ct = resp.headers.get('Content-Type', '').lower()
    cl = int(resp.headers.get('Content-Length', 0) or 0)
    if cl > _lib.FETCH_MAX_BYTES:
        resp.close()
        raise HttpError(413, url)   # treat as "too large"
    total_deadline = timeout * 3
    chunks, dl = [], 0
    oversized = False
    try:
        for chunk in resp.iter_content(65536):
            chunks.append(chunk); dl += len(chunk)
            if dl > _lib.FETCH_MAX_BYTES:
                oversized = True
                break
            if time.time() - t0 > total_deadline:
                logger.warning('Download exceeded %ss wall time — %s', total_deadline, url[:80])
                break
    except _requests_mod.exceptions.ContentDecodingError as e:
        # Brotli / gzip decode failure mid-stream — retry without Accept-Encoding
        resp.close()
        logger.warning('ContentDecodingError during download, retrying without br — %s: %s',
                       domain, e)
        sess_retry = sess
        resp2 = sess_retry.get(
            url, timeout=(min(timeout, 8), timeout),
            stream=True, allow_redirects=True, verify=verify,
            headers={'Accept-Encoding': 'gzip, deflate'},
        )
        if not resp2.ok:
            resp2.close()
            raise HttpError(resp2.status_code, url)
        chunks, dl = [], 0
        for chunk in resp2.iter_content(65536):
            chunks.append(chunk); dl += len(chunk)
            if dl > _lib.FETCH_MAX_BYTES:
                oversized = True
                break
            if time.time() - t0 > total_deadline:
                break
        elapsed_ms = int((time.time() - t0) * 1000)
        logger.debug('← 200 %sB in %dms (no-br retry) ct=%s — %s',
                     f'{dl:,}', elapsed_ms, ct[:40], domain)
        return resp2, b''.join(chunks)
    if oversized:
        resp.close()
        logger.warning('Response body too large (%sB, limit %sB) — %s',
                       f'{dl:,}', f'{_lib.FETCH_MAX_BYTES:,}', url[:80])
        raise HttpError(413, url)
    elapsed_ms = int((time.time() - t0) * 1000)
    logger.debug('← 200 %sB in %dms  ct=%s — %s', f'{dl:,}', elapsed_ms, ct[:40], domain)
    return resp, b''.join(chunks)


def try_playwright_fallback(url, max_chars, timeout):
    """尝试用 Playwright 渲染页面获取内容 (SPA / 反爬 fallback)。"""
    from lib.fetch.playwright_pool import _pw_pool
    pw_text = _pw_pool.fetch(url, timeout=max(timeout, 15), max_chars=max_chars)
    if pw_text and len(pw_text) > 50:
        _fetch_cache.put(url, pw_text)
        if max_chars and len(pw_text) > max_chars:
            return pw_text[:max_chars] + '\n[…truncated]'
        return pw_text
    return None


# Browser fallback attempt counter (for periodic summary logging)
_browser_fallback_stats = {'attempts': 0, 'skipped': 0, 'success': 0, 'fail': 0, 'last_log': 0}
_browser_fallback_lock = __import__('threading').Lock()


def _log_browser_fallback_stats():
    """Log a periodic summary of browser fallback activity (every 60s)."""
    import time as _time
    now = _time.time()
    with _browser_fallback_lock:
        if now - _browser_fallback_stats['last_log'] < 60:
            return
        stats = dict(_browser_fallback_stats)
        _browser_fallback_stats['last_log'] = now
    if stats['attempts'] > 0:
        logger.info('[Fetch] Browser fallback stats (last 60s): '
                    'attempts=%d success=%d fail=%d skipped=%d',
                    stats['attempts'], stats['success'],
                    stats['fail'], stats['skipped'])


def try_browser_fetch(url, max_chars, reason='unknown'):
    """尝试通过浏览器扩展获取页面 (利用用户已登录的 session/cookie)。

    适用于 401/403 场景：服务端没有认证信息，但用户在浏览器中已登录该站点。
    浏览器扩展会在后台标签页打开 URL，提取文本后关闭标签页。

    Args:
        url: URL to fetch.
        max_chars: Maximum characters to return.
        reason: Why the fallback was triggered (e.g. 'HTTP 429', 'timeout',
                'ConnectionError'). Logged at INFO level for diagnostics.
    """
    try:
        from lib.browser import fetch_url_via_browser, is_extension_connected
        if not is_extension_connected():
            with _browser_fallback_lock:
                _browser_fallback_stats['skipped'] += 1
            logger.debug('[Fetch] Browser fallback skipped (extension not connected) — %s', url[:80])
            return None
        with _browser_fallback_lock:
            _browser_fallback_stats['attempts'] += 1
            attempt_num = _browser_fallback_stats['attempts']
        logger.info('[Fetch] Browser fallback ATTEMPT #%d reason=%s — %s',
                    attempt_num, reason, url[:100])
        text = fetch_url_via_browser(url, max_chars=max_chars, timeout=25)
        if text:
            with _browser_fallback_lock:
                _browser_fallback_stats['success'] += 1
            _fetch_cache.put(url, text)
            if max_chars and len(text) > max_chars:
                return text[:max_chars] + '\n[…truncated]'
            return text
        with _browser_fallback_lock:
            _browser_fallback_stats['fail'] += 1
        logger.info('[Fetch] Browser fallback returned empty — %s', url[:80])
        _log_browser_fallback_stats()
        return None
    except Exception as e:
        with _browser_fallback_lock:
            _browser_fallback_stats['fail'] += 1
        logger.error('[Fetch] Browser fallback error — %s: %s', url[:80], e, exc_info=True)
        _log_browser_fallback_stats()
        return None


# Backward-compatible aliases (originally _-prefixed private names)
_HttpError = HttpError
_do_request = do_request
_try_playwright_fallback = try_playwright_fallback
_try_browser_fetch = try_browser_fetch
