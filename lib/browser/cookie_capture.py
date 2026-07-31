"""lib/browser/cookie_capture.py — Login-wall-triggered cookie capture.

When the browser-extension fetch (``lib/browser/fetch.py``) comes back with a
LOGIN WALL instead of content — the page redirected to an SSO/login host —
the user's browser demonstrably lacks a session for that site. This module is
the remediation chain (epic pt_c009ff1c36ba4527, design
docs/FETCH_IDENTITY_PATHS_DESIGN.md):

  1. ask the user ONCE per domain ("允许读取 <domain> 的 cookies 吗?") —
     the grant is persisted, the denial is remembered for a cooldown so a
     declined site is not re-asked on every fetch;
  2. try an IMMEDIATE ``get_cookies(domain)`` — if the user is in fact logged
     in (e.g. consent granted earlier), cookies land in
     :mod:`lib.auth_sources` synchronously and the caller retries inline;
  3. otherwise open the walled page in a FOREGROUND tab (the user logs in —
     usually a QR scan) and poll ``get_cookies`` in a daemon thread until the
     session appears; then persist + audit + push a completion frame so the
     user knows a retry will now succeed.

Security posture (charter-aligned, non-negotiable):
  * never capture without a recorded grant for that exact domain;
  * never read the WHOLE jar — ``get_cookies`` is always domain-scoped;
  * cookie VALUES never enter logs/conversation; only names/counts;
  * every capture is ``audit_log('cookie_capture', …)``;
  * the bridge is credential-authenticated + user-scoped since B0
    (commit 973edd92) — this module must not weaken that.
"""

from __future__ import annotations

import threading
import time
import uuid
from urllib.parse import urlparse

from lib.config_dir import config_path
from lib.json_store import read_json, update_json_atomic
from lib.log import audit_log, get_logger

logger = get_logger(__name__)

__all__ = [
    'looks_like_login_wall',
    'handle_login_wall',
    'request_consent',
    'resolve_consent',
    'pending_consents',
    'consent_grants',
    'revoke_consent',
]

_CONSENT_PATH = config_path('cookie_capture_consent.json')
_CONSENT_TIMEOUT_S = 180        # how long the consent prompt waits for a click
_DENIAL_COOLDOWN_S = 24 * 3600  # a denial suppresses re-asking for this long
_CAPTURE_POLL_S = 3.0           # get_cookies poll cadence while user logs in
_CAPTURE_TIMEOUT_S = 600        # give up waiting for the session after this
_IMMEDIATE_WAIT_S = 12          # bounded inline wait for an existing session

_SSO_HOST_MARKERS = ('sso', 'passport', 'login', 'cas', 'auth')
_SSO_PATH_PREFIXES = ('/sso', '/login', '/signin', '/passport', '/auth')
_LOGIN_TITLE_MARKERS = ('登录', '登陆', 'log in', 'login', 'sign in')

_store_lock = threading.RLock()
_pending_lock = threading.RLock()
_pending: dict[str, dict] = {}          # consent_id → {event, domain, url, created_at, approved}
_capture_threads: dict[str, threading.Thread] = {}  # domain → running capture thread


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
#  Consent store (persistent grants + cooldown denials)
# ══════════════════════════════════════════════════════════

def _load_store() -> dict:
    store = read_json(_CONSENT_PATH, default=None)
    if isinstance(store, dict):
        return store
    return {'version': 1, 'grants': {}, 'denials': {}}


def _save_store(store: dict) -> None:
    update_json_atomic(_CONSENT_PATH, lambda _: store, default=store)


def consent_grants() -> list[dict]:
    """Redacted list of granted domains (for Settings visibility)."""
    with _store_lock:
        grants = dict(_load_store().get('grants', {}))
    return [{'domain': d, 'granted_at': meta.get('ts', 0.0)}
            for d, meta in sorted(grants.items())]


def revoke_consent(domain: str) -> bool:
    """Forget a grant — the next wall for this domain asks again."""
    from lib.auth_sources import normalize_domain
    dom = normalize_domain(domain)
    with _store_lock:
        store = _load_store()
        existed = dom in store.get('grants', {})
        store.get('grants', {}).pop(dom, None)
        if existed:
            _save_store(store)
    if existed:
        audit_log('cookie_capture_consent_revoke', domain=dom)
        logger.info('[CookieCapture] consent revoked domain=%s', dom)
    return existed


def _grant_for(dom: str) -> bool:
    with _store_lock:
        return dom in _load_store().get('grants', {})


def _denial_fresh(dom: str) -> bool:
    with _store_lock:
        meta = _load_store().get('denials', {}).get(dom)
    return bool(meta) and (time.time() - meta.get('ts', 0.0)) < _DENIAL_COOLDOWN_S


