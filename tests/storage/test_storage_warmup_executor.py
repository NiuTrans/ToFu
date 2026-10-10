"""Warm-up executor reuse: one demand owner, no cross-pass duplication.

The old per-pass ThreadPoolExecutor (shutdown(wait=False)) abandoned
stragglers whose per-op deadline can reach 180 s; across 300 s keep-warm
passes those stacked as extra threads and occupied storage.v2 long-call
lanes until user-facing RPCs shed. One long-lived demand executor now owns
all passes, and an operation still in flight from a previous pass is
deferred (merged), never run concurrently with itself.
"""

from __future__ import annotations

import threading
import time

import pytest

from lib.storage import warmup


pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _drain_warmup_state():
    yield
    # No pass outlives a test: wait out stragglers so the shared pool and
    # the _inflight registry never leak into the next test.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with warmup._pool_lock:
            pending = [f for f in warmup._inflight.values() if not f.done()]
        if not pending:
            break
        for future in pending:
            try:
                future.result(timeout=1)
            except Exception:
                pass


class _SlowClient:
    """Every read takes ``delay`` seconds; counts calls per operation."""

    def __init__(self, delay: float):
        self._delay = delay
        self._lock = threading.Lock()
        self.calls: dict[str, int] = {}
        self.concurrent: dict[str, int] = {}
        self.max_concurrent: dict[str, int] = {}

    def query(self, operation, payload, deadline=None):
        with self._lock:
            self.calls[operation] = self.calls.get(operation, 0) + 1
            self.concurrent[operation] = self.concurrent.get(operation, 0) + 1
            self.max_concurrent[operation] = max(
                self.max_concurrent.get(operation, 0),
                self.concurrent[operation])
        try:
            time.sleep(self._delay)
            return {}
        finally:
            with self._lock:
                self.concurrent[operation] -= 1


def test_overlapping_passes_never_duplicate_an_operation():
    client = _SlowClient(delay=0.4)
    first = warmup.run_warmup_pass(
        client, deadline_seconds=30.0, budget_seconds=0.05)
    # The whole pass was shed by the budget but the ops are still running.
    assert first['skipped'] or first['warmed']
    second = warmup.run_warmup_pass(
        client, deadline_seconds=30.0, budget_seconds=0.05)
    # Every operation still in flight is merged, not duplicated.
    assert second['deferred'], second
    for operation, count in client.calls.items():
        assert count == 1, (operation, count)
    # Let the stragglers finish; a third pass runs fresh.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with client._lock:
            if (len(client.concurrent) == 6
                    and all(count == 0
                            for count in client.concurrent.values())):
                break
        time.sleep(0.05)
    third = warmup.run_warmup_pass(
        client, deadline_seconds=30.0, budget_seconds=30.0)
    assert third['deferred'] == []
    assert not third['skipped']
    for operation, peak in client.max_concurrent.items():
        assert peak == 1, (operation, peak)


def test_one_long_lived_executor_serves_all_passes():
    client = _SlowClient(delay=0.0)
    warmup.run_warmup_pass(client, deadline_seconds=5.0, budget_seconds=5.0)
    pool = warmup._warmup_pool()
    warmup.run_warmup_pass(client, deadline_seconds=5.0, budget_seconds=5.0)
    assert warmup._warmup_pool() is pool
    assert pool.metric_pool == 'storage-warmup'


def test_stats_shape_keeps_existing_keys():
    client = _SlowClient(delay=0.0)
    stats = warmup.run_warmup_pass(
        client, deadline_seconds=5.0, budget_seconds=5.0)
    for key in ('warmed', 'failed', 'skipped', 'elapsed_seconds'):
        assert key in stats
    assert stats['deferred'] == []


def test_all_independent_probes_start_with_idle_hint_one(monkeypatch):
    from lib.observability import InstrumentedThreadPoolExecutor

    operations = [(f'probe.{i}', {}) for i in range(32)]
    barrier = threading.Barrier(len(operations), timeout=10)
    pool = InstrumentedThreadPoolExecutor(max_workers=1, metric_pool='storage-warmup',
                                         thread_name_prefix='storage-warmup-proof')
    monkeypatch.setattr(warmup, '_operations', lambda owner: operations)
    monkeypatch.setattr(warmup, '_warmup_pool', lambda: pool)

    class Client:
        def query(self, operation, payload, deadline=None):
            barrier.wait()

    started = time.monotonic()
    try:
        stats = warmup.run_warmup_pass(Client(), budget_seconds=15)
        assert stats['warmed'] == [op for op, _ in operations]
        assert stats['failed'] == stats['skipped'] == stats['deferred'] == []
    finally:
        pool.shutdown(wait=True)
    assert not warmup._inflight
    assert not any(t.name.startswith('storage-warmup-proof') for t in threading.enumerate())
    print(f'32 demand probes: {(time.monotonic() - started) * 1000:.2f} ms; 0 retained workers')


