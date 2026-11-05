"""Project journal admission is first-come first-served and reports its holder."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import fcntl
import threading
import time

import pytest

from lib.project_mod import session_lock as locks
from lib.project_mod.tool_admission import tool_admission_scope

pytestmark = pytest.mark.unit


def test_earliest_waiter_is_admitted_before_a_later_arrival(tmp_path, monkeypatch):
    """A release goes to the oldest waiter even when it is not running then.

    Under GIL or scheduler pressure a waiter may sleep across the release; a
    later arrival polling at that moment must not take its turn, otherwise a
    stream of later operations exhausts the earliest waiter's budget.
    """
    session = str(tmp_path)
    monkeypatch.setattr(locks, '_wait_seconds', lambda: 10)
    order = []
    first_waiting, later_waiting, later_admitted = (
        threading.Event(), threading.Event(), threading.Event())
    flock = fcntl.flock

    def observed_flock(descriptor, operation):
        try:
            return flock(descriptor, operation)
        except BlockingIOError:
            if threading.current_thread().name == 'first':
                first_waiting.set()
            raise

    real_sleep = time.sleep

    def descheduled(seconds):
        if threading.current_thread().name == 'first':
            later_admitted.wait(2)
        real_sleep(seconds)

    phase = locks.timing_phase

    @contextmanager
    def observed_phase(name, **kwargs):
        if threading.current_thread().name == 'later':
            later_waiting.set()
        with phase(name, **kwargs) as measured:
            yield measured

    monkeypatch.setattr(fcntl, 'flock', observed_flock)
    monkeypatch.setattr(time, 'sleep', descheduled)
    monkeypatch.setattr(locks, 'timing_phase', observed_phase)

    def admit(name):
        with locks.locked_session(session):
            order.append(name)
            if name == 'later':
                later_admitted.set()

    first = threading.Thread(target=admit, args=('first',), name='first')
    later = threading.Thread(target=admit, args=('later',), name='later')
    with locks.locked_session(session):
        first.start()
        assert first_waiting.wait(5)
        later.start()
        assert later_waiting.wait(5)
        real_sleep(0.05)
    first.join(10)
    later.join(10)
    assert not first.is_alive() and not later.is_alive()
    assert order == ['first', 'later']
    assert not locks._active_descriptors


def test_waiter_that_gives_up_leaves_nothing_ahead_of_the_next_arrival(tmp_path, monkeypatch):
    session = str(tmp_path)
    monkeypatch.setattr(locks, '_wait_seconds', lambda: 0.2)

    def enter():
        with locks.locked_session(session):
            return 'admitted'

    with ThreadPoolExecutor(max_workers=1) as pool:
        with locks.locked_session(session):
            with pytest.raises(locks.SessionLockBusy):
                pool.submit(enter).result(timeout=5)
    # No budget at all: only an empty line of earlier waiters admits at once.
    monkeypatch.setattr(locks, '_wait_seconds', lambda: 0)
    assert enter() == 'admitted'
    assert not locks._active_descriptors


def test_failed_admission_leaves_nothing_ahead_of_the_next_arrival(tmp_path, monkeypatch):
    session = str(tmp_path)
    flock = fcntl.flock

    def broken(descriptor, operation):
        raise OSError('lock storage failed')

    monkeypatch.setattr(fcntl, 'flock', broken)
    with pytest.raises(OSError, match='lock storage failed'):
        with locks.locked_session(session):
            pass
    monkeypatch.setattr(fcntl, 'flock', flock)
    monkeypatch.setattr(locks, '_wait_seconds', lambda: 0)
    with locks.locked_session(session):
        pass
    assert not locks._active_descriptors


@pytest.mark.parametrize('same_task', [True, False])
def test_busy_names_the_holding_tool_call_and_how_long_it_held(tmp_path, monkeypatch, same_task):
    """Every task's tool pool names its first worker tool-parallel_0, so a
    thread name alone implicates the wrong call. The refusal says which tool
    call holds the journal, whether it belongs to the waiting task, and for
    how long it has held it."""
    session = str(tmp_path)
    monkeypatch.setattr(locks, '_wait_seconds', lambda: 0.3)
    held, release = threading.Event(), threading.Event()
    holder_task = {'id': 'task-a'}

    def hold():
        with tool_admission_scope(holder_task, tool_name='run_command'):
            with locks.locked_session(session):
                held.set()
                assert release.wait(10)

    holder = threading.Thread(target=hold, name='tool-parallel_0')
    holder.start()
    try:
        assert held.wait(5)
        with tool_admission_scope(holder_task if same_task else {'id': 'task-b'},
                                  tool_name='edit_file'):
            with pytest.raises(locks.SessionLockBusy) as caught:
                with locks.locked_session(session):
                    pass
    finally:
        release.set()
        holder.join(10)
    message = str(caught.value)
    relation = 'this task' if same_task else 'another task'
    assert f'Held by run_command in {relation} (thread tool-parallel_0) for ' in message, message
    held_for = float(message.split(') for ', 1)[1].split('s', 1)[0])
    assert held_for + 0.01 >= caught.value.waited_seconds > 0
    assert 'no file mutation was performed' in message and 're-read' in message


def test_busy_counts_earlier_waiters_still_ahead(tmp_path, monkeypatch):
    session = str(tmp_path)
    monkeypatch.setattr(locks, '_wait_seconds', lambda: 5)
    waiting = threading.Event()
    phase = locks.timing_phase

    @contextmanager
    def observed_phase(name, **kwargs):
        if threading.current_thread().name == 'earlier':
            waiting.set()
        with phase(name, **kwargs) as measured:
            yield measured

    monkeypatch.setattr(locks, 'timing_phase', observed_phase)

    def enter():
        with locks.locked_session(session):
            pass

    earlier = threading.Thread(target=enter, name='earlier')
    with locks.locked_session(session):
        earlier.start()
        assert waiting.wait(5)
        time.sleep(0.05)
        monkeypatch.setattr(locks, '_wait_seconds', lambda: 0.2)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(locks.SessionLockBusy) as caught:
                pool.submit(enter).result(timeout=5)
    earlier.join(10)
    assert not earlier.is_alive()
    assert '1 earlier operation of this process is still waiting ahead of this call' in str(caught.value)
