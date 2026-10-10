"""Application result for one bounded durable-run replay page."""

from __future__ import annotations

from dataclasses import dataclass

from lib.orchestration.run_lifecycle_policy import RunLifecycle
from lib.task_replay import TASK_REPLAY_CAUGHT_UP_FIELD, TaskReplayPage
from lib.task_replay import safe_replay_cursor


@dataclass(frozen=True)
class RunReplayResult(TaskReplayPage):
    """Replay page carrying a terminal snapshot only after catch-up."""

    run: dict | None = None
    caught_up: bool = True

    def payload(self, extras: dict | None = None) -> dict:
        additions = dict(extras or {})
        additions[TASK_REPLAY_CAUGHT_UP_FIELD] = bool(self.caught_up)
        if self.done and self.caught_up and self.run is not None:
            additions['run'] = self.run
        return super().payload(additions)


def normalize_run_replay_cursor(cursor: int) -> int:
    """Normalize a consumer cursor before it reaches the persistence port."""
    return safe_replay_cursor(cursor)


def project_run_replay_result(
    *,
    run: dict,
    requested_cursor: int,
    events: list[dict],
    next_cursor: int,
    cursor_reset: bool,
    caught_up: bool,
) -> RunReplayResult:
    """Project one bounded event page plus its lifecycle snapshot."""
    lifecycle = RunLifecycle.from_run(run)
    return RunReplayResult(
        events=events,
        next_cursor=next_cursor,
        run_status=lifecycle.status,
        done=lifecycle.terminal,
        requested_cursor=requested_cursor,
        cursor_reset=cursor_reset,
        run=run,
        caught_up=bool(caught_up),
    )


__all__ = [
    'RunReplayResult', 'normalize_run_replay_cursor',
    'project_run_replay_result',
]
