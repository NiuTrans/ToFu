"""Prepared restoration admits independent identities and fences duplicate carriers."""
from concurrent.futures import ThreadPoolExecutor
import gc
import threading

import pytest

pytestmark = pytest.mark.unit


def task_id(index):
    return f'prepared-{index:048x}'


def restore(index, *, owner=7, digest='a'*64):
    from lib.tasks_pkg.manager.prepared import restore_prepared_task
    return restore_prepared_task(task_id(index), [{'role':'user', 'content':str(index)}],
                                 {'userId':owner}, user_id=owner, input_digest=digest)


@pytest.fixture
def construction(monkeypatch):
    from lib.tasks_pkg.manager import prepared, _registry
    from lib.weak_lock_pool import WeakLockPool
    tasks = {}
    tasks_lock = threading.Lock()
    calls = []
    class Runtime:
        def get(self, key):
            with tasks_lock:
                return tasks.get(key)
    def create(conv_id, messages, config, *, supersede, prepared_task_id,
               require_durable_cancellation, user_id):
        assert user_id == config['userId']
        assert conv_id == '' and supersede is False
        assert require_durable_cancellation is True
        row = {'id':prepared_task_id, '_userId':config['userId'], 'status':'pending',
               'messages':messages, 'config':config}
        with tasks_lock:
            calls.append(prepared_task_id)
            tasks[prepared_task_id] = row
        return row
    monkeypatch.setattr(prepared, 'chat_task_runtime', Runtime())
    monkeypatch.setattr(prepared, '_restore_locks', WeakLockPool(threading.Lock))
    monkeypatch.setattr(_registry, '_create_chat_task', create)
    return prepared, _registry, create, calls


def test_independent_restores_start_beyond_old_global_lock_deadline(construction, monkeypatch):
    prepared, registry, create, calls = construction
    barrier = threading.Barrier(33)
    release = threading.Event()
    def blocked(*args, **kwargs):
        if kwargs['prepared_task_id'] != task_id(100):
            barrier.wait(5)
            assert release.wait(10)
        return create(*args, **kwargs)
    monkeypatch.setattr(registry, '_create_chat_task', blocked)
    pool = ThreadPoolExecutor(max_workers=33)
    futures = []
    try:
        futures = [pool.submit(restore, index, owner=index+1) for index in range(32)]
        barrier.wait(5)
        assert len(prepared._restore_locks) == 32
        healthy = pool.submit(restore, 100, owner=101).result(2)
        assert healthy['_userId'] == 101
        assert all(not future.done() for future in futures)
    finally:
        release.set()
        pool.shutdown(wait=True)
    assert [future.result()['_userId'] for future in futures] == list(range(1,33))
    assert len(calls) == 33
    gc.collect()
    assert len(prepared._restore_locks) == 0


def test_duplicate_restores_wait_for_one_construction_without_busy_refusal(construction, monkeypatch):
    prepared, registry, create, calls = construction
    start = threading.Barrier(33)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return create(*args, **kwargs)
    monkeypatch.setattr(registry, '_create_chat_task', blocked)
    def duplicate():
        start.wait(5)
        value = restore(1)
        finished.set()
        return value
    pool = ThreadPoolExecutor(max_workers=32)
    futures = []
    try:
        futures = [pool.submit(duplicate) for _ in range(32)]
        start.wait(5)
        assert entered.wait(5)
        assert not finished.wait(.65)
        assert all(not future.done() for future in futures)
        assert len(prepared._restore_locks) == 1
    finally:
        release.set()
        pool.shutdown(wait=True)
    results = [future.result() for future in futures]
    assert all(result is results[0] for result in results)
    assert calls == [task_id(1)]
    assert results[0]['_preparedInputDigest'] == 'a'*64
    gc.collect()
    assert len(prepared._restore_locks) == 0


def test_factory_failure_releases_identity_for_retry(construction, monkeypatch):
    prepared, registry, create, calls = construction
    def fail(*args, **kwargs):
        raise OSError('durable construction unavailable')
    monkeypatch.setattr(registry, '_create_chat_task', fail)
    with pytest.raises(OSError, match='durable construction unavailable'):
        restore(2)
    monkeypatch.setattr(registry, '_create_chat_task', create)
    assert restore(2)['id'] == task_id(2)
    assert calls == [task_id(2)]
    gc.collect()
    assert len(prepared._restore_locks) == 0


def test_resident_owner_digest_and_lifecycle_remain_authoritative(construction):
    prepared, registry, create, calls = construction
    resident = restore(3)
    with pytest.raises(ValueError, match='identity or lifecycle'):
        restore(3, owner=8)
    with pytest.raises(ValueError, match='identity or lifecycle'):
        restore(3, digest='b'*64)
    resident['status'] = 'running'
    with pytest.raises(ValueError, match='identity or lifecycle'):
        restore(3)
    assert calls == [task_id(3)]
    assert len(prepared._restore_locks) == 0


def test_recursive_same_identity_is_a_lifecycle_error_and_can_retry(construction, monkeypatch):
    prepared, registry, create, calls = construction
    monkeypatch.setattr(registry, '_create_chat_task', lambda *args, **kwargs: restore(4))
    with pytest.raises(ValueError, match='cannot recurse'):
        restore(4)
    monkeypatch.setattr(registry, '_create_chat_task', create)
    assert restore(4)['id'] == task_id(4)
    assert calls == [task_id(4)]
