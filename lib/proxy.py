"""lib/proxy.py — Centralized proxy configuration.

Manages **two** concerns that are unified for the user:

1. **Proxy address** — the ``http_proxy`` / ``https_proxy`` values that
   ``requests`` uses.  These can come from environment variables (traditional)
   **or** from the Settings UI (persisted to ``server_config.json``).

2. **Proxy bypass** — domain suffixes / hosts whose traffic should bypass
   the proxy entirely.  Configured via one Settings UI field (or the
   ``PROXY_BYPASS_DOMAINS`` env var).  Under the hood, bypass domains
   feed **both**:

   - Per-request bypass via ``proxies_for(url)`` (suffix match)
   - Global ``no_proxy`` environment variable (for any code using
     ``requests`` directly without explicit ``proxies=`` kwarg)

Usage in any module::

    from lib.proxy import proxies_for

    resp = requests.post(url, json=body, proxies=proxies_for(url), timeout=30)

The Settings UI (Network tab) lets users configure the proxy address
and a single unified bypass list without touching environment variables.
"""

import os
import threading
from urllib.parse import urlparse

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'proxies_for', 'report_outcome',
    'get_bypass_domains', 'set_bypass_domains',
    'get_proxy_config', 'set_proxy_config',
    'register_no_proxy_host', 'register_no_proxy_url',
    'async_proxy_for',
]


# ── Adaptive path selection (lib.netpath), lazily wired ──
# Kept behind a lazy import so lib.proxy stays importable in minimal
# contexts; a missing/broken netpath module degrades to env behaviour.
_netpath_mod = None
# One-shot guard so a broken netpath on the per-request hot path (proxies_for)
# logs a single warning instead of one line per request.
_np_decide_warned = False


def _np():
    global _netpath_mod
    if _netpath_mod is None:
        try:
            from lib import netpath as _m
            _netpath_mod = _m
        except Exception as e:
            # Runs once (the result is cached). Without this trace a broken /
            # missing netpath silently degrades to env behaviour with no signal.
            logger.debug('[Proxy] netpath unavailable — adaptive path selection off: %s', e)
            _netpath_mod = False
    return _netpath_mod or None


def report_outcome(url: str, ok: bool, latency_ms=None) -> None:
    """Forward a real request outcome to the netpath scorer.

    Called by the LLM transports and lib.http_client on success/failure.
    Never raises; a no-op when netpath is disabled or the host is not
    managed (explicit bypass rules win over learned decisions).
    """
    np = _np()
    if np is None:
        return
    try:
        np.report_outcome(url, ok, latency_ms)
    except Exception as e:
        # If this keeps firing the netpath scorer freezes on stale scores and
        # adaptive routing silently stops learning — surface it.
        logger.debug('[Proxy] netpath.report_outcome failed for %s: %s', url, e)

# ── The "real" bypass dict that makes requests skip env proxies ──
# NOTE: ``{'http': None, 'https': None}`` does NOT reliably bypass in all
# requests versions.  ``{'no_proxy': '*'}`` is the only fully reliable method.
_NO_PROXY = {'no_proxy': '*'}

# ── Standard always-bypass entries (never need a proxy) ──
_ALWAYS_BYPASS = ('localhost', '127.0.0.1', '0.0.0.0')

_lock = threading.Lock()

# ═══════════════════════════════════════════════════════
#  Proxy Address (http_proxy / https_proxy)
# ═══════════════════════════════════════════════════════
# Snapshot the *original* env vars at import time so we can tell the UI
# what came from the environment vs what was set via Settings.

_ENV_HTTP_PROXY = os.environ.get('http_proxy') or os.environ.get('HTTP_PROXY', '')
_ENV_HTTPS_PROXY = os.environ.get('https_proxy') or os.environ.get('HTTPS_PROXY', '')
_ENV_NO_PROXY = os.environ.get('no_proxy') or os.environ.get('NO_PROXY', '')

# Persisted proxy config (empty = not configured via Settings, use env)
_proxy_config: dict = {}   # keys: http_proxy, https_proxy


def get_proxy_config() -> dict:
    """Return the current proxy configuration for the Settings UI.

    Returns a dict with:
      - ``http_proxy``, ``https_proxy``: effective values
      - ``no_proxy``: effective no_proxy env var (read-only, auto-managed)
      - ``env_*``: original env values (read-only)
      - ``configured``: whether proxy was set via Settings
    """
    return {
        'http_proxy':  _proxy_config.get('http_proxy', '') or _ENV_HTTP_PROXY,
        'https_proxy': _proxy_config.get('https_proxy', '') or _ENV_HTTPS_PROXY,
        'no_proxy':    os.environ.get('no_proxy', ''),  # effective, read-only
        'env_http_proxy':  _ENV_HTTP_PROXY,
        'env_https_proxy': _ENV_HTTPS_PROXY,
        'env_no_proxy':    _ENV_NO_PROXY,
        'configured': bool(_proxy_config),
    }


