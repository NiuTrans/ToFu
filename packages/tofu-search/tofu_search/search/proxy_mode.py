"""tofu_search.search.proxy_mode — Adaptive per-engine proxy strategy.

The HTML-scraping engines share one ``requests.Session`` whose proxy behaviour
is otherwise dictated entirely by ambient ``HTTP(S)_PROXY`` env vars, with a
single attempt and no recovery. That makes "did search work?" depend purely on
the installer's network topology:

  * a container behind a proxy where ``HTTPS_PROXY`` isn't exported → every
    engine connection is refused / times out;
  * a direct-internet host with a stale/dead proxy env var → every engine
    fails through the dead proxy;
  * a datacenter / proxy egress IP soft-blocked by an engine → a 200 consent
    page parses to 0 results and looks like "no matches".

This module makes each engine try BOTH network paths (proxied ↔ direct) when a
proxy exists, and REMEMBER which path worked so steady state is one request per
engine:

  * :func:`ProxyModeManager.attempt_plan` returns the ordered list of
    ``(label, proxies_kwarg)`` to try for an engine — a single ``DIRECT``
    attempt when no proxy is configured (byte-identical to the historical
    behaviour), or a sticky-ordered ``[PROXY, DIRECT]`` / ``[DIRECT, PROXY]``
    pair when a proxy exists and dual-attempt is enabled.
  * :meth:`record_success` / :meth:`record_failure` maintain a short-lived
    per-engine preference so the winning path is tried first next time.

Proxy source of truth (in priority order): an explicit ``config.proxy_url``
(a host like chatui can inject its Settings-resolved proxy), else the standard
``https_proxy`` / ``http_proxy`` / ``all_proxy`` env vars (upper/lower case).
``config.proxy_fallback_urls`` extends this into an ordered failover CHAIN —
each entry becomes its own labelled attempt between the primary proxy and the
direct path, and the per-engine learning can pin ANY of them.

The ``proxies_kwarg`` values are passed straight to ``requests``:

  * ``PROXY`` with an explicit URL → ``{'http': url, 'https': url}``;
  * ``PROXY`` from env → ``None`` (let ``requests`` apply the env proxy);
  * ``DIRECT`` → ``{'no_proxy': '*'}`` — the only reliable per-call bypass of
    an env proxy across ``requests`` versions (``{'http': None}`` is not).
"""

import os
import threading
import time

from tofu_search.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'PROXY', 'DIRECT',
    'ProxyModeManager', 'proxy_mode_manager',
    'detect_proxy_url', 'detect_proxy_urls', '_reset_proxy_mode_manager',
]

PROXY = 'proxy'
DIRECT = 'direct'

# Reliable per-call env-proxy bypass (see module docstring / lib/proxy.py).
_DIRECT_PROXIES = {'no_proxy': '*'}

# How long a learned per-engine preference stays sticky before we re-evaluate
# from the default order (so a transient proxy outage doesn't pin an engine to
# one path forever).
_PREF_TTL = 600.0


def detect_proxy_url(config=None) -> str:
    """Return the effective proxy URL, or '' when none is configured.

    Priority: an explicit ``config.proxy_url`` (host-injected) over the
    standard proxy env vars (upper/lower case, https → http → all).
    """
    explicit = getattr(config, 'proxy_url', '') if config is not None else ''
    if explicit:
        return explicit
    for key in ('https_proxy', 'HTTPS_PROXY', 'http_proxy', 'HTTP_PROXY',
                'all_proxy', 'ALL_PROXY'):
        val = os.environ.get(key)
        if val:
            return val
    return ''


def detect_proxy_urls(config=None) -> list:
    """Ordered, de-duped failover chain of proxy URLs ('' entries dropped).

    The primary comes from :func:`detect_proxy_url` (explicit ``proxy_url``
    over env); ``config.proxy_fallback_urls`` extends the chain behind it.
    Only the PRIMARY may be env-derived — fallback URLs are always explicit,
    so their ``proxies=`` kwarg is always the forced dict form.
    """
    urls = []
    primary = detect_proxy_url(config)
    if primary:
        urls.append(primary)
    fallbacks = getattr(config, 'proxy_fallback_urls', None) or []
    for url in fallbacks:
        url = (url or '').strip()
        if url and url not in urls:
            urls.append(url)
    return urls


