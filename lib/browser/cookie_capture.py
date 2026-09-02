"""lib/browser/cookie_capture.py — Login-wall-triggered cookie capture.

When the browser-extension fetch (``lib/browser/fetch.py``) comes back with a
LOGIN WALL instead of content — the page redirected to an SSO/login host —
the user's browser demonstrably lacks a session for that site. This module is
the remediation chain (, design
docs/modules/browser_automation.md):

  1. try an IMMEDIATE probe + ``get_cookies(domain)`` — if the user is in
     fact logged in, cookies land in :mod:`lib.auth_sources` synchronously
     and the caller retries inline;
  2. otherwise open the walled page in a FOREGROUND tab (the user logs in —
     usually a QR scan) and poll ``get_cookies`` in a daemon thread until the
     session appears; then persist + audit + push a completion frame so the
     user knows a retry will now succeed.

Capture is AUTOMATIC on a wall (owner decision 2026-08-13: the per-domain
allow/deny consent banner, its grant/denial store and the REST resolve
endpoints were removed — this is a single-tenant self-hosted server, so
asking the owner for their own browser session was pure friction). A
per-domain in-memory cooldown keeps an ignored/failed login tab from
re-opening on every fetch round.

Security posture (non-negotiable):
  * never read the WHOLE jar — ``get_cookies`` is always domain-scoped;
  * cookie VALUES never enter logs/conversation; only names/counts;
  * every capture is ``audit_log('cookie_capture', …)``;
  * the bridge is credential-authenticated + user-scoped since B0
    (commit 973edd92) — this module must not weaken that.
"""

from __future__ import annotations

import threading
import time
from urllib.parse import urlparse

from lib.browser.log_safety import text_for_log, url_for_log
from lib.log import audit_log, get_logger

logger = get_logger(__name__)

__all__ = [
    'looks_like_login_wall',
    'handle_login_wall',
]

_CAPTURE_POLL_S = 3.0           # get_cookies poll cadence while user logs in
_CAPTURE_TIMEOUT_S = 600        # give up waiting for the session after this
_ATTEMPT_COOLDOWN_S = 900       # suppress a second login tab for this long

_SSO_HOST_MARKERS = ('sso', 'passport', 'login', 'cas', 'auth')
_SSO_PATH_PREFIXES = ('/sso', '/login', '/signin', '/passport', '/auth')
_LOGIN_TITLE_MARKERS = ('登录', '登陆', 'log in', 'login', 'sign in')

_capture_lock = threading.RLock()
_capture_threads: dict[tuple[str, str, str], threading.Thread] = {}
_last_attempt: dict[tuple[str, str, str], float] = {}


# ══════════════════════════════════════════════════════════
#  Wall detection (netloc-based — the SSO login URL carries the original
#  page as a redirect_uri query param, so whole-URL substring matching
#  both misses and over-excludes; learned in tests/_authed_fetch_capture.py)
# ══════════════════════════════════════════════════════════

def looks_like_login_wall(target_url: str, final_url: str, title: str = '') -> bool:
    """True when a fetch of ``target_url`` ended on a login/SSO page.

    Conservative by design: a bare cross-domain redirect (CDN, shortener,
    http→https host move) is NOT a wall — we require the final page to look
    like a login surface (host/path/title markers) AND to have left the
    target's registrable host family.
    """
    try:
        t_host = urlparse(target_url).netloc.lower().split(':')[0]
        f_host = urlparse(final_url).netloc.lower().split(':')[0]
        f_path = urlparse(final_url).path.lower()
    except Exception as e:
        logger.debug('[CookieCapture] wall-check URL parse failed: %s', e)
        return False
    if not t_host or not f_host:
        return False

    title_l = (title or '').lower()
    login_surface = (
        any(m in f_host for m in _SSO_HOST_MARKERS)
        or f_path.startswith(_SSO_PATH_PREFIXES)
        or any(m in title_l for m in _LOGIN_TITLE_MARKERS)
    )
    if not login_surface:
        return False
    from lib.auth_sources import _host_matches
    left_family = not (_host_matches(f_host, t_host) or _host_matches(t_host, f_host))
    # Same host: only a login PATH is a wall. The title marker is reserved
    # for cross-domain redirects — a fully rendered same-host page whose
    # title merely mentions "login" (newsletter CTA etc.) is content.
    same_host_login = f_host == t_host and f_path.startswith(_SSO_PATH_PREFIXES)
    return left_family or same_host_login


