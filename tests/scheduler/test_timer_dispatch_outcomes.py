"""Executable contract for timer continuation dispatch outcomes."""

from __future__ import annotations

import pytest

from lib.scheduler.conversation_dispatch import ScheduledTurnDispatch
from lib.scheduler.timer import _loop


pytestmark = pytest.mark.unit


def _timer() -> dict:
    return {
        "id": "tmr_dispatch", "status": "active",
        "user_id": 7,
        "conv_id": "conv_dispatch",
        "continuation_message": "continue",
        "tools_config": {},
    }


def _install_dispatch(monkeypatch, result) -> None:
    monkeypatch.setattr(_loop, "_get_timer_row", lambda *a, **k: _timer())
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
    monkeypatch.setattr(_loop, "_get_timer_row", lambda *a, **k: _timer())
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


def test_started_continuation_keeps_watcher_until_trigger_receipt_commits(monkeypatch):
    from lib.scheduler.timer import _notify
    import lib.scheduler.conversation_dispatch as dispatch_module

    row = {**_timer(), 'last_poll_decision': 'ready'}
    commands, removed, notifications = [], [], []
    monkeypatch.setattr(_loop, '_get_timer_row', lambda *a, **k: dict(row))
    monkeypatch.setattr(_loop, 'remove_live_watcher', removed.append)
    monkeypatch.setattr(_notify, 'notify_timer_changed',
                        lambda change, **kw: notifications.append(change))

    def dispatch(**kwargs):
        commands.append(kwargs['command_id'])
        return ScheduledTurnDispatch('started', 'task-once')

    class Client:
        calls = 0

        def command(self, operation, payload, command_id):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError('trigger receipt unavailable')
            row.update(status=payload['status'], execution_task_id=payload['execution_task_id'])
            return {'changed': True}

    client = Client()
    monkeypatch.setattr(_loop, '_timer_client', lambda **kw: client)
    monkeypatch.setattr(dispatch_module, 'dispatch_scheduled_turn', dispatch)

    assert _loop._execute_continuation(row) is None
    assert row['status'] == 'active'
    assert removed == [] and notifications == []
    assert _loop._execute_continuation(row) == 'task-once'
    assert commands == ['timer:tmr_dispatch', 'timer:tmr_dispatch']
    assert row['status'] == 'triggered'
    assert removed == ['tmr_dispatch'] and notifications == ['triggered']


def test_lost_active_status_never_announces_trigger_success(monkeypatch):
    from lib.scheduler.timer import _notify

    _install_dispatch(monkeypatch, ScheduledTurnDispatch('started', 'task-once'))
    notifications = []

    class Client:
        def command(self, *args):
            return {'changed': False}

    monkeypatch.setattr(_loop, '_timer_client', lambda **kw: Client())
    monkeypatch.setattr(_notify, 'notify_timer_changed',
                        lambda change, **kw: notifications.append(change))
    monkeypatch.setattr(_loop, 'remove_live_watcher', lambda *a: None)
    assert _loop._execute_continuation(_timer()) is None
    assert notifications == []
