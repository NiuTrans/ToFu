"""Read-only memory scans never serialize behind the migration lock.

The process-global lock guards only pending one-time storage migrations.
Corpus listings, exact-ID probes and skill-registry scans of migrated roots run
concurrently, a root with a pending migration still waits for it, and a
migration publishes into the shared server stores atomically because scans of
other roots no longer exclude it.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit

_WAIT_SECONDS = 10.0
_PROJECTS = 4


@pytest.fixture()
def scan_store(tmp_path, monkeypatch):
    import lib.memory.storage._dirs as storage_dirs
    import lib.memory.storage._files as memory_files
    import lib.skills.registry as registry

    data_dir = tmp_path / 'data'
    projects = []
    for index in range(_PROJECTS):
        project = tmp_path / f'project-{index}'
        (project / '.tofu' / 'memories').mkdir(parents=True)
        projects.append(project)
    monkeypatch.setenv('TOFU_DATA_DIR', str(data_dir))
    monkeypatch.setattr(
        storage_dirs, '_server_data_dir', lambda: str(data_dir))
    storage_dirs._migrated_roots.clear()
    storage_dirs._migrated_roots.update(str(project) for project in projects)
    storage_dirs._server_store_migrated = True
    memory_files._metadata_cache.clear()
    registry._invalidate_skills_cache()
    yield projects
    registry._invalidate_skills_cache()
    memory_files._metadata_cache.clear()
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False


def _write_memory(directory: Path, memory_id: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{memory_id}.md'
    path.write_text(
        '---\n'
        f'name: {memory_id}\n'
        f'description: scan locking fixture for {memory_id}\n'
        '---\n\n'
        f'body for {memory_id}\n',
        encoding='utf-8',
    )
    return path


def _write_package(directory: Path, package_id: str) -> Path:
    package_dir = directory / package_id
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / 'SKILL.md').write_text(
        '---\n'
        f'name: {package_id}\n'
        f'description: scan locking package fixture for {package_id}\n'
        '---\n\n'
        f'package body for {package_id}\n',
        encoding='utf-8',
    )
    return package_dir


def _require_overlap(monkeypatch, module, name, matches) -> None:
    """Make every matching call wait until all projects are inside it."""
    original = getattr(module, name)
    barrier = threading.Barrier(_PROJECTS)

    def rendezvous(*args, **kwargs):
        if matches(*args, **kwargs):
            # A serialized scan holds its lock here and breaks the barrier.
            barrier.wait(_WAIT_SECONDS)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, rendezvous)


def _run_each(function, arguments) -> list:
    results: list = [None] * len(arguments)
    errors: list = []

    def run(index):
        try:
            results[index] = function(arguments[index])
        except BaseException as error:  # noqa: BLE001 - asserted below
            errors.append(error)

    threads = [
        threading.Thread(target=run, args=(index,), daemon=True)
        for index in range(len(arguments))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(_WAIT_SECONDS * 2)
    assert not errors
    assert not any(thread.is_alive() for thread in threads)
    return results


def _is_project_memory_dir(dirpath, *_args, **_kwargs) -> bool:
    return str(dirpath).endswith(os.path.join('.tofu', 'memories'))


def test_listings_of_different_roots_scan_concurrently(
        scan_store, monkeypatch):
    import lib.memory.storage as storage
    import lib.memory.storage._files as memory_files

    for index, project in enumerate(scan_store):
        _write_memory(project / '.tofu' / 'memories', f'note-{index}')
    _require_overlap(
        monkeypatch, memory_files, '_sorted_visible_dir_entries',
        _is_project_memory_dir)

    listed = _run_each(
        lambda project: storage.get_eligible_memories(
            str(project), include_body=False, record_view='retrieval'),
        scan_store,
    )

    assert [[row['id'] for row in rows] for rows in listed] == [
        [f'note-{index}'] for index in range(_PROJECTS)]


def test_exact_id_probes_of_different_roots_run_concurrently(
        scan_store, monkeypatch):
    import lib.memory.storage as storage
    import lib.memory.storage._crud as crud

    for index, project in enumerate(scan_store):
        _write_memory(project / '.tofu' / 'memories', f'note-{index}')
    _require_overlap(
        monkeypatch, crud, '_memory_summary_from_id_in_dir',
        _is_project_memory_dir)

    loaded = _run_each(
        lambda project: storage.get_memory(
            f'note-{project.name.rsplit("-", 1)[1]}', str(project)),
        scan_store,
    )

    assert [memory['body'] for memory in loaded] == [
        f'body for note-{index}' for index in range(_PROJECTS)]


def test_skill_registry_scans_of_different_roots_run_concurrently(
        scan_store, monkeypatch):
    import lib.memory.storage as storage
    from lib.skills.registry import list_skills

    for index, project in enumerate(scan_store):
        _write_package(project / '.tofu' / 'skills', f'pkg-{index}')
    _require_overlap(
        monkeypatch, storage, '_memory_from_file',
        lambda filepath, *_args, **_kwargs: str(filepath).endswith(
            os.path.join('.tofu', 'skills', os.path.basename(
                os.path.dirname(filepath)), 'SKILL.md')))

    listed = _run_each(lambda project: list_skills(str(project)), scan_store)

    assert [[skill['id'] for skill in skills] for skills in listed] == [
        [f'pkg-{index}'] for index in range(_PROJECTS)]


def test_skill_changed_during_an_unlocked_scan_is_not_pinned(
        scan_store, monkeypatch):
    import lib.memory.storage as storage
    from lib.skills.registry import list_skills

    project = scan_store[0]
    package_dir = _write_package(project / '.tofu' / 'skills', 'evolving')
    skill_md = package_dir / 'SKILL.md'
    original_read = storage._memory_from_file
    edited = []

    def read_then_concurrent_edit(filepath, *args, **kwargs):
        record = original_read(filepath, *args, **kwargs)
        if not edited:
            # Another writer replaces the package right after this scan read
            # it; the registry cache must not adopt the new fingerprint for
            # the rows it already read.
            edited.append(filepath)
            skill_md.write_text(
                '---\nname: evolving\n'
                'description: edited while the registry was scanning\n'
                '---\n\nnew guide\n',
                encoding='utf-8',
            )
            stat = skill_md.stat()
            os.utime(skill_md, ns=(stat.st_atime_ns,
                                   stat.st_mtime_ns + 1_000_000_000))
        return record

    monkeypatch.setattr(storage, '_memory_from_file', read_then_concurrent_edit)

    first = list_skills(str(project))
    second = list_skills(str(project))

    assert first[0]['description'] == (
        'scan locking package fixture for evolving')
    assert second[0]['description'] == (
        'edited while the registry was scanning')


def test_pending_migration_blocks_only_its_own_root(scan_store, monkeypatch):
    import lib.memory.storage as storage
    import lib.memory.storage._dirs as storage_dirs

    migrated, pending = scan_store[0], scan_store[1]
    _write_memory(migrated / '.tofu' / 'memories', 'ready-note')
    _write_memory(pending / '.tofu' / 'skills', 'pre-split-note')
    storage_dirs._migrated_roots.discard(str(pending))
    entered = threading.Event()
    release = threading.Event()
    original_migrate = storage_dirs._migrate_project_flat_memories

    def slow_migrate(root):
        entered.set()
        assert release.wait(_WAIT_SECONDS)
        return original_migrate(root)

    monkeypatch.setattr(
        storage_dirs, '_migrate_project_flat_memories', slow_migrate)
    outcomes: dict = {}

    def list_pending(slot):
        outcomes[slot] = [
            (row['id'], row['filepath'])
            for row in storage.list_all_memories(str(pending))]

    migrating = threading.Thread(target=list_pending, args=('first',))
    migrating.start()
    assert entered.wait(_WAIT_SECONDS)
    waiting_started = threading.Event()

    def list_pending_after_start():
        waiting_started.set()
        list_pending('second')

    waiting = threading.Thread(target=list_pending_after_start)
    waiting.start()
    assert waiting_started.wait(_WAIT_SECONDS)

    started = time.monotonic()
    ready = storage.list_all_memories(str(migrated))
    assert time.monotonic() - started < _WAIT_SECONDS
    assert [row['id'] for row in ready] == ['ready-note']
    time.sleep(0.05)
    assert 'second' not in outcomes  # Its root's migration is still running.

    release.set()
    migrating.join(_WAIT_SECONDS)
    waiting.join(_WAIT_SECONDS)
    expected = [(
        'pre-split-note',
        str(pending / '.tofu' / 'memories' / 'pre-split-note.md'),
    )]
    assert outcomes == {'first': expected, 'second': expected}


def test_legacy_global_migration_never_exposes_a_partial_copy(
        scan_store, monkeypatch):
    import lib.memory.storage as storage
    import lib.memory.storage._dirs as storage_dirs

    observer, legacy_root = scan_store[0], scan_store[1]
    legacy_globals = legacy_root / '.tofu' / 'skills' / 'global'
    _write_memory(legacy_globals, 'legacy-note')
    _write_package(legacy_globals, 'legacy-pkg')
    storage_dirs._migrated_roots.discard(str(legacy_root))
    observed: list[list[str]] = []
    original_copy2 = shutil.copy2
    original_copytree = shutil.copytree

    def observe_concurrent_listing():
        # A different, already-migrated root scans the shared server stores
        # while the copy is half written.
        seen: list[str] = []
        reader = threading.Thread(target=lambda: seen.extend(
            row['id'] for row in storage.list_all_memories(str(observer))))
        reader.start()
        reader.join(_WAIT_SECONDS)
        assert not reader.is_alive()
        observed.append(seen)

    def torn_copy2(src, dst, *args, **kwargs):
        with open(dst, 'w', encoding='utf-8') as torn:
            torn.write('---\nname: legacy-note\n')
        observe_concurrent_listing()
        return original_copy2(src, dst, *args, **kwargs)

    def torn_copytree(src, dst, *args, **kwargs):
        os.makedirs(dst, exist_ok=True)
        with open(os.path.join(dst, 'SKILL.md'), 'w', encoding='utf-8') as torn:
            torn.write('---\nname: legacy-pkg\n')
        observe_concurrent_listing()
        kwargs['dirs_exist_ok'] = True
        return original_copytree(src, dst, *args, **kwargs)

    monkeypatch.setattr(storage_dirs.shutil, 'copy2', torn_copy2)
    monkeypatch.setattr(storage_dirs.shutil, 'copytree', torn_copytree)

    storage.run_storage_migrations(str(legacy_root))

    # The flat copy (sorted first) is complete before the package copy starts.
    assert observed == [[], ['legacy-note']]
    listed = {row['id']: row for row in storage.list_all_memories(str(observer))}
    assert listed['legacy-note']['description'] == (
        'scan locking fixture for legacy-note')
    assert listed['legacy-pkg']['is_package'] is True
    data_dir = Path(storage_dirs._server_data_dir())
    assert sorted(os.listdir(data_dir / 'memories' / 'global')) == [
        'legacy-note.md']
    assert sorted(os.listdir(data_dir / 'skills' / 'global')) == ['legacy-pkg']