# ══════════════════════════════════════════════════════════
#  Capture orchestration
# ══════════════════════════════════════════════════════════

def _fetch_cookies(
    dom: str,
    *,
    client_id: str,
    owner_user_id: int | str,
) -> list:
    """Domain-scoped cookie read via the extension ([] on any failure)."""
    try:
        from lib.browser.queue import send_browser_command
        result, error = send_browser_command(
            'get_cookies',
            {'domain': dom},
            timeout=10,
            client_id=client_id,
            owner_user_id=str(owner_user_id),
        )
        if error or not isinstance(result, list):
            logger.debug('[CookieCapture] get_cookies domain=%s → %s',
                         dom, (str(error)[:120] if error else 'non-list result'))
            return []
        return [c for c in result if isinstance(c, dict) and c.get('name')]
    except Exception as e:
        logger.warning('[CookieCapture] get_cookies failed domain=%s: %s', dom, e)
        return []


def _store_cookies(
    dom: str,
    cookies: list,
    source: str,
    *,
    user_id: int | str,
) -> None:
    from lib.auth_sources import upsert_source
    upsert_source(dom, enabled=True, cookies=cookies)
    names = sorted({str(c.get('name')) for c in cookies})
    audit_log('cookie_capture', domain=dom, source=source, cookie_count=len(cookies))
    logger.info('[CookieCapture] captured %d cookies for %s (source=%s, names=%s)',
                len(cookies), dom, source, names)
    try:
        from lib.agent_core.push import push_event
        push_event('cookie_capture', 'consent', {
            'type': 'captured', 'domain': dom, 'cookieCount': len(cookies),
        }, user_id=user_id)
    except Exception as e:
        logger.debug('[CookieCapture] captured-push failed: %s', e)


def _probe_no_longer_walled(
    url: str,
    *,
    client_id: str,
    owner_user_id: int | str,
) -> bool:
    """Re-fetch ``url`` through the extension; True when it no longer walls.

    This is the ONLY session signal that cannot be faked by anonymous
    cookies: the page itself renders content instead of redirecting to SSO.
    """
    try:
        from lib.browser.protocol import BrowserCapability, require_capabilities
        from lib.browser.queue import send_browser_command
        require_capabilities(client_id, [BrowserCapability.FILE_EXPORT])
        result, error = send_browser_command('fetch_url', {
            'url': url, 'maxChars': 20000, 'timeoutMs': 20000,
        }, timeout=25, client_id=client_id,
           owner_user_id=str(owner_user_id))
        if error or not isinstance(result, dict):
            logger.debug('[CookieCapture] probe fetch failed for %s: %s',
                         url_for_log(url), text_for_log(error, max_chars=120))
            return False
        walled = looks_like_login_wall(url, result.get('url', '') or '',
                                       result.get('title', '') or '')
        text = (result.get('text') or result.get('html') or '')
        return (not walled) and len(text) > 200
    except Exception as e:
        logger.warning('[CookieCapture] probe failed for %s: %s',
                       url_for_log(url), text_for_log(e))
        return False


