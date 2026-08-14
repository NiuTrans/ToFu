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

Cookies are stored in Playwright's cookie shape. Operators normally supply them
as a ``{cookie_name: value}`` mapping — the Settings UI renders one labelled
input per cookie declared in :data:`DEFAULT_SOURCES` (see
:func:`cookies_from_fields`), so there is no delimiter syntax for a user to get
wrong. Two other paths remain: a raw ``Cookie:`` header copied from devtools
(:func:`parse_cookie_header`) and the interactive-login flow's
``storage_state`` (:func:`cookies_from_storage_state`).

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
  cookies_from_fields(mapping, domain)   → list[dict]
  cookies_from_storage_state(state, …)   → list[dict]
  source_spec(domain) / source_fields(domain)
  missing_required_fields(cookies, domain) → list[str]
"""

from __future__ import annotations

import os
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
    'cookies_from_fields',
    'source_spec',
    'source_fields',
    'missing_required_fields',
    'normalize_domain',
    'invalidate_cache',
    'live_session_status',
    'DEFAULT_SOURCES',
]

_STORE_PATH = config_path('auth_sources.json')
_STORE_VERSION = 1
_MAX_SOURCES = 64
_MAX_COOKIES_PER_SOURCE = 256

_lock = threading.RLock()
_cache: list[dict] = []
_cache_loaded = False
# mtime of the store when _cache was filled — the cache's validity key. A
# different mtime means another process wrote the file, so we must re-read.
_cache_mtime = 0.0


# ── Default catalog ─────────────────────────────────────────────────
# Shipped disabled + cookie-less. They make the sites show up in the
# Settings panel so a user can "connect" them, and they prime the fetch
# router to treat the host as login-walled (skip the doomed anonymous
# attempt) ONLY once enabled with cookies. A source with no cookies is
# always treated as not-configured regardless of ``enabled``.
# ``fields`` declares the individual cookies that carry the session, so the UI
# can ask for each one in its OWN input instead of making the user hand-assemble
# a ``name=value; name=value`` string (the delimiters were a reliable source of
# silent typos). ``importance`` is a single axis:
#   required     — refuse to store without it; the login genuinely cannot work
#   recommended  — store, but warn: usually needed for the site's request signing
#   optional     — nice to have
DEFAULT_SOURCES: list[dict] = [
    {'domain': 'xiaohongshu.com', 'label': 'Xiaohongshu / RED',
     'aliases': ['xhslink.com'],
     'access_strategy': 'browser_first',
     'login_url': 'https://www.xiaohongshu.com/explore',
     # Account-risk note shown in the Settings connect flow. XHS polices
     # automated access aggressively (限流/滑块/封号), and every web search
     # replays this logged-in session — the user MUST connect with a spare
     # account, not their main one. Carried as an i18n KEY (not text) so the
     # wording lives with the other UI strings in static/js/i18n.js while the
     # per-site fact ("this site is risky") stays server-side.
     'risk_note_key': 'settings.authSrcRiskXhs',
     'fields': [
         {'name': 'web_session', 'importance': 'required'},
         {'name': 'a1', 'importance': 'recommended'},
         {'name': 'webId', 'importance': 'optional'},
     ]},
    # SSO-walled internal site. Anonymous rendering reaches the SSO login page
    # (ssosv.internal.example.com), never the content, so only a replayed logged-in
    # session can read it. NOTE: reaching this host at all ALSO requires it to
    # be listed in tofu-search's ``allow_private_hosts`` — it resolves to an
    # RFC-1918 address behind a rotating internal load balancer, so the SSRF
    # guard blocks it by default. The two gates are deliberately separate:
    # connecting an account must never silently grant an SSRF exemption.
    {'domain': 'example-corp.com', 'label': 'YourProvider internal (SSO)',
     'aliases': [],
     'access_strategy': 'browser_first',
     'login_url': 'https://api.openai.com/ml/modelPlaza/modelInfo',
     # Cookie names below are from a MEASURED anonymous run of the SSO chain
     # (Playwright, networkidle) — NOT guessed. What that run proves and what
     # it cannot:
     #   * PROVEN, pre-login: the chain sets only telemetry / device-fingerprint
     #     / PKCE-context cookies — `_lxsdk*`, `WEBDFPID`, `logan_session_token`,
     #     `webDeviceUuid`, `ctxId` / `ctxId-<client_id>` (httpOnly, per-login
     #     attempt), and `com.example-corp.speechfe.sft_strategy` on the app host.
     #     NONE of these is a session credential.
     #   * NOT PROVEN: the post-login session cookie's name. It is only issued
     #     AFTER a successful QR scan, so an anonymous probe cannot see it.
     #     `ssoid` is the long-standing assumption in this repo (it is also
     #     tests/_qr_login_capture.py's wait-for anchor) but remains UNVERIFIED
     #     against a real logged-in session.
     # Hence NOTHING here is marked `required`: a required field that turns out
     # to carry the wrong name would REJECT a perfectly good credential paste
     # at the store boundary, which is a worse failure than storing a set we
     # then discover is incomplete. Promote to `required` once a real login has
     # confirmed the name.
     # NOTE: reaching this host at all ALSO requires it in tofu-search's
     # ``allow_private_hosts`` — the two gates stay separate on purpose.
     'fields': [
         {'name': 'ssoid', 'importance': 'recommended'},
         {'name': 'ssoid_example_corp', 'importance': 'optional'},
         {'name': 'ssousername', 'importance': 'optional'},
         {'name': 'ssosession', 'importance': 'optional'},
     ]},
]

_VALID_IMPORTANCE = ('required', 'recommended', 'optional')

#: Registry field (Site Knowledge Layer P2): WHO opens the door for this
#: site's identity paths. Consumed by tofu-search's engines + fetch core
#: (unknown keys pass through the provider row untouched):
#:   browser_first  — live browser primary, pool replay fallback (default)
#:   cookies_replay — pool replay primary, browser fallback (risk-tolerant
#:                    sites / a browser that is usually offline)
#:   public         — no identity needed; engines/fetch skip identity paths
_VALID_STRATEGIES = ('browser_first', 'cookies_replay', 'public')


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


def source_spec(domain: str) -> dict:
    """Return the static catalog spec for ``domain`` (``{}`` when unknown).

    The spec is the SINGLE source of truth for a site's login URL and the
    individual cookies its session is made of. Both the REST listing and the
    Settings UI read it from here — neither keeps its own copy.
    """
    dom = normalize_domain(domain)
    for d in DEFAULT_SOURCES:
        if d['domain'] == dom:
            return d
    return {}


def source_fields(domain: str) -> list[dict]:
    """The declared cookie fields for ``domain`` (``[]`` when unknown)."""
    fields = source_spec(domain).get('fields') or []
    return [dict(f) for f in fields]


def cookies_from_fields(fields: dict, domain: str) -> list[dict]:
    """Build Playwright cookie dicts from a ``{cookie_name: value}`` mapping.

    This is the structured counterpart to :func:`parse_cookie_header`: the UI
    collects one input per cookie, so there is no delimiter for the user to get
    wrong. Blank values are dropped (an untouched optional input is not an
    instruction to store an empty cookie).

    A value may also be a ``{'value': ..., 'domain': ..., 'path': ...}`` dict to
    pin that ONE cookie's scope. This matters: the previous version stamped
    EVERY cookie with ``'.' + domain``, which silently broke host-only cookies.
    A session cookie issued for ``your-llm-gateway.example.com`` (no leading dot) became
    ``.internal.example.com``; the browser then treats it as a DIFFERENT cookie, the
    site's auth probe never receives it, and the fetch lands on the login wall
    while the store still reports "connected". Host-only is therefore the
    DEFAULT (narrower scope = the safer guess, and it is what devtools shows for
    such cookies); a parent-domain cookie must be asked for explicitly by
    passing a leading-dot ``domain``.
    """
    out: list[dict] = []
    if not isinstance(fields, dict):
        return out
    dom = normalize_domain(domain)
    for name, raw in fields.items():
        name = str(name or '').strip()
        if not name:
            continue
        # Per-cookie scope override, or a bare value.
        if isinstance(raw, dict):
            value = str(raw.get('value') if raw.get('value') is not None else '').strip()
            c_dom = str(raw.get('domain') or '').strip().lower() or dom
            c_path = str(raw.get('path') or '').strip() or '/'
        else:
            value = str(raw if raw is not None else '').strip()
            c_dom = dom          # host-only default — NO leading dot
            c_path = '/'
        if not value:
            continue
        entry = {'name': name, 'value': value, 'domain': c_dom, 'path': c_path}
        if isinstance(raw, dict) and raw.get('secure') is not None:
            entry['secure'] = bool(raw.get('secure'))
        out.append(entry)
    return out[:_MAX_COOKIES_PER_SOURCE]


def missing_required_fields(cookies: list, domain: str) -> list[str]:
    """Names of ``required`` spec fields absent (or blank) in ``cookies``.

    Without this the store happily accepted a mistyped paste and then reported
    the source as "connected", so the failure only surfaced much later as an
    unexplained empty fetch.
    """
    present = {
        str(c.get('name', '')).strip()
        for c in (cookies or [])
        if isinstance(c, dict) and str(c.get('value', '')).strip()
    }
    return [f['name'] for f in source_fields(domain)
            if f.get('importance') == 'required' and f['name'] not in present]


def _host_matches(host: str, domain: str) -> bool:
    """True if ``host`` equals ``domain`` or is a sub-domain of it."""
    return host == domain or host.endswith('.' + domain)


def parse_cookie_header(raw: str, domain: str) -> list[dict]:
    """Parse a raw ``Cookie:`` header string into Playwright cookie dicts.

    ``"a=1; b=2"`` → ``[{name:'a', value:'1', domain:'<domain>', path:'/'},
    …]``. This is the format a user copies from devtools → Network → any
    request → Request Headers → Cookie. Returns ``[]`` on empty/garbage
    input (logged at debug).

    The scope is HOST-ONLY (no leading dot) for the same reason as
    :func:`cookies_from_fields`: a ``Cookie:`` header carries no scope
    information at all, so inventing ``.<domain>`` for every entry breaks any
    host-only session cookie the site issued. A request to the host itself
    matches a host-only cookie, which is exactly the case this input covers.
    """
    out: list[dict] = []
    if not raw or not raw.strip():
        return out
    dom = normalize_domain(domain)
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
            'domain': dom,
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


def _store_mtime() -> float:
    """Store file mtime, or 0.0 when it does not exist / cannot be stat'ed."""
    try:
        return os.path.getmtime(_STORE_PATH)
    except OSError as e:
        # Expected on first run (store not yet written) — debug, not warning.
        logger.debug('[AuthSrc] store mtime stat failed for %s: %s',
                     _STORE_PATH, e)
        return 0.0


