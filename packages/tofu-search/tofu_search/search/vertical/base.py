"""Shared infrastructure for vertical-search handlers.

Every per-vertical module imports this module (``from ... import base``) and
calls ``base.http_get`` / ``base._fetch_json`` / ``base._post_json`` so the HTTP
seam stays in one place and is uniformly patchable in tests.
"""

import json
import time

from tofu_search.http_client import http_get, http_post
from tofu_search.log import get_logger

logger = get_logger(__name__)

_TIMEOUT = 10
_HEADERS = {'User-Agent': 'Mozilla/5.0 (compatible; TofuBot/1.0)'}

# Sentinel distinguishing "request/parse failed" from "API said no data".
_FETCH_FAILED = object()

# Distinct from _FETCH_FAILED: the server actively rejected our credentials
# (HTTP 401/403). Callers use this to stop hammering an endpoint that needs a
# key rather than treating it as a transient transport failure.
_FETCH_UNAUTHORIZED = object()


def _fetch_json(url, *, params=None, headers=None, timeout=_TIMEOUT,
                label='', retry_on_429=True):
    """GET ``url`` and return parsed JSON, or ``_FETCH_FAILED`` on any error.

    Centralises the ``http_get → check .ok → .json() → except`` boilerplate
    repeated across every vertical handler, plus a single bounded retry on
    HTTP 429 (rate-limited). Returns the sentinel ``_FETCH_FAILED`` — NOT
    ``None`` — so callers can tell a transport/parse failure apart from a
    successful response that simply carried no useful data.
    """
    hdrs = headers or _HEADERS
    for attempt in (0, 1):
        try:
            resp = http_get(url, params=params, headers=hdrs, timeout=timeout)
        except Exception as e:
            logger.warning('[Vertical] %s request failed for %s: %s', label or 'fetch', url[:80], e)
            return _FETCH_FAILED
        if resp.status_code == 429 and retry_on_429 and attempt == 0:
            logger.info('[Vertical] %s rate-limited (429), retry in 1s', label or 'fetch')
            time.sleep(1.0)
            continue
        if not resp.ok:
            logger.warning('[Vertical] %s returned HTTP %d for %s',
                           label or 'fetch', resp.status_code, url[:80])
            return _FETCH_FAILED
        try:
            return resp.json()
        except Exception as e:
            logger.warning('[Vertical] %s JSON parse failed for %s: %s', label or 'fetch', url[:80], e)
            return _FETCH_FAILED
    return _FETCH_FAILED


def _parse_sse_frames(text):
    """Return the LAST JSON object carried by an SSE body, or None.

    MCP's ``streamable-http`` transport is defined as returning *either*
    ``application/json`` or ``text/event-stream``; a server may answer a single
    ``tools/call`` with a one-frame event stream. Frames look like::

        event: message
        data: {"jsonrpc":"2.0","id":1,"result":{...}}

    Multi-line ``data:`` continuations are joined, and the last parseable frame
    wins (progress notifications may precede the real result).
    """
    payloads = []
    buf = []
    for raw in text.splitlines():
        line = raw.rstrip('\r')
        if line.startswith('data:'):
            buf.append(line[5:].lstrip())
        elif not line.strip():
            if buf:
                payloads.append('\n'.join(buf))
                buf = []
    if buf:
        payloads.append('\n'.join(buf))
    for chunk in reversed(payloads):
        try:
            return json.loads(chunk)
        except Exception:
            continue
    return None


def _decode_body(resp, label=''):
    """Decode a response body that may be JSON or an SSE-wrapped JSON frame."""
    ctype = ''
    try:
        ctype = (resp.headers.get('Content-Type') or '').lower()
    except Exception:
        pass
    text = resp.text or ''
    if 'text/event-stream' in ctype or text.lstrip()[:6] in ('event:', 'data: ') \
            or text.lstrip().startswith(('event:', 'data:')):
        obj = _parse_sse_frames(text)
        if obj is None:
            logger.warning('[Vertical] %s SSE body carried no parseable JSON frame',
                           label or 'post')
            return _FETCH_FAILED
        return obj
    try:
        return resp.json()
    except Exception as e:
        logger.warning('[Vertical] %s JSON parse failed: %s', label or 'post', e)
        return _FETCH_FAILED


def _post_json(url, *, payload=None, headers=None, timeout=_TIMEOUT, label='',
               retry_on_429=True, return_response=False,
               return_error_response=False, raw_body=None):
    """POST ``payload`` as JSON and return the decoded JSON object.

    The POST counterpart of :func:`_fetch_json`, and the single transport seam
    the MCP-backed verticals go through. Handles the body decode for BOTH
    content types ``streamable-http`` is allowed to answer with (raw JSON and a
    ``text/event-stream`` frame) because that choice is a property of the
    transport, not of the caller.

    Returns the parsed object, ``_FETCH_UNAUTHORIZED`` on HTTP 401/403, or
    ``_FETCH_FAILED`` on any other failure. With ``return_response=True`` the
    raw ``Response`` is returned instead (used only by the MCP session-handshake
    fallback, which needs the ``Mcp-Session-Id`` response header).  With
    ``return_error_response=True``, a non-auth HTTP error is returned raw so a
    protocol adapter can distinguish an old-server rejection from a network
    failure; ordinary callers should keep the default sentinel behaviour.
    """
    hdrs = dict(_HEADERS)
    if headers:
        hdrs.update(headers)
    for attempt in (0, 1):
        try:
            # raw_body is sent byte-for-byte (data=) — used by signed upstreams
            # whose signature covers the exact body bytes, which a json=
            # re-serialisation would change.
            if raw_body is not None:
                resp = http_post(url, data=raw_body, headers=hdrs, timeout=timeout)
            else:
                resp = http_post(url, json=payload, headers=hdrs, timeout=timeout)
        except Exception as e:
            logger.warning('[Vertical] %s POST failed for %s: %s', label or 'post', url[:80], e)
            return _FETCH_FAILED
        if resp.status_code == 429 and retry_on_429 and attempt == 0:
            logger.info('[Vertical] %s rate-limited (429), retry in 1s', label or 'post')
            time.sleep(1.0)
            continue
        if resp.status_code in (401, 403):
            logger.warning('[Vertical] %s unauthorized (HTTP %d) for %s',
                           label or 'post', resp.status_code, url[:80])
            return _FETCH_UNAUTHORIZED
        if not resp.ok:
            logger.warning('[Vertical] %s returned HTTP %d for %s',
                           label or 'post', resp.status_code, url[:80])
            return resp if return_error_response else _FETCH_FAILED
        return resp if return_response else _decode_body(resp, label)
    return _FETCH_FAILED
