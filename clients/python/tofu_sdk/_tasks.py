"""Synchronous task submission, replay cursors, terminal observation and waiting.

Dependencies: the public client transport and shared protocol vocabulary.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Iterator, Optional
import time
import requests

from ._protocol import TofuError, error_body, parse_sequence, task_terminal
from ._streaming import _parse_sse
if TYPE_CHECKING:
    from ._client import Tofu


class _TasksAPI:
    def __init__(self, client: Tofu):
        self._c = client

    # Public mapping so callers can introspect what kinds are supported.
    KIND_ROUTES: dict = {
        'paper-report': '/api/v1/agents/paper/report',
        'paper-translate': '/api/v1/agents/paper/translate',
        'translate': '/api/v1/agents/translate',
        'image-gen': '/api/v1/agents/image-gen',
        'memory-search': '/api/v1/agents/memory/search',
        'search': '/api/v1/agents/search/async',
    }

    def start(self, *, kind: str, params: Optional[dict] = None,
               **kwargs) -> dict:
        """Start any task kind via the agent-specific endpoint.

        Maps kind → endpoint via :attr:`KIND_ROUTES`. Returns the
        endpoint's response unchanged — most kinds include
        ``task_id`` (or feature-specific equivalents like ``taskId``).
        """
        path = self.KIND_ROUTES.get(kind)
        if path is None:
            raise ValueError(
                f'Unknown task kind: {kind!r}. '
                f'Known: {sorted(self.KIND_ROUTES)}')
        return self._c._json('POST', path, json_body=params or {})

    def run(self, *, kind: str, params: Optional[dict] = None,
             timeout_s: float = 600.0,
             poll_interval_s: float = 1.0) -> dict:
        """Start a task and block until it reaches a terminal state.

        Convenience wrapper around ``start`` + ``wait``. Returns the
        terminal task state dict from ``GET /api/v1/tasks/{id}``.

        Raises ``TimeoutError`` if the task hasn't finished within
        ``timeout_s`` seconds.
        """
        started = self.start(kind=kind, params=params)
        task_id = started.get('task_id') or started.get('taskId')
        if not task_id:
            raise ValueError(
                f'Started task did not return a task_id (got keys: '
                f'{list(started.keys())})')
        return self.wait(task_id, poll_interval=poll_interval_s,
                          timeout=timeout_s)

    def start_and_stream(self, *, kind: str,
                          params: Optional[dict] = None,
                          ) -> Iterator[dict]:
        """Start a task and yield its SSE events as they arrive.

        Convenience wrapper around ``start`` + ``stream``. Yields the
        same events ``stream(task_id)`` would, including the terminal
        ``done``/``error``/``aborted`` event. The first yielded item
        is a synthetic ``{'type': 'started', 'task_id': '...'}`` event
        so the caller knows the id without reading it from the start
        response separately.
        """
        started = self.start(kind=kind, params=params)
        task_id = started.get('task_id') or started.get('taskId')
        if not task_id:
            raise ValueError(
                f'Started task did not return a task_id (got keys: '
                f'{list(started.keys())})')
        yield {'type': 'started', 'task_id': task_id, 'started': started}
        yield from self.stream(task_id)

    def get(self, task_id: str) -> dict:
        return self._c._json(
            'GET', f'/api/v1/tasks/{task_id}', retryable=True)

    def list(self, *, kind: str = '', status: str = '',
              limit: int = 50) -> dict:
        params = {'limit': limit}
        if kind:
            params['kind'] = kind
        if status:
            params['status'] = status
        return self._c._json('GET', '/api/v1/tasks', params=params)

    def events(self, task_id: str, *, cursor: int = 0) -> dict:
        return self._c._json(
            'GET', f'/api/v1/tasks/{task_id}/events',
            params={'cursor': cursor})

    def stream(self, task_id: str, *, cursor: int = 0,
               reconnect: bool = True,
               max_reconnects: Optional[int] = None) -> Iterator[dict]:
        """Stream task events and resume from the last absolute sequence.

        A transport drop never re-submits the agent run. The SDK reconnects to
        the task stream with ``cursor=last_seq+1``, preventing duplicate tool
        effects while preserving every retained event.
        """
        next_cursor = max(0, int(cursor))
        attempts = 0
        limit = (self._c.max_retries if max_reconnects is None
                 else max(0, int(max_reconnects)))
        while True:
            resp = None
            try:
                resp = self._c._request(
                    'GET', f'/api/v1/tasks/{task_id}/stream',
                    params={'cursor': next_cursor}, stream=True,
                    headers={'Accept': 'text/event-stream'}, retryable=True)
                if not (200 <= resp.status_code < 300):
                    raise TofuError(resp.status_code, error_body(resp))
                for event in _parse_sse(resp):
                    attempts = 0
                    sequence = parse_sequence(event.get('seq'))
                    if sequence is not None:
                        next_cursor = max(next_cursor, sequence + 1)
                    yield event
                    if task_terminal(event):
                        return
                state = self.get(task_id)
                if state.get('status') in ('done', 'error', 'aborted'):
                    return
            except TofuError as exc:
                if not reconnect or not exc.retryable:
                    raise
            except requests.RequestException:
                if not reconnect:
                    raise
            finally:
                if resp is not None:
                    resp.close()
            attempts += 1
            if not reconnect or attempts > limit:
                raise TofuError(
                    599, {'error': {'kind': 'stream_disconnected',
                                    'message': 'task stream reconnect limit exceeded'},
                          'task_id': task_id, 'cursor': next_cursor})
            time.sleep(min(self._c.backoff_base * (2 ** (attempts - 1)), 5.0))

    def abort(self, task_id: str) -> dict:
        return self._c._json(
            'POST', f'/api/v1/tasks/{task_id}/abort', retryable=True)

    def wait(self, task_id: str, *, poll_interval: float = 1.0,
              timeout: float = 600.0) -> dict:
        """Poll until terminal; return the final task state."""
        deadline = time.time() + timeout
        while True:
            t = self.get(task_id)
            if t.get('status') in ('done', 'error', 'aborted'):
                return t
            if time.time() >= deadline:
                raise TimeoutError(f'task {task_id} did not finish')
            time.sleep(poll_interval)