def invalidate_cache() -> None:
    """Drop the in-memory cache so the next read re-loads from disk.

    The cache is normally self-invalidating (see :func:`_ensure_loaded` — it
    re-reads whenever the file's mtime moves), so callers rarely need this.
    It exists for the cases mtime cannot cover:

      * a same-second write on a coarse-mtime filesystem;
      * a test that swaps ``_STORE_PATH`` and wants a guaranteed clean read;
      * an operator forcing a re-read after editing the JSON by hand.

    Public on purpose: reaching into ``_cache_loaded`` from outside the module
    is what this replaces.
    """
    global _cache_loaded, _cache_mtime
    with _lock:
        _cache_loaded = False
        _cache_mtime = 0.0
        _cache.clear()
    logger.debug('[AuthSrc] cache invalidated — next read re-loads from disk')


def _ensure_loaded() -> None:
    """Load the store into the module cache, re-reading when the file changed.

    The cache used to be load-once-per-process, which made a LONG-LIVED reader
    (a scheduler / optimizer worker, or any non-server entrypoint) keep the
    snapshot it took at startup forever: credentials connected later through
    the Settings UI — written by a DIFFERENT process — were never picked up,
    and the fetch path kept hitting the login wall with no way to recover short
    of a restart. Keying the cache on the file's mtime makes it self-healing
    across processes; :func:`invalidate_cache` remains for the cases mtime
    cannot see.
    """
    global _cache_loaded, _cache_mtime
    mtime = _store_mtime()
    if _cache_loaded and mtime == _cache_mtime:
        return
    with _lock:
        mtime = _store_mtime()
        if _cache_loaded and mtime == _cache_mtime:
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
        _cache_mtime = mtime
        logger.info('[AuthSrc] loaded %d source(s) from %s', len(_cache), _STORE_PATH)


