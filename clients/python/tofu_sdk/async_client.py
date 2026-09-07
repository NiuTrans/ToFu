"""Async Python client for Tofu's developer runtime boundary.

The client keeps idempotency keys stable across ambiguous retries and resumes
task SSE streams from the producer-owned absolute cursor. It intentionally
returns wire dictionaries so new additive server fields remain immediately
available without an SDK release.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import json
import uuid
from typing import Any, AsyncIterator, Optional

import httpx

from . import (
    TofuError,
    __version__,
    _native_agent_payload,
    _native_model_payload,
)


def _error_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except Exception:
        return response.text


def _terminal(payload: dict) -> bool:
    return (payload.get('type') in ('done', 'error', 'aborted')
            or payload.get('event') in ('done', 'error', 'aborted'))


class AsyncTofu:
    """Async Tofu client backed by one reusable :class:`httpx.AsyncClient`."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = '',
        timeout: float = 600.0,
        verify: bool = True,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        max_concurrency: int = 16,
        client: httpx.AsyncClient | None = None,
        user_agent: str = f'tofu-sdk-python/{__version__}',
    ) -> None:
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
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=min(10.0, self.timeout)),
            verify=verify,
            headers=headers,
        )
        self._agent_slots = asyncio.Semaphore(max(1, int(max_concurrency)))
        self.tasks = _AsyncTasksAPI(self)
        self.agents = _AsyncAgentsAPI(self)

    def _url(self, path: str) -> str:
        if path.startswith(('http://', 'https://')):
            return path
        return self.base_url + (path if path.startswith('/') else '/' + path)

    async def _sleep(self, attempt: int, response=None) -> None:
        retry_after = 0.0
        if response is not None:
            try:
                retry_after = float(response.headers.get('Retry-After') or 0)
            except (TypeError, ValueError):
                retry_after = 0.0
        await asyncio.sleep(min(max(
            retry_after, self.backoff_base * (2 ** attempt)), 30.0))

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
        for attempt in range(self.max_retries + 1):
            try:
                if stream:
                    request_value = self._client.build_request(
                        method, self._url(path), json=json_body,
                        params=params, headers=headers)
                    response = await self._client.send(
                        request_value, stream=True)
                else:
                    response = await self._client.request(
                        method, self._url(path), json=json_body,
                        params=params, headers=headers)
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
        if not response.is_success:
            raise TofuError(response.status_code, _error_body(response))
        if response.headers.get('content-type', '').startswith(
                'application/json'):
            return response.json()
        return response.text

    async def capabilities(self) -> dict:
        return await self._json(
            'GET', '/api/v1/capabilities', retryable=True)

    async def chat(self, *, messages: list, model: Mapping[str, str],
                   routing: Optional[dict] = None,
                   config: Optional[dict] = None, **extra) -> dict:
        body = {
            'messages': messages,
            'model': _native_model_payload(model),
            **extra,
        }
        if routing:
            body['routing'] = routing
        if config:
            body['config'] = config
        return await self._json(
            'POST', '/api/v1/chat/completions', json_body=body)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> 'AsyncTofu':
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.aclose()


