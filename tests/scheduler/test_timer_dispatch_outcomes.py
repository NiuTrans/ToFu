"""Executable contract for timer continuation dispatch outcomes.

A readiness watcher never owns its target lane: the conversation is very often
still working when the condition becomes true. The delivery layer therefore
steers the continuation into the live attempt (with the durable queue as
fallback) instead of reporting the busy lane as a failure that burns the
one-shot obligation.
"""

from __future__ import annotations

import pytest

from lib.scheduler.conversation_dispatch import ScheduledTurnDispatch
from lib.scheduler.timer import _loop


pytestmark = pytest.mark.unit


class _FakeClient:
    """Records the settle command so the receipt fields are assertable."""

    def __init__(self):
        self.payload: dict = {}

    def command(self, _operation, payload, _command_id):
        self.payload.update(payload)
        return {'changed': True}


def _timer() -> dict:
    return {
        "id": "tmr_dispatch", "status": "active",
        "user_id": 7,
        "conv_id": "conv_dispatch",
        "continuation_message": "continue",
        "tools_config": {},
    }


def _install_dispatch(monkeypatch, result) -> dict:
    monkeypatch.setattr(_loop, "_get_timer_row", lambda *a, **k: _timer())
    import lib.scheduler.conversation_dispatch as dispatch_module

    seen: dict = {}

    def dispatch(**kwargs):
        seen.update(kwargs)
        return result

    monkeypatch.setattr(dispatch_module, "dispatch_scheduled_turn", dispatch)
    return seen


def test_dispatch_asks_for_the_steer_lane(monkeypatch):
    """A busy lane is expected, so the scheduler must not use the bare
    ``lane_busy`` contract that drops the continuation."""
    seen = _install_dispatch(monkeypatch, ScheduledTurnDispatch("started", "task-1"))
    monkeypatch.setattr(_loop, "_timer_client", lambda **kw: _FakeClient())
    monkeypatch.setattr(_loop, "remove_live_watcher", lambda *a: None)
    assert _loop._execute_continuation(_timer()).kind == "started"
    assert seen["inject_mode"] == "steer"


def test_lane_contention_stays_retryable(monkeypatch):
    _install_dispatch(monkeypatch, ScheduledTurnDispatch("busy"))
    retired: list[str] = []
    monkeypatch.setattr(
        _loop,
        "_mark_dispatch_failed",
        lambda _timer_record, reason: retired.append(reason),
    )

    outcome = _loop._execute_continuation(_timer())
    assert outcome.kind == "retry"
    assert not outcome.accepted
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

    assert _loop._execute_continuation(_timer()).kind == "closed"
    assert len(retired) == 1


@pytest.mark.parametrize(
    ("disposition", "task_id", "queue_id"),
    [("steered", "", ""), ("queued", "", "queue-1"), ("started", "task-1", "")],
)
def test_accepted_delivery_settles_the_obligation(monkeypatch, disposition,
                                                  task_id, queue_id):
    """Steer and queue are real deliveries: the watcher must retire and the row
    must record HOW the continuation reached the conversation."""
    _install_dispatch(
        monkeypatch,
        ScheduledTurnDispatch(disposition, task_id=task_id, queue_id=queue_id),
    )
    client = _FakeClient()
    removed, notifications = [], []
    monkeypatch.setattr(_loop, "_timer_client", lambda **kw: client)
    monkeypatch.setattr(_loop, "remove_live_watcher", removed.append)
    from lib.scheduler.timer import _notify
    monkeypatch.setattr(_notify, "notify_timer_changed",
                        lambda change, **kw: notifications.append(change))

    outcome = _loop._execute_continuation(_timer())
    assert outcome.kind == disposition
    assert outcome.accepted
    assert outcome.task_id == task_id
    assert outcome.queue_id == queue_id
    assert client.payload["status"] == "triggered"
    assert client.payload["last_poll_decision"] == disposition
    if task_id:
        assert client.payload["execution_task_id"] == task_id
    assert removed == ["tmr_dispatch"]
    assert notifications == ["triggered"]


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

    assert _loop._execute_continuation(_timer()).kind == "retry"
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
            row.update(status=payload['status'],
                       execution_task_id=payload['execution_task_id'])
            return {'changed': True}

    client = Client()
    monkeypatch.setattr(_loop, '_timer_client', lambda **kw: client)
    monkeypatch.setattr(dispatch_module, 'dispatch_scheduled_turn', dispatch)

    assert _loop._execute_continuation(row).kind == 'retry'
    assert row['status'] == 'active'
    assert removed == [] and notifications == []
    outcome = _loop._execute_continuation(row)
    assert outcome.kind == 'started' and outcome.task_id == 'task-once'
    assert commands == ['timer:tmr_dispatch', 'timer:tmr_dispatch']
    assert row['status'] == 'triggered'
    assert removed == ['tmr_dispatch'] and notifications == ['triggered']


def test_lost_active_status_never_announces_trigger_success(monkeypatch):
    from lib.scheduler.timer import _notify

    _install_dispatch(monkeypatch, ScheduledTurnDispatch('queued', queue_id='q-1'))
    notifications = []

    class Client:
        def command(self, *args):
            return {'changed': False}

    monkeypatch.setattr(_loop, '_timer_client', lambda **kw: Client())
    monkeypatch.setattr(_notify, 'notify_timer_changed',
                        lambda change, **kw: notifications.append(change))
    monkeypatch.setattr(_loop, 'remove_live_watcher', lambda *a: None)
    assert _loop._execute_continuation(_timer()).kind == 'closed'
    assert notifications == []
