"""Freshness and resource contracts for queued read coalescing."""

from __future__ import annotations

import threading
import time

import pytest

from lib.memory.queued_read_coalescer import QueuedReadCoalescer


pytestmark = pytest.mark.unit

_WAIT_SECONDS = 10.0


def _wait_until(predicate) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('condition not reached')
        time.sleep(0.001)


class _BlockingLoads:
    """Count loads; the first load blocks until released."""

    def __init__(self) -> None:
        self.calls: list[object] = []
        self.first_entered = threading.Event()
        self.release_first = threading.Event()
        self._lock = threading.Lock()

    def __call__(self, key):
        with self._lock:
            self.calls.append(key)
            number = len(self.calls)
        if number == 1:
            self.first_entered.set()
            assert self.release_first.wait(_WAIT_SECONDS)
        return {'key': key, 'load': number, 'rows': [number]}


def _start(target, *args) -> tuple[threading.Thread, dict]:
    outcome: dict = {}

    def run():
        try:
            outcome['value'] = target(*args)
        except BaseException as error:  # noqa: BLE001 - asserted by tests
            outcome['error'] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def test_uncontended_caller_owns_its_read_without_copy() -> None:
    coalescer = QueuedReadCoalescer()
    copies = []

    value = coalescer.run('k', lambda key: [key], copy=lambda v: copies.append(v) or list(v))

    assert value == ['k']
    assert copies == []
    assert coalescer.snapshot()['activeKeys'] == 0


def test_burst_costs_at_most_two_reads_and_never_shares_a_started_read() -> None:
    coalescer = QueuedReadCoalescer()
    loads = _BlockingLoads()
    first_thread, first = _start(coalescer.run, 'k', loads)
    assert loads.first_entered.wait(_WAIT_SECONDS)

    # Every later caller arrived after read #1 started: none may share it.
    followers = [_start(coalescer.run, 'k', loads) for _ in range(7)]
    _wait_until(lambda: coalescer.snapshot()['queuedCallers'] == 7)
    assert len(loads.calls) == 1
    loads.release_first.set()

    first_thread.join(_WAIT_SECONDS)
    for thread, _outcome in followers:
        thread.join(_WAIT_SECONDS)
    assert first['value']['load'] == 1
    assert [outcome['value']['load'] for _thread, outcome in followers] == [2] * 7
    assert len(loads.calls) == 2
    snapshot = coalescer.snapshot()
    assert snapshot['activeKeys'] == 0
    assert snapshot['reads'] == 2
    assert snapshot['joined'] == 6


def test_queued_read_starts_only_after_the_running_read_settles() -> None:
    coalescer = QueuedReadCoalescer()
    loads = _BlockingLoads()
    first_thread, _first = _start(coalescer.run, 'k', loads)
    assert loads.first_entered.wait(_WAIT_SECONDS)
    second_thread, second = _start(coalescer.run, 'k', loads)
    _wait_until(lambda: coalescer.snapshot()['queuedCallers'] == 1)

    # Holding read #1 open keeps read #2 from starting (one running per key).
    time.sleep(0.05)
    assert len(loads.calls) == 1
    loads.release_first.set()
    first_thread.join(_WAIT_SECONDS)
    second_thread.join(_WAIT_SECONDS)
    assert second['value']['load'] == 2


def test_sharers_receive_independent_copies() -> None:
    coalescer = QueuedReadCoalescer()
    loads = _BlockingLoads()
    first_thread, _first = _start(coalescer.run, 'k', loads)
    assert loads.first_entered.wait(_WAIT_SECONDS)

    def deep_copy(value):
        return {**value, 'rows': list(value['rows'])}

    sharers = [
        _start(lambda: coalescer.run('k', loads, copy=deep_copy))
        for _ in range(3)
    ]
    _wait_until(lambda: coalescer.snapshot()['queuedCallers'] == 3)
    loads.release_first.set()
    first_thread.join(_WAIT_SECONDS)
    for thread, _outcome in sharers:
        thread.join(_WAIT_SECONDS)

    values = [outcome['value'] for _thread, outcome in sharers]
    assert all(value['load'] == 2 for value in values)
    values[0]['rows'].append('mutated')
    values[1]['key'] = 'mutated'
    assert values[2] == {'key': 'k', 'load': 2, 'rows': [2]}
    assert len({id(value) for value in values}) == 3
    assert len({id(value['rows']) for value in values}) == 3


def test_distinct_keys_never_wait_for_each_other() -> None:
    coalescer = QueuedReadCoalescer()
    barrier = threading.Barrier(4)

    def load(key):
        barrier.wait(_WAIT_SECONDS)  # Every key's read must be in flight.
        return key

    threads = [_start(coalescer.run, f'k{i}', load) for i in range(4)]
    for thread, _outcome in threads:
        thread.join(_WAIT_SECONDS)
    assert sorted(outcome['value'] for _thread, outcome in threads) == [
        'k0', 'k1', 'k2', 'k3']


def test_failed_read_reaches_its_sharers_and_does_not_wedge_the_key() -> None:
    coalescer = QueuedReadCoalescer()
    loads = _BlockingLoads()
    first_thread, _first = _start(coalescer.run, 'k', loads)
    assert loads.first_entered.wait(_WAIT_SECONDS)
    failure = RuntimeError('scan failed')

    def failing_load(_key):
        raise failure

    queued = [_start(coalescer.run, 'k', failing_load) for _ in range(3)]
    _wait_until(lambda: coalescer.snapshot()['queuedCallers'] == 3)
    loads.release_first.set()
    first_thread.join(_WAIT_SECONDS)
    for thread, _outcome in queued:
        thread.join(_WAIT_SECONDS)

    assert [outcome['error'] for _thread, outcome in queued] == [failure] * 3
    assert coalescer.snapshot()['activeKeys'] == 0
    assert coalescer.run('k', lambda key: 'recovered') == 'recovered'
