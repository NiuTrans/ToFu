"""tofu_search.search.engines.xhs — Xiaohongshu (小红书 / RED) search.

Xiaohongshu has no usable public search API; real keyword search needs a
logged-in cookie. This engine reuses the **auth-source provider** seam
(:mod:`tofu_search.providers`): when a host has registered a provider and the
user has connected ``xiaohongshu.com`` (cookies stored + enabled), we drive
the headless Playwright pool to the logged-in search-results page and scrape
the note cards from the rendered DOM.

If no provider is registered, or the source isn't connected, the engine
returns ``[]`` immediately — it never blocks the multi-engine pipeline.

Account-risk guard
------------------
XHS polices automated access at the ACCOUNT level (限流 / 滑块 / 封号), and
this engine fires on EVERY web search while connected — without pacing, the
user's chat frequency becomes the request rate against their logged-in
session. :class:`_RiskGuard` applies the three cross-project consensus
mitigations for a read-only search scenario (see sibling MCP/crawler
projects: xiaohongshu-mcp, MediaCrawler, ReaJason/xhs, Spider_XHS):

  1. **Pacing + jitter** — a minimum interval between two real page loads,
     randomized a little, so requests never arrive at chat speed. A wait
     that would blow the engine's latency budget SKIPS the call instead of
     stalling the whole pipeline.
  2. **Same-keyword TTL cache** — a chat assistant re-asks near-identical
     queries constantly; each repeat served from cache is one less
     logged-in hit.
  3. **Consecutive-empty backoff** — a risk-control wall (captcha / forced
     login redirect) scrapes as ZERO note cards, indistinguishable from a
     legit no-hits query in isolation. SEVERAL consecutive empties across
     different queries is not: after ``_BACKOFF_AFTER_EMPTY`` of them the
     engine stops touching XHS for ``xhs_backoff_cooldown_s``. Hammering a
     flagged account escalates the flag; the pause is the mitigation.

All three knobs live on :class:`tofu_search.config.SearchConfig`
(``xhs_min_interval_s`` / ``xhs_cache_ttl_s`` / ``xhs_backoff_cooldown_s``).
"""

from __future__ import annotations

import random
import threading
import time
from urllib.parse import quote

from tofu_search.config import get_config
from tofu_search.log import get_logger
from tofu_search.providers import (
    _emit_site_drift,
    get_auth_source_provider,
    get_browser_provider,
    get_site_knowledge_provider,
    get_site_search_provider,
)
from tofu_search.search._common import clean_text

logger = get_logger(__name__)

__all__ = ['search_xhs', 'xhs_search_available']

_DOMAIN = 'xiaohongshu.com'
_SEARCH_URL = 'https://www.xiaohongshu.com/search_result?keyword={kw}&source=web_search_result_notes'

_WAIT_SELECTOR = 'section.note-item, div.note-item, a[href*="/search_result/"], a[href*="/explore/"]'

# Consecutive empty scrapes that trip the risk-control backoff.
_BACKOFF_AFTER_EMPTY = 3
# A pacing wait longer than this would push the engine past the orchestrator's
# per-engine timeout once the page load itself is added — skip instead.
_MAX_THROTTLE_WAIT_S = 6.0
# Same-keyword cache ceiling (FIFO eviction; entries are tiny).
_CACHE_MAX_ENTRIES = 64

_CARD_EXTRACT_JS = r"""
(() => {
  const out = [];
  const seen = new Set();
  const anchors = Array.from(document.querySelectorAll(
    'a[href*="/explore/"], a[href*="/search_result/"]'));
  for (const a of anchors) {
    let href = a.href || '';
    if (!href) continue;
    try { href = new URL(href, location.origin).href; } catch (e) { continue; }
    if (!/\/(explore|search_result)\//.test(href)) continue;
    const key = href.split('?')[0];
    if (seen.has(key)) continue;
    let card = a.closest('section.note-item, div.note-item, section, div');
    let title = '';
    const titleNode = (card && (card.querySelector('.title, .note-title, span.title')))
      || a.querySelector('.title, span');
    if (titleNode) title = (titleNode.innerText || titleNode.textContent || '').trim();
    if (!title && card) {
      const txt = (card.innerText || '').trim().split('\n').map(s => s.trim()).filter(Boolean);
      if (txt.length) title = txt[0];
    }
    if (!title) title = (a.innerText || a.textContent || '').trim();
    if (!title) continue;
    let snippet = '';
    if (card) {
      const author = card.querySelector('.author, .name, .user-name');
      const count = card.querySelector('.count, .like-wrapper, .like');
      const parts = [];
      if (author) parts.push((author.innerText || '').trim());
      if (count) parts.push((count.innerText || '').trim());
      snippet = parts.filter(Boolean).join(' · ');
    }
    seen.add(key);
    out.push({ title: title.slice(0, 200), snippet: snippet.slice(0, 300), url: href });
    if (out.length >= 30) break;
  }
  return out;
})()
"""

