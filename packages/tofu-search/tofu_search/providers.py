"""tofu_search.providers — Optional capability seams for host integration.

The standalone library runs fully without any of these. A host application
(e.g. chatui) can register implementations to unlock the capabilities that
were previously chatui-only:

  * **Browser provider** — fetch a URL / run a search through a real browser
    the user controls (e.g. a WebSocket browser extension). Used as a last
    resort when server-side HTTP is blocked (403/429/timeout) or an engine
    pool returns nothing.
  * **Auth-source provider** — supply stored cookies/proxy for login-walled
    domains (e.g. Xiaohongshu) so the Playwright pool can replay a logged-in
    session.
  * **Site-knowledge provider** — supply per-site extraction knowledge
    (wait selector / extractor JS / scroll count) as DATA, so a host can
    re-pin a site's selectors without shipping a library release. Engines
    fall back to their built-in constants when the provider has no entry.
  * **Site-drift listeners** — the library EMITS a signal when a page
    clearly rendered note-like content but the configured selectors
    extracted nothing (i.e. the selectors drifted, not a real empty
    result). A host listens and triggers its own re-con workflow.

Design mirrors the trading-plugin ``tofu.providers`` seam: dependency points
INWARD (host → library); the library never imports the host. Every hook is
optional and defended so a missing/raising provider degrades to the
anonymous pipeline rather than crashing it.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import threading
from typing import Optional

from tofu_search.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'BrowserProvider',
    'SiteSearchProvider',
    'AuthSourceProvider',
    'SiteKnowledgeProvider',
    'register_browser_provider',
    'register_site_search_provider',
    'register_auth_source_provider',
    'register_site_knowledge_provider',
    'get_browser_provider',
    'get_site_search_provider',
    'get_auth_source_provider',
    'get_site_knowledge_provider',
    'register_site_drift_listener',
    'clear_site_drift_listeners',
]


class BrowserProvider:
    """Interface for host-supplied browser automation.

    Subclass and override the methods you support; the defaults are inert
    (``is_connected`` → False, fetches → None), so a partial implementation
    is safe. Register an instance via :func:`register_browser_provider`.
    """

    def is_connected(self) -> bool:
        """Return True when the browser channel is live and usable."""
        return False

    def bind(self) -> 'BrowserProvider':
        """Return a request/task-bound view of this provider.

        Hosts with per-user browser fleets override this to capture the
        current user and browser identity before search work enters executor
        threads. Standalone providers can remain process-global.
        """
        return self

    def fetch_url(self, url: str, *, max_chars: int | None = None,
                  timeout: int = 15) -> Optional[str]:
        """Fetch ``url`` through the host browser; return extracted text or None."""
        return None

    def fetch_html(self, url: str, *, timeout: int = 20) -> Optional[str]:
        """Fetch the RAW HTML of ``url`` through the host browser.

        Unlike :meth:`fetch_url` (which returns host-extracted *text*), this
        returns the unparsed HTML so the library can run its own engine-grade
        parser on it. Used by :func:`tofu_search.search.browser_fallback` to
        scrape a search-results page through the user's browser while keeping
        the parsing logic inside the library (not duplicated in the host).

        Default returns None — hosts that can't supply raw HTML simply don't
        unlock the browser search fallback. Returning None makes
        ``search_via_browser`` fall back to the host's own :meth:`search`.
        """
        return None

    def search(self, query: str, *, max_results: int = 8) -> list[dict]:
        """Run a web search through the host browser; return result dicts or [].

        This is the LAST-resort hook: it asks the host to perform the entire
        search (fetch + parse). Prefer implementing :meth:`fetch_html` instead,
        which lets the library own the result parsing. ``search_via_browser``
        only calls this when :meth:`fetch_html` returns None.
        """
        return []

    def scrape(self, url: str, *, wait_selector: str = '',
               extractor_js: str = '[]', timeout: int = 20,
               scrolls: int = 0) -> Optional[list]:
        """Open ``url`` in a background tab, wait for ``wait_selector``, run
        ``extractor_js`` in-page, and return its JSON result (list/dict).

        Unlike :meth:`fetch_url` (extracted text) and :meth:`fetch_html` (raw
        HTML), this returns STRUCTURED data the page rendered — search cards,
        product lists — extracted by JS running inside the user's REAL session
        (native cookies and request signing, nothing exported). Use it for
        login-walled / risk-controlled sites where replaying stored cookies
        from a server-side headless browser trips account risk control.

        ``scrolls`` > 0 asks the host to scroll to the bottom that many times
        (with a human-ish pause) before extracting — lazy-loaded feeds.

        Return contract: a list/dict when the path WORKED (``[]`` is a REAL
        empty result — the caller counts it toward risk-control backoff, never
        re-hits via another transport); ``None`` when the browser path is
        unavailable (caller falls back). Default returns None — hosts without
        tab-level automation simply don't unlock structured scraping.
        """
        return None


class SiteSearchProvider:
    """Optional host-owned, read-only authenticated site search seam.

    The provider owns browser sessions and site adapters.  tofu-search only
    orchestrates and normalizes returned records; standalone users need not
    register anything.
    """

    def list_sources(self) -> list[dict]:
        """Return available read sources.

        Each item has at least ``id`` and may include ``name``, ``aliases``,
        ``domains`` and ``metadata``.  Write-only sources must not be listed.
        """
        return []

    def bind(self) -> 'SiteSearchProvider':
        """Return a request/task-bound provider view (default: ``self``)."""
        return self

    def search(self, source_id: str, query: str, *, max_results: int = 10,
               freshness: str = '') -> Optional[list[dict]]:
        """Return results, ``[]`` for a real empty, or ``None`` if unavailable."""
        return None


class AuthSourceProvider:
    """Interface for host-supplied authenticated-source lookups.

    A "source" is a dict carrying at least ``domain`` and ``cookies``
    (Playwright cookie dicts), optionally ``proxy`` and ``enabled``.
    """

    def match_source(self, url: str) -> Optional[dict]:
        """Return the auth-source row whose domain matches ``url``, or None."""
        return None

    def get_source(self, domain: str) -> Optional[dict]:
        """Return the auth-source row for ``domain``, or None."""
        return None


class SiteKnowledgeProvider:
    """Interface for host-supplied per-site extraction knowledge.

    A "knowledge" entry is a dict carrying at least ``extractor_js`` (the
    in-page JS returning a list of result dicts), optionally
    ``wait_selector`` / ``scrolls`` / ``version`` / ``verified_at``. Engines
    consult the provider FIRST and fall back to their built-in constants
    when it returns None — so the library ships sane defaults and the host
    owns re-pins (e.g. after a selector-drift re-con).
    """

    def get_knowledge(self, domain: str) -> Optional[dict]:
        """Return the knowledge entry for ``domain``, or None."""
        return None


_lock = threading.Lock()
_browser_provider: Optional[BrowserProvider] = None
_site_search_provider: Optional[SiteSearchProvider] = None
_auth_source_provider: Optional[AuthSourceProvider] = None
_site_knowledge_provider: Optional[SiteKnowledgeProvider] = None
_drift_listeners: list = []
_UNSET = object()
_browser_provider_ctx = contextvars.ContextVar(
    'tofu_search_browser_provider', default=_UNSET)
_site_search_provider_ctx = contextvars.ContextVar(
    'tofu_search_site_search_provider', default=_UNSET)


def register_browser_provider(provider: Optional[BrowserProvider]) -> None:
    """Install (or clear, with ``None``) the global browser provider."""
    global _browser_provider
    with _lock:
        _browser_provider = provider
    logger.info('[Providers] browser provider %s',
                'registered' if provider else 'cleared')


def register_site_search_provider(provider: Optional[SiteSearchProvider]) -> None:
    """Install (or clear) the global read-only site-search provider."""
    global _site_search_provider
    with _lock:
        _site_search_provider = provider
    logger.info('[Providers] site-search provider %s',
                'registered' if provider else 'cleared')


def register_auth_source_provider(provider: Optional[AuthSourceProvider]) -> None:
    """Install (or clear, with ``None``) the global auth-source provider."""
    global _auth_source_provider
    with _lock:
        _auth_source_provider = provider
    logger.info('[Providers] auth-source provider %s',
                'registered' if provider else 'cleared')


def get_browser_provider() -> Optional[BrowserProvider]:
    """Return the registered browser provider, or None."""
    scoped = _browser_provider_ctx.get()
    if scoped is not _UNSET:
        return scoped
    with _lock:
        return _browser_provider


def get_site_search_provider() -> Optional[SiteSearchProvider]:
    """Return the registered site-search provider, or None."""
    scoped = _site_search_provider_ctx.get()
    if scoped is not _UNSET:
        return scoped
    with _lock:
        return _site_search_provider


@contextlib.contextmanager
def bound_optional_providers():
    """Bind host providers once and carry them through a public operation."""
    browser = get_browser_provider()
    site_search = get_site_search_provider()
    if browser is not None:
        browser = browser.bind()
    if site_search is not None:
        site_search = site_search.bind()
    btok = _browser_provider_ctx.set(browser)
    stok = _site_search_provider_ctx.set(site_search)
    try:
        yield
    finally:
        _site_search_provider_ctx.reset(stok)
        _browser_provider_ctx.reset(btok)


def with_bound_optional_providers(fn):
    """Decorator for public calls that may cross executor-thread boundaries."""
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        with bound_optional_providers():
            return fn(*args, **kwargs)
    return wrapped


def submit_with_provider_context(pool, fn, /, *args, **kwargs):
    """Submit work with the caller's bound-provider ContextVars copied."""
    context = contextvars.copy_context()
    return pool.submit(context.run, fn, *args, **kwargs)


