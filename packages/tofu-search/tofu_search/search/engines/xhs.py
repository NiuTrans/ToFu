"""Xiaohongshu search through explicitly captured host capabilities.

Entry points: search_xhs and xhs_search_available. Native SiteSearchProvider
results, empties and terminal refusals remain authoritative. Compatibility
browser reads and cookie replay retain the configured access_strategy order;
None means unavailable and permits fallback, while [] is an observed empty.

_provider_scope owns captured views; _xhs_sessions owns their reconstructible
state and credential snapshots; _xhs_guard owns existing pacing/backoff policy.
Browser results always reenter the provider, which owns current access checks.
Only enabled, unchanged replay credentials may reuse normalized result rows.
Native outcomes never read or settle either compatibility guard.
"""

from __future__ import annotations

import time
from urllib.parse import quote

from tofu_search._provider_scope import auth_source_scope, browser_scope
from tofu_search.config import get_config
from tofu_search.log import get_logger
from tofu_search.providers import (
    SiteSearchTerminalError,
    _emit_site_drift,
    get_auth_source_provider,
    get_browser_provider,
    get_site_knowledge_provider,
    get_site_search_provider,
    with_bound_optional_providers,
)
from tofu_search.search._common import clean_text
from tofu_search.search.engines._xhs_sessions import SourceSnapshot, browser_guard, capture_source

logger = get_logger(__name__)

__all__ = ['search_xhs', 'xhs_search_available']

_DOMAIN = 'xiaohongshu.com'
_SEARCH_URL = 'https://www.xiaohongshu.com/search_result?keyword={kw}&source=web_search_result_notes'

_WAIT_SELECTOR = 'section.note-item, div.note-item, a[href*="/search_result/"], a[href*="/explore/"]'


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


def _search_via_native(query: str, max_results: int):
    """Delegate admission and execution to the bound native site owner."""
    provider = get_site_search_provider()
    if provider is None:
        return None
    try:
        raw = provider.search('xiaohongshu', query, max_results=max_results)
        return _split_extraction(raw) if raw is not None else None
    except SiteSearchTerminalError:
        raise
    except Exception as error:
        logger.warning('[Search] XHS: site-search provider unavailable, trying '
                       'compat browser provider: %s', error)
        return None


def _search_via_browser(url: str, query: str = '', max_results: int = 10):
    """Compatibility scrape through the user's REAL logged-in browser.

    The page's own JS signs every request and the IP/fingerprint match the
    site's trust decisions — nothing is exported or replayed, which is exactly
    what the server-side pool cannot offer. Returns ``(items, probe)`` —
    ``items`` possibly empty (REAL empty unless the probe counted anchors),
    or None when the browser path failed. Selectors come from the host's
    site-knowledge store when pinned (drift re-pin), else the built-ins.
    """
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


def _legacy_attempt(guard, load, *, query, max_results, cfg, via, url,
                    started, unavailable):
    """One admitted legacy transport; only replay credentials can reuse rows."""
    if guard.in_cooldown():
        logger.info('[Search] XHS %s session in cooldown', via)
        return []
    cache_ttl = cfg.xhs_cache_ttl_s if via == 'pool' else 0
    if cache_ttl > 0:
        hit = guard.cached_results(query, cache_ttl, max_results=max_results)
        if hit is not None:
            return hit
    if not guard.wait_turn(cfg.xhs_min_interval_s):
        logger.info('[Search] XHS %s session throttled', via)
        return []
    outcome = load()
    if outcome is None:
        unavailable.append(guard)
        return None
    return _finish_search(
        *outcome, url=url, query=query, max_results=max_results, via=via,
        started=started, legacy_guard=guard, legacy_config=cfg,
        cache_ttl_s=cache_ttl)