def set_proxy_config(http_proxy: str = '', https_proxy: str = ''):
    """Apply proxy address configuration at runtime.

    Updates ``os.environ`` so all ``requests`` calls pick up the new
    values immediately.

    Bypass domains (the ``no_proxy`` env var) are managed solely by
    ``set_bypass_domains()``, which auto-syncs to the ``no_proxy``
    environment variable — this function only touches the proxy address.

    Called by ``routes/config.py`` on Settings save and by ``server.py``
    at startup when loading persisted config.

    Args:
        http_proxy:  HTTP proxy URL (e.g. ``http://10.0.0.1:8080``), or
                     empty string to clear (fall back to env).
        https_proxy: HTTPS proxy URL, or empty to clear.
    """
    global _proxy_config
    with _lock:
        prev_effective = (
            _proxy_config.get('http_proxy', '') or _ENV_HTTP_PROXY,
            _proxy_config.get('https_proxy', '') or _ENV_HTTPS_PROXY,
        )
        _proxy_config = {
            'http_proxy':  http_proxy.strip(),
            'https_proxy': https_proxy.strip(),
        }
        # Apply to environment — requests reads these on every call
        _apply_to_env('http_proxy',  http_proxy.strip() or _ENV_HTTP_PROXY)
        _apply_to_env('https_proxy', https_proxy.strip() or _ENV_HTTPS_PROXY)
        # no_proxy is auto-managed — sync it so state is consistent
        _sync_no_proxy()
        new_effective = (
            _proxy_config.get('http_proxy', '') or _ENV_HTTP_PROXY,
            _proxy_config.get('https_proxy', '') or _ENV_HTTPS_PROXY,
        )
    # A changed proxy address invalidates every proxy-path measurement.
    if new_effective != prev_effective:
        np = _np()
        if np is not None:
            try:
                np.reset_proxy_stats()
            except Exception as e:
                # Failure means routing keeps deciding on stats measured for
                # the OLD proxy address.
                logger.warning('[Proxy] netpath.reset_proxy_stats failed after proxy change: %s', e)

    logger.info('[Proxy] Config updated: http=%s https=%s',
                http_proxy.strip() or '(env)', https_proxy.strip() or '(env)')


def _apply_to_env(key: str, value: str):
    """Set both lower-case and UPPER-CASE env vars for maximum compatibility."""
    if value:
        os.environ[key] = value
        os.environ[key.upper()] = value
    else:
        os.environ.pop(key, None)
        os.environ.pop(key.upper(), None)


# ═══════════════════════════════════════════════════════
#  Proxy Bypass Domains (unified: per-request + env no_proxy)
# ═══════════════════════════════════════════════════════

# ── Baseline from env var (read once at import time) ──
_env_domains: tuple = tuple(
    d.strip() for d in os.environ.get('PROXY_BYPASS_DOMAINS', '').split(',')
    if d.strip()
)

# ── Dynamic domains set via Settings UI (hot-reloaded) ──
_settings_domains: tuple = ()

# ── Merged tuple (rebuilt on any change) ──
_bypass_domains: tuple = _env_domains


def _rebuild():
    """Rebuild the merged bypass tuple from env + settings sources."""
    global _bypass_domains
    seen = set()
    merged = []
    for d in _env_domains + _settings_domains:
        dl = d.lower().strip()
        if dl and dl not in seen:
            seen.add(dl)
            merged.append(dl)
    _bypass_domains = tuple(merged)


def _sync_no_proxy():
    """Rebuild ``no_proxy`` env var from: always-bypass + env baseline + bypass domains.

    Called automatically whenever bypass domains or proxy config change,
    ensuring the global ``no_proxy`` env var stays in sync with the
    unified bypass list.
    """
    parts = []
    seen = set()

    def _add(d):
        if d and d not in seen:
            parts.append(d)
            seen.add(d)

    # 1. Standard always-bypass entries
    for d in _ALWAYS_BYPASS:
        _add(d)
    # 2. Original env no_proxy baseline
    for d in _ENV_NO_PROXY.split(','):
        _add(d.strip())
    # 3. All bypass domains (env PROXY_BYPASS_DOMAINS + Settings UI)
    for d in _bypass_domains:
        _add(d)

    merged = ','.join(parts)
    _apply_to_env('no_proxy', merged)


def proxies_for(url: str) -> dict:
    """Return ``{'no_proxy': '*'}`` when *url* should bypass the HTTP proxy.

    Returns an empty dict otherwise, letting ``requests`` use the
    environment-level ``http_proxy`` / ``https_proxy`` as normal.

    This is the **single entry point** for proxy decisions — every module
    that makes HTTP requests should call this.
    """
    host = (urlparse(url).hostname or '').lower()
    if not host:
        return {}
    # Always bypass the standard local hosts. ``requests`` already does this
    # via its NO_PROXY env handling, but ``httpx`` doesn't honour NO_PROXY
    # when given an explicit ``proxy=`` URL — so we MUST return the bypass
    # marker for these hosts too, otherwise async callers send localhost
    # traffic through the corporate proxy.
    if host in _ALWAYS_BYPASS:
        return _NO_PROXY
    # Exact-match registered hosts (typically self-hosted local LLM endpoints
    # whose IPs live in publicly-routable space and therefore aren't matched
    # by RFC1918 detection).  We need both the literal host AND the global
    # bypass-domain suffix list to bypass the proxy.
    if host in _registered_hosts:
        return _NO_PROXY
    if _bypass_domains and host.endswith(_bypass_domains):
        return _NO_PROXY
    # ── Learned decision (direct vs proxy) from lib.netpath ──
    # Explicit rules above always win; below here the host is registered
    # for probing and a measured 'direct' pin bypasses the proxy.
    np = _np()
    if np is not None:
        try:
            np.note_url(url)
            if np.decide(host) == 'direct':
                return _NO_PROXY
        except Exception as e:
            # Hot path (every request). A persistent netpath failure here means
            # every request silently falls back to the proxy — an LLM dispatch
            # latency regression with no signal. Warn ONCE, not per request.
            global _np_decide_warned
            if not _np_decide_warned:
                _np_decide_warned = True
                logger.warning('[Proxy] netpath decide/note_url failing; requests '
                               'fall back to proxy without learned direct-pin (first: %s)', e)
    return {}


