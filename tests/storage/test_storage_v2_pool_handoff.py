"""Demand session ownership under slow creation, cancellation, and bursts."""
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from lib.storage import v2_client

pytestmark = pytest.mark.unit

class Session:
    def __init__(self):
        self.closed = 0
    def close(self):
        self.closed += 1

class InjectedCancel(BaseException):
    pass

@pytest.mark.parametrize('failure', [OSError, InjectedCancel])
def test_failed_creation_releases_only_its_own_reservation(failure):
    calls = 0
    def factory(timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise failure()
        return Session()
    pool = v2_client._SessionPool(factory, 1)
    with pytest.raises(failure):
        pool.acquire(1)
    assert pool.resource_snapshot()['used'] == 0
    session = pool.acquire(1)
    pool.release(session, broken=False)
    pool.close()
    assert session.closed == 1


def test_slow_creation_does_not_queue_other_borrowers():
    entered, resume = threading.Event(), threading.Event()
    errors = []
    def factory(timeout):
        if threading.current_thread().name == 'slow-session':
            entered.set()
            assert resume.wait(3)
        return Session()
    pool = v2_client._SessionPool(factory, 1)
    def borrow():
        try:
            session = pool.acquire(2)
            pool.release(session, broken=False)
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=borrow, name='slow-session')
    worker.start()
    try:
        assert entered.wait(1)
        another = pool.acquire(.2)
        assert pool.resource_snapshot()['used'] == 2
        pool.release(another, broken=False)
    finally:
        resume.set()
        worker.join(3)
        pool.close()
    assert not errors and not worker.is_alive()
    assert pool.resource_snapshot()['used'] == 0


def test_mixed_deadline_burst_has_no_count_admission_and_retires_surplus():
    count = 64
    barrier = threading.Barrier(count)
    sessions = []
    def factory(timeout):
        session = Session()
        sessions.append(session)
        return session
    pool = v2_client._SessionPool(factory, 1)
    def borrow(index):
        session = pool.acquire(180 if index % 2 else 1)
        try:
            barrier.wait(5)
            assert pool.resource_snapshot()['waiting'] == 0
            return id(session)
        finally:
            pool.release(session, broken=False)
    with ThreadPoolExecutor(max_workers=count) as executor:
        identities = list(executor.map(borrow, range(count)))
    assert len(set(identities)) == count
    assert pool.resource_snapshot()['idle'] == 1
    assert pool.resource_snapshot()['used'] == 0
    assert sum(session.closed for session in sessions) == count - 1
    pool.close()
    assert all(session.closed == 1 for session in sessions)
