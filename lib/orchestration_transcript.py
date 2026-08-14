"""Thread-safe execution transcript and context projection for graph runs."""

from __future__ import annotations

import threading
from typing import Any


class OrchestrationTranscript:
    """Own completed node turns and verifier lookup for one executor."""

    def __init__(self, *, lock: Any | None = None) -> None:
        self._lock = lock or threading.Lock()
        self._entries: list[dict] = []

    def record(
        self,
        node_id,
        role,
        output,
        status,
        error,
        elapsed,
        *,
        state_changing=0,
        exploratory=0,
    ) -> None:
        entry = {
            'node_id': node_id,
            'role': role,
            'output': output,
            'status': status,
            'error': error,
            'elapsed': round(elapsed, 2),
            'state_changing': state_changing,
            'exploratory': exploratory,
        }
        with self._lock:
            self._entries.append(entry)

    def snapshot(self) -> list[dict]:
        """Return detached rows in completion order."""
        with self._lock:
            return [dict(entry) for entry in self._entries]

    def last_verifier_output(
        self,
        verifier_roles: set[str] | frozenset[str],
    ) -> str:
        entries = self.snapshot()
        for entry in reversed(entries):
            if entry.get('role') in verifier_roles:
                return str(entry.get('output') or '')
        return str(entries[-1].get('output') or '') if entries else ''

    def last_verifier_role(
        self,
        verifier_roles: set[str] | frozenset[str],
    ) -> str:
        for entry in reversed(self.snapshot()):
            if entry.get('role') in verifier_roles:
                return str(entry.get('role') or '')
        return ''


def append_role_context(context: str, role: str, output: str) -> str:
    """Append one labelled turn to the executor's accumulated scratchpad."""
    block = f'[{role}]\n{output}'.strip()
    return block if not context else context + '\n\n' + block


def subflow_deliverable(
    result: dict,
    *,
    verifier_roles: set[str] | frozenset[str],
) -> str:
    """Project a nested result to its last producer output membrane."""
    for entry in reversed(result.get('transcript') or []):
        if entry.get('role') in verifier_roles:
            continue
        output = entry.get('output')
        if output:
            return str(output)
    return str(result.get('final') or '')


__all__ = [
    'OrchestrationTranscript',
    'append_role_context',
    'subflow_deliverable',
]
