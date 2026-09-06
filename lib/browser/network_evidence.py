"""Bounded, redacted business-data extraction from browser network captures.

The browser extension owns transport and returns response bodies captured in
the user's authenticated page context.  This module is the only place that
turns those wire records into model-visible evidence: it applies owner-scoped
read policy to every response URL, removes likely telemetry, redacts credential
fields, ranks business-shaped JSON, and enforces one shared character budget.

Entry points:
``analyze_network_evidence`` emits token-cheap endpoint shapes,
``extract_business_records`` normalizes list-shaped responses for adapters,
``render_network_evidence`` formats useful response bodies, and
``merge_page_and_network`` combines them with rendered DOM text.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from lib.log import get_logger, log_event


logger = get_logger(__name__)

_NOISE_URL_RE = re.compile(
    r'(?:analytics|beacon|collect|telemetry|tracking|sentry|doubleclick|'
    r'google-analytics|googletagmanager|adservice|/ads?(?:[/?#]|$)|'
    r'metrics?|pixel|/events?(?:[/?#]|$)|/logs?(?:[/?#]|$))',
    re.I,
)
_BUSINESS_KEY_RE = re.compile(
    r'^(?:data|items?|results?|records?|list|rows?|edges?|nodes?|skills?|'
    r'title|name|text|content|body|description|desc|price|amount|id|url|'
    r'author|owner|total|page|cursor|next|rank|score|date|time|count)$',
    re.I,
)
_TRACKING_KEY_RE = re.compile(
    r'^(?:event|events|trace|traceid|clientid|visitorid|experiment|abtest|'
    r'beacon|analytics|metrics?|pixel|logs?)$',
    re.I,
)
_SECRET_KEY_PARTS = (
    'accesstoken', 'refreshtoken', 'authorization', 'password', 'passwd',
    'secret', 'cookie', 'credential', 'sessionid', 'ssoid', 'ticket',
)
_SECRET_KEY_EXACT = frozenset({'token', 'session', 'jwt', 'apikey', 'api_key'})
_BEARER_RE = re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}')
_TEXT_SECRET_RE = re.compile(
    r'''(?ix)
    (["']?(?:access[_-]?token|refresh[_-]?token|authorization|password|passwd|
       secret|cookie|credential|session[_-]?id|sso[_-]?id|ticket|api[_-]?key)
     ["']?\s*[:=]\s*["']?)
    ([^"'\s,;}]+)
    ''',
)

_MAX_RESPONSE_RECORDS = 80
_MAX_JSON_DEPTH = 16
_MAX_SCORE_NODES = 2_000
_MAX_SHAPE_ENTRIES = 48
_MAX_RECORD_SCAN_NODES = 3_000
_SPARSE_PAGE_CHARS = 400
_POLICY_FAILURE_LOG_INTERVAL_S = 60.0
_policy_failure_log_lock = threading.Lock()
_last_policy_failure_log_at = 0.0


def _character_budget(value) -> int:
    try:
        return max(0, min(80_000, int(value or 0)))
    except (TypeError, ValueError):
        return 0


def _truncate_text(value: str, limit: int) -> str:
    text = str(value or '')
    limit = max(0, int(limit or 0))
    if len(text) <= limit:
        return text
    marker = '\n[…context truncated]'
    if limit <= len(marker):
        return text[:limit]
    return text[:limit - len(marker)] + marker


def _is_sensitive_key(key: object) -> bool:
    raw = str(key or '').lower()
    compact = re.sub(r'[^a-z0-9]', '', raw)
    return compact in _SECRET_KEY_EXACT or any(
        part in compact for part in _SECRET_KEY_PARTS)


def _redact_text(value: str) -> str:
    value = _BEARER_RE.sub('Bearer [redacted]', str(value or ''))
    return _TEXT_SECRET_RE.sub(r'\1[redacted]', value)


def _redact_json(value, *, depth=0):
    if depth >= _MAX_JSON_DEPTH:
        return '[depth limit]'
    if isinstance(value, dict):
        return {
            str(key): ('[redacted]' if _is_sensitive_key(key)
                       else _redact_json(child, depth=depth + 1))
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(child, depth=depth + 1) for child in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _redact_url(url: str) -> str:
    try:
        parts = urlsplit(str(url or ''))
        query = [
            (key, '[redacted]' if _is_sensitive_key(key) else _redact_text(val))
            for key, val in parse_qsl(parts.query, keep_blank_values=True)
        ]
        return urlunsplit((parts.scheme, parts.netloc, parts.path,
                           urlencode(query, doseq=True), ''))
    except (TypeError, ValueError):
        return _redact_text(str(url or ''))


def _network_rows(payload: dict) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    network = payload.get('network')
    if isinstance(network, dict):
        rows = network.get('responses')
    else:
        rows = payload.get('networkResponses')
    if not isinstance(rows, list):
        return []
    return [row for row in rows[:_MAX_RESPONSE_RECORDS]
            if isinstance(row, dict)]


def _parse_body(row: dict):
    body = row.get('body')
    if body is None:
        body = row.get('responsePreview')
    if body is None:
        body = row.get('responseBody')
    if isinstance(body, (dict, list, int, float, bool)):
        return body
    if not isinstance(body, str) or not body.strip():
        return None
    stripped = body.strip()
    try:
        return json.loads(stripped)
    except (TypeError, ValueError):
        return stripped


def _shape_counts(value) -> tuple[int, int, bool]:
    """Return business-key count, tracking-key count, and non-empty-array flag."""
    business = 0
    tracking = 0
    has_array = False
    stack = [(value, 0)]
    visited = 0
    while stack and visited < _MAX_SCORE_NODES:
        current, depth = stack.pop()
        visited += 1
        if depth > 5:
            continue
        if isinstance(current, list):
            has_array = has_array or bool(current)
            stack.extend((child, depth + 1) for child in current[:3])
        elif isinstance(current, dict):
            for key, child in list(current.items())[:40]:
                if _BUSINESS_KEY_RE.match(str(key)):
                    business += 1
                if _TRACKING_KEY_RE.match(str(key)):
                    tracking += 1
                if isinstance(child, (dict, list)):
                    stack.append((child, depth + 1))
    return business, tracking, has_array


def _status(row: dict) -> int:
    raw = row.get('status', row.get('responseStatus', 0))
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _content_type(row: dict) -> str:
    return str(row.get('contentType') or row.get('responseContentType')
               or row.get('ct') or '')


def _score_response(row: dict, body) -> float:
    url = str(row.get('url') or '')
    status = _status(row)
    content_type = _content_type(row)
    score = 0.0
    if 200 <= status < 300:
        score += 2.0
    elif status in (401, 403):
        score -= 4.0
    elif status >= 400:
        score -= 2.0
    if 'json' in content_type.lower() or isinstance(body, (dict, list)):
        score += 3.0
    elif 'html' in content_type.lower():
        score -= 2.0
    elif any(part in content_type.lower()
             for part in ('text', 'xml', 'javascript')):
        score += 0.5
    if _NOISE_URL_RE.search(url):
        score -= 7.0
    if isinstance(body, list):
        score += 3.0 if body else -2.0
    elif isinstance(body, dict):
        score += 1.0 if body else -2.0
        business, tracking, has_array = _shape_counts(body)
        score += min(4.0, business * 0.35)
        if has_array:
            score += 2.0
        if tracking and not business:
            score -= min(4.0, tracking * 0.8)
    elif isinstance(body, str):
        if re.match(r'(?is)^\s*(?:<!doctype\s+html|<html\b)', body):
            score -= 3.0
        elif len(body.strip()) >= 40:
            score += 1.0
    return score


def _response_allowed(owner_user_id: str, url: str) -> bool:
    owner = str(owner_user_id or '').strip()
    if not owner.isdigit() or int(owner) < 1 or not url:
        return False
    # Page captures also see extension/content-script requests (e.g. a
    # translation add-on fetching chrome-extension://<id>/config.json);
    # those bodies are not page evidence and must never reach model context.
    if urlsplit(str(url)).scheme.lower() not in ('http', 'https', 'ws', 'wss'):
        return False
    try:
        from lib.browser.access import is_read_allowed
        return bool(is_read_allowed(owner, url))
    except Exception as exc:
        _log_policy_failure(exc)
        return False


def _log_policy_failure(exc: Exception) -> None:
    """Emit at most one durable fail-closed checkpoint per minute."""
    global _last_policy_failure_log_at
    now = time.monotonic()
    with _policy_failure_log_lock:
        if now - _last_policy_failure_log_at < _POLICY_FAILURE_LOG_INTERVAL_S:
            return
        _last_policy_failure_log_at = now
    log_event(
        logger,
        logging.WARNING,
        'browser.network_evidence_policy_failure',
        '[BrowserEvidence] response policy check failed closed; captured '
        'bodies were withheld: %s',
        exc,
        exception_type=type(exc).__name__,
    )


def _serialize_body(body) -> str:
    redacted = _redact_json(body)
    if isinstance(redacted, str):
        return redacted
    return json.dumps(redacted, ensure_ascii=False, indent=2,
                      separators=(',', ': '))


def redact_value(value):
    """Return a recursively redacted JSON-compatible value.

    Research and site-adapter code use this public seam instead of duplicating
    the credential taxonomy owned by this module.
    """
    return _redact_json(value)


def redact_url(url: str) -> str:
    """Return a URL with credential-shaped query values removed."""
    return _redact_url(url)


def _shape_descriptor(value) -> str:
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, str):
        return 'string' if len(value) <= 80 else f'string(len={len(value)})'
    if isinstance(value, (int, float)):
        return 'number'
    if isinstance(value, list):
        return f'array({len(value)})'
    if isinstance(value, dict):
        return 'object' if value else 'object(empty)'
    return type(value).__name__


def _infer_shape(value) -> dict[str, str]:
    """Infer one bounded first-item JSON shape without copying body values."""
    shape: dict[str, str] = {}
    stack = [('$', value, 0)]
    while stack and len(shape) < _MAX_SHAPE_ENTRIES:
        path, current, depth = stack.pop()
        shape[path] = _shape_descriptor(current)
        if depth >= 6:
            continue
        if isinstance(current, list) and current:
            stack.append((path + '[0]', current[0], depth + 1))
        elif isinstance(current, dict):
            for key, child in reversed(list(current.items())[:24]):
                key_text = str(key)
                if re.fullmatch(r'[A-Za-z_$][\w$]*', key_text):
                    child_path = f'{path}.{key_text}'
                else:
                    child_path = f'{path}[{json.dumps(key_text, ensure_ascii=False)}]'
                stack.append((child_path, child, depth + 1))
    if stack:
        shape['(truncated)'] = (
            f'reached {_MAX_SHAPE_ENTRIES}-entry budget')
    return shape


def _endpoint_key(row: dict) -> str:
    operation = str(row.get('operationName') or '').strip()
    if operation:
        return f'GraphQL:{operation[:120]}'
    method = str(row.get('method') or 'GET').upper()[:12]
    try:
        parts = urlsplit(str(row.get('url') or ''))
        endpoint = (parts.hostname or '') + (parts.path or '/')
    except ValueError:
        endpoint = str(row.get('url') or '').split('?', 1)[0]
    return f'{method} {endpoint[:300]}'


def _score_reasons(row: dict, body, score: float) -> list[str]:
    reasons = []
    status = _status(row)
    content_type = _content_type(row).lower()
    if 200 <= status < 300:
        reasons.append('successful response')
    elif status in (401, 403):
        reasons.append('authentication required')
    if 'json' in content_type or isinstance(body, (dict, list)):
        reasons.append('JSON-shaped response')
    if _NOISE_URL_RE.search(str(row.get('url') or '')):
        reasons.append('telemetry-like URL')
    if isinstance(body, (dict, list)):
        business, tracking, has_array = _shape_counts(body)
        if business:
            reasons.append(f'{business} business-like keys')
        if has_array:
            reasons.append('non-empty list data')
        if tracking and not business:
            reasons.append('tracking-shaped fields without business keys')
    if not reasons:
        reasons.append(f'body score {score:.1f}')
    return reasons[:5]


def analyze_network_evidence(
    payload: dict,
    *,
    owner_user_id: str,
    max_entries: int = 12,
) -> dict:
    """Return a bounded, redacted endpoint inventory for site reconnaissance.

    Repeated pagination calls collapse under a stable method/host/path key.
    Response values never enter this report; only redacted URLs and inferred
    shapes do. Body rendering remains an explicit separate operation.
    """
    try:
        entry_limit = max(1, min(20, int(max_entries)))
    except (TypeError, ValueError):
        entry_limit = 12
    grouped: dict[str, dict] = {}
    denied = 0
    for row in _network_rows(payload):
        url = str(row.get('url') or '')
        if not _response_allowed(owner_user_id, url):
            denied += 1
            continue
        status = _status(row)
        body = _parse_body(row)
        score = _score_response(row, body)
        blocked = status in (401, 403)
        if not blocked and (body is None or score < 3.0):
            continue
        key = _endpoint_key(row)
        if blocked:
            verdict = 'blocked'
        elif score >= 7.0:
            verdict = 'likely_data'
        else:
            verdict = 'maybe_data'
        candidate = {
            'key': key,
            'method': str(row.get('method') or 'GET').upper()[:12],
            'url': _redact_url(url),
            'status': status,
            'content_type': _content_type(row).split(';', 1)[0][:80],
            'real_data_score': round(max(0.0, min(1.0, (score + 1.0) / 12.0)), 2),
            'verdict': verdict,
            'reasons': _score_reasons(row, body, score),
            'shape': _infer_shape(body) if body is not None else {},
            'observations': 1,
            'body_truncated': bool(
                row.get('bodyTruncated') or row.get('responseBodyTruncated')),
        }
        prior = grouped.get(key)
        if prior is None:
            grouped[key] = candidate
            continue
        prior['observations'] += 1
        prior['body_truncated'] = bool(
            prior['body_truncated'] or candidate['body_truncated'])
        if candidate['real_data_score'] > prior['real_data_score']:
            observations = prior['observations']
            grouped[key] = candidate
            grouped[key]['observations'] = observations
    order = {'likely_data': 0, 'maybe_data': 1, 'blocked': 2}
    candidates = sorted(
        grouped.values(),
        key=lambda item: (
            order.get(item['verdict'], 9), -item['real_data_score'],
            item['key']),
    )[:entry_limit]
    network = payload.get('network') if isinstance(payload, dict) else {}
    network = network if isinstance(network, dict) else {}
    return {
        'candidates': candidates,
        'candidate_count': len(candidates),
        'denied_response_count': denied,
        'capture': {
            'body_capture': bool(network.get('bodyCapture')),
            'response_count': len(_network_rows(payload)),
            'dropped_entries': int(network.get('droppedEntries') or 0),
            'dropped_bodies': int(network.get('droppedBodies') or 0),
            'websocket_frames': int(network.get('webSocketFrameCount') or 0),
            'priority_hint_count': int(network.get('priorityHintCount') or 0),
            'priority_body_matches': int(network.get('priorityBodyMatches') or 0),
            'priority_reserve_chars': int(network.get('priorityReserveChars') or 0),
        },
    }


_TITLE_KEYS = (
    'name', 'title', 'skillName', 'displayName', 'label', 'subject',
)
_URL_KEYS = ('url', 'detailUrl', 'skillUrl', 'link', 'href', 'webUrl')
_DESCRIPTION_KEYS = (
    'description', 'desc', 'summary', 'snippet', 'introduction', 'content',
)
_ID_KEYS = ('id', 'skillId', 'uuid', 'key', 'slug')


def _first_scalar(record: dict, keys: tuple[str, ...]):
    lowered = {str(key).lower(): value for key, value in record.items()}
    for key in keys:
        value = record.get(key, lowered.get(key.lower()))
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return ''


def extract_business_records(
    payload: dict,
    *,
    owner_user_id: str,
    source_url: str = '',
    query: str = '',
    limit: int = 30,
) -> list[dict]:
    """Normalize the strongest captured list of objects into adapter cards."""
    try:
        item_limit = max(1, min(100, int(limit)))
    except (TypeError, ValueError):
        item_limit = 30
    arrays = []
    seen_bodies = set()
    for row in _network_rows(payload):
        if not _response_allowed(owner_user_id, str(row.get('url') or '')):
            continue
        body = _parse_body(row)
        if not isinstance(body, (dict, list)) or _score_response(row, body) < 3:
            continue
        digest = hashlib.sha256(
            json.dumps(body, ensure_ascii=False, sort_keys=True, default=str)
            .encode('utf-8', 'replace')).digest()
        if digest in seen_bodies:
            continue
        seen_bodies.add(digest)
        stack = [(body, '$', 0)]
        scanned = 0
        while stack and scanned < _MAX_RECORD_SCAN_NODES:
            current, path, depth = stack.pop()
            scanned += 1
            if depth > 7:
                continue
            if isinstance(current, list):
                objects = [item for item in current if isinstance(item, dict)]
                if objects:
                    keys = {str(key) for item in objects[:5] for key in item}
                    business_keys = sum(
                        1 for key in keys if _BUSINESS_KEY_RE.match(key))
                    identity = sum(
                        1 for key in keys
                        if key in _TITLE_KEYS + _ID_KEYS + _URL_KEYS)
                    arrays.append((
                        min(len(objects), 100) + business_keys * 3 + identity * 5,
                        path, objects,
                    ))
                for index, child in enumerate(current[:5]):
                    if isinstance(child, (dict, list)):
                        stack.append((child, f'{path}[{index}]', depth + 1))
            elif isinstance(current, dict):
                for key, child in list(current.items())[:40]:
                    if isinstance(child, (dict, list)):
                        stack.append((child, f'{path}.{key}', depth + 1))
    if not arrays:
        return []
    arrays.sort(key=lambda item: (-item[0], item[1]))
    records = arrays[0][2]
    needle = str(query or '').strip().lower()
    normalized = []
    seen = set()
    for raw in records:
        safe = redact_value(raw)
        if not isinstance(safe, dict):
            continue
        searchable = json.dumps(safe, ensure_ascii=False, default=str).lower()
        if needle and needle not in searchable:
            continue
        title = _first_scalar(safe, _TITLE_KEYS)
        record_id = _first_scalar(safe, _ID_KEYS)
        if not title:
            title = record_id
        if not title:
            continue
        raw_url = _first_scalar(safe, _URL_KEYS)
        item_url = urljoin(source_url, raw_url) if raw_url else source_url
        snippet = _first_scalar(safe, _DESCRIPTION_KEYS)[:500]
        dedupe_key = (record_id or title, item_url)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        metadata = {
            str(key): value for key, value in safe.items()
            if str(key) not in _TITLE_KEYS + _URL_KEYS + _DESCRIPTION_KEYS
            and isinstance(value, (str, int, float, bool, type(None)))
        }
        normalized.append({
            'id': record_id,
            'title': title[:240],
            'url': item_url,
            'snippet': snippet,
            'metadata': dict(list(metadata.items())[:24]),
        })
        if len(normalized) >= item_limit:
            break
    # A server-side query filter may not match localized/encoded results even
    # though the site already applied the query. Fall back to the captured list
    # instead of turning a successful remote search into a false empty result.
    if needle and not normalized:
        return extract_business_records(
            payload, owner_user_id=owner_user_id, source_url=source_url,
            query='', limit=item_limit)
    return normalized


def render_network_evidence(
    payload: dict,
    *,
    owner_user_id: str,
    max_chars: int = 30_000,
) -> str:
    """Return ranked response bodies as bounded model context.

    Empty output means no captured response passed all four gates: owner read
    policy, usable body, non-noise score, and the shared character budget.
    """
    budget = _character_budget(max_chars)
    if budget < 256:
        return ''
    candidates = []
    seen_bodies = set()
    for row in _network_rows(payload):
        url = str(row.get('url') or '')
        if not _response_allowed(owner_user_id, url):
            continue
        body = _parse_body(row)
        if body is None:
            continue
        serialized = _serialize_body(body)
        if len(serialized.strip()) < 20:
            continue
        digest = hashlib.sha256(serialized.encode('utf-8', 'replace')).digest()
        if digest in seen_bodies:
            continue
        seen_bodies.add(digest)
        score = _score_response(row, body)
        if score < 3.0:
            continue
        candidates.append((score, row, serialized))
    candidates.sort(key=lambda item: (-item[0], -_status(item[1]),
                                      str(item[1].get('url') or '')))
    if not candidates:
        return ''

    parts = ['Captured API data from the authenticated browser session:']
    used = len(parts[0])
    included = 0
    for _score, row, serialized in candidates:
        status = _status(row)
        method = str(row.get('method') or 'GET').upper()[:12]
        url = _redact_url(str(row.get('url') or ''))
        content_type = _content_type(row).split(';', 1)[0][:80]
        truncation = ' (browser body truncated)' if (
            row.get('bodyTruncated') or row.get('responseBodyTruncated')) else ''
        header = f'\n\n[{method} {status} {content_type}] {url}{truncation}\n'
        remaining = budget - used - len(header)
        if remaining <= 64:
            break
        if len(serialized) > remaining:
            serialized = serialized[:max(0, remaining - 24)] + '\n[…context truncated]'
        parts.extend((header, serialized))
        used += len(header) + len(serialized)
        included += 1
        if used >= budget:
            break
    return ''.join(parts) if included else ''


def merge_page_and_network(
    page_text: str,
    network_text: str,
    *,
    max_chars: int,
) -> str:
    """Combine DOM and API evidence without exceeding one context budget."""
    budget = _character_budget(max_chars)
    if budget <= 0:
        return ''
    page = str(page_text or '').strip()
    network = str(network_text or '').strip()
    if not network:
        return page[:budget]
    if not page:
        return network[:budget]
    separator = '\n\n---\n\n'
    if len(page) < _SPARSE_PAGE_CHARS:
        # A SPA shell's raw API response is the authoritative content. Retain
        # its small visible shell only as orientation after the structured data.
        first, second = network, 'Rendered page text:\n' + page
    else:
        first, second = page, network
    first_budget = max(0, budget - len(separator) - min(len(second), budget // 2))
    first = _truncate_text(first, first_budget)
    remaining = max(0, budget - len(first) - len(separator))
    second = _truncate_text(second, remaining)
    if not first:
        return _truncate_text(second, budget)
    if not second or len(first) + len(separator) > budget:
        return _truncate_text(first, budget)
    return _truncate_text(first + separator + second, budget)


__all__ = [
    'analyze_network_evidence', 'extract_business_records',
    'merge_page_and_network', 'redact_url', 'redact_value',
    'render_network_evidence',
]