def _background_capture(
    dom: str,
    url: str,
    *,
    client_id: str,
    user_id: int | str,
    capture_key: tuple[str, str, str],
) -> None:
    """Open the walled page in a FOREGROUND tab; poll until the session lands.

    The poll watches the LOGIN TAB's own URL: the SSO flow redirects that tab
    back to the target site on success, so "tab URL left the SSO family" is
    the completion signal — cookie-counting is not (anonymous cookies exist
    before login too). Only then do we read the domain's cookies and store.
    """
    try:
        from lib.browser.queue import send_browser_command
        result, error = send_browser_command(
            'create_tab', {'url': url, 'active': True}, timeout=15,
            client_id=client_id, owner_user_id=str(user_id))
        if error or not isinstance(result, dict):
            logger.warning('[CookieCapture] create_tab failed for %s: %s',
                           url_for_log(url),
                           text_for_log(error, max_chars=160))
            return
        tab_id = result.get('id')
        audit_log('cookie_capture_login_tab', domain=dom,
                  url=url_for_log(url))
        logger.info('[CookieCapture] login tab #%s opened for %s — polling for session '
                    '(user logs in in their browser)', tab_id, dom)
        deadline = time.time() + _CAPTURE_TIMEOUT_S
        tab_errors = 0
        while time.time() < deadline:
            time.sleep(_CAPTURE_POLL_S)
            loc, loc_err = send_browser_command(
                'execute_js', {'tabId': int(tab_id), 'code': 'location.href'},
                timeout=10, client_id=client_id,
                owner_user_id=str(user_id))
            if loc_err:
                tab_errors += 1
                if tab_errors >= 3:
                    logger.info('[CookieCapture] login tab #%s gone (closed?) — aborting '
                                'capture for %s', tab_id, dom)
                    return
                continue
            tab_errors = 0
            tab_url = loc if isinstance(loc, str) else ''
            if tab_url and not looks_like_login_wall(url, tab_url, ''):
                cookies = _fetch_cookies(
                    dom,
                    client_id=client_id,
                    owner_user_id=user_id,
                )
                if cookies:
                    _store_cookies(
                        dom, cookies, source='extension', user_id=user_id)
                    return
        logger.info('[CookieCapture] capture timed out for %s after %ds (no session)',
                    dom, _CAPTURE_TIMEOUT_S)
    except Exception as e:
        logger.error('[CookieCapture] background capture failed domain=%s: %s',
                     dom, text_for_log(e))
    finally:
        with _capture_lock:
            _capture_threads.pop(capture_key, None)


def handle_login_wall(
    url: str,
    final_url: str = '',
    *,
    client_id: str,
    user_id: int | str,
) -> bool:
    """Entry point from ``lib/browser/fetch.py`` when a fetch hit a login wall.

    Returns True ONLY when cookies were captured synchronously (the caller
    then retries the fetch inline). Otherwise kicks the asynchronous chain
    (foreground login tab → poll → store) and returns False so this fetch
    round fails cleanly; the NEXT fetch for the domain then succeeds via
    auth-source replay.
    """
    from lib.auth_sources import match_source, normalize_domain
    from lib.browser.queue import is_extension_connected

    dom = normalize_domain(url)
    if not dom:
        return False
    if not client_id or user_id in (None, ''):
        raise ValueError('login-wall capture requires client_id and user_id')
    if not is_extension_connected(
            client_id, owner_user_id=str(user_id)):
        return False
    capture_key = (str(user_id), str(client_id), dom)
    existing = match_source(url)
    if existing and time.time() - existing.get('updated_at', 0.0) < 3600:
        # A fresh session is already stored — the wall is likely transient
        # (or the stored cookies JUST failed); re-capturing immediately would
        # loop. Only re-capture when the stored session is stale.
        logger.debug('[CookieCapture] fresh auth-source exists for %s — no capture', dom)
        return False

    with _capture_lock:
        if capture_key in _capture_threads:
            logger.debug('[CookieCapture] capture already running for %s', dom)
            return False
        if time.time() - _last_attempt.get(capture_key, 0.0) < _ATTEMPT_COOLDOWN_S:
            logger.debug('[CookieCapture] login-tab cooldown active for %s — skip', dom)
            return False

    # VERIFY before storing anything. ``get_cookies(domain)`` also returns
    # anonymous tracking cookies, so "non-empty" is NOT proof of a session —
    # storing those would poison auth_sources for an hour (the fresh-source
    # check above would then suppress real capture). The only honest session
    # signal is a re-fetch that no longer walls.
    if _probe_no_longer_walled(
            url, client_id=client_id, owner_user_id=user_id):
        cookies = _fetch_cookies(
            dom, client_id=client_id, owner_user_id=user_id)
        if cookies:
            _store_cookies(
                dom, cookies, source='extension', user_id=user_id)
            return True

    # No live session: open the login page for the user and poll in the
    # background. This fetch round fails; the next one succeeds.
    thread = threading.Thread(
                              target=_background_capture,
                              args=(dom, url),
                              kwargs={
                                  'client_id': client_id,
                                  'user_id': user_id,
                                  'capture_key': capture_key,
                              },
                              name=f'cookie-capture-{dom}', daemon=True)
    with _capture_lock:
        _capture_threads[capture_key] = thread
        _last_attempt[capture_key] = time.time()
    thread.start()
    logger.info('[CookieCapture] async capture started for %s', dom)
    return False
