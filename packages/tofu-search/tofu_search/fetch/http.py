"""tofu_search.fetch.http — HTTP request execution with SSL, Playwright,
authenticated, and browser-provider fallbacks.

Contains the low-level HTTP request logic, Playwright fallback, authenticated
(cookie-replay) fetch, optional host-browser fallback (via
:mod:`tofu_search.providers`), and the HttpError exception.
"""

import threading
import time
from urllib.parse import urlparse

import requests as _requests_mod

from tofu_search.config import get_config
from tofu_search.fetch.utils import (
    _HAS_LEGACY_SSL,
    _fetch_cache,
    _is_bot_extracted_text,
    _session,
    _session_legacy_ssl,
    _session_no_ssl,
)
from tofu_search.log import get_logger
from tofu_search.providers import get_browser_provider

logger = get_logger(__name__)

# ── Browser fallback concurrency cap ──
# The browser provider dispatches fetches via real browser tabs (parallel).
# The semaphore prevents extreme scenarios (30+ tabs) from overwhelming it.
_BROWSER_CONCURRENCY_LIMIT = 16
_browser_semaphore = threading.Semaphore(_BROWSER_CONCURRENCY_LIMIT)

# ── Browser fallback timeout ──
_BROWSER_TIMEOUT = 15

# ── Domains where browser fallback consistently fails (paywalls, login walls) ──
_BROWSER_SKIP_DOMAINS = frozenset({
    'medium.com',
    'stackademic.com',
    'towardsdatascience.com',
    'betterprogramming.pub',
    'levelup.gitconnected.com',
    'tandfonline.com',
    'preprints.org',
    'ouci.dntb.gov.ua',
    'ieeexplore.ieee.org',
    'dl.acm.org',
    'link.springer.com',
    'wiley.com',
    'onlinelibrary.wiley.com',
    'jstor.org',
    'nature.com',
    'science.org',
})

__all__ = [
    'HttpError',
    'do_request',
    'try_playwright_fallback',
    'try_authenticated_fetch',
    'try_browser_fetch',
]


class HttpError(Exception):
    """HTTP error carrying a status code so callers can differentiate 404 vs 5xx."""
    def __init__(self, status_code, url):
        self.status_code = status_code
        self.url = url
        super().__init__(f'HTTP {status_code} for {url[:120]}')


