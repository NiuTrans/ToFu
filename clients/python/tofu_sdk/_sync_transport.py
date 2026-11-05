"""Requests transport: session ownership, retries and response consumption.

Entry: SyncTransport, used by the public Tofu facade. Protocol projection is
shared with AsyncTransport; native library timeout semantics are retained.
"""
from __future__ import annotations

import time
from typing import Any, Optional

import requests

from ._protocol import TofuError, _api_v4_path, _unwrap_envelope, error_body, retry_delay


class SyncTransport:
    def __init__(self, *, base_url: str, api_key: str, timeout: float,
                 verify: bool, max_retries: int, backoff_base: float,
                 user_agent: str):
        if not base_url:
            raise ValueError('base_url required')
        self.base_url = base_url.rstrip('/')
        self.api_key = str(api_key or '')
        self.timeout = timeout
        self.verify = verify
        self.max_retries = max(0, int(max_retries))
        self.backoff_base = max(0.05, float(backoff_base))
        self._session = requests.Session()
        self._session.headers.update({
            'User-Agent': user_agent,
            'Accept': 'application/json',
        })
        if self.api_key:
            self._session.headers['Authorization'] = f'Bearer {self.api_key}'

    def _url(self, path: str) -> str:
        if path.startswith(('http://', 'https://')):
            return path
        path = _api_v4_path(path)
        return self.base_url + (path if path.startswith('/') else '/' + path)

    def _request(self, method: str, path: str, *,
                  json_body: Any = None, params: Optional[dict] = None,
                  headers: Optional[dict] = None,
                  stream: bool = False, timeout: Optional[float] = None,
                  retryable: bool = False):
        h = dict(headers or {})
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._session.request(
                    method, self._url(path),
                    json=json_body, params=params, headers=h,
                    stream=stream, timeout=timeout or self.timeout,
                    verify=self.verify,
                )
            except requests.RequestException:
                if not retryable or attempt >= self.max_retries:
                    raise
                time.sleep(retry_delay(attempt, self.backoff_base, cap=10.0))
                continue
            if (retryable and (resp.status_code == 429
                               or resp.status_code >= 500)
                    and attempt < self.max_retries):
                resp.close()
                time.sleep(retry_delay(attempt, self.backoff_base,
                                       resp.headers.get('Retry-After')))
                continue
            return resp
        raise RuntimeError('unreachable retry loop')

    def _json(self, method: str, path: str, **kwargs):
        response = self._request(method, path, **kwargs)
        try:
            if not 200 <= response.status_code < 300:
                raise TofuError(response.status_code, error_body(response))
            if response.headers.get('content-type', '').startswith('application/json'):
                return _unwrap_envelope(response.json())
            return response.text
        finally:
            response.close()

    def close(self) -> None:
        """Release this client's owned Requests connection pool."""
        self._session.close()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
