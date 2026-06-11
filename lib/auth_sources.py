"""lib/auth_sources.py — Per-domain authenticated-fetch source store.

Some sites (Xiaohongshu / RED, Weibo, Zhihu, …) serve a login wall to
anonymous server-side requests: a plain HTTP GET returns an SPA shell, not
content, and their anti-bot signing rotates too fast to reimplement. The
only robust way to read them server-side is to replay a *logged-in browser
session* — i.e. drive Playwright with the user's own cookies (and, when the
host bans datacenter IPs, an optional proxy).

This module is the persistence + lookup layer for those per-domain
credentials. It is deliberately generic: Xiaohongshu is merely the first
entry. The fetch pipeline (``tofu_search.fetch``, via the chatui auth-source
provider seam) calls :func:`match_source`
for every URL; when an *enabled* source matches the host, the request is
routed through ``tofu_search.fetch.playwright_pool`` ``fetch_authenticated``
with the stored cookies + proxy instead of the anonymous path.

Persistence
-----------
``data/config/auth_sources.json`` via :mod:`lib.json_store` (atomic, locked)::

    {
      "version": 1,
      "sources": [
        {
          "domain":     "xiaohongshu.com",   # bare registrable host (no scheme)
          "label":      "Xiaohongshu",
          "enabled":    true,
          "cookies":    [{"name": "web_session", "value": "…",
                          "domain": ".xiaohongshu.com", "path": "/"}],
          "proxy":      "http://user:pass@host:port",   # optional, '' = none
          "updated_at": 1701000000.0
        }
      ]
    }

Cookies are stored in Playwright's cookie shape. Operators usually supply
them either as a raw ``Cookie:`` header copied from devtools (parsed by
:func:`parse_cookie_header`) or captured by the interactive-login flow,
which hands us a ``storage_state`` (normalised by :func:`cookies_from_storage_state`).

Security
--------
Cookie *values* are session secrets. They live in plaintext on disk (the
fetch path needs them) under ``data/config/`` — which ``export.py`` already
excludes wholesale — and the public listing (:func:`list_sources`) never
echoes a value back, only a count + ``updated_at``.

Public API
----------
  list_sources()                         → list[dict]   (redacted, no values)
  get_source(domain)                     → dict | None  (FULL, incl. cookies)
  match_source(url)                      → dict | None  (FULL; enabled only)
  upsert_source(domain, **fields)        → dict         (redacted)
  set_enabled(domain, enabled)           → bool
  delete_source(domain)                  → bool
  parse_cookie_header(raw, domain)       → list[dict]
  cookies_from_storage_state(state, …)   → list[dict]
"""

from __future__ import annotations

import threading
import time
from typing import Optional
from urllib.parse import urlparse

from lib.config_dir import config_path
from lib.json_store import read_json, update_json_atomic
from lib.log import audit_log, get_logger

logger = get_logger(__name__)

__all__ = [
    'list_sources',
    'get_source',
    'match_source',
    'upsert_source',
    'set_enabled',
    'delete_source',
    'parse_cookie_header',
    'cookies_from_storage_state',
    'normalize_domain',
    'DEFAULT_SOURCES',
]

_STORE_PATH = config_path('auth_sources.json')
_STORE_VERSION = 1
_MAX_SOURCES = 64
_MAX_COOKIES_PER_SOURCE = 256

_lock = threading.RLock()
_cache: list[dict] = []
_cache_loaded = False


# ── Default catalog ─────────────────────────────────────────────────
# Shipped disabled + cookie-less. They make the sites show up in the
# Settings panel so a user can "connect" them, and they prime the fetch
# router to treat the host as login-walled (skip the doomed anonymous
# attempt) ONLY once enabled with cookies. A source with no cookies is
# always treated as not-configured regardless of ``enabled``.
DEFAULT_SOURCES: list[dict] = [
    {'domain': 'xiaohongshu.com', 'label': 'Xiaohongshu / RED',
     'aliases': ['xhslink.com']},
]


def normalize_domain(value: str) -> str:
    """Reduce a URL or host to its bare lowercase host (no scheme/port/path).

    ``https://www.Xiaohongshu.com/explore`` → ``xiaohongshu.com``.
    A leading ``www.`` is stripped; everything else is preserved so
    ``open.feishu.cn`` stays distinct from ``feishu.cn``.
    """
    if not value:
        return ''
    raw = value.strip().lower()
    if '://' in raw:
        raw = urlparse(raw).netloc or raw
    raw = raw.split('/')[0].split('@')[-1].split(':')[0].strip()
    if raw.startswith('www.'):
        raw = raw[4:]
    return raw


def _host_matches(host: str, domain: str) -> bool:
    """True if ``host`` equals ``domain`` or is a sub-domain of it."""
    return host == domain or host.endswith('.' + domain)