def _persist() -> None:
    global _cache_mtime
    payload = {'version': _STORE_VERSION, 'sources': list(_cache)}
    update_json_atomic(_STORE_PATH, lambda _: payload, default=payload)
    # Our own write must not look like someone else's: record the new mtime so
    # the next read trusts the cache we just updated in memory.
    _cache_mtime = _store_mtime()


def _redact(row: dict, knowledge: Optional[dict] = None) -> dict:
    """Public view: replace cookie list with a count, drop raw values.

    ``knowledge`` is the site_knowledge entry for this domain (or None),
    injected by list_sources so a 64-row listing costs ONE store read —
    projected to a small badge ``{pinned, version, verified_at}``.
    """
    out = dict(row)
    cookies = out.get('cookies') or []
    out['cookie_count'] = len(cookies)
    out['has_cookies'] = bool(cookies)
    out.pop('cookies', None)
    spec = source_spec(out.get('domain', ''))
    if not out.get('access_strategy'):
        out['access_strategy'] = spec.get('access_strategy') or 'browser_first'
    # Site knowledge is now keyed by operation.  Keep accepting the legacy
    # flat search entry so upgrades do not temporarily lose their badge.
    operations = knowledge.get('operations') if isinstance(knowledge, dict) \
        and isinstance(knowledge.get('operations'), dict) else {}
    pinned = operations.get('search') if operations else knowledge
    if isinstance(pinned, dict) and pinned.get('extractor_js'):
        out['knowledge'] = {'pinned': True,
                            'version': pinned.get('version'),
                            'verified_at': pinned.get('verified_at')}
    else:
        out['knowledge'] = {'pinned': False}
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
    # Catalog spec (login URL + the individual cookies that make up the
    # session) travels with the row so the UI never hardcodes a second copy.
    out['login_url'] = spec.get('login_url', '')
    out['fields'] = [dict(f) for f in (spec.get('fields') or [])]
    out['risk_note_key'] = spec.get('risk_note_key', '')
    return out


