"""tofu_search/search/engines/searxng.py — SearXNG public meta-search instances."""

import random

import requests

from tofu_search.config import get_config
from tofu_search.log import get_logger
from tofu_search.search._common import (
    HEADERS,
    engine_circuit,
    make_result,
    search_session,
    soup_of,
)
from tofu_search.search.proxy_mode import proxy_mode_manager

logger = get_logger(__name__)

__all__ = ['search_searxng']


def _searxng_parse_html(html, max_results=6):
    """Parse SearXNG HTML search results page (bs4 selectors)."""
    results = []
    soup = soup_of(html)
    # SearXNG renders each result as <article class="result result-default">;
    # older / themed instances use <div class="result ...">.
    blocks = soup.select('article.result, div.result')
    for block in blocks:
        if len(results) >= max_results:
            break
        a = block.select_one('h3 a[href^="http"], a.url_header[href^="http"], a[href^="http"]')
        if not a:
            continue
        url = a['href']
        title = a.get_text(' ', strip=True)
        if not title or not url.startswith('http'):
            continue
        snip = block.select_one('.content, p.content')
        snippet = snip.get_text(' ', strip=True) if snip else ''
        results.append(make_result(title, snippet, url, 'SearXNG'))
    return results


def _searxng_parse_json(data, max_results=6):
    """Parse SearXNG JSON API response."""
    results = []
    for item in data.get('results', []):
        if len(results) >= max_results:
            break
        url = item.get('url', '')
        title = item.get('title', '')
        if not url or not title:
            continue
        result = make_result(title, item.get('content', ''), url, 'SearXNG')
        upstream = list(dict.fromkeys(item.get('engines') or []))
        if upstream:
            result['upstream_engines'] = upstream
            result['engine_count'] = len(upstream)
        results.append(result)
    return results


# 200-OK anti-bot interstitials: Anubis proof-of-work ("Making sure you're
# not a bot!") and Cloudflare ("Just a moment…"). A non-JS client can NEVER
# pass these, so the page must count as a block, not a genuine empty result.
_BOT_WALL_MARKERS = ("making sure you", 'just a moment', 'attention required')


def _is_bot_wall(text: str) -> bool:
    head = (text or '')[:20000].lower()
    return any(marker in head for marker in _BOT_WALL_MARKERS)