def _record_grant(dom: str) -> None:
    with _store_lock:
        store = _load_store()
        store.setdefault('grants', {})[dom] = {'ts': time.time()}
        store.setdefault('denials', {}).pop(dom, None)
        _save_store(store)


def _record_denial(dom: str) -> None:
    with _store_lock:
        store = _load_store()
        store.setdefault('denials', {})[dom] = {'ts': time.time()}
        _save_store(store)


# ══════════════════════════════════════════════════════════
#  Consent prompt (push + REST resolve)
# ══════════════════════════════════════════════════════════

def pending_consents() -> list[dict]:
    """Pending (unanswered) consent prompts — banner restore after reload."""
    with _pending_lock:
        rows = [
            {'id': cid, 'domain': p['domain'], 'url': p['url'],
             'created_at': p['created_at']}
            for cid, p in _pending.items()
        ]
    rows.sort(key=lambda r: r['created_at'])
    return rows


def request_consent(domain: str, url: str, timeout: int = _CONSENT_TIMEOUT_S) -> bool:
    """Ask the user once whether we may capture ``domain``'s cookies.

    Pushes a ``cookie_capture`` frame (the frontend renders an allow/deny
    banner) and blocks until resolved or ``timeout``. A grant is persisted
    (one-time per domain); a denial is remembered for ``_DENIAL_COOLDOWN_S``.
    """
    consent_id = f'cc_{uuid.uuid4().hex[:10]}'
    evt = threading.Event()
    with _pending_lock:
        _pending[consent_id] = {
            'event': evt, 'domain': domain, 'url': url,
            'created_at': time.time(), 'approved': False,
        }
    try:
        from lib.push import push_event
        push_event('cookie_capture', 'consent', {
            'type': 'request', 'id': consent_id, 'domain': domain, 'url': url,
        })
    except Exception as e:
        logger.warning('[CookieCapture] consent push failed (frontend may not '
                       'see the banner; REST pending list still has it): %s', e)
    logger.info('[CookieCapture] consent requested domain=%s id=%s', domain, consent_id)
    audit_log('cookie_capture_consent_request', domain=domain, url=url[:200])

    approved = False
    if evt.wait(timeout=timeout):
        with _pending_lock:
            approved = bool(_pending.get(consent_id, {}).get('approved'))
    else:
        logger.info('[CookieCapture] consent timed out domain=%s id=%s', domain, consent_id)
    with _pending_lock:
        _pending.pop(consent_id, None)

    if approved:
        _record_grant(domain)
        audit_log('cookie_capture_consent_grant', domain=domain)
    else:
        _record_denial(domain)
        audit_log('cookie_capture_consent_deny', domain=domain,
                  reason='rejected' if evt.is_set() else 'timeout')
    return approved


def resolve_consent(consent_id: str, approved: bool) -> bool:
    """Called by the REST endpoint when the user clicks Allow/Deny."""
    with _pending_lock:
        entry = _pending.get(consent_id)
        if not entry:
            logger.warning('[CookieCapture] resolve for unknown consent id=%s', consent_id)
            return False
        entry['approved'] = bool(approved)
        entry['event'].set()
    logger.info('[CookieCapture] consent resolved id=%s approved=%s', consent_id, approved)
    return True


# ══════════════════════════════════════════════════════════
#  Capture orchestration
# ══════════════════════════════════════════════════════════

def _fetch_cookies(dom: str) -> list:
    """Domain-scoped cookie read via the extension ([] on any failure)."""
    try:
        from lib.browser.queue import send_browser_command
        result, error = send_browser_command('get_cookies', {'domain': dom}, timeout=10)
        if error or not isinstance(result, list):
            logger.debug('[CookieCapture] get_cookies domain=%s → %s',
                         dom, (str(error)[:120] if error else 'non-list result'))
            return []
        return [c for c in result if isinstance(c, dict) and c.get('name')]
    except Exception as e:
        logger.warning('[CookieCapture] get_cookies failed domain=%s: %s', dom, e)
        return []


def _store_cookies(dom: str, cookies: list, source: str) -> None:
    from lib.auth_sources import upsert_source
    upsert_source(dom, enabled=True, cookies=cookies)
    names = sorted({str(c.get('name')) for c in cookies})
    audit_log('cookie_capture', domain=dom, source=source, cookie_count=len(cookies))
    logger.info('[CookieCapture] captured %d cookies for %s (source=%s, names=%s)',
                len(cookies), dom, source, names)
    try:
        from lib.push import push_event
        push_event('cookie_capture', 'consent', {
            'type': 'captured', 'domain': dom, 'cookieCount': len(cookies),
        })
    except Exception as e:
        logger.debug('[CookieCapture] captured-push failed: %s', e)