# The POOL path contract is a bare list (``_do_search_authenticated`` coerces
# anything else to []), so the pool keeps the legacy list form. The BROWSER
# path wraps the same card body with a PROBE: the dict result tells drift
# apart from a real empty — anchors>0 while items==0 means the page rendered
# note links but our selectors could not make cards of them.
_EXTRACTOR_JS = _CARD_EXTRACT_JS


def _probed_extractor(list_extractor_js: str) -> str:
    """Wrap a LIST-form extractor with the drift probe (dict result)."""
    return (
        '(() => { const items = ' + list_extractor_js + ';'
        ' const anchors = document.querySelectorAll('
        '  \'a[href*="/explore/"], a[href*="/search_result/"]\').length;'
        ' return {items: (Array.isArray(items) ? items : []),'
        '  probe: {anchors: anchors, title: document.title || \'\','
        '          url: location.href}}; })()'
    )


def _split_extraction(raw):
    """Normalize an extraction result to ``(items, probe)``.

    ``(list, None)`` for the legacy list form; ``(items, probe)`` for the
    probed dict form; ``([], None)`` for anything else. A [] from EITHER
    form is a REAL empty unless the probe says the page had anchors.
    """
    if isinstance(raw, list):
        return raw, None
    if isinstance(raw, dict):
        items = raw.get('items')
        probe = raw.get('probe') if isinstance(raw.get('probe'), dict) else None
        return (items if isinstance(items, list) else []), probe
    return [], None


def _knowledge() -> dict | None:
    """Host-pinned extraction knowledge for XHS, or None (use built-ins).

    This is the re-pin seam: when selectors drift, the host's site-doctor
    verifies new ones against the LIVE page and stores them as DATA; the
    engine picks them up here without a library release.
    """
    provider = get_site_knowledge_provider()
    if provider is None:
        return None
    try:
        k = provider.get_knowledge(_DOMAIN)
    except Exception as e:
        logger.debug('[Search] XHS: site-knowledge lookup failed: %s', e)
        return None
    if isinstance(k, dict) and isinstance(k.get('extractor_js'), str) \
            and k['extractor_js'].strip():
        return k
    return None


class _RiskGuard:
    """Process-local XHS request pacing, query cache, and backoff state.

    One instance guards the one XHS account the process holds cookies for;
    the lock serializes real page loads so two concurrent chat searches can
    never hit XHS in the same instant (the pacing decision + wait happen
    under the lock — serializing XHS searches is precisely the point).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._last_request_ts = 0.0
        self._consecutive_empty = 0
        self._cooldown_until = 0.0
        self._cache: dict = {}  # normalized query -> (stored_ts, results)

    def reset(self):
        """Test hook: drop all pacing/backoff/cache state."""
        with self._lock:
            self._last_request_ts = 0.0
            self._consecutive_empty = 0
            self._cooldown_until = 0.0
            self._cache.clear()

    def in_cooldown(self) -> bool:
        with self._lock:
            return time.time() < self._cooldown_until

    def cached_results(self, query: str, ttl_s: int):
        """Fresh cached results for ``query``, or None. Defensive copies."""
        if ttl_s <= 0:
            return None
        key = query.strip().lower()
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if not hit:
                return None
            ts, results = hit
            if now - ts > ttl_s:
                self._cache.pop(key, None)
                return None
            return [dict(r) for r in results]

    def wait_turn(self, min_interval_s: float) -> bool:
        """Block until this thread may fire the next page load.

        Returns False when the required wait would exceed
        ``_MAX_THROTTLE_WAIT_S`` — the caller then skips this round rather
        than blowing the engine's latency budget.
        """
        if min_interval_s <= 0:
            return True
        with self._lock:
            now = time.time()
            wait = (self._last_request_ts + min_interval_s
                    + random.uniform(0.0, min(3.0, min_interval_s))) - now
            if wait > _MAX_THROTTLE_WAIT_S:
                return False
            if wait > 0:
                time.sleep(wait)
            self._last_request_ts = time.time()
            return True

    def report_outcome(self, query: str, results: list, *, ttl_s: int,
                       cooldown_s: int) -> None:
        """Fold one real page load's outcome into cache + backoff state."""
        key = query.strip().lower()
        with self._lock:
            if results:
                self._consecutive_empty = 0
                if ttl_s > 0:
                    self._cache[key] = (time.time(), list(results))
                    while len(self._cache) > _CACHE_MAX_ENTRIES:
                        self._cache.pop(next(iter(self._cache)))
                return
            self._consecutive_empty += 1
            if self._consecutive_empty >= _BACKOFF_AFTER_EMPTY and cooldown_s > 0:
                self._cooldown_until = time.time() + cooldown_s
                self._consecutive_empty = 0
                logger.warning(
                    '[Search] XHS: %d consecutive empty scrapes — entering %ds '
                    'cooldown. A risk-control wall (安全验证/滑块) or an expired '
                    'cookie scrapes as zero note cards; continuing to hit a '
                    'flagged account escalates the flag. Re-check the account '
                    'in Settings → 需要登录的来源.',
                    _BACKOFF_AFTER_EMPTY, cooldown_s)


