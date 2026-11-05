"""tofu_search/search/engines/gateway.py — Keyed search-gateway sources.

A search gateway multiplexes several commercial search APIs behind one keyed
JSON endpoint. Wire format ("universal-search v1"):

    POST <gateway_url>
    Authorization: Bearer <gateway_api_key>
    {"query": str, "isFast": bool, "timeout": <ms>, "sources": [<id>], "topK": n}
    → {"status": 0, "data": {"code": 0, "msg": str,
                             "results": [{"url", "title", "snippet", "content",
                                          "content_filled", "publish_time", ...}],
                             "internal_err_map": {"[Main]<id>": str}}}

Failures arrive as HTTP 200 with ``data.code`` != 0 (400 invalid request,
1029 rate limited, 500 upstream failure). ``data.searchParameters`` echoes the
caller's key, so response bodies are never logged or copied into errors.

Each configured source is its own engine tagged ``API:<id>``. One merged
multi-source request truncates the union to ``topK`` (observed: every row came
from a single source), which erases the per-source ranks that reciprocal-rank
fusion uses and makes every source wait for the slowest one.

Entry: search_gateway_source(source_id, query, max_results, freshness)
Config: SearchConfig.gateway_url / gateway_api_key / gateway_sources /
        gateway_full_content / gateway_timeout_secs
"""

import hashlib
import json
import re
import threading
import time
import unicodedata
from datetime import date, timedelta
from importlib import import_module
from urllib.parse import urlsplit, urlunsplit

import requests

from tofu_search.config import get_config
from tofu_search.http_client import http_post
from tofu_search.log import get_logger
from tofu_search.search._common import EngineUnavailable, engine_circuit, make_result

logger = get_logger(__name__)

__all__ = ['GATEWAY_TAG_PREFIX', 'GatewayError', 'search_gateway_source']

GATEWAY_TAG_PREFIX = 'API:'

_RESPONSE_BYTE_LIMIT = 8 * 1024 * 1024
_MAX_TOP_K = 50
_RATE_LIMIT_CODE = 1029
_RATE_LIMIT_COOLDOWN_SECS = 60.0
# A provider body must be page-sized before it can replace our own fetch; one
# source answered full-content requests with 65-character placeholders.
_MIN_PROVIDER_CONTENT_CHARS = 200
_FRESHNESS_DAYS = {'day': 1, 'week': 7, 'month': 31, 'year': 366}
_DATE_PREFIX = re.compile(r'^\s*(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})')
_CONTROL_CHARS = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')

_cooldown_lock = threading.Lock()
_cooldown_until: dict[tuple[str, str, str], float] = {}


class GatewayError(RuntimeError):
    """A gateway failure whose message is safe to surface (no key, no body)."""


def _cooldown_key(url: str, api_key: str, source_id: str) -> tuple[str, str, str]:
    return url, hashlib.sha256(api_key.encode('utf-8')).hexdigest()[:16], source_id


def _cooling_down(key: tuple[str, str, str]) -> bool:
    with _cooldown_lock:
        until = _cooldown_until.get(key)
        if until is None:
            return False
        if time.monotonic() >= until:
            del _cooldown_until[key]
            return False
        return True


def _start_cooldown(key: tuple[str, str, str]) -> None:
    now = time.monotonic()
    with _cooldown_lock:
        # Expired entries go first, so rotated keys or removed sources never accumulate.
        for stale in [k for k, until in _cooldown_until.items() if until <= now]:
            del _cooldown_until[stale]
        _cooldown_until[key] = now + _RATE_LIMIT_COOLDOWN_SECS


def _safe_message(value, api_key: str, limit: int = 160) -> str:
    text = ' '.join(str(value or '').split())
    if api_key:
        text = text.replace(api_key, '***')
    return text[:limit]


def _published_date(value) -> str:
    """Normalize the provider's many date spellings to ``YYYY-MM-DD``."""
    match = _DATE_PREFIX.match(str(value or ''))
    if not match:
        return ''
    try:
        return date(*(int(part) for part in match.groups())).isoformat()
    except ValueError:
        return ''


def _source_url(raw_url: str) -> str:
    """Unwrap Bing click-tracking links; every other URL passes unchanged.

    Gateways re-host Bing's ``/ck/a?...&u=a1<base64url>`` redirect on their own
    domain (observed: ``api.microsoftol.com``), which the SERP decoder rightly
    leaves alone for arbitrary hosts. Gateway rows are API results, never
    pages, so the same strict decoder applies to that path on any host; a
    wrapper that does not decode stays as received.
    """
    bing = import_module('tofu_search.search.engines.bing')
    parts = urlsplit(raw_url)
    if parts.path == '/ck/a' and parts.query:
        decoded = bing._bing_decode_url(
            urlunsplit(('https', 'www.bing.com', parts.path, parts.query, '')))
        if decoded:
            return decoded
    return bing._bing_decode_url(raw_url) or ''


def _clean_body(text: str) -> str:
    return unicodedata.normalize('NFC', _CONTROL_CHARS.sub('', text)).strip()


