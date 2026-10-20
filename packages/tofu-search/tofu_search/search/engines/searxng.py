"""Free SearXNG metasearch with bounded, evidence-aware instance failover.

Entry: search_searxng. Config owns eligible instances and query policy; the
shared search_session owns transport. This adapter validates response shape,
retains uncertain candidates, and distinguishes upstream loss from no matches.
No new pool, cache, credentials or query-rewriting path is introduced.
"""

import json
import random
import time
from urllib.parse import urlsplit

import requests

from tofu_search.config import get_config
from tofu_search.log import get_logger
from tofu_search.search._common import (
    HEADERS,
    EngineUnavailable,
    engine_circuit,
    make_result,
    search_session,
    soup_of,
)
from tofu_search.search.dedup import canonical_url_key
from tofu_search.search.proxy_mode import proxy_mode_manager
from tofu_search.search.quality import classify_relevance, matches_site_constraints, site_scopes

logger = get_logger(__name__)
__all__ = ['search_searxng']
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class _SearxResults(list):
    def __init__(self, rows=(), warnings=()):
        super().__init__(rows)
        self.coverage_warnings = sorted(set(warnings))


class _SearxFailure(requests.RequestException):
    """Adapter-owned static failure code, safe to expose without response data."""
    def __init__(self, code):
        self.code = code
        super().__init__('SearXNG: ' + code)


def _valid_url(url):
    if not isinstance(url, str) or not url or len(url) > 8192:
        return False
    try:
        parsed = urlsplit(url)
        return (parsed.scheme in ('http', 'https') and bool(parsed.hostname)
                and not parsed.username and not parsed.password
                and not any(ord(c) <= 32 for c in url))
    except ValueError:
        return False


def _html_rows(soup, max_results):
    results = []
    for block in soup.select('article.result, div.result'):
        if len(results) >= max_results:
            break
        a = block.select_one('h3 a[href^="http"], a.url_header[href^="http"], a[href^="http"]')
        if not a or not _valid_url(a['href']):
            continue
        title = a.get_text(' ', strip=True)
        if not title:
            continue
        snip = block.select_one('.content, p.content')
        results.append(make_result(title, snip.get_text(' ', strip=True) if snip else '',
                                   a['href'], 'SearXNG'))
    return results


def _searxng_parse_html(html, max_results=6):
    """Parse source listings; search_searxng separately checks coverage/health."""
    return _html_rows(soup_of(html), max_results)


def _searxng_parse_json(data, max_results=6):
    """Parse a real result envelope, refusing malformed success-shaped data."""
    if not isinstance(data, dict) or not isinstance(data.get('results'), list) or data.get('error'):
        raise _SearxFailure('invalid_json_envelope')
    results = []
    for item in data['results']:
        if len(results) >= max_results:
            break
        if not isinstance(item, dict):
            continue
        url, title = item.get('url'), item.get('title')
        if not _valid_url(url) or not isinstance(title, str) or not title.strip():
            continue
        content = item.get('content')
        result = make_result(title, content if isinstance(content, str) else '', url, 'SearXNG')
        engines = item.get('engines')
        if isinstance(engines, list):
            upstream = list(dict.fromkeys(name for name in engines
                                          if isinstance(name, str) and 0 < len(name) <= 64))[:32]
            if upstream:
                result['upstream_engines'] = upstream
                result['engine_count'] = len(upstream)
        results.append(result)
    if data['results'] and not results:
        raise _SearxFailure('invalid_result_records')
    return results


_BOT_WALL_MARKERS = ("making sure you", 'just a moment', 'attention required')


def _is_bot_wall(text, *, soup=None):
    # Titles and challenge forms describe the response. A result snippet about
    # bot challenges is still ordinary searchable evidence.
    if soup is None:
        soup = soup_of(text or '')
    title = soup.title.get_text(' ', strip=True).lower() if soup.title else ''
    return (any(marker in title for marker in _BOT_WALL_MARKERS)
            or soup.select_one('form#challenge-form, #anubis_challenge, #cf-challenge-running') is not None)


