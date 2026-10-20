"""Startup transactions release scheduler locks without accepting rejected work."""
import threading
import time
from concurrent.futures import TimeoutError
import pytest
from tofu_workers import DemandExecutor

pytestmark = pytest.mark.unit


def kernel(prefix='startup-causal'):
    return DemandExecutor(max_workers=1, queue_capacity=0,
                          max_abandoned_workers=32, thread_name_prefix=prefix)


def test_stalled_start_does_not_block_previous_job_bookkeeping(monkeypatch):
    pool=kernel(); started=threading.Event(); first_release=threading.Event()
    start_entered=threading.Event(); start_release=threading.Event(); submitted=[]; failures=[]
    first=pool.submit(lambda:(started.set(),first_release.wait(2)))
    assert started.wait(1)
    real_start=threading.Thread.start
    def controlled_start(thread):
        if thread.name=='startup-causal_1':
            start_entered.set();assert start_release.wait(2)
        return real_start(thread)
    monkeypatch.setattr(threading.Thread,'start',controlled_start)
    def submit():
        try:submitted.append(pool.submit(lambda:'second'))
        except BaseException as e:failures.append(e)
    submitter=threading.Thread(target=submit);submitter.start()
    try:
        assert start_entered.wait(1);begin=time.monotonic();first_release.set()
        # Completion must be independent of the still-blocked physical start.
        completed=threading.Event()
        def observe():
            try:first.result(.15);completed.set()
            except TimeoutError:pass
        observer=threading.Thread(target=observe);observer.start()
        start_release.wait(.2)  # controlled 200ms exposure, no runtime retry/budget
        before_release=completed.is_set();blocked_ms=(time.monotonic()-begin)*1000
        print({'controlled_start_block_ms':round(blocked_ms,3),
               'prior_job_completed_before_start_release':before_release})
        assert before_release
    finally:
        start_release.set();first_release.set();submitter.join(2)
        if 'observer' in locals():observer.join(2)
        for future in submitted:future.result(1)
        pool.shutdown(wait=True,cancel_futures=True)
    assert not failures
    assert submitted[0].result(1)=='second'


def test_shutdown_does_not_wait_start_and_final_join_owns_reservation(monkeypatch):
    pool=kernel('startup-shutdown');entered=threading.Event();release=threading.Event();ran=threading.Event();submitted=[]
    real=threading.Thread.start
    def start(thread):
        if thread.name=='startup-shutdown_0':entered.set();assert release.wait(2)
        return real(thread)
    monkeypatch.setattr(threading.Thread,'start',start)
    creator=threading.Thread(target=lambda:submitted.append(pool.submit(ran.set)));creator.start()
    waiter_done=threading.Event();waiter=None
    try:
        assert entered.wait(1)
        snap=pool.scheduling_snapshot();assert snap['startingWorkers']==1;assert snap['residentThreads']==0
        pool.shutdown(wait=False,cancel_futures=True)
        waiter=threading.Thread(target=lambda:(pool.shutdown(wait=True),waiter_done.set()));waiter.start()
        assert not waiter_done.wait(.05)
        assert not ran.is_set()
    finally:
        release.set();creator.join(2)
        if waiter:waiter.join(2)
        pool.shutdown(wait=True,cancel_futures=True)
    assert waiter_done.is_set();assert submitted[0].cancelled();assert not ran.is_set()
    assert pool.scheduling_snapshot()['startingWorkers']==0
    assert pool.scheduling_snapshot()['residentThreads']==0


@pytest.mark.parametrize('physically_started',[False,True])
def test_late_start_failure_never_executes_rejected_item(monkeypatch,physically_started):
    class StartupInterrupted(BaseException):
        pass
    pool=kernel('startup-late-fail');ran=threading.Event();real=threading.Thread.start
    def start(thread):
        if thread.name.startswith('startup-late-fail_'):
            if physically_started:real(thread)
            raise StartupInterrupted('controlled late startup rejection')
        return real(thread)
    monkeypatch.setattr(threading.Thread,'start',start)
    try:
        with pytest.raises(StartupInterrupted,match='controlled late'):
            pool.submit_task('rejected',ran.set)
        assert not ran.is_set()
        assert pool.scheduling_snapshot('rejected')['taskState']=='unknown'
    finally:pool.shutdown(wait=True,cancel_futures=True)
    assert not ran.is_set();assert pool.scheduling_snapshot()['startingWorkers']==0


def test_late_recoverable_start_failure_uses_its_existing_physical_carrier(monkeypatch):
    pool = kernel('startup-late-recover')
    calls, starts = [], []
    real = threading.Thread.start
    def start(thread):
        result = real(thread)
        if thread.name.startswith('startup-late-recover'):
            starts.append(thread.name)
            raise RuntimeError('startup response lost after physical start')
        return result
    monkeypatch.setattr(threading.Thread, 'start', start)
    try:
        future = pool.submit_task('owned', lambda: calls.append('effect'))
        future.result(2)
        assert calls == ['effect'] and len(starts) == 1
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    assert not pool.worker_threads_snapshot()