def test_cross_pass_merge_preserves_client_and_payload_identity():
    from lib.observability import InstrumentedThreadPoolExecutor

    entered = threading.Barrier(4, timeout=5)
    release = threading.Event()
    pool = InstrumentedThreadPoolExecutor(max_workers=1, metric_pool='storage-warmup')

    def read():
        entered.wait()
        release.wait(5)
        return 'warmed'

    keys = [(1, 'read', 'a'), (2, 'read', 'a'), (1, 'read', 'b')]
    try:
        futures = [warmup._submit_tracked(pool, read, key) for key in keys]
        entered.wait()
        assert warmup._submit_tracked(pool, read, keys[0]) is None
        assert all(future is not None for future in futures)
    finally:
        release.set()
        pool.shutdown(wait=True)
    assert not warmup._inflight


def test_unstarted_probe_failure_does_not_suppress_other_reads(monkeypatch):
    from lib.observability import InstrumentedThreadPoolExecutor

    pool = InstrumentedThreadPoolExecutor(max_workers=1, metric_pool='storage-warmup',
                                         thread_name_prefix='warmup-start-proof')
    original = threading.Thread.start
    count = 0
    rendezvous = threading.Barrier(5, timeout=10)
    called = []
    lock = threading.Lock()

    def start(thread):
        nonlocal count
        if thread.name.startswith('warmup-start-proof'):
            count += 1
            if count == 2:
                raise RuntimeError('unstarted probe creation failed')
        return original(thread)

    class Client:
        def query(self, operation, payload, deadline=None):
            with lock:
                called.append(operation)
            rendezvous.wait()

    monkeypatch.setattr(threading.Thread, 'start', start)
    monkeypatch.setattr(warmup, '_warmup_pool', lambda: pool)
    try:
        stats = warmup.run_warmup_pass(Client(), budget_seconds=15)
        assert stats['failed'] == ['conversation.list']
        assert len(stats['warmed']) == 5
        assert 'conversation.list' not in called
        assert len(called) == 5
    finally:
        pool.shutdown(wait=True)
    assert not warmup._inflight


def test_cancelled_and_broken_workers_keep_per_probe_outcomes(monkeypatch):
    from concurrent.futures import Future

    class Pool:
        def __init__(self):
            self.count = 0

        def submit(self, fn):
            future = Future()
            self.count += 1
            if self.count == 1:
                future.cancel()
                future.set_running_or_notify_cancel()
            elif self.count == 2:
                future.set_exception(RuntimeError('worker initializer failed'))
            else:
                future.set_result(fn())
            return future

    monkeypatch.setattr(warmup, '_warmup_pool', Pool)
    stats = warmup.run_warmup_pass(_SlowClient(0), budget_seconds=5)
    assert stats['skipped'] == ['conversation.count']
    assert stats['failed'] == ['conversation.list']
    assert len(stats['warmed']) == 4
    assert not warmup._inflight


def test_stopped_keeper_closes_pool_without_abandoning_live_read(monkeypatch):
    from lib.observability import InstrumentedThreadPoolExecutor

    entered, release = threading.Event(), threading.Event()
    pool = InstrumentedThreadPoolExecutor(max_workers=1, metric_pool='storage-warmup',
                                         thread_name_prefix='warmup-shutdown-proof')
    monkeypatch.setattr(warmup, '_pool', pool)

    def read():
        entered.set()
        release.wait(5)
        return 'warmed'

    future = warmup._submit_tracked(pool, read, (100, 'shutdown-proof', '{}'))
    try:
        assert entered.wait(2)
        warmup.stop_keep_warm()
        assert warmup._pool is None
        assert not future.done()
        assert future in warmup._inflight.values()
        with pytest.raises(RuntimeError, match='shutdown'):
            pool.submit(read)
    finally:
        release.set()
        pool.shutdown(wait=True)
    assert future.result() == 'warmed'
    assert not warmup._inflight
    assert not any(t.name.startswith('warmup-shutdown-proof') for t in threading.enumerate())