def do_request(url, timeout, verify=True, legacy_ssl=False, deadline_ts=None):
    """Execute a single GET request, return (resp, raw_bytes) or raise.

    Non-2xx responses raise HttpError so the caller can branch on status.

    Args:
        deadline_ts: Optional absolute ``time.time()`` deadline that bounds the
            body-download loop. The internal ``timeout*3`` wall-time cap is
            reduced to whichever comes first, so a caller enforcing a per-URL
            budget can stop a slow trickle-download early.
    """
    cfg = get_config()
    if legacy_ssl and _HAS_LEGACY_SSL:
        sess = _session_legacy_ssl
    elif verify:
        sess = _session
    else:
        sess = _session_no_ssl
    domain = urlparse(url).netloc[:40]
    logger.debug('[Fetch] GET %s  (timeout=%ds, ssl=%s)', url[:100], timeout, 'on' if verify else 'off')

    # Lazy import: search/__init__ → orchestrator → fetch is a parent-package
    # cycle at import time; inside the function it's a cached lookup.
    from tofu_search.search.proxy_mode import proxy_mode_manager

    # ── Adaptive proxy plan (shared machinery with the search engines) ──
    # No proxy configured → exactly ONE direct attempt, no ``proxies=`` kwarg
    # (byte-identical to the historical behaviour). Otherwise the proxy chain
    # → direct fallback, with per-HOST sticky learning. A connect-level
    # failure (dead proxy / no direct egress / DNS) tries the next path; an
    # answered HTTP status never does. Without this, the whole fetch layer
    # silently went direct even when a proxy was configured — on a host
    # without direct egress every page fetch failed (2026-08-21 incident).
    host_key = 'fetch:' + (urlparse(url).hostname or '')[:60]
    plan = proxy_mode_manager.attempt_plan(host_key, cfg)

    def _open(proxies_kwarg, extra_headers=None):
        kw = {'timeout': (min(timeout, 8), timeout), 'stream': True,
              'allow_redirects': True, 'verify': verify}
        if extra_headers:
            kw['headers'] = extra_headers
        if proxies_kwarg is not None:
            kw['proxies'] = proxies_kwarg
        return sess.get(url, **kw)

    t0 = time.time()
    resp = None
    winning_proxies = None
    last_conn_exc = None
    for mode, proxies_kwarg in plan:
        try:
            resp = _open(proxies_kwarg)
        except _requests_mod.exceptions.ConnectionError as e:
            # ProxyError/SSLError/ConnectTimeout subclass ConnectionError —
            # all connect-level, all worth trying the other path for.
            proxy_mode_manager.record_failure(host_key, mode)
            last_conn_exc = e
            logger.debug('[Fetch] connect-level failure via %s — %s: %.120s',
                         mode, url[:60], e)
            continue
        proxy_mode_manager.record_success(host_key, mode)
        winning_proxies = proxies_kwarg
        break
    if resp is None:
        raise last_conn_exc
    conn_ms = int((time.time() - t0) * 1000)
    if not resp.ok:
        status = resp.status_code
        resp.close()
        logger.debug('[Fetch] resp %d in %dms — %s', status, conn_ms, domain)
        raise HttpError(status, url)
    ct = resp.headers.get('Content-Type', '').lower()
    cl = int(resp.headers.get('Content-Length', 0) or 0)
    if cl > cfg.fetch_max_bytes:
        resp.close()
        raise HttpError(413, url)   # treat as "too large"
    total_deadline = timeout * 3
    _body_stop = t0 + total_deadline
    if deadline_ts is not None:
        _body_stop = min(_body_stop, deadline_ts)
    chunks, dl = [], 0
    oversized = False
    try:
        for chunk in resp.iter_content(65536):
            chunks.append(chunk); dl += len(chunk)
            if dl > cfg.fetch_max_bytes:
                oversized = True
                break
            if time.time() > _body_stop:
                logger.warning('Download exceeded wall time (%.0fs budget) — %s',
                               _body_stop - t0, url[:80])
                break
    except _requests_mod.exceptions.ContentDecodingError as e:
        resp.close()
        logger.warning('ContentDecodingError during download, retrying without br — %s: %s',
                       domain, e)
        resp2 = _open(winning_proxies,
                      extra_headers={'Accept-Encoding': 'gzip, deflate'})
        if not resp2.ok:
            status = resp2.status_code
            resp2.close()
            raise HttpError(status, url) from e
        chunks, dl = [], 0
        try:
            for chunk in resp2.iter_content(65536):
                chunks.append(chunk); dl += len(chunk)
                if dl > cfg.fetch_max_bytes:
                    oversized = True
                    break
                if time.time() > _body_stop:
                    break
        finally:
            resp2.close()
        elapsed_ms = int((time.time() - t0) * 1000)
        logger.debug('[Fetch] resp 200 %sB in %dms (no-br retry) ct=%s — %s',
                     f'{dl:,}', elapsed_ms, ct[:40], domain)
        return resp2, b''.join(chunks)
    except Exception:
        # Any other error mid-stream (pool closed, socket reset, etc.):
        # always close to release the decompressor before GC can crash.
        resp.close()
        raise
    resp.close()
    if oversized:
        logger.warning('Response body too large (%sB, limit %sB) — %s',
                       f'{dl:,}', f'{cfg.fetch_max_bytes:,}', url[:80])
        raise HttpError(413, url)
    elapsed_ms = int((time.time() - t0) * 1000)
    logger.debug('[Fetch] resp 200 %sB in %dms  ct=%s — %s', f'{dl:,}', elapsed_ms, ct[:40], domain)
    return resp, b''.join(chunks)


def try_authenticated_fetch(url, source, max_chars, timeout):
    """Fetch a login-walled URL with a matched auth-source's cookies/proxy.

    ``source`` is a row dict (carries ``cookies`` + optional ``proxy``).
    Drives the Playwright pool's authenticated context. Returns text or None;
    guards against a returned bot/login-wall page and caches success.
    """
    from tofu_search.fetch.playwright_pool import _pw_pool
    cookies = source.get('cookies') or []
    proxy = source.get('proxy') or ''
    domain = source.get('domain', '?')
    logger.info('[Fetch] authenticated fetch domain=%s cookies=%d proxy=%s — %s',
                domain, len(cookies), bool(proxy), url[:100])
    text = _pw_pool.fetch_authenticated(
        url, cookies=cookies, proxy=proxy,
        timeout=max(timeout, 20), max_chars=max_chars)
    if text and len(text) > 50:
        if _is_bot_extracted_text(text):
            logger.info('[Fetch] authenticated fetch returned bot/login text (%d chars), '
                        'discarding — %s (cookies may be expired)', len(text), url[:80])
            return None
        _fetch_cache.put(url, text)
        if max_chars and len(text) > max_chars:
            return text[:max_chars] + '\n[…truncated]'
        return text
    logger.info('[Fetch] authenticated fetch empty — %s (cookies may be expired)', url[:80])
    return None