def async_proxy_for(url: str) -> 'str | None':
    """Proxy decision for httpx-style clients that take an explicit ``proxy=``.

    httpx does NOT honour the ``no_proxy`` environment variable once an
    explicit proxy URL is handed in, so the async path cannot defer to the
    environment the way ``requests`` does when :func:`proxies_for` returns
    ``{}`` — before this helper, async LLM calls to an internal gateway
    (e.g. your-llm-gateway.example.com, bypassed via env ``no_proxy`` for the sync path)
    silently hairpinned through the corporate proxy.  The async decision is
    made identical to the sync one BY CONSTRUCTION: bypass when
    ``proxies_for()`` says so (explicit rules / registered hosts / learned
    direct pins), or when the SAME predicate ``requests`` itself uses
    (``should_bypass_proxies`` over the live env ``no_proxy``, kept in sync
    by ``_sync_no_proxy``) matches — otherwise return the env proxy URL.

    Returns ``None`` for a direct connection.
    """
    if proxies_for(url):
        return None
    try:
        from requests.utils import should_bypass_proxies
        if should_bypass_proxies(url, no_proxy=None):
            return None
    except Exception as e:
        # A broken bypass probe must not break the request — fall through to
        # the env proxy (the pre-fix behaviour), never raise here.
        logger.debug('[Proxy] env no_proxy check failed for %s: %s', url, e)
    return (os.environ.get('https_proxy')
            or os.environ.get('HTTPS_PROXY')
            or os.environ.get('http_proxy')
            or os.environ.get('HTTP_PROXY')
            or None)

# Hosts (typically raw IPs of self-hosted LLM endpoints) that must bypass the
# proxy. Populated at runtime by the dispatcher / probe paths so we don't have
# to hardcode private-but-publicly-routable corporate IP ranges in env vars.
_registered_hosts: set = set()


def register_no_proxy_host(host: str) -> bool:
    """Mark *host* as proxy-bypass for the lifetime of this process.

    Idempotent and thread-safe. Updates both the per-request ``proxies_for``
    decision AND the ``no_proxy`` env var (so any third-party library using
    ``requests`` without our wrapper still bypasses correctly).

    Returns True if the host was newly registered.
    """
    if not host:
        return False
    h = host.strip().lower()
    if not h:
        return False
    with _lock:
        if h in _registered_hosts:
            return False
        _registered_hosts.add(h)
        # Append to no_proxy env var if not already present (substring check
        # is cheap and false positives are harmless — they only over-bypass).
        cur = os.environ.get('no_proxy', '')
        if h not in cur.split(','):
            new = (cur + ',' + h) if cur else h
            _apply_to_env('no_proxy', new)
    logger.info('[Proxy] Registered no-proxy host: %s', h)
    return True


def register_no_proxy_url(url: str) -> bool:
    """Convenience wrapper: register the hostname extracted from *url*."""
    if not url:
        return False
    try:
        host = urlparse(url).hostname or ''
    except Exception as e:
        logger.debug('[Proxy] Failed to parse URL %s: %s', url, e)
        return False
    return register_no_proxy_host(host)


def get_bypass_domains() -> list:
    """Return the current *settings-only* bypass domains (for the UI)."""
    return list(_settings_domains)


def set_bypass_domains(domains: list):
    """Hot-reload bypass domains from the Settings UI.

    Updates both the per-request ``proxies_for()`` bypass tuple **and**
    the ``no_proxy`` environment variable (auto-synced).

    Called by ``routes/common.py`` when the user saves settings, and
    once at startup when loading persisted config.

    Args:
        domains: List of domain suffixes (e.g. ``['.corp.net', '.internal.example.com']``).
    """
    global _settings_domains
    with _lock:
        _settings_domains = tuple(
            d.strip() for d in domains if d and d.strip()
        )
        _rebuild()
        _sync_no_proxy()
    if _settings_domains:
        logger.info('[Proxy] Bypass domains updated: %s (no_proxy synced)',
                    ', '.join(_settings_domains))
    else:
        logger.debug('[Proxy] Settings bypass domains cleared')


# ── Initial merge + env sync ──
_rebuild()
_sync_no_proxy()
