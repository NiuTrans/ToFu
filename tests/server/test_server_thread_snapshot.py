"""Safe, bounded metadata capture while live thread/frame owners change."""

import linecache
import threading

import pytest

from lib.server.server_fault_watchdog import capture_thread_stacks

pytestmark = pytest.mark.unit


def test_snapshot_preserves_live_thread_stack_without_reading_sources(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def owned_worker():
        secret_local = 'locals-must-not-be-captured'
        entered.set()
        release.wait()
        return secret_local

    def source_access(*_args, **_kwargs):
        raise AssertionError('diagnostics must not fetch sources from FUSE')

    monkeypatch.setattr(linecache, 'getline', source_access)
    monkeypatch.setattr(linecache, 'getlines', source_access)
    worker = threading.Thread(target=owned_worker, name='snapshot-owned-worker')
    worker.start()
    try:
        assert entered.wait(2)
        text = capture_thread_stacks(max_bytes=128 * 1024,
                                     priority_thread_id=worker.ident)
        assert text.startswith(f'Thread {worker.ident:#x} (snapshot-owned-worker)')
        assert 'owned_worker' in text
        assert 'locals-must-not-be-captured' not in text
    finally:
        release.set()
        worker.join(2)
        assert not worker.is_alive()


@pytest.mark.parametrize('budget', [0, 1, 20, 100, 1000])
def test_snapshot_obeys_utf8_byte_budget(budget):
    text = capture_thread_stacks(max_bytes=budget)
    assert len(text.encode('utf-8')) <= budget


def test_snapshot_captures_more_than_native_faulthandler_thread_ceiling():
    release = threading.Event()
    workers = [threading.Thread(target=release.wait,
                                name=f'snapshot-owned-{index}')
               for index in range(105)]
    started = []
    try:
        for worker in workers:
            worker.start()
            started.append(worker)
        text = capture_thread_stacks(max_bytes=1024 * 1024)
        assert all(f'(snapshot-owned-{index})' in text for index in range(105))
    finally:
        release.set()
        for worker in started:
            worker.join(2)
            assert not worker.is_alive()


def test_retained_snapshots_survive_generator_and_thread_churn():
    release = threading.Event()
    def churn():
        while not release.is_set():
            generator = (value for value in range(20))
            for _ in generator:
                pass
    worker = threading.Thread(target=churn, name='snapshot-generator-churn')
    worker.start()
    try:
        for _ in range(30):
            text = capture_thread_stacks(max_bytes=32 * 1024)
            assert '(snapshot-generator-churn)' in text
    finally:
        release.set()
        worker.join(2)
        assert not worker.is_alive()
