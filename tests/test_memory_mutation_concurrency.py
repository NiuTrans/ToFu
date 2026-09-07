"""Linearizability and bounded-lock contracts for memory mutations.

Cooperating threads and POSIX processes must serialize each record's complete
discover/read/check/publish-or-delete boundary.  Independent field updates
must compose, two toggles must not collapse into one, delete/merge must not be
undone by a late writer, multi-source locks must be ordered, and lock sidecars
must stay capped per durable directory rather than grow per historical ID.
"""

from __future__ import annotations

import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit


@pytest.fixture()
def mutation_store(tmp_path, monkeypatch):
    import lib.memory.storage._dirs as storage_dirs
    import lib.memory.storage._files as memory_files

    data_dir = tmp_path / 'data'
    project = tmp_path / 'project'
    (project / '.tofu' / 'memories').mkdir(parents=True)
    monkeypatch.setenv('TOFU_DATA_DIR', str(data_dir))
    monkeypatch.setattr(
        storage_dirs, '_server_data_dir', lambda: str(data_dir))
    storage_dirs._migrated_roots.clear()
    storage_dirs._migrated_roots.add(str(project))
    storage_dirs._server_store_migrated = True
    memory_files._metadata_cache.clear()
    yield project
    memory_files._metadata_cache.clear()
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False


def _create_memory(project: Path, name: str = 'Concurrent Mutation'):
    from lib.memory.storage import create_memory

    return create_memory(
        name=name,
        description='original durable description for concurrency testing',
        body='original durable body',
        scope='project',
        project_path=str(project),
    )


def _process_update(
    project_path,
    memory_id,
    updates,
    ready_queue,
    start_event,
    result_queue,
):
    try:
        import lib.memory.storage._crud as crud

        original_write = crud._write_memory_file

        def slow_write(filepath, record):
            time.sleep(0.05)
            return original_write(filepath, record)

        crud._write_memory_file = slow_write
        ready_queue.put(memory_id)
        if not start_event.wait(timeout=10):
            raise TimeoutError('update start event was not released')
        result = crud.update_memory(
            memory_id, updates, project_path=project_path)
        result_queue.put(('ok', result and result['id']))
    except BaseException as error:
        result_queue.put(('error', repr(error)))


