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
    'proxies_for',
    'get_bypass_domains', 'set_bypass_domains',
    'get_proxy_config', 'set_proxy_config',
]

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


def set_proxy_config(http_proxy: str = '', https_proxy: str = '',
                     no_proxy: str = ''):
    """Apply proxy address configuration at runtime.

    Updates ``os.environ`` so all ``requests`` calls pick up the new
    values immediately.

    The ``no_proxy`` parameter is **deprecated** — bypass domains are now
    managed solely by ``set_bypass_domains()``, which auto-syncs to the
    ``no_proxy`` environment variable.  Any value passed here is ignored.

    Called by ``routes/common.py`` on Settings save and by ``server.py``
    at startup when loading persisted config.

    Args:
        http_proxy:  HTTP proxy URL (e.g. ``http://10.0.0.1:8080``), or
                     empty string to clear (fall back to env).
        https_proxy: HTTPS proxy URL, or empty to clear.
        no_proxy:    Deprecated — ignored (auto-managed by bypass domains).
    """
    global _proxy_config
    with _lock:
        _proxy_config = {
            'http_proxy':  http_proxy.strip(),
            'https_proxy': https_proxy.strip(),
        }
        # Apply to environment — requests reads these on every call
        _apply_to_env('http_proxy',  http_proxy.strip() or _ENV_HTTP_PROXY)
        _apply_to_env('https_proxy', https_proxy.strip() or _ENV_HTTPS_PROXY)
        # no_proxy is auto-managed — sync it so state is consistent
        _sync_no_proxy()

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
    if not _bypass_domains:
        return {}
    host = (urlparse(url).hostname or '').lower()
    if host.endswith(_bypass_domains):
        return _NO_PROXY
    return {}


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
