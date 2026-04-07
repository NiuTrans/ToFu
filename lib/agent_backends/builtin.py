"""lib/agent_backends/builtin.py — Built-in Tofu backend wrapper.

Wraps the existing ``run_task()`` orchestrator as an ``AgentBackend``.
This is the default backend that provides ALL Tofu features: web search,
fetch, project tools, image gen, browser extension, desktop agent, swarm,
scheduler, skills, error tracker, etc.

For the built-in backend, ``start_turn()`` is NOT the primary code path.
The existing ``chat_start()`` in ``routes/chat.py`` calls ``run_task()``
directly when ``agentBackend == 'builtin'`` (or unset).  This wrapper
exists to satisfy the ``AgentBackend`` interface for the registry and
status API.

The ``start_turn()`` method here creates a task, starts ``run_task()`` in
a thread, and polls ``task['events']`` to yield ``NormalizedEvent``s.
This round-trip (SSE → NormalizedEvent → SSE) validates the abstraction
but is intentionally bypassed in production for zero-regression safety.
"""

from __future__ import annotations

import time
from typing import Any, Iterator

from lib.agent_backends.protocol import (
    AgentBackend,
    BackendCapabilities,
    NormalizedEvent,
    NormalizedEventKind,
)
from lib.log import get_logger

logger = get_logger(__name__)

K = NormalizedEventKind


class BuiltinBackend(AgentBackend):
    """Wraps the existing Tofu orchestrator as an AgentBackend.

    Always available, always authenticated.  All capabilities enabled.
    """

    @property
    def name(self) -> str:
        return 'builtin'

    @property
    def display_name(self) -> str:
        return 'Tofu (Built-in)'

    def get_capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            # Core
            streaming=True,
            multi_turn=True,
            abort=True,
            # All tool capabilities
            has_web_search=True,
            has_file_tools=True,
            has_code_exec=True,
            # Tofu-only features — ALL available
            has_image_gen=True,
            has_browser_ext=True,
            has_desktop_agent=True,
            has_error_tracker=True,
            has_swarm=True,
            has_scheduler=True,
            has_conv_ref=True,
            has_human_guidance=True,
            # Full UI controls
            model_selector=True,
            thinking_depth=True,
            search_toggle=True,
            project_selector=True,
            preset_selector=True,
            temperature_control=True,
            endpoint_mode=True,
            approval_system='tool-level',
        )

    def is_available(self) -> bool:
        return True

    def is_authenticated(self) -> bool:
        return True

    def get_version(self) -> str | None:
        try:
            from lib.version import VERSION
            return VERSION
        except ImportError:
            logger.debug('[Builtin] VERSION not available')
            return None

    def start_turn(
        self,
        task: dict[str, Any],
        user_message: str,
        *,
        images: list[dict] | None = None,
        project_path: str | None = None,
        session_id: str | None = None,
    ) -> Iterator[NormalizedEvent]:
        """Start a turn using the built-in orchestrator.

        NOTE: In production, the built-in path bypasses this method entirely —
        ``routes/chat.py`` calls ``run_task()`` directly.  This method exists
        for interface completeness and for testing the abstraction.

        It creates a task, starts ``run_task()`` in a thread, then polls
        the task's event queue and reverse-maps SSE events to NormalizedEvents.
        """
        import threading

        from lib.tasks_pkg import create_task, run_task

        # Create a sub-task for the orchestrator
        builtin_task = create_task(
            task.get('convId', ''),
            task.get('messages', []),
            task.get('config', {}),
        )

        # Start orchestrator thread
        thread = threading.Thread(target=run_task, args=(builtin_task,), daemon=True)
        thread.start()

        # Poll events and translate to NormalizedEvents
        cursor = 0
        while True:
            if task.get('aborted'):
                builtin_task['aborted'] = True
                yield NormalizedEvent(kind=K.DONE, finish_reason='aborted')
                return

            with builtin_task['events_lock']:
                new_events = builtin_task['events'][cursor:]
                cursor = len(builtin_task['events'])

            for evt in new_events:
                normalized = self._sse_to_normalized(evt)
                if normalized:
                    yield normalized
                    if normalized.kind in (K.DONE, K.ERROR):
                        return

            if builtin_task['status'] != 'running' and not new_events:
                yield NormalizedEvent(
                    kind=K.DONE,
                    finish_reason=builtin_task.get('finishReason', 'stop'),
                    usage=builtin_task.get('usage') or {},
                    error_message=builtin_task.get('error') or '',
                )
                return

            time.sleep(0.05)

    def abort(self, task_id: str) -> bool:
        from lib.tasks_pkg import tasks, tasks_lock

        with tasks_lock:
            task = tasks.get(task_id)
        if task:
            task['aborted'] = True
            return True
        return False

    def _sse_to_normalized(self, evt: dict) -> NormalizedEvent | None:
        """Reverse-map an SSE event dict to a NormalizedEvent.

        This is the inverse of ``sse_bridge.normalized_to_sse()``.
        Used only for the start_turn() polling path.
        """
        etype = evt.get('type', '')

        if etype == 'delta':
            if evt.get('thinking'):
                return NormalizedEvent(kind=K.THINKING_DELTA, text=evt['thinking'])
            elif evt.get('content'):
                return NormalizedEvent(kind=K.TEXT_DELTA, text=evt['content'])

        elif etype == 'tool_start':
            return NormalizedEvent(
                kind=K.TOOL_START,
                tool_name=evt.get('tool', ''),
                tool_id=evt.get('toolId', ''),
                tool_input=evt.get('args', {}),
            )

        elif etype == 'tool_result':
            if evt.get('streaming'):
                return NormalizedEvent(
                    kind=K.TOOL_OUTPUT,
                    tool_id=evt.get('toolId', ''),
                    tool_output=evt.get('result', ''),
                )
            return NormalizedEvent(
                kind=K.TOOL_COMPLETE,
                tool_name=evt.get('tool', ''),
                tool_id=evt.get('toolId', ''),
                tool_output=evt.get('result', ''),
                tool_is_error=evt.get('isError', False),
            )

        elif etype == 'phase':
            return NormalizedEvent(
                kind=K.PHASE,
                text=evt.get('detail', evt.get('phase', '')),
            )

        elif etype == 'done':
            return NormalizedEvent(
                kind=K.DONE,
                finish_reason=evt.get('finishReason', 'stop'),
                usage=evt.get('usage', {}),
                error_message=evt.get('error', ''),
            )

        elif etype == 'state':
            # State snapshot — skip (only used for SSE reconnection)
            return None

        return None