def test_concurrent_field_updates_compose_instead_of_losing_one(
        mutation_store, monkeypatch):
    import lib.memory.storage._crud as crud

    memory = _create_memory(mutation_store)
    original_write = crud._write_memory_file
    start = threading.Barrier(2)
    state_lock = threading.Lock()
    active_writers = 0
    max_active_writers = 0
    failures = []

    def slow_write(filepath, record):
        nonlocal active_writers, max_active_writers
        with state_lock:
            active_writers += 1
            max_active_writers = max(max_active_writers, active_writers)
        try:
            time.sleep(0.03)
            return original_write(filepath, record)
        finally:
            with state_lock:
                active_writers -= 1

    def update(fields):
        try:
            start.wait(timeout=5)
            crud.update_memory(
                memory['id'], fields, project_path=str(mutation_store))
        except BaseException as error:
            failures.append(error)

    monkeypatch.setattr(crud, '_write_memory_file', slow_write)
    threads = [
        threading.Thread(
            target=update,
            args=({'description': 'new durable description'},),
        ),
        threading.Thread(target=update, args=({'body': 'new durable body'},)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert failures == []
    assert all(not thread.is_alive() for thread in threads)
    assert max_active_writers == 1
    final = crud.get_memory(memory['id'], project_path=str(mutation_store))
    assert final['description'] == 'new durable description'
    assert final['body'] == 'new durable body'


@pytest.mark.skipif(os.name != 'posix', reason='POSIX flock contract')
def test_concurrent_process_updates_compose(mutation_store):
    import lib.memory.storage._crud as crud

    memory = _create_memory(mutation_store, name='Process Mutation')
    context = multiprocessing.get_context('spawn')
    ready_queue = context.Queue()
    start_event = context.Event()
    result_queue = context.Queue()
    updates = [
        {'description': 'description written by process'},
        {'body': 'body written by process'},
    ]
    processes = [
        context.Process(
            target=_process_update,
            args=(
                str(mutation_store), memory['id'], fields,
                ready_queue, start_event, result_queue,
            ),
        )
        for fields in updates
    ]
    for process in processes:
        process.start()
    assert len([ready_queue.get(timeout=20) for _ in processes]) == len(processes)
    start_event.set()
    results = [result_queue.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)

    assert all(not process.is_alive() for process in processes)
    assert all(process.exitcode == 0 for process in processes)
    assert all(status == 'ok' for status, _value in results), results
    final = crud.get_memory(memory['id'], project_path=str(mutation_store))
    assert final['description'] == 'description written by process'
    assert final['body'] == 'body written by process'


def test_two_concurrent_toggles_do_not_collapse_into_one(
        mutation_store, monkeypatch):
    import lib.memory.storage._crud as crud

    memory = _create_memory(mutation_store, name='Toggle Twice')
    original_write = crud._write_memory_file
    start = threading.Barrier(2)
    result_lock = threading.Lock()
    enabled_results = []
    failures = []

    def slow_write(filepath, record):
        time.sleep(0.03)
        return original_write(filepath, record)

    def toggle():
        try:
            start.wait(timeout=5)
            result = crud.toggle_memory(
                memory['id'], project_path=str(mutation_store))
            with result_lock:
                enabled_results.append(result['enabled'])
        except BaseException as error:
            with result_lock:
                failures.append(error)

    monkeypatch.setattr(crud, '_write_memory_file', slow_write)
    threads = [threading.Thread(target=toggle) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert failures == []
    assert sorted(enabled_results) == [False, True]
    final = crud.get_memory(memory['id'], project_path=str(mutation_store))
    assert final['enabled'] is True


def test_independent_shards_can_publish_concurrently(
        mutation_store, monkeypatch):
    import lib.memory.storage._crud as crud

    memories = [
        _create_memory(mutation_store, name=f'Independent Record {index}')
        for index in range(crud._MEMORY_MUTATION_LOCK_SHARDS + 1)
    ]
    by_lock_path = {}
    for memory in memories:
        by_lock_path.setdefault(
            crud._memory_mutation_lock_path(memory['filepath']), memory)
    assert len(by_lock_path) >= 2
    first, second = list(by_lock_path.values())[:2]

    original_write = crud._write_memory_file
    start = threading.Barrier(2)
    state_lock = threading.Lock()
    active_writers = 0
    max_active_writers = 0
    failures = []

    def slow_write(filepath, record):
        nonlocal active_writers, max_active_writers
        with state_lock:
            active_writers += 1
            max_active_writers = max(max_active_writers, active_writers)
        try:
            time.sleep(0.05)
            return original_write(filepath, record)
        finally:
            with state_lock:
                active_writers -= 1

    def update(memory):
        try:
            start.wait(timeout=5)
            crud.update_memory(
                memory['id'], {'body': f"updated {memory['id']}"},
                project_path=str(mutation_store),
            )
        except BaseException as error:
            failures.append(error)

    monkeypatch.setattr(crud, '_write_memory_file', slow_write)
    threads = [
        threading.Thread(target=update, args=(memory,))
        for memory in (first, second)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert failures == []
    assert all(not thread.is_alive() for thread in threads)
    assert max_active_writers == 2


def test_delete_waits_for_update_and_never_allows_zombie_republish(
        mutation_store, monkeypatch):
    import lib.memory.storage._crud as crud

    memory = _create_memory(mutation_store, name='No Zombie')
    original_write = crud._write_memory_file
    writer_entered = threading.Event()
    release_writer = threading.Event()
    results = {}

    def delayed_write(filepath, record):
        writer_entered.set()
        if not release_writer.wait(timeout=5):
            raise TimeoutError('writer release was not signalled')
        return original_write(filepath, record)

    monkeypatch.setattr(crud, '_write_memory_file', delayed_write)
    updater = threading.Thread(
        target=lambda: results.setdefault(
            'update',
            crud.update_memory(
                memory['id'], {'body': 'late body'},
                project_path=str(mutation_store),
            ),
        )
    )
    deleter = threading.Thread(
        target=lambda: results.setdefault(
            'delete',
            crud.delete_memory(
                memory['id'], project_path=str(mutation_store)),
        )
    )
    updater.start()
    assert writer_entered.wait(timeout=5)
    deleter.start()
    time.sleep(0.05)
    assert deleter.is_alive(), 'delete bypassed the active record writer lock'
    release_writer.set()
    updater.join(timeout=10)
    deleter.join(timeout=10)

    assert not updater.is_alive() and not deleter.is_alive()
    assert results['update'] is not None
    assert results['delete'] is True
    assert crud.get_memory(
        memory['id'], project_path=str(mutation_store)) is None


def test_merge_holds_source_locks_until_delete_settles(
        mutation_store, monkeypatch):
    import lib.memory.storage._crud as crud

    first = _create_memory(mutation_store, name='Merge Source One')
    second = _create_memory(mutation_store, name='Merge Source Two')
    original_create = crud.create_memory
    replacement_created = threading.Event()
    release_merge = threading.Event()
    results = {}
    failures = []

    def paused_create(*args, **kwargs):
        result = original_create(*args, **kwargs)
        replacement_created.set()
        if not release_merge.wait(timeout=5):
            raise TimeoutError('merge release was not signalled')
        return result

    def merge():
        try:
            results['merge'] = crud.merge_memories(
                [first['id'], second['id']],
                name='Merged Concurrent Sources',
                description='replacement created while sources stay locked',
                body='merged body',
                scope='project',
                project_path=str(mutation_store),
            )
        except BaseException as error:
            failures.append(error)

    def update_source():
        try:
            results['update'] = crud.update_memory(
                first['id'], {'body': 'late source update'},
                project_path=str(mutation_store),
            )
        except BaseException as error:
            failures.append(error)

    monkeypatch.setattr(crud, 'create_memory', paused_create)
    merger = threading.Thread(target=merge)
    updater = threading.Thread(target=update_source)
    merger.start()
    assert replacement_created.wait(timeout=5)
    updater.start()
    time.sleep(0.05)
    assert updater.is_alive(), 'source update bypassed merge source locks'
    release_merge.set()
    merger.join(timeout=10)
    updater.join(timeout=10)

    assert failures == []
    assert not merger.is_alive() and not updater.is_alive()
    assert results['merge']['failed_ids'] == []
    assert results['update'] is None
    assert crud.get_memory(
        first['id'], project_path=str(mutation_store)) is None
    assert crud.get_memory(
        second['id'], project_path=str(mutation_store)) is None


def test_mutation_sidecars_are_bounded_per_directory(mutation_store):
    import lib.memory.storage._crud as crud

    memory_dir = mutation_store / '.tofu' / 'memories'
    synthetic_paths = [
        str(memory_dir / f'historical-{index}.md')
        for index in range(2_000)
    ]
    with crud._memory_mutation_locks(
            [(path, '') for path in synthetic_paths]) as held_locks:
        assert len(held_locks) <= crud._MEMORY_MUTATION_LOCK_SHARDS

    sidecars = list(memory_dir.glob('.memory-mutation-*.lock'))
    assert 1 <= len(sidecars) <= crud._MEMORY_MUTATION_LOCK_SHARDS
    assert all(path.stat().st_size == 0 for path in sidecars)


def test_package_lock_sidecar_stays_in_skills_store(mutation_store):
    import lib.memory.storage._crud as crud

    skills_store = mutation_store / '.tofu' / 'skills'
    package_dir = skills_store / 'package-lock-target'
    package_dir.mkdir(parents=True)
    skill_file = package_dir / 'SKILL.md'
    skill_file.write_text(
        '---\n'
        'name: Package Lock Target\n'
        'description: package mutation lock placement fixture\n'
        'enabled: true\n'
        '---\n\n'
        'Package body.\n',
        encoding='utf-8',
    )

    toggled = crud.toggle_memory(
        'package-lock-target',
        enabled=False,
        project_path=str(mutation_store),
    )

    assert toggled is not None and toggled['is_package'] is True
    assert {path.name for path in package_dir.iterdir()} == {'SKILL.md'}
    sidecars = list(skills_store.glob('.memory-mutation-*.lock'))
    assert len(sidecars) == 1
