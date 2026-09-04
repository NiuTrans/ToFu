"""Concurrency and resource contracts for durable-memory creation.

ID selection and whole-file publication form one transaction: concurrent
callers must retain every body, collision lookup must not issue one stat per
suffix, and a failed atomic publish must leave neither a target nor staging
garbage.  The directory lock is fixed per durable store and its in-process
lock object must not accumulate after callers leave.
"""

from __future__ import annotations

import gc
import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit


@pytest.fixture()
def project_memory_store(tmp_path):
    project_path = tmp_path / 'project'
    (project_path / '.tofu' / 'memories').mkdir(parents=True)
    return project_path


def _process_create_memory(
    project_path,
    index,
    ready_queue,
    start_event,
    result_queue,
):
    """Spawn-safe worker kept at module scope for multiprocessing."""
    try:
        import lib.memory.storage._crud as crud

        original_write = crud._write_memory_file

        def slow_write(filepath, memory):
            time.sleep(0.03)
            return original_write(filepath, memory)

        crud._write_memory_file = slow_write
        ready_queue.put(index)
        if not start_event.wait(timeout=10):
            raise TimeoutError('create start event was not released')
        memory = crud.create_memory(
            name='Concurrent Process Memory',
            description=f'process-created durable memory number {index}',
            body=f'process-body-{index}',
            scope='project',
            project_path=project_path,
        )
        result_queue.put(('ok', memory['id']))
    except BaseException as error:
        result_queue.put(('error', repr(error)))


def test_concurrent_threads_retain_every_same_name_memory(
        project_memory_store, monkeypatch):
    import lib.memory.storage._crud as crud

    original_write = crud._write_memory_file
    worker_count = 16
    start = threading.Barrier(worker_count)
    state_lock = threading.Lock()
    active_writers = 0
    max_active_writers = 0
    results = []
    failures = []

    def slow_write(filepath, memory):
        nonlocal active_writers, max_active_writers
        with state_lock:
            active_writers += 1
            max_active_writers = max(max_active_writers, active_writers)
        try:
            time.sleep(0.01)
            return original_write(filepath, memory)
        finally:
            with state_lock:
                active_writers -= 1

    def create(index):
        try:
            start.wait(timeout=5)
            memory = crud.create_memory(
                name='Concurrent Thread Memory',
                description=f'thread-created durable memory number {index}',
                body=f'thread-body-{index}',
                scope='project',
                project_path=str(project_memory_store),
            )
            with state_lock:
                results.append(memory)
        except BaseException as error:
            with state_lock:
                failures.append(error)

    monkeypatch.setattr(crud, '_write_memory_file', slow_write)
    threads = [threading.Thread(target=create, args=(index,))
               for index in range(worker_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert max_active_writers == 1
    assert len(results) == worker_count
    assert len({memory['id'] for memory in results}) == worker_count
    bodies = {
        memory['body']
        for memory in crud.list_memories(
            project_path=str(project_memory_store), scope='project')
    }
    assert bodies == {f'thread-body-{index}' for index in range(worker_count)}


@pytest.mark.skipif(os.name != 'posix', reason='POSIX flock contract')
def test_concurrent_processes_retain_every_same_name_memory(
        project_memory_store):
    import lib.memory.storage._crud as crud

    context = multiprocessing.get_context('spawn')
    worker_count = 8
    ready_queue = context.Queue()
    start_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_process_create_memory,
            args=(
                str(project_memory_store), index, ready_queue,
                start_event, result_queue,
            ),
        )
        for index in range(worker_count)
    ]
    for process in processes:
        process.start()
    assert len([ready_queue.get(timeout=20) for _ in processes]) == worker_count
    start_event.set()
    results = [result_queue.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)

    assert all(not process.is_alive() for process in processes)
    assert all(process.exitcode == 0 for process in processes)
    assert all(status == 'ok' for status, _value in results), results
    ids = [value for _status, value in results]
    assert len(set(ids)) == worker_count
    memories = crud.list_memories(
        project_path=str(project_memory_store), scope='project')
    assert {memory['body'] for memory in memories} == {
        f'process-body-{index}' for index in range(worker_count)
    }