def search_searxng(query, max_results=6, freshness=''):
    """Query public SearXNG instances with automatic failover.

    Tries JSON API first (fast, structured), falls back to HTML scraping.
    Rotates across instances to spread load and survive rate-limits.

    Optimised for speed: a handful of instances max, 2s timeout per request.
    Most public instances block datacenter IPs (302→homepage redirect, 429,
    or a 200 anti-bot interstitial); each form is detected and skipped.

    Returns [] ONLY on a genuine no-match (an instance answered a real
    results/API page that held zero results) or a circuit-breaker skip.
    Raises ``requests.RequestException`` when EVERY tried instance was
    blocked or unreachable — total blockage is a network failure, and the
    orchestrator must classify it as engine_errors, never as "no matches"
    (the 2026-08-21 incident: SearXNG looked like the one surviving engine
    while every default instance was in fact 429/dead/bot-walled).
    """
    import time as _time
    t0 = _time.time()
    if engine_circuit.is_open('SearXNG'):
        logger.info('[Search] SearXNG skipped (circuit open) query=%r', query[:60])
        return []
    cfg = get_config()
    shuffled = list(cfg.searxng_instances)
    random.shuffle(shuffled)
    preferred = (getattr(cfg, 'searxng_url', '') or '').rstrip('/')
    instances = ([preferred] if preferred else []) + [
        inst.rstrip('/') for inst in shuffled if inst.rstrip('/') != preferred]
    if not instances:
        return []   # engine deliberately unconfigured
    _TIMEOUT = 2  # seconds — if SearXNG can't respond in 2s, it won't
    # A configured self-host plus at most three public fallbacks. The public
    # default list is mostly 429-rate-limited from datacenter egress IPs, so
    # two random picks rarely land on an open instance (measured 2026-08-21).
    _MAX_INSTANCES = 3 if preferred else 4
    genuine = 0      # instances that answered a REAL page (even a no-match)
    blockage = []    # per-instance block/failure reasons, for the diag line
    # Resolve the preferred network path once (honours an explicit
    # config.proxy_url / the DIRECT env-bypass marker). SearXNG's own per-
    # instance rotation already provides path diversity, so we take just the
    # first planned attempt rather than looping both paths per instance.
    _proxies = proxy_mode_manager.attempt_plan('SearXNG', cfg)[0][1]
    _pkw = {'proxies': _proxies} if _proxies is not None else {}
    # SearXNG supports time_range param: day, week, month, year
    _FRESHNESS_MAP = {'day': 'day', 'week': 'week', 'month': 'month', 'year': 'year'}
    time_range = _FRESHNESS_MAP.get(freshness, '')

    for inst in instances[:_MAX_INSTANCES]:
        try:
            # Try JSON first (don't follow redirects — detect 302→homepage)
            json_params = {'q': query, 'format': 'json'}
            configured_engines = (getattr(cfg, 'searxng_engines', '') or '').strip()
            if configured_engines:
                json_params['engines'] = configured_engines
            if time_range:
                json_params['time_range'] = time_range
            resp = search_session.get(
                f'{inst}/search',
                params=json_params,
                headers=HEADERS, timeout=_TIMEOUT, allow_redirects=False,
                **_pkw,
            )

            # 302/301 → homepage redirect = bot block, skip immediately
            if resp.status_code in (301, 302):
                logger.debug('[Search] SearXNG %s redirected (%d) — bot block, skipping',
                             inst, resp.status_code)
                blockage.append('redirect')
                continue

            # Rate-limited — skip to next instance immediately
            if resp.status_code == 429:
                logger.debug('[Search] SearXNG 429 from %s, trying next instance', inst)
                blockage.append('429')
                continue

            json_results = []
            if resp.ok and 'json' in resp.headers.get('content-type', ''):
                json_results = _searxng_parse_json(resp.json(), max_results)
                if json_results:
                    logger.info('[Search] SearXNG JSON from %s: %d results', inst, len(json_results))
                    engine_circuit.record_success('SearXNG')
                    return json_results
                genuine += 1   # real API answer that held no matches

            # JSON blocked (403) or empty — try HTML on same instance
            if resp.status_code == 403 or not json_results:
                html_params = {'q': query}
                if configured_engines:
                    html_params['engines'] = configured_engines
                if time_range:
                    html_params['time_range'] = time_range
                resp = search_session.get(
                    f'{inst}/search',
                    params=html_params,
                    headers=HEADERS, timeout=_TIMEOUT, allow_redirects=False,
                    **_pkw,
                )
                # Detect redirect again
                if resp.status_code in (301, 302):
                    logger.debug('[Search] SearXNG %s HTML redirected (%d) — bot block',
                                 inst, resp.status_code)
                    blockage.append('redirect')
                    continue
                if resp.status_code == 429:
                    logger.debug('[Search] SearXNG HTML 429 from %s', inst)
                    blockage.append('429')
                    continue
                if resp.ok:
                    if _is_bot_wall(resp.text):
                        logger.info('[Search] SearXNG %s served an anti-bot interstitial '
                                    '(Anubis/Cloudflare) — counts as blocked', inst)
                        blockage.append('bot-wall')
                        continue
                    genuine += 1   # a REAL HTML page, parseable or not
                    if len(resp.text) > 500:
                        results = _searxng_parse_html(resp.text, max_results)
                        if results:
                            logger.info('[Search] SearXNG HTML from %s: %d results', inst, len(results))
                            engine_circuit.record_success('SearXNG')
                            return results

        except requests.Timeout:
            logger.debug('[Search] SearXNG timeout (%ds): %s', _TIMEOUT, inst)
            blockage.append('timeout')
        except requests.RequestException as e:
            logger.debug('[Search] SearXNG %s failed: %s', inst, e)
            blockage.append(type(e).__name__)
        except Exception as e:
            logger.warning('[Search] SearXNG %s unexpected error: %s', inst, e, exc_info=True)
            blockage.append(type(e).__name__)

    elapsed = _time.time() - t0
    if genuine == 0:
        # EVERY tried instance was blocked or unreachable — that is a network
        # failure, not "no matches". Raise so the orchestrator classifies
        # SearXNG under engine_errors (same contract as _common.http_search_get).
        engine_circuit.record_failure('SearXNG')
        summary = ','.join(blockage[:6])
        logger.warning('[Search] SearXNG: all %d instance(s) blocked/unreachable '
                       'in %.1fs (%s) query=%r',
                       len(blockage), elapsed, summary, query[:60])
        raise requests.RequestException(
            'SearXNG: all %d instance(s) blocked/unreachable (%s)'
            % (len(blockage), summary))
    engine_circuit.record_success('SearXNG')
    logger.info('[Search] SearXNG: genuine no-match in %.1fs  query=%r', elapsed, query[:60])
    return []