def list_sources() -> list[dict]:
    """All configured sources, redacted (no cookie values / proxy creds)."""
    _ensure_loaded()
    with _lock:
        rows = [dict(r) for r in _cache]
    rows.sort(key=lambda r: (not r.get('enabled'), r.get('label', r.get('domain', ''))))
    try:
        from lib.site_knowledge import list_knowledge
        knowledge_map = list_knowledge()
    except Exception as e:
        logger.debug('[AuthSrc] site-knowledge map unavailable: %s', e)
        knowledge_map = {}
    return [_redact(r, knowledge=knowledge_map.get(r.get('domain', '')))
            for r in rows]


def _with_spec(row: dict) -> dict:
    """Merge catalog spec fields into a FULL row copy (consumer chain).

    tofu-search receives rows via the provider seam — per the registry
    design (docs/SITE_KNOWLEDGE_LAYER_DESIGN.md §3.1) the row must carry the
    new registry fields with it: access_strategy (path ORDER is data),
    login_url, cookie ``fields`` (the login flow's hint source). The stored
    row's own value wins when present; spec fills the rest;
    ``browser_first`` is the schema default.
    """
    out = dict(row)
    spec = source_spec(out.get('domain', ''))
    out.setdefault('aliases', list(spec.get('aliases', [])))
    if not out.get('access_strategy'):
        out['access_strategy'] = spec.get('access_strategy') or 'browser_first'
    if not out.get('login_url'):
        out['login_url'] = spec.get('login_url', '')
    if not out.get('fields'):
        out['fields'] = [dict(f) for f in (spec.get('fields') or [])]
    if not out.get('risk_note_key'):
        out['risk_note_key'] = spec.get('risk_note_key', '')
    return out


def get_source(domain: str) -> Optional[dict]:
    """Internal lookup by exact domain — returns the FULL row (incl. cookies)
    with catalog spec fields merged in."""
    dom = normalize_domain(domain)
    if not dom:
        return None
    _ensure_loaded()
    with _lock:
        for r in _cache:
            if r.get('domain') == dom:
                return _with_spec(r)
    return None


