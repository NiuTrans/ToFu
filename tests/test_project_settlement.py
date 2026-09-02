"""Post-settlement project hooks and bounded summary scheduling."""

from __future__ import annotations

import threading
import time

import pytest

pytestmark = pytest.mark.unit


def test_settlement_schedules_summary_and_idle_dispatch_with_owner(monkeypatch):
    import lib.conversations.project_dispatch as dispatch
    import lib.conversations.project_summary as summary
    from lib.conversations.project_settlement import on_project_task_settled

    calls = []
    monkeypatch.setattr(
        summary,
        'ensure_summary',
        lambda conv_id, **kwargs: calls.append(('summary', conv_id, kwargs)),
    )
    monkeypatch.setattr(
        dispatch,
        'on_conv_idle',
        lambda path, conv_id, **kwargs: calls.append(
            ('dispatch', path, conv_id, kwargs)),
    )

    on_project_task_settled(
        {'convId': 'conv-a'}, '/project/a', user_id=61)

    assert calls == [
        ('summary', 'conv-a', {'user_id': 61, 'blocking': False}),
        ('dispatch', '/project/a', 'conv-a', {'user_id': 61}),
    ]


def test_abort_refreshes_summary_but_never_starts_new_board_work(monkeypatch):
    import lib.conversations.project_dispatch as dispatch
    import lib.conversations.project_summary as summary
    from lib.conversations.project_settlement import on_project_task_settled

    summaries = []
    monkeypatch.setattr(
        summary,
        'ensure_summary',
        lambda conv_id, **kwargs: summaries.append((conv_id, kwargs)),
    )
    monkeypatch.setattr(
        dispatch,
        'on_conv_idle',
        lambda *_args, **_kwargs: pytest.fail(
            'an explicit Stop must not launch autonomous work'),
    )

    on_project_task_settled(
        {'convId': 'conv-a', 'aborted': True},
        '/project/a',
        user_id=61,
    )

    assert summaries == [
        ('conv-a', {'user_id': 61, 'blocking': False})
    ]


def test_summary_worker_lane_coalesces_per_owner_and_conversation(monkeypatch):
    import lib.conversations.project_summary as summary

    gate = threading.Event()
    two_active = threading.Event()
    lock = threading.Lock()
    calls = []
    active = 0
    peak = 0

    def blocking(conv_id, *, user_id, force):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            calls.append((user_id, conv_id, force))
            if active == summary._SUMMARY_WORKERS:
                two_active.set()
        gate.wait(5)
        with lock:
            active -= 1

    monkeypatch.setattr(summary, '_ensure_summary_blocking', blocking)
    suffix = str(time.time_ns())
    try:
        for index in range(5):
            summary._schedule_summary(
                f'conv-{suffix}-{index}', user_id=61, force=False)
        repeated = f'conv-{suffix}-repeated'
        summary._schedule_summary(repeated, user_id=61, force=False)
        summary._schedule_summary(repeated, user_id=61, force=True)
        summary._schedule_summary(repeated, user_id=62, force=False)

        assert two_active.wait(2)
        with lock:
            assert peak == summary._SUMMARY_WORKERS == 2
    finally:
        gate.set()
    assert summary._wait_for_background_summaries(5)
    assert [call for call in calls if call[:2] == (61, repeated)] == [
        (61, repeated, True)
    ]
    assert [call for call in calls if call[:2] == (62, repeated)] == [
        (62, repeated, False)
    ]


def test_idle_dispatch_yields_to_a_waiting_human_message(monkeypatch):
    import lib.conversations.project_dispatch as dispatch
    import lib.message_queue as message_queue

    monkeypatch.setattr(
        message_queue,
        'get_queue_depth',
        lambda conv_id, *, user_id: 1,
    )
    monkeypatch.setattr(
        dispatch,
        '_conv_has_live_task',
        lambda *_args, **_kwargs: pytest.fail(
            'queue priority should short-circuit before task probing'),
    )

    assert dispatch.on_conv_idle(
        '/project/a', 'conv-a', user_id=61) == 0
