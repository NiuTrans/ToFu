"""Single-scan and stat-reuse contracts for memory directory listing.

The canonical union view must enumerate each physical store once, reuse each
flat DirEntry stat as the metadata-cache fingerprint, preserve flat-before-
package ordering and all hidden/symlink/global gates, and still invalidate
cached frontmatter after a durable file changes.
"""

from __future__ import annotations

import os
import tracemalloc
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit


@pytest.fixture()
def enumeration_store(tmp_path, monkeypatch):
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


def _write_flat(directory: Path, memory_id: str, name: str | None = None):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{memory_id}.md'
    path.write_text(
        '---\n'
        f'name: {name or memory_id}\n'
        f'description: enumeration fixture for {memory_id}\n'
        'enabled: true\n'
        '---\n\n'
        f'body for {memory_id}\n',
        encoding='utf-8',
    )
    return path


def _write_package(directory: Path, package_id: str):
    package_dir = directory / package_id
    package_dir.mkdir(parents=True, exist_ok=True)
    skill_path = package_dir / 'SKILL.md'
    skill_path.write_text(
        '---\n'
        f'name: {package_id}\n'
        f'description: package enumeration fixture for {package_id}\n'
        'enabled: true\n'
        '---\n\n'
        f'package body for {package_id}\n',
        encoding='utf-8',
    )
    return package_dir


def test_flat_store_is_scanned_once_and_reuses_fingerprint_stat(
        enumeration_store, monkeypatch):
    import lib.memory.storage._crud as crud
    import lib.memory.storage._files as memory_files

    memory_dir = enumeration_store / '.tofu' / 'memories'
    memory_count = 64
    for index in range(memory_count):
        _write_flat(memory_dir, f'memory-{index:03d}')

    original_scandir = memory_files.os.scandir
    original_fingerprint = memory_files._memory_file_fingerprint
    scanned_paths = []
    fingerprint_calls = 0

    def counted_scandir(path):
        scanned_paths.append(os.path.abspath(path))
        return original_scandir(path)

    def counted_fingerprint(path):
        nonlocal fingerprint_calls
        fingerprint_calls += 1
        return original_fingerprint(path)

    monkeypatch.setattr(memory_files.os, 'scandir', counted_scandir)
    monkeypatch.setattr(
        memory_files, '_memory_file_fingerprint', counted_fingerprint)
    target_dir = os.path.abspath(memory_dir)

    memory_files._metadata_cache.clear()
    cold = crud.list_all_memories(
        str(enumeration_store), include_body=False)
    assert len(cold) == memory_count
    assert scanned_paths.count(target_dir) == 1
    assert fingerprint_calls == memory_count

    # This test measures steady-state enumeration. Freshly written files are
    # deliberately quarantined from fingerprint cache hits for a short window
    # because coarse filesystem clocks can preserve every stat field across a
    # same-size rewrite.
    monkeypatch.setattr(memory_files._metadata_cache, '_clock_ns', lambda: 10**30)
    scans_before = len(scanned_paths)
    fingerprints_before = fingerprint_calls
    warm = crud.list_all_memories(
        str(enumeration_store), include_body=False)
    warm_scans = scanned_paths[scans_before:]
    assert len(warm) == memory_count
    assert warm_scans.count(target_dir) == 1
    assert fingerprint_calls - fingerprints_before == 0


def test_single_scan_preserves_flat_package_and_isolation_order(
        enumeration_store):
    import lib.memory.storage._files as memory_files

    store = enumeration_store / '.tofu' / 'memories'
    _write_flat(store, 'z-flat')
    _write_flat(store, 'a-flat')
    _write_flat(store, 'global')
    _write_package(store, 'z-package')
    _write_package(store, 'b-package')
    _write_package(store, 'global')
    _write_flat(store, '.hidden-flat')
    _write_package(store, '.hidden-package')
    (store / 'notes.txt').write_text('not a memory', encoding='utf-8')

    if hasattr(os, 'symlink'):
        try:
            (store / 'linked-flat.md').symlink_to(store / 'a-flat.md')
            (store / 'linked-package').symlink_to(store / 'b-package',
                                                   target_is_directory=True)
        except OSError:
            pass

    union = memory_files._list_memories_in_dir(
        str(store), scope='project', include_body=False)
    packages = memory_files._list_skill_packages_in_dir(
        str(store), scope='project', include_body=False)

    assert [memory['id'] for memory in union] == [
        'a-flat', 'global', 'z-flat', 'b-package', 'z-package']
    assert [memory['id'] for memory in packages] == [
        'b-package', 'z-package']
    assert all(not memory['id'].startswith('.') for memory in union)
    assert all('linked' not in memory['id'] for memory in union)


