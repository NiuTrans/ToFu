"""JSON over HTTPS for the bot platform transports (docs/CHANNELS.md C12).

Responsibility: one request through the shared proxy-aware client
(``lib.http_transport.client``, its own default timeout unless the protocol
names a hold time), answered as ``(status, JSON object)``. Redirects are not
followed, so a credential header never reaches another origin. Tests run the
same function against local fake servers.
"""
from __future__ import annotations

import json
from typing import Any


def request_json(method: str, url: str, *, headers: dict[str, str] | None = None,
                 body: Any = None, timeout: float | None = None) -> tuple[int, dict[str, Any]]:
    from lib.http_transport.client import http_request
    kwargs: dict[str, Any] = {'headers': dict(headers or {}), 'allow_redirects': False}
    if body is not None:
        kwargs['data'] = json.dumps(body, ensure_ascii=False).encode('utf-8')
        kwargs['headers'].setdefault('content-type', 'application/json')
    if timeout is not None:
        kwargs['timeout'] = timeout
    response = http_request(method, url, **kwargs)
    try:
        payload = response.json() if response.content else {}
    except ValueError:
        payload = {}
    finally:
        response.close()
    return response.status_code, payload if isinstance(payload, dict) else {}


def is_timeout(error: BaseException) -> bool:
    """A read that outlived the protocol's long-poll hold: an empty answer, not a failure."""
    import requests
    return isinstance(error, requests.exceptions.ReadTimeout)


__all__ = ['is_timeout', 'request_json']
