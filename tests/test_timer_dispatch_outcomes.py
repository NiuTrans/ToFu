"""Executable contract for timer continuation dispatch outcomes."""

from __future__ import annotations

import pytest

from lib.scheduler.conversation_dispatch import ScheduledTurnDispatch
from lib.scheduler.timer import _loop


pytestmark = pytest.mark.unit


def _timer() -> dict:
    return {
        "id": "tmr_dispatch",
        "user_id": 7,
        "conv_id": "conv_dispatch",
        "continuation_message": "continue",
        "tools_config": {},
    }


def _install_dispatch(monkeypatch, result) -> None:
    import lib.scheduler.conversation_dispatch as dispatch_module

    monkeypatch.setattr(
        dispatch_module,
        "dispatch_scheduled_turn",
        lambda **_kwargs: result,
    )


def test_lane_contention_stays_retryable(monkeypatch):
    _install_dispatch(monkeypatch, ScheduledTurnDispatch("busy"))
    retired: list[str] = []
    monkeypatch.setattr(
        _loop,
        "_mark_dispatch_failed",
        lambda _timer_record, reason: retired.append(reason),
    )

    assert _loop._execute_continuation(_timer()) is None
    assert retired == []


@pytest.mark.parametrize("disposition", ["target_missing", "start_failed"])
def test_permanent_dispatch_failure_retires_watcher(monkeypatch, disposition):
    _install_dispatch(monkeypatch, ScheduledTurnDispatch(disposition))
    retired: list[str] = []
    monkeypatch.setattr(
        _loop,
        "_mark_dispatch_failed",
        lambda _timer_record, reason: retired.append(reason),
    )

    assert _loop._execute_continuation(_timer()) is None
    assert len(retired) == 1


def test_infrastructure_failure_stays_retryable(monkeypatch):
    import lib.scheduler.conversation_dispatch as dispatch_module

    def _raise(**_kwargs):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(dispatch_module, "dispatch_scheduled_turn", _raise)
    retired: list[str] = []
    monkeypatch.setattr(
        _loop,
        "_mark_dispatch_failed",
        lambda _timer_record, reason: retired.append(reason),
    )

    assert _loop._execute_continuation(_timer()) is None
    assert retired == []
