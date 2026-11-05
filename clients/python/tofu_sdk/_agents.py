"""Synchronous native agent submissions and domain convenience operations.

Dependencies: the public client transport and shared protocol vocabulary.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterator, Optional
from collections.abc import Mapping
import time
import uuid

from ._protocol import TofuError, _native_agent_payload
if TYPE_CHECKING:
    from ._client import Tofu


class _AgentsAPI:
    def __init__(self, client: Tofu):
        self._c = client

    _run_body = staticmethod(_native_agent_payload)

    def run(self, *, messages: list,
            model: Mapping[str, str] | None = None,
            routing: Optional[Mapping[str, Any]] = None,
            model_routing: Optional[Mapping[str, Any]] = None,
            config: Optional[dict] = None,
            capabilities: Optional[dict] = None,
            tools: Optional[list] = None,
            trajectory: str = '', timeout_s: float = 600.0,
            idempotency_key: str = '', request_id: str = '',
            **extra) -> dict:
        """Run ``POST /api/v1/agent/run`` with safe automatic retries.

        ``model`` may be omitted only when a standalone runtime advertises a
        configured default. Otherwise pass one structured v2 identity. The
        generated Idempotency-Key stays stable across ambiguous network retries.
        """
        body = self._run_body(
            messages=messages, model=model, routing=routing,
            model_routing=model_routing, config=config,
            capabilities=capabilities, tools=tools, trajectory=trajectory,
            timeout_s=timeout_s, request_id=request_id, stream=False, **extra)
        key = idempotency_key or uuid.uuid4().hex
        return self._c._json(
            'POST', '/api/v1/agent/run', json_body=body,
            headers={'Idempotency-Key': key}, retryable=True,
            timeout=max(self._c.timeout, timeout_s + 10),
        )

    def start(self, *, messages: list,
              model: Mapping[str, str] | None = None,
              routing: Optional[Mapping[str, Any]] = None,
              model_routing: Optional[Mapping[str, Any]] = None,
              config: Optional[dict] = None,
              capabilities: Optional[dict] = None,
              tools: Optional[list] = None,
              trajectory: str = '', timeout_s: float = 600.0,
              idempotency_key: str = '', request_id: str = '',
              **extra) -> dict:
        """Start an agent run and return its task handle (HTTP 202)."""
        body = self._run_body(
            messages=messages, model=model, routing=routing,
            model_routing=model_routing, config=config,
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
