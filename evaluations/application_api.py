"""Bounded loopback HTTP transport shared by application evaluations.

Only transports the public application API. Routing, execution, ownership and
storage remain with the server. Credentials never follow proxies or redirects.
"""
from __future__ import annotations

import ipaddress
import json
from urllib import error, parse, request


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise error.HTTPError(req.full_url, code, 'Redirect refused', headers, fp)


class JsonAPI:
    def __init__(self, origin, *, token=None, maximum_bytes=16 * 1024 * 1024):
        parts = parse.urlsplit(origin)
        try:
            loopback = ipaddress.ip_address(parts.hostname or '').is_loopback
            port = parts.port
        except ValueError as exc:
            raise ValueError('Use an explicit loopback IP origin') from exc
        if (parts.scheme not in {'http', 'https'} or not loopback
                or parts.username or parts.password or parts.query or parts.fragment
                or parts.path not in {'', '/'} or port == 0):
            raise ValueError('Use an HTTP(S) loopback IP origin without credentials/path')
        if type(maximum_bytes) is not int or not 1 <= maximum_bytes <= 64 * 1024 * 1024:
            raise ValueError('Invalid HTTP response byte budget')
        self.origin = origin.rstrip('/')
        self.maximum_bytes = maximum_bytes
        self.headers = {'Content-Type': 'application/json', 'User-Agent': 'Tofu-Evaluation/1'}
        if token:
            self.headers['Authorization'] = 'Bearer ' + token
        self.opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())

    def call(self, path, body=None, *, unwrap=True, timeout=10):
        if not path.startswith('/api/') or '\\' in path or '#' in path:
            raise ValueError('Expected an application-relative API path')
        req = request.Request(self.origin + path,
            data=json.dumps(body, ensure_ascii=False, allow_nan=False).encode() if body is not None else None,
            headers=self.headers, method='POST' if body is not None else 'GET')
        with self.opener.open(req, timeout=timeout) as response:
            raw = response.read(self.maximum_bytes + 1)
        if len(raw) > self.maximum_bytes:
            raise ValueError('HTTP evidence exceeds artifact ceiling')
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError('Expected an application JSON object')
        if not unwrap:
            return payload
        if not isinstance(payload.get('data'), dict):
            raise ValueError('Expected a native v4 response envelope')
        return payload['data']
