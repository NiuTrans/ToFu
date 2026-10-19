"""Capability contracts shared by task-runtime consumers.

The concrete :class:`TaskRuntime` owns storage, worker scheduling and push
delivery.  HTTP adapters and mutation classifiers only need a much smaller
surface, so they depend on these structural ports instead of importing the
implementation.
"""

from __future__ import annotations

from typing import Protocol


class TaskReplayRuntimePort(Protocol):
    """Read-side capability for one versioned task replay stream."""

    def poll(self, task_id: str, cursor: int = 0) -> dict: ...

    def get_owned(self, task_id: str, *, user_id: int) -> dict | None: ...


class TaskAbortRuntimePort(Protocol):
    """Mutation/read capabilities needed to classify an abort race."""

    def abort_owned(self, task_id: str, *, user_id: int) -> bool: ...

    def get_owned(self, task_id: str, *, user_id: int) -> dict | None: ...


class TaskRouteRuntimePort(
    TaskReplayRuntimePort,
    TaskAbortRuntimePort,
    Protocol,
):
    """Complete runtime surface consumed by the generic task route factory."""

    kind: str


__all__ = [
    'TaskReplayRuntimePort',
    'TaskAbortRuntimePort',
    'TaskRouteRuntimePort',
]
