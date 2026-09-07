"""Behavioral contracts for bounded reconstructible background work."""

from __future__ import annotations

import threading
import time

import pytest

from lib.conversations._bounded_lane import BoundedCoalescingLane


pytestmark = pytest.mark.unit


def test_lane_bounds_unique_scopes_and_coalesces_active_scope():
    entered = threading.Event()
    release = threading.Event()
    calls: list[tuple[str, tuple[str, bool]]] = []

    def consume(key, payload):
        calls.append((key, payload))
        if key == 'active' and len(calls) == 1:
            entered.set()
            release.wait(2)

    lane = BoundedCoalescingLane[str, tuple[str, bool]](
        name='bounded-lane-test',
        workers=1,
        capacity=1,
        merge=lambda current, newest: (
            newest[0], current[1] or newest[1]),
        consume=consume,
        idle_seconds=0.05,
    )

    assert lane.submit('active', ('first', False)) is True
    assert entered.wait(1)
    assert lane.submit('queued', ('queued', False)) is True
    assert lane.submit('overflow', ('overflow', False)) is False
    assert lane.submit('active', ('latest', True)) is True

    saturated = lane.snapshot()
    assert saturated['capacity'] == 1
    assert saturated['trackedScopes'] <= 2
    assert saturated['rejected'] == 1
    assert saturated['coalesced'] == 1

    release.set()
    assert lane.wait_idle(2)
    assert calls == [
        ('active', ('first', False)),
        ('active', ('latest', True)),
        ('queued', ('queued', False)),
    ]
    assert lane.snapshot()['trackedScopes'] == 0


def test_lane_survives_consumer_failure_and_drains_following_scope():
    calls = []
    errors = []

    def consume(key, _payload):
        calls.append(key)
        if key == 'broken':
            raise RuntimeError('boom')

    lane = BoundedCoalescingLane[str, bool](
        name='failure-lane-test',
        workers=1,
        capacity=2,
        merge=lambda current, newest: current or newest,
        consume=consume,
        on_error=lambda key, error: errors.append((key, str(error))),
        idle_seconds=0.05,
    )

    assert lane.submit('broken', False)
    assert lane.submit('healthy', False)
    assert lane.wait_idle(2)
    assert calls == ['broken', 'healthy']
    assert errors == [('broken', 'boom')]


def test_lane_retires_workers_and_rebuilds_full_capacity_without_loss():
    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    calls: list[str] = []
    calls_lock = threading.Lock()
    batch_calls = [0, 0]

    def consume(key, _payload):
        batch = int(key.split('-', 1)[0])
        with calls_lock:
            calls.append(key)
            batch_calls[batch] += 1
            if batch_calls[batch] == 2:
                entered[batch].set()
        release[batch].wait(1)

    lane = BoundedCoalescingLane[str, bool](
        name='retiring-lane-test',
        workers=2,
        capacity=2,
        merge=lambda current, newest: current or newest,
        consume=consume,
        idle_seconds=0.03,
    )

    try:
        for batch in range(2):
            assert lane.submit(f'{batch}-first', False)
            assert lane.submit(f'{batch}-second', False)
            assert entered[batch].wait(1)
            active = lane.snapshot()
            assert active['liveWorkers'] == 2
            assert active['trackedScopes'] == 2

            release[batch].set()
            assert lane.wait_idle(1)
            deadline = time.monotonic() + 1
            while lane.snapshot()['liveWorkers']:
                assert time.monotonic() < deadline
                time.sleep(0.01)
            retired = lane.snapshot()
            assert retired['workerStarts'] == 2 * (batch + 1)
            assert retired['retiredWorkers'] == 2 * (batch + 1)
    finally:
        for event in release:
            event.set()

    assert sorted(calls) == [
        '0-first', '0-second', '1-first', '1-second']