def normalize_site_search_results(items, *, source_id: str,
                                  source_name: str = '',
                                  max_results: int = 10) -> list[dict]:
    """Normalize provider records to the stable search-result wire shape."""
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get('title') or '').strip()
        url = str(item.get('url') or '').strip()
        if not title or not url.startswith(('http://', 'https://')):
            continue
        metadata = item.get('metadata') if isinstance(item.get('metadata'), dict) else {}
        metadata = dict(metadata)
        metadata.setdefault('site_source', source_id)
        row = dict(item)
        row.update({
            'title': title[:500], 'url': url,
            'snippet': str(item.get('snippet') or '')[:2000],
            'source': str(item.get('source') or source_name or source_id),
            'metadata': metadata,
        })
        out.append(row)
        if len(out) >= max(1, int(max_results)):
            break
    return out


def get_auth_source_provider() -> Optional[AuthSourceProvider]:
    """Return the registered auth-source provider, or None."""
    with _lock:
        return _auth_source_provider


def register_site_knowledge_provider(
        provider: Optional[SiteKnowledgeProvider]) -> None:
    """Install (or clear, with ``None``) the global site-knowledge provider."""
    global _site_knowledge_provider
    with _lock:
        _site_knowledge_provider = provider
    logger.info('[Providers] site-knowledge provider %s',
                'registered' if provider else 'cleared')


