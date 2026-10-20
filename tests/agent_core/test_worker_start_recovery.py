"""Recover physical startup only while one owned Future is still provisional."""
import threading
from concurrent.futures.thread import BrokenThreadPool

import pytest

from tofu_workers import DemandExecutor

pytestmark = pytest.mark.unit


def pool_for(kind, prefix):
    if kind == 'kernel':
        return DemandExecutor(max_workers=1, queue_capacity=0,
            max_abandoned_workers=1, thread_name_prefix=prefix)
    if kind == 'standalone':
        from tofu_workers import DemandThreadPoolExecutor
        return DemandThreadPoolExecutor(max_workers=1, thread_name_prefix=prefix)
    from lib.observability import InstrumentedThreadPoolExecutor
    return InstrumentedThreadPoolExecutor(max_workers=1,
        thread_name_prefix=prefix, metric_pool='startup-recovery-test')


@pytest.mark.parametrize('kind', ['kernel', 'standalone', 'application'])
@pytest.mark.parametrize('boundary', ['constructor', 'start'])
def test_unpublished_job_recovers_a_transient_startup_fault(kind, boundary, monkeypatch):
    prefix = 'startup-recover-once'
    pool = pool_for(kind, prefix)
    original_thread, original_start = threading.Thread, threading.Thread.start
    attempts, calls = [], []

    def start(thread):
        if thread.name.startswith(prefix):
            attempts.append(thread.name)
            if len(attempts) == 1:
                raise OSError('transient physical startup refusal')
        return original_start(thread)

    def construct(*args, **kwargs):
        if str(kwargs.get('name', '')).startswith(prefix):
            attempts.append(kwargs['name'])
            if len(attempts) == 1:
                raise OSError('transient physical reservation refusal')
        return original_thread(*args, **kwargs)

    if boundary == 'start':
        monkeypatch.setattr(threading.Thread, 'start', start)
    else:
        monkeypatch.setattr(threading, 'Thread', construct)
    try:
        def execute(identity):
            calls.append(identity)
            return 'completed:' + identity
        assert pool.submit(execute, 'owned-once').result(2) == 'completed:owned-once'
        assert calls == ['owned-once'] and len(attempts) == 2
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    threads = pool.worker_threads_snapshot() if kind != 'application' else pool._threads
    assert not threads


@pytest.mark.parametrize('kind', ['kernel', 'standalone', 'application'])
def test_persistent_startup_fault_is_bounded_and_retains_both_causes(kind, monkeypatch):
    pool = pool_for(kind, 'startup-persistent')
    original = threading.Thread.start
    failures, calls = [], []
    def start(thread):
        if thread.name.startswith('startup-persistent'):
            error = OSError(f'physical refusal {len(failures) + 1}')
            failures.append(error)
            raise error
        return original(thread)
    monkeypatch.setattr(threading.Thread, 'start', start)
    try:
        with pytest.raises(OSError, match='physical refusal 2') as raised:
            pool.submit(lambda: calls.append('unexpected')).result(2)
        assert len(failures) == 2 and raised.value is failures[1]
        assert raised.value.__cause__ is failures[0]
        assert not calls
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize('withdrawal', ['cancel', 'shutdown'])
def test_withdrawal_during_recovery_cannot_resurrect_work(withdrawal, monkeypatch):
    pool = pool_for('kernel', 'startup-withdraw')
    original = threading.Thread.start
    entered, release = threading.Event(), threading.Event()
    attempts, futures, errors, calls = [], [], [], []
    def start(thread):
        if thread.name.startswith('startup-withdraw'):
            attempts.append(thread.name)
            if len(attempts) == 1:
                raise OSError('first reservation failed')
            entered.set()
            assert release.wait(3), 'fixture did not release its recovery start'
        return original(thread)
    monkeypatch.setattr(threading.Thread, 'start', start)
    def submit():
        try:
            futures.append(pool.submit_task('owned', lambda: calls.append('unexpected')))
        except BaseException as error:
            errors.append(error)
    creator = threading.Thread(target=submit)
    creator.start()
    try:
        assert entered.wait(1), 'recovery never acquired its second startup reservation'
        if withdrawal == 'cancel':
            assert pool.cancel_task('owned')
        else:
            pool.shutdown(wait=False, cancel_futures=True)
    finally:
        release.set()
        creator.join(3)
        pool.shutdown(wait=True, cancel_futures=True)
    assert not creator.is_alive() and not errors and len(futures) == 1
    assert futures[0].cancelled() and not calls
    assert pool.scheduling_snapshot()['startingWorkers'] == 0