_GUARD = _RiskGuard()


def _get_source():
    provider = get_auth_source_provider()
    if provider is None:
        return None
    try:
        return provider.get_source(_DOMAIN)
    except Exception as e:
        logger.debug('[Search] XHS: auth-source lookup failed: %s', e)
        return None


def _browser_connected() -> bool:
    """True when the host browser channel is live and can take tab commands."""
    site_provider = get_site_search_provider()
    if site_provider is not None:
        try:
            for source in site_provider.list_sources() or []:
                if isinstance(source, str):
                    source = {'id': source}
                aliases = {str(a).lower() for a in source.get('aliases', [])} \
                    if isinstance(source, dict) else set()
                if isinstance(source, dict) and (
                        str(source.get('id') or '').lower() in ('xiaohongshu', 'xhs')
                        or 'xhs' in aliases):
                    return True
        except Exception as e:
            logger.debug('[Search] XHS: site provider discovery failed: %s', e)
    provider = get_browser_provider()
    if provider is None:
        return False
    try:
        return bool(provider.is_connected())
    except Exception as e:
        logger.debug('[Search] XHS: browser is_connected failed: %s', e)
        return False


def _search_via_browser(url: str, query: str = '', max_results: int = 10):
    """Scrape the search page through the user's REAL logged-in browser.

    The page's own JS signs every request and the IP/fingerprint match the
    site's trust decisions — nothing is exported or replayed, which is exactly
    what the server-side pool cannot offer. Returns ``(items, probe)`` —
    ``items`` possibly empty (REAL empty unless the probe counted anchors),
    or None when the browser path failed. Selectors come from the host's
    site-knowledge store when pinned (drift re-pin), else the built-ins.
    """
    site_provider = get_site_search_provider()
    if site_provider is not None:
        try:
            raw = site_provider.search('xiaohongshu', query,
                                       max_results=max_results)
            if raw is not None:
                return _split_extraction(raw)
        except Exception as e:
            logger.warning('[Search] XHS: site-search provider failed, trying '
                           'compat browser provider: %s', e)
    provider = get_browser_provider()
    if provider is None:
        return None
    k = _knowledge()
    wait_selector = (k or {}).get('wait_selector') or _WAIT_SELECTOR
    list_extractor = (k or {}).get('extractor_js') or _CARD_EXTRACT_JS
    try:
        scrolls = int((k or {}).get('scrolls') or 2)
    except (TypeError, ValueError):
        scrolls = 2
    try:
        raw = provider.scrape(url, wait_selector=wait_selector,
                              extractor_js=_probed_extractor(list_extractor),
                              timeout=20, scrolls=scrolls)
        if raw is None:
            # Path UNAVAILABLE — propagate None so the pool fallback fires.
            # (Normalizing None to ([], None) would read as a REAL empty and
            # skip the fallback, inverting the None-vs-[] contract.)
            return None
        return _split_extraction(raw)
    except Exception as e:
        logger.warning('[Search] XHS: browser scrape failed, will fall back '
                       'to the pool: %s', e)
        return None


def xhs_search_available() -> bool:
    """True when the source is enabled AND some identity path exists — stored
    cookies (pool replay) OR the live browser (native session)."""
    if _browser_connected() and get_site_search_provider() is not None:
        return True
    src = _get_source()
    if not (src and src.get('enabled')):
        return False
    return bool(src.get('cookies')) or _browser_connected()