class _AsyncTasksAPI:
    def __init__(self, client: AsyncTofu) -> None:
        self._c = client

    async def get(self, task_id: str) -> dict:
        return await self._c._json(
            'GET', f'/api/v1/tasks/{task_id}', retryable=True)

    async def events(self, task_id: str, *, cursor: int = 0) -> dict:
        return await self._c._json(
            'GET', f'/api/v1/tasks/{task_id}/events',
            params={'cursor': max(0, int(cursor))}, retryable=True)

    async def abort(self, task_id: str) -> dict:
        return await self._c._json(
            'POST', f'/api/v1/tasks/{task_id}/abort', retryable=True)

    async def stream(
        self,
        task_id: str,
        *,
        cursor: int = 0,
        reconnect: bool = True,
        max_reconnects: int | None = None,
    ) -> AsyncIterator[dict]:
        next_cursor = max(0, int(cursor))
        attempts = 0
        limit = (self._c.max_retries if max_reconnects is None
                 else max(0, int(max_reconnects)))
        while True:
            response = None
            try:
                response = await self._c._request(
                    'GET', f'/api/v1/tasks/{task_id}/stream',
                    params={'cursor': next_cursor},
                    headers={'Accept': 'text/event-stream'},
                    retryable=True, stream=True)
                if not response.is_success:
                    body = (await response.aread()).decode(
                        errors='replace')
                    raise TofuError(response.status_code, body)
                async for payload in _parse_async_sse(response):
                    attempts = 0
                    try:
                        sequence = int(payload.get('seq'))
                    except (TypeError, ValueError):
                        sequence = next_cursor
                    next_cursor = max(next_cursor, sequence + 1)
                    yield payload
                    if _terminal(payload):
                        return
                state = await self.get(task_id)
                if state.get('status') in ('done', 'error', 'aborted'):
                    return
            except TofuError as exc:
                if not reconnect or not exc.retryable:
                    raise
            except (httpx.TimeoutException, httpx.TransportError):
                if not reconnect:
                    raise
            finally:
                if response is not None:
                    await response.aclose()
            attempts += 1
            if not reconnect or attempts > limit:
                raise TofuError(599, {
                    'error': {
                        'kind': 'stream_disconnected',
                        'message': 'task stream reconnect limit exceeded',
                    },
                    'task_id': task_id,
                    'cursor': next_cursor,
                })
            await self._c._sleep(attempts - 1)


class _AsyncAgentsAPI:
    def __init__(self, client: AsyncTofu) -> None:
        self._c = client

    _body = staticmethod(_native_agent_payload)

    async def run(self, *, idempotency_key: str = '', **params) -> dict:
        body = self._body(stream=False, **params)
        key = idempotency_key or uuid.uuid4().hex
        async with self._c._agent_slots:
            return await self._c._json(
                'POST', '/api/v1/agent/run', json_body=body,
                headers={'Idempotency-Key': key}, retryable=True)

    async def start(self, *, idempotency_key: str = '', **params) -> dict:
        body = self._body(**params)
        body['async'] = True
        key = idempotency_key or uuid.uuid4().hex
        async with self._c._agent_slots:
            return await self._c._json(
                'POST', '/api/v1/agent/run', json_body=body,
                headers={
                    'Idempotency-Key': key,
                    'Prefer': 'respond-async',
                },
                retryable=True,
            )

    async def stream(
        self,
        *,
        cursor: int = 0,
        reconnect: bool = True,
        **params,
    ) -> AsyncIterator[dict]:
        started = await self.start(**params)
        task_id = str(started.get('task_id') or started.get('taskId') or '')
        if not task_id:
            raise ValueError(
                f'agent.run did not return task_id (got {started!r})')
        async for event in self._c.tasks.stream(
                task_id, cursor=cursor, reconnect=reconnect):
            yield event

    run_stream = stream


async def _parse_async_sse(response: httpx.Response) -> AsyncIterator[dict]:
    event_name = ''
    event_id: int | None = None
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line.startswith(':'):
            continue
        if line.startswith('event:'):
            event_name = line[6:].strip()
            continue
        if line.startswith('id:'):
            try:
                event_id = int(line[3:].strip())
            except ValueError:
                event_id = None
            continue
        if line.startswith('data:'):
            data_lines.append(line[5:].strip())
            continue
        if line != '' or not data_lines:
            continue
        data = '\n'.join(data_lines)
        data_lines = []
        if data == '[DONE]':
            return
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        if event_name:
            payload.setdefault('event', event_name)
        if event_id is not None:
            payload.setdefault('seq', event_id)
        yield payload
        event_name = ''
        event_id = None
        if _terminal(payload):
            return


TofuAsync = AsyncTofu

__all__ = ['AsyncTofu', 'TofuAsync']