@pytest.mark.parametrize('failure_boundary', ['initializer', 'handler', 'control'])
def test_recovery_never_replays_entered_work_or_control_exceptions(failure_boundary, monkeypatch):
    class ControlStop(BaseException):
        pass
    entries, starts = [], []
    def fail():
        entries.append(failure_boundary)
        raise RuntimeError('execution failure must not be retried')
    pool = DemandExecutor(max_workers=1, queue_capacity=0, max_abandoned_workers=1,
        thread_name_prefix='startup-no-replay', initializer=fail if failure_boundary == 'initializer' else None)
    original = threading.Thread.start
    def start(thread):
        if thread.name.startswith('startup-no-replay'):
            starts.append(thread.name)
            if failure_boundary == 'control':
                raise ControlStop('owner interrupted startup')
        return original(thread)
    monkeypatch.setattr(threading.Thread, 'start', start)
    try:
        expected = {'initializer': BrokenThreadPool, 'handler': RuntimeError, 'control': ControlStop}[failure_boundary]
        with pytest.raises(expected):
            pool.submit(fail).result(2)
        assert entries == ([] if failure_boundary == 'control' else [failure_boundary])
        assert len(starts) == 1
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def test_recovery_start_does_not_block_completed_sibling_bookkeeping(monkeypatch):
    pool = pool_for('kernel', 'startup-sibling')
    first_entered, first_release = threading.Event(), threading.Event()
    recovery_entered, recovery_release = threading.Event(), threading.Event()
    first = pool.submit(lambda: (first_entered.set(), first_release.wait(3)))
    assert first_entered.wait(1)
    original = threading.Thread.start
    futures, failures = [], []
    def start(thread):
        if thread.name == 'startup-sibling_1':
            raise OSError('temporary startup refusal')
        if thread.name == 'startup-sibling_2':
            recovery_entered.set()
            assert recovery_release.wait(3)
        return original(thread)
    monkeypatch.setattr(threading.Thread, 'start', start)
    def submit():
        try:
            futures.append(pool.submit(lambda: 'second'))
        except BaseException as error:
            failures.append(error)
    creator = threading.Thread(target=submit)
    creator.start()
    try:
        assert recovery_entered.wait(1)
        first_release.set()
        assert first.result(.5) == (None, True)
    finally:
        recovery_release.set()
        first_release.set()
        creator.join(3)
        pool.shutdown(wait=True, cancel_futures=False)
    assert not creator.is_alive() and not failures
    assert len(futures) == 1 and futures[0].result(1) == 'second'


@pytest.mark.parametrize('persistent', [False, True])
@pytest.mark.parametrize('observer_fails', [False, True])
def test_recovery_observation_cannot_replace_execution_outcome(persistent, observer_fails, monkeypatch):
    from lib import observability
    outcomes, attempts = [], []
    original = threading.Thread.start
    def observe(pool, *, outcome):
        outcomes.append((pool, outcome))
        if observer_fails:
            raise RuntimeError('optional metrics unavailable')
    monkeypatch.setattr(observability, 'record_executor_startup_recovery', observe)
    def start(thread):
        if thread.name.startswith('startup-observe'):
            attempts.append(thread.name)
            if persistent or len(attempts) == 1:
                raise OSError('actual startup failure')
        return original(thread)
    monkeypatch.setattr(threading.Thread, 'start', start)
    pool = pool_for('application', 'startup-observe')
    try:
        if persistent:
            with pytest.raises(OSError, match='actual startup failure'):
                pool.submit(lambda: 'result')
        else:
            assert pool.submit(lambda: 'result').result(2) == 'result'
        assert outcomes == [('startup-recovery-test', 'failed' if persistent else 'accepted')]
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
