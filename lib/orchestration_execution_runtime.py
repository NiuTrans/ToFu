"""Top-level lifecycle boundary for one orchestration graph execution.

The graph interpreter owns node scheduling. This runtime owns the surrounding
entry seed, timing, failure classification, terminal event and detached result
projection through explicit ports. It deliberately has no node-kind policy.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Protocol

from lib.log import get_logger
from lib.orchestration_graph import FlowExecutionError
from lib.orchestration.outcome_domain import TerminalOutcome


logger = get_logger(__name__)


class OrchestrationExecutionNavigatorPort(Protocol):
    def find_start(self) -> str: ...


class OrchestrationExecutionDataflowPort(Protocol):
    def set_initial_context(self, context: str) -> None: ...


class OrchestrationExecutionOutcomePort(Protocol):
    def classify(
        self,
        engine_status: str,
        *,
        error: str = '',
        failure_kind: str = '',
    ) -> TerminalOutcome: ...

    def loop_exits_snapshot(self) -> list[dict]: ...

    def artifacts_snapshot(self) -> list[dict]: ...


class OrchestrationExecutionSnapshotPort(Protocol):
    def snapshot(self) -> list[dict]: ...


class OrchestrationExecutionRuntime:
    """Execute a graph walk inside the canonical top-level lifecycle."""

    def __init__(
        self,
        *,
        definition: Mapping,
        nodes: Mapping[str, dict],
        navigator: OrchestrationExecutionNavigatorPort,
        dataflow: OrchestrationExecutionDataflowPort,
        outcomes: OrchestrationExecutionOutcomePort,
        transcript: OrchestrationExecutionSnapshotPort,
        trace: OrchestrationExecutionSnapshotPort,
        walk: Callable[[str, str], str],
        agents_run: Callable[[], int],
        emit: Callable[[dict], None],
        abort_errors: tuple[type[BaseException], ...],
        structural_errors: tuple[type[BaseException], ...] = (
            FlowExecutionError,
        ),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._definition = definition
        self._nodes = nodes
        self._navigator = navigator
        self._dataflow = dataflow
        self._outcomes = outcomes
        self._transcript = transcript
        self._trace = trace
        self._walk = walk
        self._agents_run = agents_run
        self._emit = emit
        self._abort_errors = abort_errors
        self._structural_errors = structural_errors
        self._clock = clock

    def run(self, *, initial_context: str = '') -> dict:
        """Walk from Start and return the canonical detached engine result."""
        start = self._navigator.find_start()
        context = initial_context
        if not (context or '').strip():
            start_node = self._nodes.get(start) or {}
            seed = (start_node.get('params') or {}).get('seed')
            if isinstance(seed, str) and seed.strip():
                context = seed
        context = context or ''
        self._dataflow.set_initial_context(context)
        started = self._clock()
        self._emit({
            'type': 'flow_start',
            'name': self._definition.get('name'),
            'nodes': len(self._nodes),
        })
        logger.info(
            '[FlowEngine] run START name=%r nodes=%d start=%s',
            self._definition.get('name'), len(self._nodes), start,
        )

        status, final, error, failure_kind = 'completed', '', None, ''
        try:
            final = self._walk(start, context)
        except self._abort_errors:
            status, error = 'aborted', 'aborted'
            logger.info('[FlowEngine] run ABORTED')
        except self._structural_errors as exc:
            status, error, failure_kind = 'failed', str(exc), 'structural'
            logger.error('[FlowEngine] structural failure: %s', exc)
        except Exception as exc:
            status = 'failed'
            error = f'{type(exc).__name__}: {exc}'
            failure_kind = 'exception'
            logger.error('[FlowEngine] run crashed: %s', exc, exc_info=True)

        terminal = self._outcomes.classify(
            status, error=error or '', failure_kind=failure_kind)
        elapsed = self._clock() - started
        agent_count = self._agents_run()
        self._emit({
            'type': 'flow_complete',
            'status': terminal.engine_status,
            'agents_run': agent_count,
            'elapsed': round(elapsed, 1),
            **terminal.event_fields(),
        })
        logger.info(
            '[FlowEngine] run DONE status=%s reason=%s agents=%d elapsed=%.1fs',
            terminal.engine_status, terminal.stop_reason, agent_count, elapsed,
        )
        return {
            'ok': terminal.ok,
            'status': terminal.engine_status,
            'stop_reason': terminal.stop_reason,
            'outcome': terminal.as_dict(),
            'final': final,
            'transcript': self._transcript.snapshot(),
            'trace': self._trace.snapshot(),
            'loop_exits': self._outcomes.loop_exits_snapshot(),
            'agents_run': agent_count,
            'artifacts': self._outcomes.artifacts_snapshot(),
            'error': error,
        }


__all__ = [
    'OrchestrationExecutionDataflowPort',
    'OrchestrationExecutionNavigatorPort',
    'OrchestrationExecutionOutcomePort',
    'OrchestrationExecutionRuntime',
    'OrchestrationExecutionSnapshotPort',
]