def parse_cookie_header(raw: str, domain: str) -> list[dict]:
    """Parse a raw ``Cookie:`` header string into Playwright cookie dicts.

    ``"a=1; b=2"`` → ``[{name:'a', value:'1', domain:'.<domain>', path:'/'},
    …]``. This is the format a user copies from devtools → Network → any
    request → Request Headers → Cookie. Returns ``[]`` on empty/garbage
    input (logged at debug).
    """
    out: list[dict] = []
    if not raw or not raw.strip():
        return out
    dom = normalize_domain(domain)
    cookie_domain = '.' + dom if dom else ''
    # Strip an accidental leading "Cookie:" prefix the user may have pasted.
    text = raw.strip()
    if text.lower().startswith('cookie:'):
        text = text.split(':', 1)[1].strip()
    for pair in text.split(';'):
        pair = pair.strip()
        if not pair or '=' not in pair:
            continue
        name, _, value = pair.partition('=')
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        out.append({
            'name': name,
            'value': value,
            'domain': cookie_domain or dom,
            'path': '/',
        })
    if not out:
        logger.debug('[AuthSrc] cookie header parsed to 0 cookies for domain=%s', dom)
    return out[:_MAX_COOKIES_PER_SOURCE]


def cookies_from_storage_state(state, domain: Optional[str] = None) -> list[dict]:
    """Extract Playwright cookie dicts from a ``storage_state`` payload.

    The interactive-login flow (headful persistent context) yields a
    ``storage_state`` dict ``{"cookies": [...], "origins": [...]}``. We keep
    only the cookie list, optionally filtered to ``domain`` (and its
    sub-domains) so connecting one site can't smuggle in another's cookies.
    """
    if not isinstance(state, dict):
        return []
    cookies = state.get('cookies')
    if not isinstance(cookies, list):
        return []
    dom = normalize_domain(domain) if domain else ''
    out: list[dict] = []
    for c in cookies:
        if not isinstance(c, dict) or not c.get('name'):
            continue
        if dom:
            cdom = str(c.get('domain', '')).lstrip('.').lower()
            if not (cdom == dom or cdom.endswith('.' + dom) or dom.endswith('.' + cdom)):
                continue
        out.append(c)
    return out[:_MAX_COOKIES_PER_SOURCE]


def _ensure_loaded() -> None:
    global _cache_loaded
    if _cache_loaded:
        return
    with _lock:
        if _cache_loaded:
            return
        store = read_json(_STORE_PATH, default=None)
        rows: list[dict] = []
        if isinstance(store, dict) and isinstance(store.get('sources'), list):
            rows = [r for r in store['sources']
                    if isinstance(r, dict) and r.get('domain')]
        # Merge in any default sources not already persisted, so they appear
        # in the Settings catalog. Defaults arrive disabled + cookie-less.
        known = {r['domain'] for r in rows}
        for d in DEFAULT_SOURCES:
            if d['domain'] not in known:
                rows.append({
                    'domain': d['domain'],
                    'label': d.get('label', d['domain']),
                    'aliases': list(d.get('aliases', [])),
                    'enabled': False,
                    'cookies': [],
                    'proxy': '',
                    'updated_at': 0.0,
                })
        _cache.clear()
        _cache.extend(rows)
        _cache_loaded = True
        logger.info('[AuthSrc] loaded %d source(s) from %s', len(_cache), _STORE_PATH)


def _persist() -> None:
    payload = {'version': _STORE_VERSION, 'sources': list(_cache)}
    update_json_atomic(_STORE_PATH, lambda _: payload, default=payload)


def _redact(row: dict) -> dict:
    """Public view: replace cookie list with a count, drop raw values."""
    out = dict(row)
    cookies = out.get('cookies') or []
    out['cookie_count'] = len(cookies)
    out['has_cookies'] = bool(cookies)
    out.pop('cookies', None)
    proxy = out.get('proxy') or ''
    out['has_proxy'] = bool(proxy)
    # Show only the proxy host, never embedded credentials.
    if proxy:
        try:
            out['proxy_hint'] = urlparse(proxy).hostname or '(set)'
        except Exception as e:
            logger.debug('[AuthSrc] proxy hint parse failed: %s', e)
            out['proxy_hint'] = '(set)'
    else:
        out['proxy_hint'] = ''
    out.pop('proxy', None)
    return out


def list_sources() -> list[dict]:
    """All configured sources, redacted (no cookie values / proxy creds)."""
    _ensure_loaded()
    with _lock:
        rows = [dict(r) for r in _cache]
    rows.sort(key=lambda r: (not r.get('enabled'), r.get('label', r.get('domain', ''))))
    return [_redact(r) for r in rows]