def test_recent_identical_fingerprint_is_not_trusted_after_frontmatter_change(
        enumeration_store, monkeypatch):
    import lib.memory.storage._files as memory_files

    memory_dir = enumeration_store / '.tofu' / 'memories'
    path = _write_flat(memory_dir, 'freshness', name='Before Change')
    recent_timestamp_ns = 1_000_000_000_000
    fixed_fingerprint = (
        1, 2, path.stat().st_size,
        recent_timestamp_ns, recent_timestamp_ns,
    )
    monkeypatch.setattr(
        memory_files, '_memory_file_fingerprint_from_stat',
        lambda _stat_result: fixed_fingerprint,
    )
    monkeypatch.setattr(
        memory_files, '_memory_file_fingerprint',
        lambda _path: fixed_fingerprint,
    )
    monkeypatch.setattr(
        memory_files._metadata_cache, '_clock_ns',
        lambda: recent_timestamp_ns,
    )
    first = memory_files._list_memories_in_dir(
        str(memory_dir), include_body=False)
    path.write_text(
        path.read_text(encoding='utf-8').replace(
            'Before Change', 'After Change!'),
        encoding='utf-8',
    )
    second = memory_files._list_memories_in_dir(
        str(memory_dir), include_body=False)

    assert first[0]['name'] == 'Before Change'
    assert second[0]['name'] == 'After Change!'
    assert memory_files._metadata_cache.snapshot()['unstable'] == 1


def test_unreadable_or_missing_directory_fails_soft(monkeypatch):
    import lib.memory.storage._files as memory_files

    def denied(_path):
        raise PermissionError('injected directory denial')

    monkeypatch.setattr(memory_files.os, 'scandir', denied)
    assert memory_files._list_memories_in_dir('/denied') == []
    assert memory_files._list_skill_packages_in_dir('/denied') == []


def test_retrieval_view_bounds_1365_record_residency(
        enumeration_store, monkeypatch):
    import lib.memory.storage._crud as crud
    import lib.memory.storage._files as memory_files

    memory_dir = enumeration_store / '.tofu' / 'memories'
    for index in range(1_365):
        _write_flat(memory_dir, f'memory-{index:04d}')

    # Populate the bounded frontmatter cache outside the measured region, then
    # model the settled read path used by per-turn retrieval.
    complete = crud.get_eligible_memories(
        str(enumeration_store), include_body=False)
    assert len(complete) == 1_365
    assert 'ineligible_reasons' in complete[0]
    del complete
    monkeypatch.setattr(
        memory_files._metadata_cache, '_clock_ns', lambda: 10**30)

    tracemalloc.start()
    try:
        retrieval = crud.get_eligible_memories(
            str(enumeration_store),
            include_body=False,
            record_view='retrieval',
        )
        current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(retrieval) == 1_365
    assert set(retrieval[0]) == {
        'id', 'name', 'description', 'enabled', 'tags', 'scope',
        'filepath', 'is_package', 'package_dir', 'eligible',
    }
    assert current_bytes < 1_000_000
    assert peak_bytes < 2_400_000


def test_invalid_record_view_fails_before_directory_io(monkeypatch):
    import lib.memory.storage._crud as crud

    monkeypatch.setattr(
        crud,
        '_list_memories_in_dir',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError('invalid record view reached directory I/O')),
    )

    with pytest.raises(ValueError, match='record_view'):
        crud.list_all_memories(record_view='wide')
    with pytest.raises(ValueError, match='include_body=False'):
        crud.list_all_memories(record_view='retrieval', include_body=True)
