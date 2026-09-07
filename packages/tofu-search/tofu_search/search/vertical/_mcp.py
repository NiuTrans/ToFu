"""Minimal MCP ``streamable-http`` client for vertical handlers.

Scope is deliberately narrow: issue ONE ``tools/call`` against a remote MCP
endpoint and hand back the unwrapped business payload. This is not a full MCP
client — there is no capability negotiation, no notification stream, no tool
discovery. Hosts that want the whole protocol (booking chains, OAuth, order
management) should keep using a real MCP client; the vertical layer only needs
a stateless read.

Two protocol details this module exists to absorb:

1. A ``streamable-http`` server may answer with ``text/event-stream`` rather
   than plain JSON, and rejects requests that do not advertise both in
   ``Accept``. Handled by :data:`_ACCEPT` + ``base._post_json``'s SSE decode.
2. A tool result is DOUBLE wrapped: the JSON-RPC envelope carries
   ``result.content[]``, whose ``text`` member is itself a JSON *string*
   holding the business payload.

The first request uses MCP 2026-07-28's self-contained metadata and HTTP
routing headers.  If an older server rejects it, :func:`call_tool` performs
one legacy ``initialize`` handshake, adopts the ``Mcp-Session-Id``, and
retries exactly once.  This stays intentionally smaller than a general MCP
client while interoperating across both protocol eras.
"""

import json

from tofu_search.search.vertical import base
from tofu_search.search.vertical.base import _FETCH_FAILED, _FETCH_UNAUTHORIZED, logger

_ACCEPT = 'application/json, text/event-stream'
_PROTOCOL_VERSION = '2026-07-28'
_LEGACY_PROTOCOL_VERSION = '2025-11-25'
_CLIENT_INFO = {'name': 'tofu-search', 'version': '1'}

# Sentinel re-exported so handlers can branch on "needs a credential" without
# importing base directly.
UNAUTHORIZED = _FETCH_UNAUTHORIZED


def _headers(api_key='', session_id='', *, method='', name=''):
    hdrs = {'Content-Type': 'application/json', 'Accept': _ACCEPT}
    if api_key:
        hdrs['Authorization'] = f'Bearer {api_key}'
    if session_id:
        hdrs['Mcp-Session-Id'] = session_id
    if method:
        hdrs['MCP-Protocol-Version'] = _PROTOCOL_VERSION
        hdrs['Mcp-Method'] = method
        if name:
            hdrs['Mcp-Name'] = name
    return hdrs


def _rpc(method, params, req_id=1):
    return {'jsonrpc': '2.0', 'id': req_id, 'method': method, 'params': params}


def _modern_meta():
    """Required per-request identity for the stateless 2026 protocol."""
    return {
        'io.modelcontextprotocol/protocolVersion': _PROTOCOL_VERSION,
        'io.modelcontextprotocol/clientCapabilities': {},
        'io.modelcontextprotocol/clientInfo': _CLIENT_INFO,
    }


def unwrap_tool_result(envelope):
    """Extract the business payload from a JSON-RPC ``tools/call`` envelope.

    Returns a dict, or None when the envelope carried an error / no content.
    """
    if not isinstance(envelope, dict):
        return None
    if envelope.get('error'):
        logger.warning('[Vertical/MCP] server error: %s',
                       str(envelope['error'])[:200])
        return None
    result = envelope.get('result')
    if not isinstance(result, dict):
        return None
    if result.get('isError'):
        logger.warning('[Vertical/MCP] tool reported isError: %s',
                       str(result.get('content'))[:200])
        return None

    content = result.get('content')
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get('text')
            if not isinstance(text, str) or not text.strip():
                continue
            try:
                return json.loads(text)
            except Exception:
                # A tool is allowed to answer with prose; surface it verbatim
                # rather than dropping a successful call on the floor.
                return {'_text': text}
    structured = result.get('structuredContent')
    if isinstance(structured, dict):
        return structured
    return None


def _open_session(endpoint, *, api_key, timeout, label):
    """Run one ``initialize`` handshake; return a session id ('' if none)."""
    payload = _rpc('initialize', {
        'protocolVersion': _LEGACY_PROTOCOL_VERSION,
        'capabilities': {},
        'clientInfo': _CLIENT_INFO,
    })
    resp = base._post_json(endpoint, payload=payload,
                           headers=_headers(api_key), timeout=timeout,
                           label=f'{label} initialize', return_response=True)
    if resp is _FETCH_UNAUTHORIZED or resp is _FETCH_FAILED:
        return None
    try:
        session_id = resp.headers.get('Mcp-Session-Id') or ''
    except Exception:
        session_id = ''
    # Complete the legacy lifecycle before the retry.  Requesting the raw
    # response avoids trying to JSON-decode the notification's normal HTTP 202.
    base._post_json(
        endpoint,
        payload={'jsonrpc': '2.0', 'method': 'notifications/initialized'},
        headers=_headers(api_key, session_id),
        timeout=timeout,
        label=f'{label} initialized',
        return_response=True,
    )
    return session_id


def call_tool(endpoint, tool, arguments, *, api_key='', timeout=25, label=''):
    """Invoke ``tool`` on ``endpoint`` and return its unwrapped payload.

    Returns the business dict, :data:`UNAUTHORIZED` when the server rejected
    the credentials, or None on any other failure.
    """
    label = label or tool
    payload = _rpc('tools/call', {
        'name': tool,
        'arguments': arguments,
        '_meta': _modern_meta(),
    })

    envelope = base._post_json(endpoint, payload=payload,
                               headers=_headers(
                                   api_key, method='tools/call', name=tool),
                               timeout=timeout,
                               label=label,
                               return_error_response=True)
    if envelope is _FETCH_UNAUTHORIZED:
        return UNAUTHORIZED
    if envelope is _FETCH_FAILED:  # transport failure: another POST will not help
        return None

    # Non-2xx response objects are a common legacy server's way to reject a
    # sessionless request. A JSON-RPC error envelope expresses the same thing.
    modern_http_rejection = not isinstance(envelope, dict)
    out = None if modern_http_rejection else unwrap_tool_result(envelope)
    if out is not None:
        return out

    # A server that demands an initialised session answers the bare call with
    # a JSON-RPC error. Handshake once and retry.
    if not modern_http_rejection \
            and not (isinstance(envelope, dict) and envelope.get('error')):
        return None
    session_id = _open_session(endpoint, api_key=api_key, timeout=timeout,
                               label=label)
    if session_id is None:
        return None
    logger.info('[Vertical/MCP] %s retrying with session handshake', label)
    legacy_payload = _rpc('tools/call', {
        'name': tool,
        'arguments': arguments,
    })
    envelope = base._post_json(endpoint, payload=legacy_payload,
                               headers=_headers(api_key, session_id),
                               timeout=timeout, label=f'{label} retry')
    if envelope is _FETCH_UNAUTHORIZED:
        return UNAUTHORIZED
    if envelope is _FETCH_FAILED:
        return None
    return unwrap_tool_result(envelope)
