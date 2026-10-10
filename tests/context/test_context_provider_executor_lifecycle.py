"""Demand context reads preserve physical startup errors and joined ownership."""
import threading

import pytest

from lib.tasks_pkg.context_composer._provider_executor import DemandContextProviderExecutor

pytestmark = pytest.mark.unit


def test_submission_after_started_executor_shutdown_fails_immediately():
    executor = DemandContextProviderExecutor(max_workers=1, queue_capacity=1)
    assert executor.submit(lambda: 'first').result(timeout=2) == 'first'
    executor.shutdown()
    future = executor.submit(lambda: 'must not run')
    assert future.done(), 'closed executor accepted an orphaned future'
    with pytest.raises(RuntimeError, match='closed|shutdown'):
        future.result()
    assert executor.snapshot()['queued'] == 0


def test_nonwaiting_shutdown_retains_accepted_work_until_joined_close():
    executor = DemandContextProviderExecutor(max_workers=1, queue_capacity=1)
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    def blocking():
        entered.set()
        assert release.wait(5)
    active = executor.submit(blocking)
    assert entered.wait(1)
    pending = executor.submit(lambda: 'accepted before close')
    def close():
        executor.shutdown(wait_for_threads=False)
        closed.set()
    closer = threading.Thread(target=close)
    closer.start()
    try:
        assert closed.wait(1), 'shutdown waited for capacity despite wait_for_threads=False'
    finally:
        release.set()
        active.result(timeout=2)
        assert pending.result(timeout=2) == 'accepted before close'
        closer.join(timeout=2)
        executor.shutdown()
    assert not closer.is_alive()
    assert executor.snapshot()['residentThreads'] == 0


@pytest.mark.parametrize('failed_start', [1, 2])
@pytest.mark.parametrize('error_type', [RuntimeError, OSError])
def test_worker_start_failure_withdraws_only_unstarted_job(monkeypatch, failed_start, error_type):
    executor = DemandContextProviderExecutor(max_workers=2, queue_capacity=2)
    original = threading.Thread.start
    calls = 0
    entered = threading.Event()
    release = threading.Event()
    effects = []
    def blocking():
        entered.set()
        assert release.wait(5)
        return 'accepted'
    def start(thread):
        nonlocal calls
        if thread.name.startswith('context-provider'):
            calls += 1
            if calls == failed_start:
                raise error_type('injected physical start failure')
        return original(thread)
    active = None
    try:
        with monkeypatch.context() as patch:
            patch.setattr(threading.Thread, 'start', start)
            if failed_start == 2:
                active = executor.submit(blocking)
                assert entered.wait(1)
            failed = executor.submit(lambda: effects.append('unstarted'))
            assert failed.done()
            with pytest.raises(error_type, match='injected physical start failure'):
                failed.result()
            assert effects == []
            assert executor.snapshot()['queued'] == 0
        # Accepted predecessor can remain blocked while a fresh read succeeds.
        assert executor.submit(lambda: 'recovered').result(timeout=1) == 'recovered'
    finally:
        release.set()
        executor.shutdown()
    if active is not None:
        assert active.result(timeout=0) == 'accepted'
    assert executor.snapshot()['residentThreads'] == 0


def test_worker_can_close_its_executor_without_joining_itself():
    executor = DemandContextProviderExecutor(max_workers=1, queue_capacity=1)
    try:
        assert executor.submit(lambda: (executor.shutdown(), 'closed')[1]).result(timeout=2) == 'closed'
    finally:
        executor.shutdown()
    assert executor.snapshot()['residentThreads'] == 0


def test_submission_racing_shutdown_is_rejected_or_drained():
    for _ in range(20):
        executor = DemandContextProviderExecutor(max_workers=2, queue_capacity=2)
        start = threading.Barrier(3)
        submitted = []
        def submit():
            start.wait(timeout=2)
            submitted.append(executor.submit(lambda: 'completed'))
        def close():
            start.wait(timeout=2)
            executor.shutdown(wait_for_threads=False)
        submitter = threading.Thread(target=submit)
        closer = threading.Thread(target=close)
        submitter.start()
        closer.start()
        start.wait(timeout=2)
        submitter.join(timeout=2)
        closer.join(timeout=2)
        assert not submitter.is_alive() and not closer.is_alive()
        executor.shutdown()
        assert len(submitted) == 1
        # The executor has fully shut down (wait=True above), so the admitted
        # submission is in a terminal state: drained to completion, or rejected
        # at the closed boundary. Dispatch on the outcome instead of a
        # try/except that could launder an unexpected result.
        future = submitted[0]
        rejection = future.exception()
        if rejection is None:
            assert future.result(timeout=0) == 'completed'
        else:
            assert isinstance(rejection, RuntimeError)
            assert 'closed' in str(rejection) or 'shutdown' in str(rejection)
        assert executor.snapshot()['queued'] == 0
        assert executor.snapshot()['residentThreads'] == 0


def test_waiting_close_does_not_return_after_old_one_second_join_bound():
    executor = DemandContextProviderExecutor(max_workers=1, queue_capacity=1)
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    def work():
        entered.set()
        assert release.wait(5)
        return 'joined'
    future = executor.submit(work)
    assert entered.wait(1)
    def close():
        executor.shutdown()
        closed.set()
    closer = threading.Thread(target=close)
    closer.start()
    try:
        assert not closed.wait(1.1), 'close claimed completion while the read still ran'
        assert executor.snapshot()['residentThreads'] == 1
        rejected = executor.submit(lambda: 'unstarted')
        with pytest.raises(RuntimeError, match='shutdown'):
            rejected.result(timeout=0)
    finally:
        release.set()
        closer.join(2)
        executor.shutdown()
    assert closed.is_set() and not closer.is_alive()
    assert future.result(timeout=0) == 'joined'
    assert executor.snapshot()['residentThreads'] == 0