def test_constructor_failure_rolls_back_provisional_job(monkeypatch):
    pool=kernel('startup-constructor');ran=threading.Event();real=threading.Thread
    def thread(*a,**kw):
        if str(kw.get('name','')).startswith('startup-constructor_'):raise RuntimeError('constructor rejected')
        return real(*a,**kw)
    monkeypatch.setattr(threading,'Thread',thread)
    with pytest.raises(RuntimeError,match='constructor rejected'):pool.submit_task('rejected',ran.set)
    assert pool.scheduling_snapshot()['queued']==0
    assert pool.scheduling_snapshot()['startingWorkers']==0
    assert not ran.is_set();pool.shutdown()


def test_cancel_provisional_job_and_concurrent_duplicate_fences(monkeypatch):
    pool=kernel('startup-cancel');entered=threading.Event();release=threading.Event();ran=threading.Event();futures=[]
    real=threading.Thread.start
    def start(thread):
        if thread.name=='startup-cancel_0':entered.set();assert release.wait(2)
        return real(thread)
    monkeypatch.setattr(threading.Thread,'start',start)
    creator=threading.Thread(target=lambda:futures.append(pool.submit_task('cancelled',ran.set)));creator.start()
    try:
        assert entered.wait(1);assert pool.cancel_task('cancelled');assert not ran.is_set()
    finally:release.set();creator.join(2);pool.shutdown(wait=True)
    assert futures[0].cancelled();assert not ran.is_set()

    pool=kernel('startup-duplicate');body_release=threading.Event();body_started=threading.Event()
    first=pool.submit_task('same',lambda:(body_started.set(),body_release.wait(2)))
    try:
        assert body_started.wait(1)
        with pytest.raises(RuntimeError,match='already scheduled'):pool.submit_task('same',ran.set)
    finally:body_release.set();first.result(1);pool.shutdown()
    assert not ran.is_set()


def test_initializer_failure_does_not_run_provisional_job():
    from concurrent.futures.thread import BrokenThreadPool
    ran=threading.Event()
    def initialize():raise RuntimeError('controlled initializer failure')
    pool=DemandExecutor(max_workers=1,queue_capacity=0,max_abandoned_workers=1,initializer=initialize)
    try:
        future=pool.submit(ran.set)
        with pytest.raises(BrokenThreadPool):future.result(1)
    finally:pool.shutdown(wait=True)
    assert not ran.is_set();assert pool.scheduling_snapshot()['startingWorkers']==0


@pytest.mark.parametrize('fail',[False,True])
def test_abandon_start_completion_race_keeps_accounting(monkeypatch,fail):
    pool=kernel('startup-abandon');first_release=threading.Event();body_entered=threading.Event()
    first=pool.submit_task('running',lambda:(body_entered.set(),first_release.wait(2)))
    assert body_entered.wait(1);start_entered=threading.Event();start_release=threading.Event();out=[];errors=[];real=threading.Thread.start
    def start(thread):
        if thread.name=='startup-abandon_1':
            start_entered.set();assert start_release.wait(2)
            if fail:raise RuntimeError('replacement startup rejected')
        return real(thread)
    monkeypatch.setattr(threading.Thread,'start',start)
    def abandon():
        try:out.append(pool.abandon_task('running'))
        except RuntimeError as e:errors.append(str(e))
    actor=threading.Thread(target=abandon);actor.start()
    try:
        assert start_entered.wait(1);first_release.set();first.result(.5)
        snap=pool.scheduling_snapshot();assert snap['active']==0;assert snap['abandoned']==0
    finally:start_release.set();first_release.set();actor.join(2);pool.shutdown(wait=True)
    assert bool(errors)==fail
    assert out==([] if fail else [True])
    assert pool.scheduling_snapshot()['active']==0
    assert pool.scheduling_snapshot()['abandoned']==0


def test_resize_reservation_failure_restores_hint(monkeypatch):
    pool=kernel()
    def reject():raise RuntimeError('reserve failed')
    monkeypatch.setattr(pool,'_reserve_workers_locked',reject)
    with pytest.raises(RuntimeError,match='reserve failed'):pool.resize(8)
    assert pool._max_workers==1
    monkeypatch.undo();pool.shutdown()


def test_idle_hint_one_keeps_32_way_rendezvous_without_active_cap():
    pool=kernel('startup-rendezvous');rendezvous=threading.Barrier(33)
    try:
        jobs=[pool.submit(lambda:rendezvous.wait(5)) for _ in range(32)]
        main_index = rendezvous.wait(5)
        assert sorted([main_index, *(job.result(1) for job in jobs)]) == list(range(33))
    finally:pool.shutdown(wait=True)


def test_initializer_exit_before_start_returns_does_not_resurrect_worker(monkeypatch):
    from concurrent.futures.thread import BrokenThreadPool
    exited=threading.Event();release=threading.Event();ran=threading.Event()
    def fail():raise RuntimeError('initializer rejected')
    pool=DemandExecutor(max_workers=1,queue_capacity=0,max_abandoned_workers=1,
                        initializer=fail,thread_name_prefix='startup-exit')
    original_worker=pool._worker
    def worker():
        try:original_worker()
        finally:exited.set();release.wait(2)
    pool._worker=worker
    real=threading.Thread.start
    def start(thread):
        result=real(thread)
        if thread.name.startswith('startup-exit_'):assert exited.wait(1)
        return result
    monkeypatch.setattr(threading.Thread,'start',start)
    try:
        future=pool.submit(ran.set)
        with pytest.raises(BrokenThreadPool):future.result(1)
        assert pool.scheduling_snapshot()['residentThreads']==0
    finally:release.set();pool.shutdown(wait=True)
    assert not ran.is_set()