def get_site_knowledge_provider() -> Optional[SiteKnowledgeProvider]:
    """Return the registered site-knowledge provider, or None."""
    with _lock:
        return _site_knowledge_provider


def register_site_drift_listener(cb) -> None:
    """Subscribe ``cb(site, url, evidence)`` to selector-drift signals.

    Fired by engines when a page demonstrably rendered the kind of content
    the extractor targets (probe counted matching anchors) yet extraction
    yielded ZERO items — the signature of SELECTOR DRIFT, not a real empty
    result and not a risk-control wall (a wall renders no content anchors).
    Listeners must be fast and non-blocking; the engine continues
    immediately. A raising listener is logged and swallowed — it can never
    break a search.
    """
    with _lock:
        _drift_listeners.append(cb)
    logger.info('[Providers] site-drift listener registered (%d total)',
                len(_drift_listeners))


def clear_site_drift_listeners() -> None:
    """Drop all drift listeners (test hygiene)."""
    with _lock:
        _drift_listeners.clear()


def _emit_site_drift(site: str, url: str, evidence: dict) -> None:
    """Notify listeners of a suspected selector drift. Never raises."""
    with _lock:
        listeners = list(_drift_listeners)
    for cb in listeners:
        try:
            cb(site, url, evidence)
        except Exception as e:
            logger.warning('[Providers] site-drift listener failed for %s: %s',
                           site, e)
