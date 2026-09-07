"""Normalize agent-runner tool usage for orchestration execution.

Runner implementations historically returned either ``tool_names`` or a
SubAgent-style ``tool_log``.  This module is the single compatibility boundary
for those shapes.  Flow execution, progress accounting and trace projection
consume one immutable value instead of learning each runner's wire format.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from lib.agent_verdict import STATE_CHANGING_TOOLS_WITH_CODE_EXEC


@dataclass(frozen=True)
class OrchestrationToolUsage:
    """Detached classification of one runner result's tool activity."""

    state_changing_tools: tuple[str, ...] = ()
    exploratory_tools: tuple[str, ...] = ()
    reported: bool = False

    @property
    def state_changing_count(self) -> int:
        return len(self.state_changing_tools)

    @property
    def exploratory_count(self) -> int:
        return len(self.exploratory_tools)

    def engine_tuple(self) -> tuple[int, int, list[str], bool]:
        """Return the legacy FlowExecutor tuple with a detached names list."""
        return (
            self.state_changing_count,
            self.exploratory_count,
            list(self.state_changing_tools),
            self.reported,
        )


def _entry_name(entry: Any) -> str:
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, Mapping):
        for key in ('tool', 'toolName'):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ''


def _entries(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, Mapping)):
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=repr)
    return []


def classify_orchestration_tool_usage(
    result: Mapping[str, Any] | None,
) -> OrchestrationToolUsage:
    """Project supported runner result shapes into one safe usage contract.

    ``reported`` intentionally records key presence, not whether a recognized
    name was found.  An explicit empty list means "the runner reported zero
    calls" and may drive the zero-deliverable guard; omitting both fields means
    "this runner does not support tool telemetry" and must not trigger it.
    ``tool_names`` remains authoritative when supplied, matching the legacy
    engine behavior; a ``None`` value falls back to ``tool_log``.
    """
    if not isinstance(result, Mapping):
        return OrchestrationToolUsage()

    reported = 'tool_names' in result or 'tool_log' in result
    raw = result.get('tool_names')
    if raw is None:
        raw = result.get('tool_log')

    state_changing: list[str] = []
    exploratory: list[str] = []
    for entry in _entries(raw):
        name = _entry_name(entry)
        if not name:
            continue
        if name in STATE_CHANGING_TOOLS_WITH_CODE_EXEC:
            state_changing.append(name)
        else:
            exploratory.append(name)
    return OrchestrationToolUsage(
        state_changing_tools=tuple(state_changing),
        exploratory_tools=tuple(exploratory),
        reported=reported,
    )


__all__ = [
    'OrchestrationToolUsage',
    'classify_orchestration_tool_usage',
]