def _parse_gateway_payload(payload, *, source_id: str, tag: str, api_key: str,
                           full_content: bool, freshness: str = '',
                           today: date | None = None):
    """Return result dicts, or ``None`` when the gateway reports a rate limit.

    Raises:
        GatewayError: the envelope is malformed or reports a failure.
    """
    if not isinstance(payload, dict):
        raise GatewayError('malformed gateway response')
    data = payload.get('data')
    if payload.get('status') not in (0, None) or not isinstance(data, dict):
        raise GatewayError('gateway status %s: %s' % (
            payload.get('status'), _safe_message(payload.get('message'), api_key)))
    code = data.get('code')
    if code == _RATE_LIMIT_CODE:
        return None
    if code not in (0, None):
        detail = _safe_message(data.get('msg'), api_key)
        upstream = data.get('internal_err_map')
        if isinstance(upstream, dict) and upstream.get('[Main]' + source_id):
            detail += ' (%s)' % _safe_message(upstream['[Main]' + source_id], api_key)
        raise GatewayError('gateway code %s: %s' % (code, detail))

    cutoff = None
    if freshness in _FRESHNESS_DAYS:
        cutoff = (today or date.today()) - timedelta(days=_FRESHNESS_DAYS[freshness])
    rows = data.get('results')
    results = []
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict):
            continue
        url = _source_url(str(row.get('url') or '').strip())
        if not url.startswith(('http://', 'https://')):
            continue
        published = _published_date(row.get('publish_time'))
        # Best effort: the protocol has no time filter, so drop only rows whose
        # known date falls outside the window and keep undated rows.
        if cutoff and published and date.fromisoformat(published) < cutoff:
            continue
        snippet = str(row.get('snippet') or row.get('content') or '')
        result = make_result(str(row.get('title') or '') or url, snippet, url, tag)
        if published:
            result['published_date'] = published
        if full_content and row.get('content_filled') is True:
            body = _clean_body(str(row.get('content') or ''))
            if len(body) >= _MIN_PROVIDER_CONTENT_CHARS and len(body) > len(result['snippet']):
                result['full_content'] = body
        results.append(result)
    return results


def _post_json(url: str, body: dict, api_key: str, timeout_secs: float):
    deadline = time.monotonic() + timeout_secs
    response = http_post(
        url, json=body, timeout=(5, timeout_secs), stream=True,
        headers={'Authorization': 'Bearer ' + api_key})
    try:
        if response.status_code != 200:
            raise GatewayError('gateway HTTP %d' % response.status_code)
        length = response.headers.get('content-length')
        if length is not None and (not length.isdigit() or int(length) > _RESPONSE_BYTE_LIMIT):
            raise GatewayError('gateway response length rejected')
        raw = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            raw.extend(chunk)
            if len(raw) > _RESPONSE_BYTE_LIMIT:
                raise GatewayError('gateway response exceeds byte budget')
            if time.monotonic() > deadline:
                raise GatewayError('gateway timed out after %ds' % timeout_secs)
    finally:
        response.close()
    try:
        return json.loads(bytes(raw))
    except ValueError:
        raise GatewayError('gateway response is not JSON') from None


def search_gateway_source(source_id, query, max_results=10, freshness=''):
    """Query one gateway source and return tofu-search result dicts.

    Returns ``EngineUnavailable`` when the gateway is unconfigured, the source
    is cooling down after a rate limit, or its circuit is open after repeated
    failures; raises ``GatewayError`` on any other gateway, transport or
    protocol failure.
    """
    config = get_config()
    url = str(config.gateway_url or '').strip()
    api_key = str(config.gateway_api_key or '').strip()
    source_id = str(source_id or '').strip()
    if not url or not api_key or not source_id:
        return EngineUnavailable('not_configured')
    tag = GATEWAY_TAG_PREFIX + source_id
    cooldown_key = _cooldown_key(url, api_key, source_id)
    if _cooling_down(cooldown_key):
        return EngineUnavailable('rate_limited')
    if engine_circuit.is_open(tag):
        return EngineUnavailable('circuit_open')

    timeout_secs = max(1, int(config.gateway_timeout_secs or 10))
    deadline_secs = int(getattr(config, 'search_deadline_secs', 0) or 0)
    if deadline_secs > 0:
        timeout_secs = max(1, min(timeout_secs, deadline_secs - 2))
    body = {
        'query': query,
        'isFast': not config.gateway_full_content,
        'timeout': timeout_secs * 1000,
        'sources': [source_id],
        'topK': max(1, min(int(max_results or 10), _MAX_TOP_K)),
    }
    try:
        try:
            payload = _post_json(url, body, api_key, timeout_secs + 3)
        except requests.Timeout:
            raise GatewayError('gateway timed out after %ds' % (timeout_secs + 3)) from None
        except requests.RequestException as exc:
            raise GatewayError('gateway request failed (%s)' % type(exc).__name__) from None
        results = _parse_gateway_payload(
            payload, source_id=source_id, tag=tag, api_key=api_key,
            full_content=bool(config.gateway_full_content), freshness=freshness)
    except GatewayError:
        engine_circuit.record_failure(tag)
        raise
    if results is None:
        _start_cooldown(cooldown_key)
        logger.warning('[Search] %s rate limited by the gateway; cooling down %ds',
                       tag, int(_RATE_LIMIT_COOLDOWN_SECS))
        return EngineUnavailable('rate_limited')
    engine_circuit.record_success(tag)
    return results
