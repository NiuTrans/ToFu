"""Detach context-provider metadata without copying executor replay buffers.

Entry point: context_provider_snapshot. Dependency: observer_snapshot. Providers
read configuration, identities, tools and frozen context evidence; transcript and
rendering state remain with the synchronous Composer and its message argument.
"""
from __future__ import annotations

from lib.tasks_pkg.observer_snapshot import observer_snapshot

_EXECUTOR_BUFFER_KEYS = frozenset({
    'messages', '_turn_input_messages', 'events', 'result', 'toolRounds',
})


def context_provider_snapshot(task: dict) -> dict:
    """Keep metadata detached and runtime handles shared, independent of history.

    The removed buffers are never provider inputs. Do not alias them as a faster
    copy: a provider finishing after its deadline must not mutate live history.
    Renderer/checklist/state projections continue to receive the original task.
    """
    return observer_snapshot({
        key: value for key, value in task.items()
        if key not in _EXECUTOR_BUFFER_KEYS
    })