def test_collision_allocation_uses_one_snapshot_not_suffix_stats(
        project_memory_store, monkeypatch):
    import lib.memory.storage._crud as crud

    memory_dir = project_memory_store / '.tofu' / 'memories'
    collision_count = 1_000
    for index in range(collision_count):
        suffix = '' if index == 0 else f'_{index}'
        (memory_dir / f'repeated_name{suffix}.md').write_text(
            '', encoding='utf-8')

    original_lexists = crud.os.path.lexists
    original_listdir = crud.os.listdir
    calls = {'lexists': 0, 'listdir': 0}

    def counted_lexists(path):
        calls['lexists'] += 1
        return original_lexists(path)

    def counted_listdir(path):
        calls['listdir'] += 1
        return original_listdir(path)

    monkeypatch.setattr(crud.os.path, 'lexists', counted_lexists)
    monkeypatch.setattr(crud.os, 'listdir', counted_listdir)
    memory = crud.create_memory(
        name='Repeated Name',
        description='collision allocation resource budget fixture',
        body='new body',
        scope='project',
        project_path=str(project_memory_store),
    )

    assert memory['id'] == f'repeated_name_{collision_count}'
    assert calls == {'lexists': 1, 'listdir': 1}


def test_failed_publish_cleans_staging_and_releases_allocation_lock(
        project_memory_store, monkeypatch):
    from lib import json_store
    from lib.memory.storage import create_memory

    memory_dir = project_memory_store / '.tofu' / 'memories'
    original_replace = json_store.os.replace

    def fail_memory_publish(source, destination):
        if destination.endswith('failed_publish.md'):
            raise OSError('injected memory publish failure')
        return original_replace(source, destination)

    monkeypatch.setattr(json_store.os, 'replace', fail_memory_publish)
    with pytest.raises(OSError, match='injected memory publish failure'):
        create_memory(
            name='Failed Publish',
            description='atomic failure cleanup contract fixture',
            body='must not leak',
            scope='project',
            project_path=str(project_memory_store),
        )

    assert not (memory_dir / 'failed_publish.md').exists()
    assert not list(memory_dir.glob('.jsonstore-*.tmp'))
    monkeypatch.setattr(json_store.os, 'replace', original_replace)
    retried = create_memory(
        name='Failed Publish',
        description='successful retry after atomic publish failure',
        body='retained retry',
        scope='project',
        project_path=str(project_memory_store),
    )
    assert retried['id'] == 'failed_publish'
    assert Path(retried['filepath']).is_file()


def test_package_symlink_and_unicode_suffix_collisions_are_safe(
        project_memory_store):
    from lib.memory.contracts import MEMORY_GENERATED_ID_MAX_BYTES
    from lib.memory.storage import create_memory
    from lib.memory.storage._files import _make_memory_id

    memory_dir = project_memory_store / '.tofu' / 'memories'
    (memory_dir / 'package_collision').mkdir()
    package_collision = create_memory(
        name='Package Collision',
        description='directory-shaped collision is preserved',
        scope='project',
        project_path=str(project_memory_store),
    )
    assert package_collision['id'] == 'package_collision_1'
    assert (memory_dir / 'package_collision').is_dir()

    if hasattr(os, 'symlink'):
        broken_link = memory_dir / 'broken_link.md'
        try:
            broken_link.symlink_to(memory_dir / 'missing-target.md')
        except OSError:
            broken_link = None
        if broken_link is not None:
            symlink_collision = create_memory(
                name='Broken Link',
                description='broken symlink collision is not overwritten',
                scope='project',
                project_path=str(project_memory_store),
            )
            assert symlink_collision['id'] == 'broken_link_1'
            assert broken_link.is_symlink()

    # The public name limit is 160 characters; CJK still exceeds the generated
    # ID's 192-byte limit and therefore exercises suffix re-truncation.
    unicode_name = '界' * 160
    base_id = _make_memory_id(unicode_name)
    (memory_dir / f'{base_id}.md').write_text('', encoding='utf-8')
    unicode_collision = create_memory(
        name=unicode_name,
        description='Unicode collision suffix remains filesystem safe',
        scope='project',
        project_path=str(project_memory_store),
    )
    assert unicode_collision['id'] != base_id
    assert len(unicode_collision['id'].encode('utf-8')) <= (
        MEMORY_GENERATED_ID_MAX_BYTES)
    assert len(Path(unicode_collision['filepath']).name.encode('utf-8')) <= 255


def test_idle_path_locks_are_not_retained_forever(tmp_path):
    from lib import json_store

    lock_identity = os.path.abspath(str(tmp_path / 'one-shot'))
    with json_store.locked_path(lock_identity):
        assert lock_identity in json_store._PATH_LOCKS
    gc.collect()
    assert lock_identity not in json_store._PATH_LOCKS
