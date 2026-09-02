"""Storage-free public composition of Tofu's production agent kernel.

``AgentRuntime`` creates normal orchestrator tasks with a structured principal,
but marks them transient before the first event. The full app and this package
therefore share one agent loop while database, billing, frontend, and route
modules stay outside the embed/headless lifecycle.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import uuid
from collections.abc import AsyncIterator, Iterator, Mapping
from typing import Any

from lib.agent_core.run_contract import (
    apply_storage_free_runtime_policy,
    build_agent_config,
    project_agent_evidence,
    project_agent_result,
)
from lib.identity import PERSONAL_USER_ID, PrincipalContext
from lib.log import get_logger
from tofu_agent.models import (
    AgentClosedError,
    AgentConfigurationError,
    AgentOverloadedError,
    AgentRequest,
    AgentResult,
    AgentTimeoutError,
    ProviderConfig,
)

logger = get_logger(__name__)
_TERMINAL_STATES = frozenset({'done', 'error', 'aborted'})
_TERMINAL_EVENTS = frozenset({'done', 'error', 'aborted'})


class _CapacityGate:
    """Small process-local admission gate with no external state backend."""

    def __init__(self, capacity: int) -> None:
        self.capacity = max(0, int(capacity))
        self._in_flight = 0
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        with self._lock:
            if self.capacity and self._in_flight >= self.capacity:
                return False
            self._in_flight += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._in_flight:
                self._in_flight -= 1

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight


class AgentExecution:
    """A submitted run: stream its events, await its result, or abort it."""

    def __init__(
        self,
        runtime: 'AgentRuntime',
        task: dict,
        request: AgentRequest,
        *,
        model: str,
        public_provider_id: str,
    ) -> None:
        self._runtime = runtime
        self._task = task
        self._request = request
        self._model = model
        self._public_provider_id = public_provider_id
        self._terminal = threading.Event()
        self._nudge = threading.Event()

    @property
    def task_id(self) -> str:
        return str(self._task.get('id') or '')

    @property
    def status(self) -> str:
        return str(self._task.get('status') or '')

    @property
    def request_id(self) -> str:
        """Stable public run identifier shared by retries and projections."""
        return self._request.request_id

    @property
    def model(self) -> str:
        """Resolved model identifier (never provider credentials)."""
        return self._model

    @property
    def timeout_s(self) -> float:
        return self._request.timeout_s

    @property
    def done(self) -> bool:
        return self.status in _TERMINAL_STATES

    def abort(self) -> bool:
        """Request cooperative cancellation for this owned run."""
        return self._runtime._abort_execution(self)

    def result(self, timeout_s: float | None = None) -> AgentResult:
        """Wait for the terminal task and return its typed projection."""
        timeout = self._request.timeout_s if timeout_s is None else float(timeout_s)
        if timeout <= 0:
            raise AgentConfigurationError('timeout_s must be positive')
        if not self.done and not self._terminal.wait(timeout):
            raise AgentTimeoutError(
                f'agent run {self.task_id[:8]} did not finish within '
                f'{timeout:g}s')
        payload = project_agent_result(
            self._task,
            model=self._model,
            requested_id=self._request.request_id,
            trajectory_fmt=self._request.trajectory,
            provider_id=self._public_provider_id,
        )
        return AgentResult.from_payload(payload)

    def snapshot(self) -> dict:
        """Return the current task state in the public ``agent.run`` shape."""
        return project_agent_result(
            self._task,
            model=self._model,
            requested_id=self._request.request_id,
            trajectory_fmt=(self._request.trajectory if self.done else None),
            provider_id=self._public_provider_id,
        )

    def evidence_snapshot(self) -> dict:
        """Return bounded, credential-free runtime evidence for this run.

        This is an evaluation/diagnostic projection of the production task,
        not a second transcript authority. It exposes exact usage and the
        versioned optimization telemetry retained by the kernel.
        """
        return project_agent_evidence(
            self._task,
            model=self._model,
            requested_id=self._request.request_id,
            provider_id=self._public_provider_id,
        )

    def resolve_custom_tool_call(
        self,
        call_id: str,
        content: str,
        *,
        is_error: bool = False,
    ) -> bool:
        """Resolve one client-mode custom call owned by this execution."""
        call_id = str(call_id or '').strip()
        if not call_id:
            raise AgentConfigurationError('custom tool call_id is required')
        if self._runtime.get(self.task_id) is not self:
            return False
        from lib.tools.tool_env import resolve_client_tool_result
        return resolve_client_tool_result(
            call_id,
            str(content or ''),
            task_id=self.task_id,
            user_id=self._runtime.principal.require_owner(
                context='AgentExecution.resolve_custom_tool_call'),
            is_error=bool(is_error),
        )

    def event_page(self, *, cursor: int = 0) -> dict:
        """Return one bounded in-memory replay page."""
        from lib.agent_core.run_contract import project_agent_event_evidence
        from lib.task_replay import task_memory_replay_page
        payload = task_memory_replay_page(
            self._task, max(0, int(cursor))).payload({
                'task_id': self.task_id,
            })
        payload['events'] = [
            project_agent_event_evidence(event)
            for event in payload.get('events') or []
        ]
        return payload

    def events(
        self,
        *,
        cursor: int = 0,
        timeout_s: float | None = None,
    ) -> Iterator[dict]:
        """Yield native Tofu events from an absolute replay cursor.

        The cursor is process-lifetime state for this storage-free runtime.
        Every real event includes ``seq``; pass the last received sequence back
        to the sidecar/SDK to resume a disconnected stream.
        """
        from lib.agent_core.run_contract import project_agent_event_evidence
        from lib.task_replay import task_memory_replay_page

        deadline = None
        if timeout_s is not None:
            timeout = float(timeout_s)
            if timeout <= 0:
                raise AgentConfigurationError('timeout_s must be positive')
            deadline = time.monotonic() + timeout
        next_cursor = max(0, int(cursor))
        emitted_terminal = False
        while True:
            page = task_memory_replay_page(self._task, next_cursor)
            next_cursor = page.next_cursor
            for event in page.events:
                yield project_agent_event_evidence(event)
                if event.get('type') in _TERMINAL_EVENTS:
                    emitted_terminal = True
                    return

            if self.done:
                if not emitted_terminal:
                    yield project_agent_event_evidence({
                        'type': self.status,
                        'taskId': self.task_id,
                        'finishReason': (
                            self._task.get('finishReason') or 'stop'),
                        'usage': self._task.get('usage') or {},
                        'error': self._task.get('error'),
                    })
                return

            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AgentTimeoutError(
                        f'agent event stream {self.task_id[:8]} timed out')
                wait_for = min(remaining, 15.0)
            else:
                wait_for = 15.0

            # Clear, re-check, then wait: an event landing between the first
            # drain and clear cannot be missed.
            self._nudge.clear()
            retry_page = task_memory_replay_page(self._task, next_cursor)
            if retry_page.events or self.done:
                continue
            self._nudge.wait(wait_for)

    async def result_async(self, timeout_s: float | None = None) -> AgentResult:
        return await asyncio.to_thread(self.result, timeout_s)

    async def events_async(
        self,
        *,
        cursor: int = 0,
        timeout_s: float | None = None,
    ) -> AsyncIterator[dict]:
        iterator = self.events(cursor=cursor, timeout_s=timeout_s)
        sentinel = object()

        def _next():
            try:
                return next(iterator)
            except StopIteration:
                return sentinel

        while True:
            item = await asyncio.to_thread(_next)
            if item is sentinel:
                return
            yield item


class AgentRuntime:
    """Embeddable Tofu runtime with no database or ChatUI frontend lifecycle.

    Use :meth:`local` for the personal composition. Enterprise adapters should
    construct the class with an authenticated :class:`PrincipalContext`.
    """

    def __init__(
        self,
        *,
        principal: PrincipalContext,
        provider: ProviderConfig | Mapping[str, Any] | None = None,
        provider_source: str = 'runtime',
        default_model: str = '',
        max_inflight: int = 4,
        max_retained_runs: int = 1024,
    ) -> None:
        if not isinstance(principal, PrincipalContext):
            raise TypeError('AgentRuntime principal must be PrincipalContext')
        principal.require_owner(context='AgentRuntime')
        if provider is not None and not isinstance(provider, ProviderConfig):
            provider = ProviderConfig.from_mapping(provider)
        self.principal = principal
        self.provider = provider
        self.provider_source = str(provider_source or 'runtime')
        self.default_model = str(
            default_model
            or (provider.model if provider else '')
            or os.environ.get('TOFU_AGENT_MODEL', '')
            or os.environ.get('LLM_MODEL', '')
        ).strip()
        self._gate = _CapacityGate(max_inflight)
        self._max_retained_runs = max(16, int(max_retained_runs))
        self._executions: dict[str, AgentExecution] = {}
        self._active: set[str] = set()
        self._lock = threading.RLock()
        self._closed = False

    @classmethod
    def local(
        cls,
        *,
        provider: ProviderConfig | Mapping[str, Any] | None = None,
        provider_source: str = 'runtime',
        owner_user_id: int = PERSONAL_USER_ID,
        subject_id: str = 'local:developer',
        default_model: str = '',
        max_inflight: int = 4,
    ) -> 'AgentRuntime':
        """Compose a personal runtime, auto-loading the simple provider env."""
        if provider is None:
            provider = ProviderConfig.from_env(default_model=default_model)
        principal = PrincipalContext.user(
            subject_id=subject_id,
            owner_user_id=owner_user_id,
            scopes={'agents:run', 'tasks:read', 'tasks:abort'},
        )
        return cls(
            principal=principal,
            provider=provider,
            provider_source=provider_source,
            default_model=default_model,
            max_inflight=max_inflight,
        )

    @property
    def in_flight(self) -> int:
        return self._gate.in_flight

    @property
    def capacity(self) -> int:
        return self._gate.capacity

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def configure_provider(
        self,
        provider: ProviderConfig | Mapping[str, Any] | None,
        *,
        source: str = 'runtime',
    ) -> None:
        """Atomically replace the default Provider for subsequently started runs.

        Every admitted execution already owns an ephemeral dispatch slot, so a
        control-panel update cannot retarget work that is currently in flight.
        """
        if provider is not None and not isinstance(provider, ProviderConfig):
            provider = ProviderConfig.from_mapping(provider)
        with self._lock:
            if self._closed:
                raise AgentClosedError('AgentRuntime is closed')
            self.provider = provider
            self.default_model = provider.model if provider is not None else ''
            self.provider_source = str(source or 'runtime')

    def _coerce_request(
        self,
        messages: list[dict] | AgentRequest,
        *,
        model: str = '',
        provider: ProviderConfig | Mapping[str, Any] | None = None,
        config: dict | None = None,
        capabilities: dict | None = None,
        custom_tools: list[dict] | None = None,
        custom_tools_mode: str = 'augment',
        trajectory: str | None = None,
        conversation_id: str = '',
        request_id: str = '',
        timeout_s: float = 600.0,
    ) -> AgentRequest:
        if isinstance(messages, AgentRequest):
            if any((model, provider, config, capabilities, custom_tools,
                    trajectory, conversation_id, request_id)) \
                    or custom_tools_mode != 'augment' or timeout_s != 600.0:
                raise AgentConfigurationError(
                    'an AgentRequest cannot be combined with keyword overrides')
            return messages
        provider_value = provider
        if isinstance(provider_value, Mapping) and not (
                provider_value.get('model')
                or provider_value.get('model_id')):
            provider_value = dict(provider_value)
            provider_value['model'] = model or self.default_model
        return AgentRequest(
            messages=messages,
            model=model,
            provider=provider_value,
            config=config or {},
            capabilities=capabilities or {},
            custom_tools=custom_tools or [],
            custom_tools_mode=custom_tools_mode,
            trajectory=trajectory,
            conversation_id=conversation_id,
            request_id=request_id,
            timeout_s=timeout_s,
        )

    def start(
        self,
        messages: list[dict] | AgentRequest,
        *,
        model: str = '',
        provider: ProviderConfig | Mapping[str, Any] | None = None,
        config: dict | None = None,
        capabilities: dict | None = None,
        custom_tools: list[dict] | None = None,
        custom_tools_mode: str = 'augment',
        trajectory: str | None = None,
        conversation_id: str = '',
        request_id: str = '',
        timeout_s: float = 600.0,
    ) -> AgentExecution:
        """Submit one run and return its lifecycle handle immediately."""
        request = self._coerce_request(
            messages,
            model=model,
            provider=provider,
            config=config,
            capabilities=capabilities,
            custom_tools=custom_tools,
            custom_tools_mode=custom_tools_mode,
            trajectory=trajectory,
            conversation_id=conversation_id,
            request_id=request_id,
            timeout_s=timeout_s,
        )
        with self._lock:
            if self._closed:
                raise AgentClosedError('AgentRuntime is closed')
            runtime_provider = self.provider
            runtime_default_model = self.default_model

        selected_provider = request.provider or runtime_provider
        selected_model = str(
            request.model
            or (selected_provider.model if selected_provider else '')
            or runtime_default_model
        ).strip()
        if not selected_model:
            raise AgentConfigurationError(
                'no model configured; pass model=... or set '
                'TOFU_AGENT_PROVIDER_MODEL')

        if request.trajectory:
            from lib.trajectory import AVAILABLE_FORMATS
            if request.trajectory not in AVAILABLE_FORMATS:
                raise AgentConfigurationError(
                    f'unknown trajectory format {request.trajectory!r}; '
                    f'expected one of {list(AVAILABLE_FORMATS)}')

        # Mint once so retries, task GET, blocking responses, and resumed
        # streams all project the same public completion id.
        if not request.request_id:
            request.request_id = f'run-{uuid.uuid4().hex[:20]}'

        cfg = apply_storage_free_runtime_policy(build_agent_config(
            selected_model, request.config, request.capabilities))
        provider_handle = None
        tool_env = None
        owner_tag = self.principal.subject_id
        try:
            if selected_provider is not None:
                from lib.llm_dispatch.ephemeral import mint_ephemeral_slot
                provider_handle = mint_ephemeral_slot(
                    base_url=selected_provider.base_url,
                    api_key=selected_provider.api_key,
                    model_id=selected_model,
                    owner=owner_tag,
                    extra_headers=dict(selected_provider.extra_headers),
                    thinking_format=selected_provider.thinking_format,
                    capabilities=(set(selected_provider.capabilities)
                                  if selected_provider.capabilities else None),
                )
            if request.custom_tools:
                from lib.tools.tool_env import mint_tool_env
                tool_env = mint_tool_env(
                    tools=request.custom_tools, owner=owner_tag)
                cfg['_customToolSchemas'] = tool_env.schemas
                cfg['_customToolsMode'] = request.custom_tools_mode
                if request.custom_tools_mode == 'exclusive':
                    # Reuse the established explicit-tool precedence branch:
                    # the model and dispatch authority see exactly this clean,
                    # task-local catalog and no host built-ins.
                    cfg['_explicitToolSchemas'] = list(tool_env.schemas)
        except Exception:
            if provider_handle is not None:
                from lib.llm_dispatch.ephemeral import dispose_ephemeral_slot
                dispose_ephemeral_slot(provider_handle)
            raise

        if not self._gate.try_acquire():
            self._dispose_resources(provider_handle, tool_env)
            raise AgentOverloadedError(
                f'AgentRuntime is at capacity ({self.capacity} in flight)')

        from lib.tasks_pkg.manager import create_task
        conversation = request.conversation_id or f'agent-{uuid.uuid4().hex[:12]}'
        try:
            task = create_task(
                conversation,
                request.messages,
                cfg,
                principal=self.principal,
                supersede=False,
                transient=True,
            )
        except Exception:
            self._gate.release()
            self._dispose_resources(provider_handle, tool_env)
            raise

        task['_api_v1'] = True
        task['_via_agent_run'] = True
        task['_publicAgentRuntime'] = True
        if request.request_id:
            task['_requestId'] = request.request_id
        if tool_env is not None:
            task['_tool_env'] = tool_env
        if provider_handle is not None:
            task['_pinned_provider_id'] = provider_handle.slot.provider_id

        execution = AgentExecution(
            self,
            task,
            request,
            model=selected_model,
            public_provider_id=('inline' if provider_handle else ''),
        )
        task['_transientEventNotifier'] = execution._nudge.set
        cleaned = {'value': False}

        def _cleanup(_task_id: str) -> None:
            with self._lock:
                if cleaned['value']:
                    return
                cleaned['value'] = True
                self._active.discard(_task_id)
            execution._terminal.set()
            execution._nudge.set()
            task.pop('_transientEventNotifier', None)
            self._gate.release()
            self._dispose_resources(provider_handle, tool_env)

        from lib.agent_core.admission import on_terminal
        on_terminal(task['id'], _cleanup)
        with self._lock:
            self._executions[task['id']] = execution
            self._active.add(task['id'])
            self._prune_retained_locked()

        try:
            from lib.tasks_pkg.spawn import spawn_task
            spawn_task(task)
        except Exception as exc:
            task['status'] = 'error'
            task['finishReason'] = 'error'
            task['error'] = {
                'kind': 'runtime_spawn_failed',
                'message': str(exc),
            }
            from lib.agent_core.admission import fire_terminal_callbacks
            fire_terminal_callbacks(task['id'])
            raise
        return execution

    @staticmethod
    def _dispose_resources(provider_handle, tool_env) -> None:
        if provider_handle is not None:
            try:
                from lib.llm_dispatch.ephemeral import dispose_ephemeral_slot
                dispose_ephemeral_slot(provider_handle)
            except Exception as exc:
                logger.error('provider cleanup failed: %s', exc, exc_info=True)
        if tool_env is not None:
            try:
                from lib.tools.tool_env import dispose_tool_env
                dispose_tool_env(tool_env)
            except Exception as exc:
                logger.error('tool environment cleanup failed: %s',
                             exc, exc_info=True)

    def _prune_retained_locked(self) -> None:
        overflow = len(self._executions) - self._max_retained_runs
        if overflow <= 0:
            return
        for task_id, execution in tuple(self._executions.items()):
            if overflow <= 0:
                break
            if execution.done and task_id not in self._active:
                self._executions.pop(task_id, None)
                overflow -= 1

    def get(self, task_id: str) -> AgentExecution | None:
        """Return one execution owned by this runtime's explicit principal."""
        with self._lock:
            execution = self._executions.get(str(task_id or ''))
        if execution is None:
            return None
        if int(execution._task.get('_userId') or 0) != \
                self.principal.require_owner(context='AgentRuntime.get'):
            return None
        return execution

    def _abort_execution(self, execution: AgentExecution) -> bool:
        if self.get(execution.task_id) is not execution:
            return False
        from lib.tasks_pkg.manager.runtime import chat_task_runtime
        return chat_task_runtime.abort_owned(
            execution.task_id,
            user_id=self.principal.require_owner(
                context='AgentRuntime.abort'),
        )

    def run(self, messages: list[dict] | AgentRequest, **kwargs) -> AgentResult:
        execution = self.start(messages, **kwargs)
        return execution.result()

    def stream(
        self,
        messages: list[dict] | AgentRequest,
        **kwargs,
    ) -> Iterator[dict]:
        execution = self.start(messages, **kwargs)
        yield from execution.events(timeout_s=execution.timeout_s)

    async def run_async(
        self,
        messages: list[dict] | AgentRequest,
        **kwargs,
    ) -> AgentResult:
        execution = self.start(messages, **kwargs)
        return await execution.result_async()

    async def stream_async(
        self,
        messages: list[dict] | AgentRequest,
        **kwargs,
    ) -> AsyncIterator[dict]:
        execution = self.start(messages, **kwargs)
        async for event in execution.events_async(
                timeout_s=execution.timeout_s):
            yield event

    def close(self, *, abort: bool = True, timeout_s: float = 5.0) -> None:
        """Reject new work and optionally abort/wait briefly for active runs."""
        with self._lock:
            self._closed = True
            active = [self._executions[task_id]
                      for task_id in tuple(self._active)
                      if task_id in self._executions]
        if abort:
            for execution in active:
                execution.abort()
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        for execution in active:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            execution._terminal.wait(remaining)

    def __enter__(self) -> 'AgentRuntime':
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


__all__ = ['AgentExecution', 'AgentRuntime']