@with_bound_optional_providers
def search_xhs(query, max_results=10, freshness=''):
    """Search through captured native/browser/source authority in declared order.

    Native ownership is authoritative. Compatibility browser calls always
    reenter scrape's live access check; only an enabled, exact cookie snapshot
    can reuse rows. Empty or throttled outcomes stop transport fallback.
    """
    started = time.time()
    try:
        snapshot = capture_source(auth_source_scope.current_binding(), _DOMAIN)
    except Exception as error:
        logger.debug('[Search] XHS: source snapshot unavailable: %s', error)
        snapshot = SourceSnapshot(None, None)
    source = snapshot.source
    browser_live = _browser_connected()
    native_live = get_site_search_provider() is not None and browser_live
    if not (source and source.get('enabled')) and not native_live:
        return []
    strategy = str((source or {}).get('access_strategy') or 'browser_first').strip()
    if strategy == 'public':
        return []
    url = _SEARCH_URL.format(kw=quote(query))
    cfg = get_config()
    unavailable = []

    def browser_path():
        if not browser_live:
            return None
        native = _search_via_native(query, max_results)
        if native is not None:
            return _finish_search(
                *native, url=url, query=query, max_results=max_results,
                via='native', started=started)
        binding = browser_scope.current_binding()
        if binding is None:
            return None
        return _legacy_attempt(
            browser_guard(binding), lambda: _search_via_browser(url),
            query=query, max_results=max_results, cfg=cfg, via='browser',
            url=url, started=started, unavailable=unavailable)

    def replay_path():
        if snapshot.guard is None:
            return None

        def load():
            from tofu_search.fetch.playwright_pool import _pw_pool
            raw = _pw_pool.search_authenticated(
                url, cookies=source['cookies'], proxy=source.get('proxy') or '',
                timeout=20, extractor_js=_EXTRACTOR_JS, wait_selector=_WAIT_SELECTOR)
            return (raw, None) if raw is not None else None

        return _legacy_attempt(
            snapshot.guard, load, query=query, max_results=max_results, cfg=cfg,
            via='pool', url=url, started=started, unavailable=unavailable)

    paths = ((replay_path, browser_path) if strategy == 'cookies_replay'
             else (browser_path, replay_path))
    for path in paths:
        result = path()
        if result is not None:
            return result
    # Unavailable paths count as an empty failure only when no transport
    # delivered. Native outcomes never settle a compatibility guard.
    for guard in unavailable:
        guard.report_outcome(query, [], max_results=max_results, ttl_s=0,
                             cooldown_s=cfg.xhs_backoff_cooldown_s)
    return _finish_search(
        None, None, url=url, query=query, max_results=max_results,
        via='none', started=started)


def _finish_search(items, probe, *, url, query, max_results, via, started,
                   legacy_guard=None, legacy_config=None, cache_ttl_s=0):
    """Project one result; only legacy execution updates the legacy guard."""

    # Selector-drift signal: the page rendered note anchors (probe saw them)
    # yet extraction made ZERO cards — the selectors, not the query, are at
    # fault. Distinct from a real empty (anchors==0) and from a risk wall
    # (a wall renders no note anchors at all). Only the browser path carries
    # a probe; the pool runs the SAME selectors and drifts alike.
    if via in ('browser', 'native') and not items and probe \
            and int(probe.get('anchors') or 0) > 0:
        logger.warning(
            '[Search] XHS: SELECTOR DRIFT suspected — page rendered %d note '
            'anchors but extraction made 0 cards (page title %.60r). The '
            'site DOM changed; re-pinning knowledge is the fix, not retry.',
            int(probe.get('anchors') or 0), probe.get('title') or '')
        _emit_site_drift(_DOMAIN, url, {
            'transport': via,
            'anchors': int(probe.get('anchors') or 0),
            'page_title': probe.get('title') or '',
            'knowledge': 'pinned' if _knowledge() else 'builtin',
        })
    if not items:
        if legacy_guard is not None:
            legacy_guard.report_outcome(
                query, [], max_results=max_results, ttl_s=cache_ttl_s,
                cooldown_s=legacy_config.xhs_backoff_cooldown_s)
        logger.info('[Search] XHS: 0 results in %.1fs query=%r', time.time() - started, query[:60])
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

    if legacy_guard is not None:
        legacy_guard.report_outcome(
            query, results, max_results=max_results, ttl_s=cache_ttl_s,
            cooldown_s=legacy_config.xhs_backoff_cooldown_s)
    logger.info('[Search] XHS: %d results in %.1fs via=%s query=%r',
                len(results), time.time() - started, via, query[:60])
    return results
