"""Thread-safe execution facts used to classify terminal outcomes."""

from __future__ import annotations

from typing import Any

from lib.orchestration.outcome_domain import (
    TerminalOutcome,
    classify_terminal_outcome,
)


class OrchestrationOutcomeLedger:
    """Own facts that decide one execution's terminal outcome."""

    def __init__(self, *, lock: Any):
        self._lock = lock
        self._loop_exits: list[dict] = []
        self._node_failures: list[dict] = []
        self._artifacts: list[dict] = []

    def record_loop_exit(
        self,
        *,
        node_id: str,
        reason: str,
        iterations: int,
    ) -> None:
        with self._lock:
            self._loop_exits.append({
                'node_id': node_id,
                'reason': reason,
                'iterations': iterations,
            })

    def record_node_failure(
        self,
        *,
        node_id: str,
        role: str | None,
        error: str,
    ) -> None:
        with self._lock:
            self._node_failures.append({
                'node_id': node_id,
                'role': role,
                'error': str(error or 'failed'),
            })

    def record_artifact(self, artifact: dict) -> None:
        with self._lock:
            self._artifacts.append(dict(artifact))

    def loop_exits_snapshot(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._loop_exits]

    def node_failures_snapshot(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._node_failures]

    def artifacts_snapshot(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._artifacts]

    def classify(
        self,
        engine_status: str,
        *,
        error: str = '',
        failure_kind: str = '',
    ) -> TerminalOutcome:
        return classify_terminal_outcome(
            engine_status,
            error=error,
            failure_kind=failure_kind,
            loop_exits=self.loop_exits_snapshot(),
            node_failures=self.node_failures_snapshot(),
        )

    @property
    def loop_exits_live(self) -> list[dict]:
        return self._loop_exits

    @property
    def node_failures_live(self) -> list[dict]:
        return self._node_failures

    @property
    def artifacts_live(self) -> list[dict]:
        return self._artifacts


__all__ = ['OrchestrationOutcomeLedger']
