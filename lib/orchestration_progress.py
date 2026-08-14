"""Producer progress ledger for orchestration verifier loops.

The ledger folds concurrent producer snapshots deterministically and projects
the engine-injected deliverables summary consumed by verifier roles. It is
independent of graph walking, verdict classification and agent execution.
"""

from __future__ import annotations

import threading
from typing import Any

REPLAN_SUMMARY_CHARS = 2000


class OrchestrationProgressLedger:
    """Thread-safe latest-turn and per-iteration producer accounting."""

    def __init__(self, *, lock: Any | None = None):
        self._lock = lock or threading.Lock()
        self._latest: dict = {}
        self._iteration: list[dict] = []

    def record_producer(self, snapshot: dict) -> None:
        """Record one producer as both latest turn and iteration member."""
        entry = dict(snapshot or {})
        with self._lock:
            self._latest = entry
            self._iteration.append(entry)

    def reset_iteration(self) -> None:
        with self._lock:
            self._iteration = []

    def replace_iteration(self, snapshots: list[dict]) -> None:
        """Compatibility/test seam for replacing the current iteration."""
        with self._lock:
            self._iteration = [dict(snapshot) for snapshot in snapshots or []]

    def iteration_snapshot(self) -> list[dict]:
        with self._lock:
            return [dict(snapshot) for snapshot in self._iteration]

    def replace_latest(self, snapshot: dict) -> None:
        """Compatibility/test seam for replacing the latest producer."""
        with self._lock:
            self._latest = dict(snapshot or {})

    def latest_snapshot(self) -> dict:
        with self._lock:
            return dict(self._latest)

    def aggregate_iteration(self) -> dict:
        """Fold all producers in this iteration into one deterministic row."""
        producers = self.iteration_snapshot()
        if not producers:
            return {}
        if len(producers) == 1:
            return producers[0]

        names: list[str] = []
        state_changing = exploratory = 0
        reported = False
        for producer in producers:
            state_changing += int(producer.get('sc_count') or 0)
            exploratory += int(producer.get('explore_count') or 0)
            names.extend(producer.get('names') or [])
            reported = reported or bool(producer.get('reported'))
        return {
            'node_id': ','.join(
                str(producer.get('node_id') or '') for producer in producers
            ),
            'role': 'parallel',
            'sc_count': state_changing,
            'explore_count': exploratory,
            'names': names,
            'reported': reported,
        }

    def append_deliverables_snapshot(
        self,
        context: str,
        *,
        in_loop: bool,
    ) -> str:
        """Append the latest/aggregate tool summary for a verifier role."""
        snapshot = self.aggregate_iteration() if in_loop else None
        if not snapshot:
            snapshot = self.latest_snapshot()
        if not snapshot or not snapshot.get('reported'):
            return context

        state_changing = snapshot.get('sc_count', 0)
        counts: dict[str, int] = {}
        for name in snapshot.get('names') or []:
            counts[name] = counts.get(name, 0) + 1
        names_text = ', '.join(
            f'{name}×{count}' if count > 1 else name
            for name, count in counts.items()
        ) or '(none)'
        if state_changing == 0:
            hint = (
                'GUIDANCE: the producer made ZERO state-changing calls '
                'this turn. The correct verdict is almost always '
                'CONTINUE with "execute, stop analyzing" feedback.'
            )
        else:
            hint = (
                'GUIDANCE: the producer made real edits — verify they '
                'close the checklist before approving.'
            )
        block = (
            '\n\n───── Deliverables Snapshot (engine-injected) ─────\n'
            f'- Producer latest turn: {state_changing} state-changing, '
            f'{snapshot.get("explore_count", 0)} exploratory tool calls.\n'
            f'- State-changing calls: {names_text}\n'
            f'- {hint}\n'
            '───────────────────────────────────────────────────'
        )
        return context + block

    def build_replan_summary(
        self,
        transcript: list[dict],
        *,
        verifier_roles: set[str] | frozenset[str],
        limit: int = REPLAN_SUMMARY_CHARS,
    ) -> str:
        """Project producer transcript entries into a bounded replan summary."""
        lines: list[str] = []
        for entry in transcript or []:
            if entry.get('role') in verifier_roles:
                continue
            state_changing = entry.get('state_changing', 0)
            preview = (
                (entry.get('output') or '').strip().replace('\n', ' ')[:160]
            )
            lines.append(
                f'- {entry.get("role")}: {state_changing} '
                f'state-changing calls. {preview}'
            )
        summary = '\n'.join(lines)
        return summary[-max(1, int(limit)):]


__all__ = ['REPLAN_SUMMARY_CHARS', 'OrchestrationProgressLedger']