def try_playwright_fallback(url, max_chars, timeout):
    """Render a page with Playwright (SPA / bot-protection fallback)."""
    from tofu_search.fetch.playwright_pool import _pw_pool
    pw_text = _pw_pool.fetch(url, timeout=max(timeout, 15), max_chars=max_chars)
    if pw_text and len(pw_text) > 50:
        # ── Guard: Playwright may also return bot-protection content ──
        if _is_bot_extracted_text(pw_text):
            logger.debug('[Fetch] Playwright returned bot-protection text (%d chars), discarding — %s',
                         len(pw_text), url[:80])
            return None
        _fetch_cache.put(url, pw_text)
        if max_chars and len(pw_text) > max_chars:
            return pw_text[:max_chars] + '\n[…truncated]'
        return pw_text
    return None


# Browser fallback attempt counter (for periodic summary logging)
_browser_fallback_stats = {'attempts': 0, 'skipped': 0, 'success': 0, 'fail': 0, 'last_log': 0}
_browser_fallback_lock = threading.Lock()


def _log_browser_fallback_stats():
    """Log a periodic summary of browser fallback activity (every 60s)."""
    now = time.time()
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
    """Fetch a page via the host browser provider (if one is registered).

    Useful for 401/403/429/timeout: the server lacks auth/network access but
    the user's browser may have a logged-in session. Returns text or None.
    No-op (returns None) when no browser provider is registered — this is the
    standalone default.
    """
    provider = get_browser_provider()
    if provider is None:
        return None
    try:
        # ── Skip known-paywall domains that never succeed via browser ──
        domain = urlparse(url).netloc.lower()
        bare_domain = domain[4:] if domain.startswith('www.') else domain
        if bare_domain in _BROWSER_SKIP_DOMAINS or any(
                bare_domain.endswith('.' + d) for d in _BROWSER_SKIP_DOMAINS):
            with _browser_fallback_lock:
                _browser_fallback_stats['skipped'] += 1
            logger.debug('[Fetch] Browser fallback skipped (paywall domain %s) — %s',
                         bare_domain, url[:80])
            return None

        if not provider.is_connected():
            with _browser_fallback_lock:
                _browser_fallback_stats['skipped'] += 1
            logger.debug('[Fetch] Browser fallback skipped (provider not connected) — %s', url[:80])
            return None

        # ── Concurrency cap: skip if too many browser fetches in flight ──
        if not _browser_semaphore.acquire(blocking=False):
            with _browser_fallback_lock:
                _browser_fallback_stats['skipped'] += 1
            logger.debug('[Fetch] Browser fallback skipped (concurrency limit %d) — %s',
                         _BROWSER_CONCURRENCY_LIMIT, url[:80])
            return None

        with _browser_fallback_lock:
            _browser_fallback_stats['attempts'] += 1
            attempt_num = _browser_fallback_stats['attempts']
        logger.info('[Fetch] Browser fallback ATTEMPT #%d reason=%s — %s',
                    attempt_num, reason, url[:100])
        bf_t0 = time.time()
        try:
            text = provider.fetch_url(url, max_chars=max_chars, timeout=_BROWSER_TIMEOUT)
        finally:
            _browser_semaphore.release()
        bf_elapsed = time.time() - bf_t0
        if text:
            # ── Guard: browser may also return bot-protection pages ──
            if _is_bot_extracted_text(text):
                logger.debug('[Fetch] Browser fallback returned bot-protection text (%d chars), '
                             'discarding — %s', len(text), url[:80])
                with _browser_fallback_lock:
                    _browser_fallback_stats['fail'] += 1
                return None
            with _browser_fallback_lock:
                _browser_fallback_stats['success'] += 1
            _fetch_cache.put(url, text)
            logger.info('[Fetch] Browser fallback OK in %.1fs — %s (%d chars)',
                        bf_elapsed, url[:80], len(text))
            if max_chars and len(text) > max_chars:
                return text[:max_chars] + '\n[…truncated]'
            return text
        with _browser_fallback_lock:
            _browser_fallback_stats['fail'] += 1
        logger.info('[Fetch] Browser fallback returned empty in %.1fs — %s',
                    bf_elapsed, url[:80])
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
_try_authenticated_fetch = try_authenticated_fetch
_try_browser_fetch = try_browser_fetch
