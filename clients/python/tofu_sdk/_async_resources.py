"""Async native agent submission and resumable task streams.

Endpoint workflows use AsyncTofu's transport, shared request vocabulary and SSE
reader. The caller's existing max_concurrency policy stays at agent submission.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, AsyncIterator
import uuid
import httpx

from ._protocol import TofuError, _native_agent_payload, error_body, parse_sequence, task_terminal
from ._streaming import _parse_async_sse
if TYPE_CHECKING:
    from .async_client import AsyncTofu


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
                    await response.aread()
                    body = error_body(response)
                    raise TofuError(response.status_code, body)
                async for payload in _parse_async_sse(response):
                    attempts = 0
                    sequence = parse_sequence(payload.get('seq'))
                    if sequence is not None:
                        next_cursor = max(next_cursor, sequence + 1)
                    yield payload
                    if task_terminal(payload):
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