def _read_response(url, params, *, deadline, proxies):
    """One existing-session request; close on every path and bound decoded bytes."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise requests.Timeout('SearXNG: deadline')
    response = search_session.get(url, params=params, headers=HEADERS,
                                  timeout=min(2.0, remaining), allow_redirects=False,
                                  stream=True, **proxies)
    try:
        headers = {key.lower(): value for key, value in response.headers.items()}
        status = response.status_code
        if status != 200 or headers.get('cf-mitigated') == 'challenge':
            return status, headers, ''
        try:
            if int(headers.get('content-length', '0')) > _MAX_RESPONSE_BYTES:
                raise _SearxFailure('response_too_large')
        except ValueError:
            raise _SearxFailure('invalid_content_length') from None
        body = bytearray()
        for chunk in response.iter_content(chunk_size=16384):
            if time.monotonic() >= deadline:
                raise requests.Timeout('SearXNG: deadline')
            if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                raise _SearxFailure('response_too_large')
            body.extend(chunk)
        if time.monotonic() >= deadline:
            raise requests.Timeout('SearXNG: deadline')
        return status, headers, body.decode('utf-8', errors='replace')
    finally:
        response.close()


def _outcome(status, headers, text, max_results):
    """Return listings + static coverage warnings; never expose upstream errors."""
    if headers.get('cf-mitigated') == 'challenge':
        raise _SearxFailure('bot-wall')
    if status != 200:
        raise _SearxFailure('http_status_%d' % status)
    if 'json' in headers.get('content-type', '').lower():
        try:
            data = json.loads(text)
        except (ValueError, RecursionError):
            raise _SearxFailure('invalid_json') from None
        rows = _searxng_parse_json(data, max_results)
        unavailable = bool(data.get('unresponsive_engines'))
    else:
        soup = soup_of(text)
        if _is_bot_wall(text, soup=soup):
            raise _SearxFailure('bot-wall')
        rows = _html_rows(soup, max_results)
        unavailable = soup.select_one('#engines_msg .response-error') is not None
        if not rows and not soup.select_one('#results #urls .dialog-error-block[role="alert"]'):
            raise _SearxFailure('unrecognized_html')
    if not rows and unavailable:
        raise _SearxFailure('upstream_unavailable')
    return rows, ['upstream_unavailable'] if unavailable else []


def search_searxng(query, max_results=6, freshness=''):
    """Try only configured instances, preserving query, freshness and engines.

    A nonempty but lexically weak first response does not stop recovery. Keep
    its in-scope candidates if no later instance supplies better evidence.
    A validated JSON empty is final for that instance, without another HTML
    request. JSON-disabled (403/406) services get one HTML attempt. Genuine
    empty HTML requires the known results/empty-state structure, in any language.
    """
    if max_results <= 0:
        return []
    if engine_circuit.is_open('SearXNG'):
        return EngineUnavailable('circuit_open')
    cfg = get_config()
    started = time.monotonic()
    budget = cfg.search_deadline_secs if cfg.search_deadline_secs > 0 else 20.0
    deadline = started + min(20.0, budget)
    shuffled = list(cfg.searxng_instances)
    random.shuffle(shuffled)
    preferred = (cfg.searxng_url or '').rstrip('/')
    instances = list(dict.fromkeys(([preferred] if preferred else []) +
                                  [inst.rstrip('/') for inst in shuffled]))
    instances = [inst for inst in instances if inst][:3 if preferred else 4]
    if not instances:
        return EngineUnavailable('unconfigured')
    plan = proxy_mode_manager.attempt_plan('SearXNG', cfg)[0][1]
    proxies = {'proxies': plan} if plan is not None else {}
    params = {'q': query}
    if cfg.searxng_engines.strip():
        params['engines'] = cfg.searxng_engines.strip()
    if freshness in ('day', 'week', 'month', 'year'):
        params['time_range'] = freshness
    warnings, candidates, seen = set(), [], set()
    genuine, failures = 0, []
    scopes = site_scopes(query)
    for instance in instances:
        if time.monotonic() >= deadline:
            failures.append('deadline')
            break
        try:
            status, headers, text = _read_response(
                instance + '/search', {**params, 'format': 'json'},
                deadline=deadline, proxies=proxies)
            if status in (403, 406) and headers.get('cf-mitigated') != 'challenge':
                status, headers, text = _read_response(
                    instance + '/search', params, deadline=deadline, proxies=proxies)
            rows, partial = _outcome(status, headers, text, max_results)
            warnings.update(partial)
            if not rows:
                genuine += 1
                continue
            scoped = [row for row in rows if matches_site_constraints(row['url'], scopes)]
            if not scoped:
                failures.append('site_scope_rejected')
                continue
            genuine += 1
            strong = any(classify_relevance(row, query)['status'] == 'lexical_match' for row in scoped)
            if strong:
                # Prefer the useful response while retaining bounded earlier
                # uncertainty when space remains. Canonical URL dedup is shared.
                combined = []
                combined_seen = set()
                for row in scoped + candidates:
                    key = canonical_url_key(row['url'])
                    if key not in combined_seen:
                        combined.append(row)
                        combined_seen.add(key)
                if failures:
                    warnings.add('instance_failures')
                engine_circuit.record_success('SearXNG')
                return _SearxResults(combined[:max_results], warnings)
            for row in scoped:
                key = canonical_url_key(row['url'])
                if key not in seen and len(candidates) < max_results:
                    candidates.append(row)
                    seen.add(key)
        except Exception as error:
            # Exception strings may contain proxy credentials or response data.
            # Only fixed classifications enter diagnostics/logs.
            category = (error.code if isinstance(error, _SearxFailure)
                        else 'timeout' if isinstance(error, requests.Timeout) else 'request_failed')
            failures.append(category)
            logger.debug('[Search] SearXNG instance failed: %s', category)
    if genuine:
        if failures:
            warnings.add('instance_failures')
        engine_circuit.record_success('SearXNG')
        return _SearxResults(candidates, warnings)
    engine_circuit.record_failure('SearXNG')
    raise requests.RequestException(
        'SearXNG: all instances blocked/unreachable or invalid (%s)' % ','.join(failures[:4]))
