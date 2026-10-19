"""Exact identity locks across processes, crash reclamation and fork ownership."""
import gc
import multiprocessing
import threading

import pytest
from lib.oauth import identity_flight as locks

pytestmark = pytest.mark.unit
_A = '0' * 23 + '1'
_B = '0' * 23 + '2'  # same legacy first-eight-hex / 16 stripe


def _hold(scope, key, ready, entered, release):
    ready.set()
    with locks.identity_flight(scope, key):
        entered.set()
        assert release.wait(10)


def test_colliding_legacy_stripes_are_independent_and_active_files_retire(tmp_path):
    scope = str(tmp_path/'cache.json')
    entered, release = threading.Event(), threading.Event()
    def other():
        with locks.identity_flight(scope,_B):
            entered.set()
            assert release.wait(5)
    thread = threading.Thread(target=other)
    try:
        with locks.identity_flight(scope,_A):
            thread.start()
            assert entered.wait(2)
            assert len(list(tmp_path.glob('*.flight.active.*')))==2
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    assert not list(tmp_path.glob('*.flight.active.*'))
    gc.collect()
    assert len(locks._identity_locks)==len(locks._guard_locks)==0


@pytest.mark.parametrize('method', ['spawn','fork'])
def test_exact_identity_excludes_processes_and_fork_does_not_keep_parent_lease(tmp_path, method):
    if locks.fcntl is None or method not in multiprocessing.get_all_start_methods():
        pytest.skip('POSIX process locking unavailable')
    context = multiprocessing.get_context(method)
    scope=str(tmp_path/'cache.json')
    ready, entered, release = (context.Event() for _ in range(3))
    process=context.Process(target=_hold,args=(scope,_A,ready,entered,release))
    try:
        with locks.identity_flight(scope,_A):
            process.start()
            assert ready.wait(5)
            assert not entered.wait(0.1)
        assert entered.wait(3)
        release.set()
        process.join(3)
        assert process.exitcode==0
    finally:
        release.set()
        if process.is_alive():process.terminate();process.join(3)
    assert not list(tmp_path.glob('*.flight.active.*'))


def test_another_process_with_colliding_old_stripe_does_not_wait(tmp_path):
    if locks.fcntl is None:pytest.skip('POSIX process locking unavailable')
    context=multiprocessing.get_context('spawn')
    ready,entered,release=(context.Event() for _ in range(3))
    scope=str(tmp_path/'cache.json')
    process=context.Process(target=_hold,args=(scope,_B,ready,entered,release))
    try:
        with locks.identity_flight(scope,_A):
            process.start()
            assert entered.wait(5)
            release.set()
            process.join(3)
            assert process.exitcode==0
    finally:
        release.set()
        if process.is_alive():process.terminate();process.join(3)


def test_dead_owner_file_is_reclaimed_without_time_lease(tmp_path):
    if locks.fcntl is None:pytest.skip('POSIX process locking unavailable')
    context=multiprocessing.get_context('spawn')
    ready,entered,release=(context.Event() for _ in range(3))
    scope=str(tmp_path/'cache.json')
    process=context.Process(target=_hold,args=(scope,_A,ready,entered,release))
    try:
        process.start()
        assert entered.wait(5)
        assert len(list(tmp_path.glob('*.flight.active.*')))==1
        process.terminate();process.join(3)
        assert not process.is_alive()
        with locks.identity_flight(scope,_B):
            assert len(list(tmp_path.glob('*.flight.active.*')))==1
        assert not list(tmp_path.glob('*.flight.active.*'))
    finally:
        if process.is_alive():process.terminate();process.join(3)


def test_lock_failure_is_closed_and_releases_every_descriptor(tmp_path,monkeypatch):
    if locks.fcntl is None:pytest.skip('POSIX process locking unavailable')
    scope=str(tmp_path/'cache.json')
    flock=locks.fcntl.flock
    def broken(fd,operation):
        if operation & locks.fcntl.LOCK_NB:raise OSError('injected lock failure')
        return flock(fd,operation)
    monkeypatch.setattr(locks.fcntl,'flock',broken)
    called=[]
    with pytest.raises(OSError,match='injected'):
        with locks.identity_flight(scope,_A):called.append('unsafe effect')
    assert not called
    assert not locks._fds
    monkeypatch.setattr(locks.fcntl,'flock',flock)
    with locks.identity_flight(scope,_A):pass
    assert not list(tmp_path.glob('*.flight.active.*'))


def test_exception_releases_exact_identity_and_idle_state(tmp_path):
    scope=str(tmp_path/'cache.json')
    with pytest.raises(RuntimeError,match='effect failed'):
        with locks.identity_flight(scope,_A):raise RuntimeError('effect failed')
    with locks.identity_flight(scope,_A):pass
    assert not locks._fds
    assert not list(tmp_path.glob('*.flight.active.*'))


def test_no_fcntl_platform_retains_thread_identity_exclusion(tmp_path,monkeypatch):
    monkeypatch.setattr(locks,'fcntl',None)
    scope=str(tmp_path/'cache.json');entered=threading.Event()
    def wait():
        with locks.identity_flight(scope,_A):entered.set()
    thread=threading.Thread(target=wait)
    with locks.identity_flight(scope,_A):
        thread.start()
        assert not entered.wait(0.02)
        with locks.identity_flight(scope,_B):pass
    assert entered.wait(2)
    thread.join(2)
    assert not list(tmp_path.iterdir())