def get_source(domain: str) -> Optional[dict]:
    """Internal lookup by exact domain — returns the FULL row (incl. cookies)."""
    dom = normalize_domain(domain)
    if not dom:
        return None
    _ensure_loaded()
    with _lock:
        for r in _cache:
            if r.get('domain') == dom:
                return dict(r)
    return None


def match_source(url: str) -> Optional[dict]:
    """Return the enabled, cookie-bearing source governing ``url``'s host.

    Matches the host against each source's ``domain`` and ``aliases``
    (sub-domains included). A source with no cookies is treated as
    not-configured and never matched, so enabling a site without
    supplying credentials cannot break its fetch.
    """
    try:
        host = urlparse(url).netloc.lower().split(':')[0]
    except Exception as e:
        logger.debug('[AuthSrc] match_source host parse failed for %.80s: %s', url, e)
        return None
    if not host:
        return None
    _ensure_loaded()
    with _lock:
        for r in _cache:
            if not r.get('enabled') or not r.get('cookies'):
                continue
            domains = [r.get('domain', '')] + list(r.get('aliases', []))
            if any(d and _host_matches(host, d) for d in domains):
                return dict(r)
    return None


def upsert_source(domain: str, *, label: Optional[str] = None,
                  enabled: Optional[bool] = None,
                  cookies: Optional[list] = None,
                  cookie_header: Optional[str] = None,
                  proxy: Optional[str] = None,
                  aliases: Optional[list] = None) -> dict:
    """Create or update a source. Returns the redacted row.

    Only the fields you pass are touched (None = leave unchanged), except
    that supplying ``cookie_header`` parses + replaces ``cookies``. Raises
    ``ValueError`` on a bad domain or when the source cap is hit.
    """
    dom = normalize_domain(domain)
    if not dom:
        raise ValueError('domain is required')

    if cookie_header is not None:
        cookies = parse_cookie_header(cookie_header, dom)

    _ensure_loaded()
    with _lock:
        row = None
        for r in _cache:
            if r.get('domain') == dom:
                row = r
                break
        if row is None:
            if len(_cache) >= _MAX_SOURCES:
                raise ValueError(f'source quota reached (max {_MAX_SOURCES})')
            row = {'domain': dom, 'label': dom, 'aliases': [],
                   'enabled': False, 'cookies': [], 'proxy': '', 'updated_at': 0.0}
            _cache.append(row)

        if label is not None:
            row['label'] = str(label).strip()[:80] or dom
        if aliases is not None:
            row['aliases'] = [normalize_domain(a) for a in aliases if normalize_domain(a)]
        if cookies is not None:
            row['cookies'] = list(cookies)[:_MAX_COOKIES_PER_SOURCE]
        if proxy is not None:
            row['proxy'] = str(proxy).strip()
        if enabled is not None:
            row['enabled'] = bool(enabled)
        row['updated_at'] = time.time()
        _persist()
        snapshot = dict(row)

    audit_log('auth_source_upsert', domain=dom,
              enabled=snapshot.get('enabled'),
              cookie_count=len(snapshot.get('cookies') or []),
              has_proxy=bool(snapshot.get('proxy')))
    logger.info('[AuthSrc] upsert domain=%s enabled=%s cookies=%d proxy=%s',
                dom, snapshot.get('enabled'),
                len(snapshot.get('cookies') or []), bool(snapshot.get('proxy')))
    return _redact(snapshot)


def set_enabled(domain: str, enabled: bool) -> bool:
    """Toggle a source on/off. Returns True iff the source exists."""
    dom = normalize_domain(domain)
    _ensure_loaded()
    with _lock:
        for r in _cache:
            if r.get('domain') == dom:
                r['enabled'] = bool(enabled)
                r['updated_at'] = time.time()
                _persist()
                audit_log('auth_source_toggle', domain=dom, enabled=bool(enabled))
                logger.info('[AuthSrc] toggle domain=%s enabled=%s', dom, bool(enabled))
                return True
    return False


def delete_source(domain: str) -> bool:
    """Remove a source (or, if it's a default, reset it to disabled/empty).

    Idempotent. Default-catalog domains are kept (reset) so they reappear in
    the Settings panel rather than vanishing.
    """
    dom = normalize_domain(domain)
    is_default = any(d['domain'] == dom for d in DEFAULT_SOURCES)
    _ensure_loaded()
    with _lock:
        for i, r in enumerate(_cache):
            if r.get('domain') != dom:
                continue
            if is_default:
                r['enabled'] = False
                r['cookies'] = []
                r['proxy'] = ''
                r['updated_at'] = time.time()
            else:
                _cache.pop(i)
            _persist()
            audit_log('auth_source_delete', domain=dom, reset_default=is_default)
            logger.info('[AuthSrc] delete domain=%s (reset_default=%s)', dom, is_default)
            return True
    return False
