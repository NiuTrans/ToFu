"""HTTPX transport: borrowed-client configuration and owned response lifetimes.

Entry: AsyncTransport. Requests carry SDK headers and timeouts without mutating
borrowed clients; only an internally created connection pool is closed here.
"""
from __future__ import annotations

import asyncio
from typing import Any, Optional
import httpx

from ._protocol import TofuError, _api_v4_path, _unwrap_envelope, error_body, retry_delay


class AsyncTransport:
    def __init__(self, *, base_url: str, api_key: str, timeout: float,
                 verify: bool, max_retries: int, backoff_base: float,
                 client: httpx.AsyncClient | None, user_agent: str):
        if not base_url:
            raise ValueError('base_url required')
        self.base_url = base_url.rstrip('/')
        self.api_key = str(api_key or '')
        self.timeout = float(timeout)
        self.max_retries = max(0, int(max_retries))
        self.backoff_base = max(0.05, float(backoff_base))
        headers = {'Accept': 'application/json', 'User-Agent': user_agent}
        if self.api_key:
            headers['Authorization'] = f'Bearer {self.api_key}'
        self._headers = httpx.Headers(headers)
        self._request_timeout = httpx.Timeout(self.timeout, connect=min(10.0, self.timeout))
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=min(10.0, self.timeout)),
            verify=verify,
            headers=headers,
        )

    def _url(self, path: str) -> str:
        if path.startswith(('http://', 'https://')):
            return path
        path = _api_v4_path(path)
        return self.base_url + (path if path.startswith('/') else '/' + path)

    async def _sleep(self, attempt: int, response=None) -> None:
        retry_after = response.headers.get('Retry-After') if response is not None else None
        await asyncio.sleep(retry_delay(attempt, self.backoff_base, retry_after))

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        retryable: bool = False,
        stream: bool = False,
    ) -> httpx.Response:
        merged_headers = self._headers.copy()
        merged_headers.update(headers or {})
        authentication = None if self.api_key else httpx.USE_CLIENT_DEFAULT
        for attempt in range(self.max_retries + 1):
            try:
                if stream:
                    request_value = self._client.build_request(
                        method, self._url(path), json=json_body,
                        params=params, headers=merged_headers, timeout=self._request_timeout)
                    response = await self._client.send(
                        request_value, stream=True, auth=authentication)
                else:
                    response = await self._client.request(
                        method, self._url(path), json=json_body,
                        params=params, headers=merged_headers, timeout=self._request_timeout,
                        auth=authentication)
            except (httpx.TimeoutException, httpx.TransportError):
                if not retryable or attempt >= self.max_retries:
                    raise
                await self._sleep(attempt)
                continue
            if (retryable and (response.status_code == 429
                               or response.status_code >= 500)
                    and attempt < self.max_retries):
                await response.aclose()
                await self._sleep(attempt, response)
                continue
            return response
        raise RuntimeError('unreachable retry loop')

    async def _json(self, method: str, path: str, **kwargs) -> Any:
        response = await self._request(method, path, **kwargs)
        try:
            if not response.is_success:
                raise TofuError(response.status_code, error_body(response))
            if response.headers.get('content-type', '').startswith('application/json'):
                return _unwrap_envelope(response.json())
            return response.text
        finally:
            await response.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.aclose()