def match_source(url: str) -> Optional[dict]:
    """Return the enabled source governing ``url``'s host.

    Matches the host against each source's ``domain`` and ``aliases``
    (sub-domains included). Credential rule (P2 后修订——live session is
    a first-class credential):

      * stored cookies present → match (any strategy: replay can fire);
      * no cookies but ``access_strategy == 'browser_first'`` → match:
        the credential is the user's LIVE browser session, not anything
        stored here. Requiring a cookie paste for such rows forced users
        through a devtools copy-paste the browser path never needed.
      * otherwise (cookies_replay without cookies / public) → no match.
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
            if not r.get('enabled'):
                continue
            strategy = (r.get('access_strategy')
                        or source_spec(r.get('domain', '')).get('access_strategy')
                        or 'browser_first')
            if strategy == 'public':
                continue          # no identity needed — never matches
            if not r.get('cookies') and strategy != 'browser_first':
                continue
            domains = [r.get('domain', '')] + list(r.get('aliases', []))
            if any(d and _host_matches(host, d) for d in domains):
                return _with_spec(r)
    return None


def upsert_source(domain: str, *, label: Optional[str] = None,
                  enabled: Optional[bool] = None,
                  cookies: Optional[list] = None,
                  cookie_header: Optional[str] = None,
                  cookie_fields: Optional[dict] = None,
                  proxy: Optional[str] = None,
                  aliases: Optional[list] = None,
                  access_strategy: Optional[str] = None) -> dict:
    """Create or update a source. Returns the redacted row.

    Only the fields you pass are touched (None = leave unchanged), except that
    supplying ``cookie_fields`` (a ``{cookie_name: value}`` mapping — the
    structured path the Settings UI uses) or ``cookie_header`` (a raw devtools
    ``Cookie:`` string) parses + replaces ``cookies``.

    Raises ``ValueError`` on a bad domain, when the source cap is hit, or when
    the supplied cookies omit a cookie the catalog marks ``required`` — storing
    a credential set that cannot possibly authenticate only defers the failure
    to a later, much less explainable, empty fetch.
    """
    dom = normalize_domain(domain)
    if not dom:
        raise ValueError('domain is required')

    if access_strategy is not None \
            and access_strategy not in _VALID_STRATEGIES:
        raise ValueError(
            f'access_strategy must be one of {_VALID_STRATEGIES}')

    if cookie_fields is not None:
        cookies = cookies_from_fields(cookie_fields, dom)
    elif cookie_header is not None:
        cookies = parse_cookie_header(cookie_header, dom)

    if cookies is not None and cookies:
        missing = missing_required_fields(cookies, dom)
        if missing:
            raise ValueError('missing required cookie(s): ' + ', '.join(missing))

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
        if access_strategy is not None:
            row['access_strategy'] = access_strategy
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


#: Live-session probe cache — asking the bridge costs a command round-trip,
#: and the Settings panel re-renders on every toggle.
_LIVE_SESSION_TTL_S = 20.0
_live_session_cache: dict = {}
_live_session_lock = threading.Lock()


def live_session_status(domain: str, *, refresh: bool = False) -> dict:
    """Probe the user's REAL browser for a live login on ``domain``.

    The bridge's ``get_cookies`` reads the browser's own jar (never stored
    here) and we match cookie NAMES against the catalog's declared fields:
    any ``required``/``recommended`` name present = the user is logged in.
    Returns ``{extension, live_session, matched, missing_required}``:

      * ``extension`` False — the bridge is offline; detection impossible
        (the browser-first path is unavailable too);
      * ``live_session`` True — the user is logged into the site in their
        browser RIGHT NOW; a browser_first entry works with zero stored
        cookies (OpenCLI-parity: login once in the daily browser, done).
    """
    dom = normalize_domain(domain)
    if not dom:
        return {'extension': False, 'live_session': False,
                'matched': [], 'missing_required': []}
    now = time.time()
    with _live_session_lock:
        hit = _live_session_cache.get(dom)
        if hit and not refresh and now - hit[0] < _LIVE_SESSION_TTL_S:
            return dict(hit[1])
    out = {'extension': False, 'live_session': False,
           'matched': [], 'missing_required': []}
    try:
        from lib.browser import is_extension_connected, send_browser_command
        if is_extension_connected():
            out['extension'] = True
            res, err = send_browser_command('get_cookies', {'domain': dom},
                                            timeout=10)
            if err:
                logger.info('[AuthSrc] live-session probe failed for %s: %s',
                            dom, str(err)[:120])
            names = {str(c.get('name', '')) for c in (res or [])
                     if isinstance(c, dict)}
            fields = source_fields(dom)
            wanted = [f['name'] for f in fields
                      if f.get('importance') in ('required', 'recommended')]
            matched = [n for n in wanted if n in names]
            out['matched'] = matched
            out['missing_required'] = [
                f['name'] for f in fields
                if f.get('importance') == 'required' and f['name'] not in names]
            # Logged-in verdict: any declared session cookie present. For an
            # unknown domain (no declared fields) ANY cookie on the domain is
            # the best available signal.
            out['live_session'] = bool(matched) if wanted else bool(names)
    except Exception as e:
        logger.debug('[AuthSrc] live-session probe crashed for %s: %s', dom, e)
    with _live_session_lock:
        _live_session_cache[dom] = (now, dict(out))
    return out


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
                spec = source_spec(dom)
                r['enabled'] = False
                r['cookies'] = []
                r['proxy'] = ''
                # A reset must restore the whole mutable catalog contract, not
                # just credentials. Keeping a user-selected cookies_replay
                # strategy made a reset browser-first source stay unusable with
                # no cookies and leaked that state across processes/tests.
                r['label'] = spec.get('label', dom)
                r['aliases'] = list(spec.get('aliases', []))
                r['access_strategy'] = (
                    spec.get('access_strategy') or 'browser_first')
                r['updated_at'] = time.time()
            else:
                _cache.pop(i)
            _persist()
            audit_log('auth_source_delete', domain=dom, reset_default=is_default)
            logger.info('[AuthSrc] delete domain=%s (reset_default=%s)', dom, is_default)
            return True
    return False
