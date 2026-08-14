"""lib/browser/fetch.py — Fetch a URL using the browser extension."""

import threading
import time
from urllib.parse import urlsplit

from lib.browser.queue import _get_active_client, is_extension_connected, send_browser_command
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['fetch_url_via_browser', 'last_browser_fallback']

_last_fallbacks = {}
_last_fallback_lock = threading.Lock()


def _record_fallback(url, *, ok, client_id='', detail=''):
    try:
        host = (urlsplit(url).hostname or '').lower()
    except ValueError as exc:
        logger.debug('[Browser] fallback URL parse failed: %s', exc)
        host = ''
    try:
        from lib.browser.queue import client_user_id
        user_id = client_user_id(client_id)
    except Exception as exc:
        logger.debug('[Browser] fallback user lookup failed: %s', exc)
        user_id = ''
    with _last_fallback_lock:
        _last_fallbacks[str(user_id or '')] = {
            'at': time.time(), 'host': host, 'ok': bool(ok),
            'client_id': str(client_id or ''), 'detail': str(detail or '')[:160],
        }
        if len(_last_fallbacks) > 64:
            oldest = min(_last_fallbacks,
                         key=lambda key: _last_fallbacks[key].get('at', 0))
            _last_fallbacks.pop(oldest, None)


def last_browser_fallback(*, user_id: str | None = None) -> dict | None:
    with _last_fallback_lock:
        if user_id is not None:
            row = _last_fallbacks.get(str(user_id or ''))
        else:
            row = max(_last_fallbacks.values(),
                      key=lambda item: item.get('at', 0), default=None)
        return dict(row) if row else None


def fetch_url_via_browser(url, max_chars=50000, timeout=25, client_id=None):
    """Fetch a URL using the browser extension (inherits user's session/cookies).

    This is used as a fallback when server-side fetch gets 401/403 — the user
    may be logged in on that site in their browser.

    Returns text content (str) on success, None on failure.
    """
    # Use explicit client_id or fall back to thread-local active client
    _cid = client_id or _get_active_client()
    if not _cid:
        try:
            from lib.browser.protocol import client_protocol
            _cid = client_protocol(None).get('client_id') or None
        except Exception as exc:
            logger.debug('[Browser] default client resolution failed: %s', exc)
            _cid = None
    if not is_extension_connected(_cid):
        _record_fallback(url, ok=False, client_id=_cid, detail='extension_offline')
        return None

    try:
        from lib.browser.access import require_access
        from lib.browser.protocol import client_protocol
        from lib.browser.queue import client_user_id
        info = client_protocol(_cid)
        require_access(client_user_id(_cid), url, access='read',
                       client_id=_cid, profile=info.get('profile', ''))
    except Exception as exc:
        logger.info('[BrowserFetch] access denied url=%s client=%s: %s',
                    url[:100], (_cid or 'any')[:12], exc)
        _record_fallback(url, ok=False, client_id=_cid, detail='access_denied')
        return None

    result, error = send_browser_command('fetch_url', {
        'url': url,
        'maxChars': max_chars,
        'timeoutMs': min(timeout * 1000, 30000),
    }, timeout=timeout, client_id=_cid)

    if error:
        logger.warning('[BrowserFetch] FAILED url=%s client=%s error=%s',
                       url[:100], (_cid or 'any')[:12], str(error)[:200])
        _record_fallback(url, ok=False, client_id=_cid, detail=str(error))
        return None

    if isinstance(result, dict):
        # ── Login-wall gate: a fetch that landed on an SSO/login page is NOT
        # content. Detect it by final URL (netloc-based), engage the cookie-
        # capture chain (probe → login tab → store; auto-approved), and only
        # when a session was captured synchronously retry once inline.
        # Failing that, return None so the caller never mistakes wall text
        # for page content.
        final_url = result.get('url', '') or ''
        if final_url:
            try:
                from lib.browser.access import require_access
                from lib.browser.protocol import client_protocol
                from lib.browser.queue import client_user_id
                info = client_protocol(_cid)
                require_access(client_user_id(_cid), final_url, access='read',
                               client_id=_cid, profile=info.get('profile', ''))
            except Exception as exc:
                logger.info('[BrowserFetch] final redirect denied url=%s: %s',
                            final_url[:100], exc)
                _record_fallback(
                    final_url, ok=False, client_id=_cid,
                    detail='redirect_access_denied')
                return None
            from lib.browser import cookie_capture
            if cookie_capture.looks_like_login_wall(url, final_url, result.get('title', '') or ''):
                captured = cookie_capture.handle_login_wall(url, final_url=final_url)
                if not captured:
                    logger.info('[BrowserFetch] login wall for %s (final=%s) — capture '
                                'flow engaged, failing this round', url[:80], final_url[:80])
                    return None
                logger.info('[BrowserFetch] session captured for %s — retrying fetch inline',
                            url[:80])
                result, error = send_browser_command('fetch_url', {
                    'url': url,
                    'maxChars': max_chars,
                    'timeoutMs': min(timeout * 1000, 30000),
                }, timeout=timeout, client_id=_cid)
                if error or not isinstance(result, dict):
                    logger.warning('[BrowserFetch] post-capture retry FAILED url=%s error=%s',
                                   url[:100], str(error)[:200])
                    return None
                retry_final = result.get('url', '') or url
                try:
                    require_access(
                        client_user_id(_cid), retry_final, access='read',
                        client_id=_cid, profile=info.get('profile', ''))
                except Exception as exc:
                    logger.info('[BrowserFetch] post-capture redirect denied '
                                'url=%s: %s', retry_final[:100], exc)
                    return None

        # ── Prefer server-side extraction from HTML (same pipeline as fetch_page_content) ──
        html = result.get('html', '')
        if html and len(html) > 200:
            try:
                from tofu_search.fetch.html_extract import extract_html_text
                extracted = extract_html_text(html, max_chars, url=url)
                if extracted and len(extracted) > 50:
                    title = result.get('title', '')
                    logger.debug('Browser fetch OK (HTML→extract %s chars) title="%s" — %s',
                                 f'{len(extracted):,}', title[:60], url[:80])
                    _record_fallback(url, ok=True, client_id=_cid,
                                     detail=f'html:{len(extracted)}')
                    return extracted
            except Exception as e:
                logger.warning('Browser fetch HTML extraction failed, falling back to innerText: %s', e)

        # ── Fallback: use raw innerText from extension ──
        text = result.get('text', '')
        if text and len(text) > 50:
            title = result.get('title', '')
            logger.debug('Browser fetch OK (innerText %s chars) title="%s" — %s',
                     f'{len(text):,}', title[:60], url[:80])
            _record_fallback(url, ok=True, client_id=_cid,
                             detail=f'text:{len(text)}')
            return text
        err = result.get('error', '')
        logger.debug('Browser fetch empty for %s%s', url[:80],
                 f' ({err})' if err else '')

    _record_fallback(url, ok=False, client_id=_cid, detail='empty')
    return None
