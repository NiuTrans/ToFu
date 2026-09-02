"""tofu_sdk — Python client for the Tofu headless API.

Quick start
-----------

    from tofu_sdk import Tofu

    client = Tofu(base_url="https://your-tofu", api_key="tofu_live_…")

    # Sync chat
    resp = client.chat(model="claude-opus-4-7",
                       messages=[{"role":"user","content":"Hi"}])
    print(resp["choices"][0]["message"]["content"])

    # Streaming
    for ev in client.stream(model="claude-opus-4-7",
                             messages=[{"role":"user","content":"Hi"}]):
        if ev.get("choices", [{}])[0].get("delta", {}).get("content"):
            print(ev["choices"][0]["delta"]["content"], end="", flush=True)

    # Generic task
    task = client.tasks.start(kind="paper-report",
                               params={"paper_text": "...", "lang": "zh"})
    for ev in client.tasks.stream(task["task_id"]):
        print(ev)

The SDK is intentionally small (~250 lines) and depends only on
``requests`` (sync) and the optional ``httpx`` (async). It mirrors
the public REST surface 1:1 — every method documents which endpoint
it calls.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterator, Optional

import requests

from .api_v4_generated import (
    ApiMetaResponse,
    require_desktop_api_compatibility,
)


__version__ = '0.17.0'


class TofuError(RuntimeError):
    """Raised when Tofu returns a non-2xx status."""
    def __init__(self, status: int, body: Any):
        super().__init__(f'Tofu API error {status}: {body!r}')
        self.status = status
        self.body = body
        payload = body if isinstance(body, dict) else {}
        error = payload.get('error') if isinstance(payload, dict) else None
        self.kind = str((
            (error or {}).get('kind') if isinstance(error, dict)
            else payload.get('error_kind')) or '')
        self.retryable = status == 429 or status >= 500
        try:
            self.retry_after = float(
                payload.get('retry_after') or 0) or None
        except (TypeError, ValueError):
            self.retry_after = None


class Tofu:
    """Synchronous Tofu API client."""

    def __init__(self, *, base_url: str, api_key: str = '',
                 timeout: float = 600.0, verify: bool = True,
                 max_retries: int = 3, backoff_base: float = 0.5,
                 user_agent: str = f'tofu-sdk-python/{__version__}'):
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
        self.tasks = _TasksAPI(self)
        self.agents = _AgentsAPI(self)
        self.keys = _KeysAPI(self)
        self.webhooks = _WebhooksAPI(self)

    # ── HTTP helpers ─────────────────────────────────────────────

    def _url(self, path: str) -> str:
        if path.startswith(('http://', 'https://')):
            return path
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
                time.sleep(min(self.backoff_base * (2 ** attempt), 10.0))
                continue
            if (retryable and (resp.status_code == 429
                               or resp.status_code >= 500)
                    and attempt < self.max_retries):
                try:
                    retry_after = float(resp.headers.get('Retry-After') or 0)
                except (TypeError, ValueError):
                    retry_after = 0
                resp.close()
                time.sleep(min(max(
                    retry_after, self.backoff_base * (2 ** attempt)), 30.0))
                continue
            return resp
        raise RuntimeError('unreachable retry loop')

    def _json(self, method: str, path: str, **kwargs):
        resp = self._request(method, path, **kwargs)
        if not (200 <= resp.status_code < 300):
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            raise TofuError(resp.status_code, body)
        if resp.headers.get('content-type', '').startswith('application/json'):
            return resp.json()
        return resp.text

    # ── Capabilities ─────────────────────────────────────────────

    def capabilities(self) -> dict:
        """``GET /api/v1/capabilities`` — runtime registry."""
        return self._json('GET', '/api/v1/capabilities')

    def api_meta(self) -> ApiMetaResponse:
        """``GET /api/v4/meta`` — reject an incompatible SDK build."""
        return require_desktop_api_compatibility(
            self._json('GET', '/api/v4/meta'),
            __version__,
        )

    # ── Chat ─────────────────────────────────────────────────────

    def chat(self, *, messages: list, model: str = '',
              config: Optional[dict] = None, **kwargs) -> dict:
        """Sync chat completion via ``POST /api/v1/chat/completions``."""
        body = {'messages': messages}
        if model:
            body['model'] = model
        if config:
            body['config'] = config
        for k in ('temperature', 'max_tokens', 'top_p', 'stop',
                   'tools', 'tool_choice', 'response_format',
                   'idempotency_key', 'conversation_id', 'timeout_s'):
            if k in kwargs:
                body[k] = kwargs[k]
        return self._json('POST', '/api/v1/chat/completions', json_body=body)

    def stream(self, *, messages: list, model: str = '',
                config: Optional[dict] = None, **kwargs
                ) -> Iterator[dict]:
        """Streaming chat completion. Yields parsed SSE event payloads."""
        body = {'messages': messages, 'stream': True}
        if model:
            body['model'] = model
        if config:
            body['config'] = config
        for k in ('temperature', 'max_tokens', 'top_p', 'stop',
                   'tools', 'tool_choice', 'idempotency_key',
                   'conversation_id'):
            if k in kwargs:
                body[k] = kwargs[k]
        resp = self._request('POST', '/api/v1/chat/completions',
                              json_body=body, stream=True,
                              headers={'Accept': 'text/event-stream'})
        if not (200 <= resp.status_code < 300):
            try:
                err = resp.json()
            except Exception:
                err = resp.text
            raise TofuError(resp.status_code, err)
        yield from _parse_sse(resp)


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
            terminal = False
            try:
                resp = self._c._request(
                    'GET', f'/api/v1/tasks/{task_id}/stream',
                    params={'cursor': next_cursor}, stream=True,
                    headers={'Accept': 'text/event-stream'}, retryable=True)
                if not (200 <= resp.status_code < 300):
                    try:
                        body = resp.json()
                    except Exception:
                        body = resp.text
                    raise TofuError(resp.status_code, body)
                for event in _parse_sse(resp):
                    attempts = 0
                    if isinstance(event, dict):
                        try:
                            sequence = int(event.get('seq'))
                        except (TypeError, ValueError):
                            sequence = next_cursor
                        next_cursor = max(next_cursor, sequence + 1)
                    yield event
                    if isinstance(event, dict) and (
                            event.get('type') in ('done', 'error', 'aborted')):
                        terminal = True
                        return
                if terminal:
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


class _AgentsAPI:
    def __init__(self, client: Tofu):
        self._c = client

    @staticmethod
    def _run_body(*, messages: list, model: str = '',
                  provider: Optional[dict] = None,
                  config: Optional[dict] = None,
                  capabilities: Optional[dict] = None,
                  tools: Optional[list] = None,
                  trajectory: str = '', timeout_s: float = 600.0,
                  request_id: str = '', **extra) -> dict:
        body: dict = {
            'messages': messages,
            'timeout_s': timeout_s,
        }
        if model:
            body['model'] = model
        if provider:
            body['provider'] = provider
        if config:
            body['config'] = config
        if capabilities:
            body['capabilities'] = capabilities
        if tools:
            body['tools'] = tools
        if trajectory:
            body['trajectory'] = trajectory
        if request_id:
            body['id'] = request_id
        body.update(extra)
        return body

    def run(self, *, messages: list, model: str = '',
            provider: Optional[dict] = None,
            config: Optional[dict] = None,
            capabilities: Optional[dict] = None,
            tools: Optional[list] = None,
            trajectory: str = '', timeout_s: float = 600.0,
            idempotency_key: str = '', request_id: str = '',
            **extra) -> dict:
        """Run ``POST /api/v1/agent/run`` with safe automatic retries.

        ``model`` may be omitted when the server has a managed default. An
        inline provider only needs ``base_url``/``api_key``/``model``. The
        generated Idempotency-Key stays stable across ambiguous network retries.
        """
        body = self._run_body(
            messages=messages, model=model, provider=provider, config=config,
            capabilities=capabilities, tools=tools, trajectory=trajectory,
            timeout_s=timeout_s, request_id=request_id, stream=False, **extra)
        key = idempotency_key or uuid.uuid4().hex
        return self._c._json(
            'POST', '/api/v1/agent/run', json_body=body,
            headers={'Idempotency-Key': key}, retryable=True,
            timeout=max(self._c.timeout, timeout_s + 10),
        )

    def start(self, *, messages: list, model: str = '',
              provider: Optional[dict] = None,
              config: Optional[dict] = None,
              capabilities: Optional[dict] = None,
              tools: Optional[list] = None,
              trajectory: str = '', timeout_s: float = 600.0,
              idempotency_key: str = '', request_id: str = '',
              **extra) -> dict:
        """Start an agent run and return its task handle (HTTP 202)."""
        body = self._run_body(
            messages=messages, model=model, provider=provider, config=config,
            capabilities=capabilities, tools=tools, trajectory=trajectory,
            timeout_s=timeout_s, request_id=request_id, **extra)
        body['async'] = True
        key = idempotency_key or uuid.uuid4().hex
        return self._c._json(
            'POST', '/api/v1/agent/run', json_body=body,
            headers={'Idempotency-Key': key, 'Prefer': 'respond-async'},
            retryable=True,
        )

    def stream(self, *, cursor: int = 0, reconnect: bool = True,
               **run_params) -> Iterator[dict]:
        """Start a run, then stream native events with cursor resume."""
        started = self.start(**run_params)
        task_id = started.get('task_id') or started.get('taskId')
        if not task_id:
            raise ValueError(
                f'agent.run did not return task_id (got {started!r})')
        yield from self._c.tasks.stream(
            str(task_id), cursor=cursor, reconnect=reconnect)

    run_stream = stream

    def paper_report(self, **params) -> dict:
        return self._c._json('POST', '/api/v1/agents/paper/report',
                              json_body=params)

    def paper_translate(self, **params) -> dict:
        return self._c._json('POST', '/api/v1/agents/paper/translate',
                              json_body=params)

    def translate(self, *, text: str, target_lang: str = 'zh',
                   **kwargs) -> dict:
        body = {'text': text, 'target_lang': target_lang, **kwargs}
        return self._c._json('POST', '/api/v1/agents/translate',
                              json_body=body)

    def image_gen(self, *, prompt: str, **kwargs) -> dict:
        body = {'prompt': prompt, **kwargs}
        return self._c._json('POST', '/api/v1/agents/image-gen',
                              json_body=body)

    def memory_search(self, *, query: str, top_k: int = 30) -> dict:
        return self._c._json('POST', '/api/v1/agents/memory/search',
                              json_body={'query': query, 'top_k': top_k})

    def fetch(self, *, url: str) -> dict:
        return self._c._json('POST', '/api/v1/agents/browser/fetch',
                              json_body={'url': url})

    def clean_log(self, *, text: str) -> dict:
        """Detect log noise via ``POST /api/v1/logs/clean``.

        Returns the structured cleaning result, or ``{ok:true, no_noise:true}``
        when nothing actionable is found. Same heuristic the UI uses
        for its "log noise detected" banner.
        """
        return self._c._json('POST', '/api/v1/logs/clean',
                              json_body={'text': text})

    def extract_file_changes(self, *, tool_rounds: list) -> dict:
        """Extract a deduplicated file-change list from a tool-rounds blob.

        Same logic the UI uses to render the file-changes bar when
        the orchestrator's git-derived ``modifiedFileList`` isn't yet
        available. Useful for CI pipelines and evaluation harnesses
        that want to summarise what an agent turn touched.
        """
        return self._c._json(
            'POST', '/api/v1/messages/extract-file-changes',
            json_body={'toolRounds': tool_rounds})

    # ── Feature-shaped poll surfaces (translate / paper) ────────────
    # Generic event replay: ``client.tasks.events(id)`` /
    # ``client.tasks.stream(id)`` work for any TaskRuntime task.
    # The methods below expose the FEATURE-SPECIFIC poll shapes the
    # legacy UI uses (``{translated, partial, …}`` for translate;
    # ``{events, next_cursor, status, …}`` for paper) under the v1
    # path so SDK callers don't have to translate event streams to
    # those structures themselves.

    def poll_translate(self, task_id: str) -> dict:
        return self._c._json(
            'GET', f'/api/v1/agents/translate/poll/{task_id}')

    def poll_translate_batch(self, task_ids: list) -> dict:
        return self._c._json(
            'POST', '/api/v1/agents/translate/poll/batch',
            json_body={'taskIds': list(task_ids or [])})

    def poll_paper_report(self, task_id: str, cursor: int = 0) -> dict:
        return self._c._json(
            'GET', '/api/v1/agents/paper/report/poll'
                   f'?task_id={task_id}&cursor={int(cursor)}')

    def poll_paper_translate(self, task_id: str, cursor: int = 0) -> dict:
        return self._c._json(
            'GET', '/api/v1/agents/paper/translate/poll'
                   f'?task_id={task_id}&cursor={int(cursor)}')

    # ── Web search (sync + async) ──────────────────────────────────

    def search(self, *, query: str, max_results: int = 10,
                freshness: str = '', **kwargs) -> dict:
        """Synchronous web search via ``POST /api/v1/agents/search``.

        Returns the full result dict including `results`, `dedup_count`,
        and `summary` when ``include_summary=True`` is passed in kwargs.
        Use :meth:`search_async` for long-running multi-engine searches.
        """
        body = {'query': query, 'max_results': max_results}
        if freshness:
            body['freshness'] = freshness
        body.update(kwargs)
        return self._c._json('POST', '/api/v1/agents/search', json_body=body)

    def search_async(self, *, query: str, max_results: int = 10,
                      freshness: str = '', **kwargs) -> dict:
        """Async web search via ``POST /api/v1/agents/search/async``.

        Returns ``{task_id, status}``. Use ``client.tasks.stream(task_id)``
        or ``client.tasks.wait(task_id)`` to consume results.
        """
        body = {'query': query, 'max_results': max_results}
        if freshness:
            body['freshness'] = freshness
        body.update(kwargs)
        return self._c._json('POST', '/api/v1/agents/search/async',
                              json_body=body)

    # ── Feature-shaped polling convenience helpers ────────────────
    # These hide the start + poll-loop boilerplate that callers would
    # otherwise reproduce in every CI script. They preserve the
    # feature-specific result shape (translated text for translate,
    # cursor-based event log for paper) instead of returning a generic
    # task dict.

    def translate_and_wait(self, *, text: str, target_lang: str = 'zh',
                            source_lang: str = '',
                            poll_interval_s: float = 2.0,
                            timeout_s: float = 180.0,
                            **kwargs) -> dict:
        """Run a translation task to completion and return the flat result.

        Equivalent to ``translate()`` + a ``poll_translate`` loop. Returns
        the structured shape ``{status, translated, model, …}`` the
        UI uses. Raises ``TimeoutError`` after ``timeout_s`` seconds.
        """
        started = self.translate(text=text, target_lang=target_lang,
                                  source_lang=source_lang, **kwargs)
        task_id = started.get('task_id') or started.get('taskId')
        if not task_id:
            raise ValueError(
                f'translate did not return a taskId (got: {started})')
        deadline = time.time() + timeout_s
        while True:
            result = self.poll_translate(task_id)
            status = result.get('status')
            if status == 'done':
                return result
            if status in ('error', 'not_found'):
                raise TofuError(
                    422, result.get('error') or f'translate {status}')
            if time.time() >= deadline:
                raise TimeoutError(f'translate task {task_id} did not finish')
            time.sleep(poll_interval_s)

    def paper_report_and_wait(self, *, paper_text: str = '',
                                lang: str = 'zh',
                                poll_interval_s: float = 2.0,
                                timeout_s: float = 600.0,
                                **kwargs) -> dict:
        """Run a paper-report task to completion. Returns the final task
        state from ``GET /api/v1/tasks/{id}`` (which carries the report
        in its ``result`` field per the legacy contract)."""
        body = {'paper_text': paper_text, 'lang': lang, **kwargs}
        started = self.paper_report(**body)
        task_id = started.get('task_id') or started.get('taskId')
        if not task_id:
            raise ValueError(
                f'paper_report did not return a taskId (got: {started})')
        deadline = time.time() + timeout_s
        cursor = 0
        while True:
            result = self.poll_paper_report(task_id, cursor=cursor)
            cursor = result.get('next_cursor', cursor)
            status = result.get('status')
            if status == 'done':
                return result
            if status == 'error':
                raise TofuError(
                    422, result.get('error') or 'paper-report error')
            if time.time() >= deadline:
                raise TimeoutError(
                    f'paper-report task {task_id} did not finish')
            time.sleep(poll_interval_s)


class _KeysAPI:
    def __init__(self, client: Tofu):
        self._c = client

    def whoami(self) -> dict:
        return self._c._json('GET', '/api/v1/keys/whoami')

    def list(self) -> dict:
        return self._c._json('GET', '/api/v1/keys')

    def create(self, *, name: str, scopes: list, **kwargs) -> dict:
        body = {'name': name, 'scopes': scopes, **kwargs}
        return self._c._json('POST', '/api/v1/keys', json_body=body)

    def revoke(self, key_id: str) -> dict:
        return self._c._json('DELETE', f'/api/v1/keys/{key_id}')


class _WebhooksAPI:
    def __init__(self, client: Tofu):
        self._c = client

    def list(self) -> dict:
        return self._c._json('GET', '/api/v1/webhooks')

    def subscribe(self, *, url: str, channel: str = '',
                   event_types: Optional[list] = None,
                   task_id: str = '*') -> dict:
        body = {'url': url, 'channel': channel,
                'event_types': event_types or [], 'task_id': task_id}
        return self._c._json('POST', '/api/v1/webhooks', json_body=body)

    def unsubscribe(self, sub_id: str) -> dict:
        return self._c._json('DELETE', f'/api/v1/webhooks/{sub_id}')


# ── SSE parser ────────────────────────────────────────────────────

def _parse_sse(resp) -> Iterator[dict]:
    """Yield JSON-decoded SSE events from a streaming response.

    Handles all three SSE conventions Tofu emits:
      * OpenAI style: bare ``data: {…}`` lines, terminated by
        ``data: [DONE]``.
      * Anthropic style: ``event: <name>`` + ``data: {…}`` blocks,
        terminated by ``event: message_stop``.
      * Generic task stream: ``id: <seq>`` + ``data: {…}`` blocks,
        terminated when the parsed payload's ``type`` is one of
        ``done`` / ``error`` / ``aborted``.

    Comment lines (``: heartbeat``) and empty lines are skipped silently.
    Unparseable JSON chunks are skipped silently as well — matches the
    JS SDK's lenient behaviour.
    """
    pending_event = ''
    pending_id: Optional[int] = None
    for raw in resp.iter_lines(decode_unicode=True):
        if raw is None:
            continue
        line = raw
        if line == '':
            pending_event = ''
            continue
        if line.startswith(':'):
            # Comment / heartbeat.
            continue
        if line.startswith('event:'):
            pending_event = line[len('event:'):].strip()
            continue
        if line.startswith('id:'):
            try:
                pending_id = int(line[len('id:'):].strip())
            except (TypeError, ValueError):
                pending_id = None
            continue
        if not line.startswith('data:'):
            continue
        data = line[len('data:'):].strip()
        if data == '[DONE]':
            return
        if not data:
            continue
        try:
            payload = json.loads(data)
        except (ValueError, json.JSONDecodeError):
            continue
        if pending_event and isinstance(payload, dict):
            payload.setdefault('event', pending_event)
        if pending_id is not None and isinstance(payload, dict):
            payload.setdefault('seq', pending_id)
        yield payload
        pending_id = None
        # Auto-terminate on terminal task events so callers using
        # ``client.tasks.stream(id)`` don't hang on a still-open
        # connection if the server forgot to close it.
        if isinstance(payload, dict):
            t = payload.get('type')
            if t in ('done', 'error', 'aborted'):
                return
            if pending_event == 'message_stop':
                return


from .async_client import AsyncTofu, TofuAsync


__all__ = [
    'ApiMetaResponse', 'AsyncTofu', 'Tofu', 'TofuAsync', 'TofuError',
    '__version__',
]