class ProxyModeManager:
    """Per-engine adaptive proxy-path selection with sticky learning."""

    def __init__(self, pref_ttl: float = _PREF_TTL):
        self._lock = threading.Lock()
        # engine name -> {'mode': str, 'ts': float}
        self._pref: dict[str, dict] = {}
        self._pref_ttl = pref_ttl

    # ── plan ──────────────────────────────────────────────────────

    def attempt_plan(self, engine: str, config=None) -> list[tuple]:
        """Return the ordered ``[(label, proxies_kwarg), ...]`` to try.

        * No proxy configured → ``[(DIRECT, None)]`` — one attempt, identical
          to the historical env-only behaviour.
        * Proxy configured but dual-attempt disabled → the single primary
          ``[(PROXY, kwarg)]`` (fallback URLs are deliberately ignored — the
          operator asked for exactly one proxied attempt).
        * Dual-attempt (default) → the proxy failover chain
          ``[PROXY, proxy#1, …]`` followed by ``DIRECT``, with the
          sticky-preferred path (any label) moved to the front.

        Labels: the primary proxy keeps the historical ``PROXY`` label;
        fallback URLs are ``proxy#1``, ``proxy#2`` … by chain position —
        stable across calls (so learning sticks) and credential-free (a
        resolved URL may carry userinfo and is never logged).
        """
        urls = detect_proxy_urls(config)
        dual = getattr(config, 'proxy_dual_attempt', True) if config is not None else True

        if not urls:
            return [(DIRECT, None)]

        explicit_primary = bool(
            getattr(config, 'proxy_url', '') if config is not None else '')
        attempts = []
        for i, url in enumerate(urls):
            label = PROXY if i == 0 else f'{PROXY}#{i}'
            # Explicit URL → forced proxies dict; an env-derived primary →
            # None so ``requests`` applies the ambient env proxy itself.
            kwarg = ({'http': url, 'https': url}
                     if (explicit_primary or i > 0) else None)
            attempts.append((label, kwarg))

        if not dual:
            return attempts[:1]

        attempts.append((DIRECT, dict(_DIRECT_PROXIES)))
        preferred = self._preferred(engine)
        if preferred:
            for j, (mode, _) in enumerate(attempts):
                if mode == preferred:
                    attempts.insert(0, attempts.pop(j))
                    break
        return attempts

    # ── learning ──────────────────────────────────────────────────

    def _preferred(self, engine: str):
        """Return the fresh sticky preference for ``engine``, or None."""
        with self._lock:
            st = self._pref.get(engine)
            if not st:
                return None
            if time.time() - st['ts'] > self._pref_ttl:
                del self._pref[engine]
                return None
            return st['mode']

    def record_success(self, engine: str, mode: str):
        """Remember ``mode`` as the working path for ``engine``."""
        with self._lock:
            prev = self._pref.get(engine, {}).get('mode')
            self._pref[engine] = {'mode': mode, 'ts': time.time()}
        if prev != mode:
            logger.info('[Search] proxy-path learned: %s -> %s', engine, mode)

    def record_failure(self, engine: str, mode: str):
        """Forget a sticky preference that just failed on its own path.

        Only clears when the failed ``mode`` is the currently-pinned one — a
        failure on the fallback path shouldn't unstick a good preference.
        """
        with self._lock:
            st = self._pref.get(engine)
            if st and st['mode'] == mode:
                del self._pref[engine]

    def status(self) -> dict:
        """Snapshot of current per-engine preferences (for diagnostics)."""
        now = time.time()
        with self._lock:
            return {
                eng: {'mode': st['mode'], 'age_s': round(now - st['ts'], 1)}
                for eng, st in self._pref.items()
            }


proxy_mode_manager = ProxyModeManager()


def _reset_proxy_mode_manager():
    """Clear all learned preferences (tests only)."""
    with proxy_mode_manager._lock:
        proxy_mode_manager._pref.clear()