def search_xhs(query, max_results=10, freshness=''):
    """Search Xiaohongshu via the user's logged-in session.

    Returns ``{title, snippet, url, source}`` dicts, or ``[]`` when the source
    isn't connected, the guard skips the round (pacing / cooldown), or the
    scrape yields nothing. ``freshness`` is accepted for signature uniformity
    but unused.
    """
    t0 = time.time()
    src = _get_source()
    site_provider_live = get_site_search_provider() is not None and _browser_connected()
    if not (src and src.get('enabled')) and not site_provider_live:
        logger.debug('[Search] XHS not enabled — skipping')
        return []
    if not src:
        src = {'enabled': True, 'access_strategy': 'browser_first', 'cookies': []}
    strategy = str(src.get('access_strategy') or 'browser_first').strip()
    if strategy == 'public':
        # The registry says this site needs NO identity — the engine's whole
        # value is the logged-in session, so there is nothing to do.
        logger.debug('[Search] XHS: access_strategy=public — identity paths '
                     'disabled, skipping')
        return []
    cookies = src.get('cookies') or []
    browser_live = _browser_connected()
    if not cookies and not browser_live:
        logger.debug('[Search] XHS: no cookies and no live browser — skipping')
        return []

    cfg = get_config()

    if _GUARD.in_cooldown():
        logger.info('[Search] XHS in risk-control cooldown — skipping query=%r', query[:60])
        return []

    hit = _GUARD.cached_results(query, cfg.xhs_cache_ttl_s)
    if hit is not None:
        logger.info('[Search] XHS: %d cached results query=%r', len(hit), query[:60])
        return hit[:max_results]

    if not _GUARD.wait_turn(cfg.xhs_min_interval_s):
        logger.info('[Search] XHS throttled (next slot beyond latency budget) — '
                    'skipping query=%r', query[:60])
        return []

    url = _SEARCH_URL.format(kw=quote(query))

    # Path ORDER comes from the registry's access_strategy (P2):
    #   browser_first (default) — the user's live Chrome is primary (native
    #     login/signing, matching IP+fingerprint); the pool's cookie replay —
    #     a headless browser on a foreign IP, THE account risk-control
    #     trigger — only runs when the browser path is unavailable (None),
    #     never when it merely returned zero cards ([] = real empty, feeds
    #     the backoff below).
    #   cookies_replay — the OLD order, for risk-tolerant sites or a browser
    #     that is usually offline: pool replay primary, browser fallback.
    # Either way a path only falls through when it returned None
    # (unavailable), never when it returned [] (real empty).
    items = None
    probe = None
    via = 'none'

    def _try_browser():
        nonlocal items, probe, via
        if not browser_live:
            return
        got = _search_via_browser(url, query=query, max_results=max_results)
        if got is not None:
            items, probe = got
            via = 'browser'

    def _try_pool():
        nonlocal items, via
        if not cookies:
            return
        from tofu_search.fetch.playwright_pool import _pw_pool
        got = _pw_pool.search_authenticated(
            url,
            cookies=cookies,
            proxy=src.get('proxy') or '',
            timeout=20,
            extractor_js=_EXTRACTOR_JS,
            wait_selector=_WAIT_SELECTOR,
        )
        if got is not None:
            items = got
            via = 'pool'

    if strategy == 'cookies_replay':
        _try_pool()
        if items is None:
            _try_browser()
    else:
        _try_browser()
        if items is None:
            _try_pool()

    # Selector-drift signal: the page rendered note anchors (probe saw them)
    # yet extraction made ZERO cards — the selectors, not the query, are at
    # fault. Distinct from a real empty (anchors==0) and from a risk wall
    # (a wall renders no note anchors at all). Only the browser path carries
    # a probe; the pool runs the SAME selectors and drifts alike.
    if via == 'browser' and not items and probe \
            and int(probe.get('anchors') or 0) > 0:
        logger.warning(
            '[Search] XHS: SELECTOR DRIFT suspected — page rendered %d note '
            'anchors but extraction made 0 cards (page title %.60r). The '
            'site DOM changed; re-pinning knowledge is the fix, not retry.',
            int(probe.get('anchors') or 0), probe.get('title') or '')
        _emit_site_drift(_DOMAIN, url, {
            'anchors': int(probe.get('anchors') or 0),
            'page_title': probe.get('title') or '',
            'knowledge': 'pinned' if _knowledge() else 'builtin',
        })
    if not items:
        _GUARD.report_outcome(query, [], ttl_s=cfg.xhs_cache_ttl_s,
                              cooldown_s=cfg.xhs_backoff_cooldown_s)
        logger.info('[Search] XHS: 0 results in %.1fs query=%r', time.time() - t0, query[:60])
        return []

    results = []
    for it in items:
        if not isinstance(it, dict):
            continue
        u = (it.get('url') or '').strip()
        title = clean_text(it.get('title') or '')
        if not u or not title or not u.startswith('http'):
            continue
        results.append({
            'title': title[:200],
            'snippet': clean_text(it.get('snippet') or '')[:300],
            'url': u,
            'source': 'Xiaohongshu',
        })
        if len(results) >= max_results:
            break

    _GUARD.report_outcome(query, results, ttl_s=cfg.xhs_cache_ttl_s,
                          cooldown_s=cfg.xhs_backoff_cooldown_s)
    logger.info('[Search] XHS: %d results in %.1fs via=%s query=%r',
                len(results), time.time() - t0, via, query[:60])
    return results