def _probe_no_longer_walled(url: str) -> bool:
    """Re-fetch ``url`` through the extension; True when it no longer walls.

    This is the ONLY session signal that cannot be faked by anonymous
    cookies: the page itself renders content instead of redirecting to SSO.
    """
    try:
        from lib.browser.queue import send_browser_command
        result, error = send_browser_command('fetch_url', {
            'url': url, 'maxChars': 20000, 'timeoutMs': 20000,
        }, timeout=25)
        if error or not isinstance(result, dict):
            logger.debug('[CookieCapture] probe fetch failed for %s: %s',
                         url[:80], str(error)[:120])
            return False
        walled = looks_like_login_wall(url, result.get('url', '') or '',
                                       result.get('title', '') or '')
        text = (result.get('text') or result.get('html') or '')
        return (not walled) and len(text) > 200
    except Exception as e:
        logger.warning('[CookieCapture] probe failed for %s: %s', url[:80], e)
        return False


def _background_capture(dom: str, url: str) -> None:
    """Open the walled page in a FOREGROUND tab; poll until the session lands.

    The poll watches the LOGIN TAB's own URL: the SSO flow redirects that tab
    back to the target site on success, so "tab URL left the SSO family" is
    the completion signal — cookie-counting is not (anonymous cookies exist
    before login too). Only then do we read the domain's cookies and store.
    """
    try:
        from lib.browser.queue import send_browser_command
        result, error = send_browser_command('create_tab', {'url': url, 'active': True}, timeout=15)
        if error or not isinstance(result, dict):
            logger.warning('[CookieCapture] create_tab failed for %s: %s', url[:80], str(error)[:160])
            return
        tab_id = result.get('id')
        logger.info('[CookieCapture] login tab #%s opened for %s — polling for session '
                    '(user logs in in their browser)', tab_id, dom)
        deadline = time.time() + _CAPTURE_TIMEOUT_S
        tab_errors = 0
        while time.time() < deadline:
            time.sleep(_CAPTURE_POLL_S)
            loc, loc_err = send_browser_command(
                'execute_js', {'tabId': int(tab_id), 'code': 'location.href'},
                timeout=10)
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
                cookies = _fetch_cookies(dom)
                if cookies:
                    _store_cookies(dom, cookies, source='extension')
                    return
        logger.info('[CookieCapture] capture timed out for %s after %ds (no session)',
                    dom, _CAPTURE_TIMEOUT_S)
    except Exception as e:
        logger.error('[CookieCapture] background capture failed domain=%s: %s',
                     dom, e, exc_info=True)
    finally:
        with _pending_lock:
            _capture_threads.pop(dom, None)


def handle_login_wall(url: str, final_url: str = '') -> bool:
    """Entry point from ``lib/browser/fetch.py`` when a fetch hit a login wall.

    Returns True ONLY when cookies were captured synchronously (the caller
    then retries the fetch inline). Otherwise kicks the asynchronous chain
    (consent prompt → foreground login tab → poll → store) and returns False
    so this fetch round fails cleanly; the NEXT fetch for the domain then
    succeeds via auth-source replay.
    """
    from lib.auth_sources import match_source, normalize_domain
    from lib.browser.queue import is_extension_connected

    dom = normalize_domain(url)
    if not dom:
        return False
    if not is_extension_connected():
        return False
    existing = match_source(url)
    if existing and time.time() - existing.get('updated_at', 0.0) < 3600:
        # A fresh session is already stored — the wall is likely transient
        # (or the stored cookies JUST failed); re-capturing immediately would
        # loop. Only re-capture when the stored session is stale.
        logger.debug('[CookieCapture] fresh auth-source exists for %s — no capture', dom)
        return False

    with _pending_lock:
        if dom in _capture_threads:
            logger.debug('[CookieCapture] capture already running for %s', dom)
            return False

    if not _grant_for(dom):
        if _denial_fresh(dom):
            logger.debug('[CookieCapture] consent denied recently for %s — skip', dom)
            return False
        if not request_consent(dom, url):
            return False

    # Consent held: VERIFY before storing anything. ``get_cookies(domain)``
    # also returns anonymous tracking cookies, so "non-empty" is NOT proof of
    # a session — storing those would poison auth_sources for an hour (the
    # fresh-source check above would then suppress real capture). The only
    # honest session signal is a re-fetch that no longer walls.
    if _probe_no_longer_walled(url):
        cookies = _fetch_cookies(dom)
        if cookies:
            _store_cookies(dom, cookies, source='extension')
            return True

    # No live session: open the login page for the user and poll in the
    # background. This fetch round fails; the next one succeeds.
    thread = threading.Thread(target=_background_capture, args=(dom, url),
                              name=f'cookie-capture-{dom}', daemon=True)
    with _pending_lock:
        _capture_threads[dom] = thread
    thread.start()
    logger.info('[CookieCapture] async capture started for %s', dom)
    return False
