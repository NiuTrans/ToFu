"""Canonical direct-path lookup contracts for durable memories.

Single-ID CRUD and bounded merge source resolution must scale with visible
workspace roots, not corpus size.  Direct probes share the listing authority's
store order, preserve legacy migrations/package semantics, validate basenames
before path construction, and read no unrelated frontmatter or bodies.
"""

from __future__ import annotations

import os
import shutil
import tracemalloc
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit


@pytest.fixture()
def direct_store(tmp_path, monkeypatch):
    import lib.memory.storage._dirs as storage_dirs
    import lib.memory.storage._files as memory_files

    data_dir = tmp_path / 'data'
    primary = tmp_path / 'primary'
    extra = tmp_path / 'extra'
    (primary / '.tofu').mkdir(parents=True)
    (extra / '.tofu').mkdir(parents=True)
    monkeypatch.setenv('TOFU_DATA_DIR', str(data_dir))
    monkeypatch.setattr(
        storage_dirs, '_server_data_dir', lambda: str(data_dir))
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False
    memory_files._metadata_cache.clear()
    yield data_dir, primary, extra
    memory_files._metadata_cache.clear()
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False


def _write_flat(directory: Path, memory_id: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{memory_id}.md'
    path.write_text(
        '---\n'
        f'name: {memory_id}\n'
        f'description: direct lookup fixture for {memory_id}\n'
        'enabled: true\n'
        'tags: [lookup]\n'
        '---\n\n'
        f'{body}\n',
        encoding='utf-8',
    )
    return path


def _write_package(directory: Path, memory_id: str, body: str) -> Path:
    package_dir = directory / memory_id
    package_dir.mkdir(parents=True, exist_ok=True)
    _write_flat(package_dir, 'SKILL', body)
    return package_dir


def _mark_migrations_complete(primary: Path, extra: Path | None = None):
    import lib.memory.storage._dirs as storage_dirs

    storage_dirs._migrated_roots.add(str(primary))
    if extra is not None:
        storage_dirs._migrated_roots.add(str(extra))
    storage_dirs._server_store_migrated = True


def test_single_get_reads_only_exact_target_in_1365_file_corpus(
        direct_store, monkeypatch):
    import lib.memory.storage._crud as crud
    import lib.memory.storage._files as memory_files

    _data_dir, primary, _extra = direct_store
    memory_dir = primary / '.tofu' / 'memories'
    for index in range(1_365):
        _write_flat(memory_dir, f'memo_{index}', 'x' * 2_000)
    _mark_migrations_complete(primary)

    original_read = memory_files._read_memory_source
    reads = {'metadata': [], 'full': [], 'returned_chars': 0}

    def recorded(filepath, *, include_body=True, body_char_limit=None):
        value = original_read(
            filepath,
            include_body=include_body,
            body_char_limit=body_char_limit,
        )
        reads['full' if include_body else 'metadata'].append(filepath)
        reads['returned_chars'] += len(value)
        return value

    def unexpected_enumeration(*_args, **_kwargs):
        raise AssertionError('single-ID lookup enumerated a memory directory')

    memory_files._metadata_cache.clear()
    monkeypatch.setattr(
        memory_files._metadata_cache, '_clock_ns', lambda: 10**30)
    monkeypatch.setattr(memory_files, '_read_memory_source', recorded)
    monkeypatch.setattr(crud.os, 'listdir', unexpected_enumeration)
    monkeypatch.setattr(crud, 'list_all_memories', unexpected_enumeration)
    tracemalloc.start()
    try:
        memory = crud.get_memory('memo_1364', project_path=str(primary))
        _current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    target = str(memory_dir / 'memo_1364.md')
    assert memory is not None and memory['id'] == 'memo_1364'
    assert reads['metadata'] == [target]
    assert reads['full'] == [target]
    assert reads['returned_chars'] < 2_500
    assert peak_bytes < 128 * 1_024


def test_selected_hydration_does_not_rebuild_1365_file_corpus(
        direct_store, monkeypatch):
    import lib.memory.storage._crud as crud
    import lib.memory.storage._files as memory_files

    _data_dir, primary, _extra = direct_store
    memory_dir = primary / '.tofu' / 'memories'
    for index in range(1_365):
        _write_flat(memory_dir, f'memo_{index}', 'x' * 2_000)
    _mark_migrations_complete(primary)

    original_read = memory_files._read_memory_source
    reads = {'metadata': [], 'body': []}

    def recorded(filepath, *, include_body=True, body_char_limit=None):
        reads['body' if include_body else 'metadata'].append(filepath)
        return original_read(
            filepath,
            include_body=include_body,
            body_char_limit=body_char_limit,
        )

    def unexpected_enumeration(*_args, **_kwargs):
        raise AssertionError('selected-ID hydration enumerated the corpus')

    memory_files._metadata_cache.clear()
    monkeypatch.setattr(
        memory_files._metadata_cache, '_clock_ns', lambda: 10**30)
    monkeypatch.setattr(memory_files, '_read_memory_source', recorded)
    monkeypatch.setattr(crud, '_list_memories_in_dir', unexpected_enumeration)
    tracemalloc.start()
    try:
        loaded = crud.load_eligible_memories(
            ['memo_1', 'memo_1364'],
            project_path=str(primary),
            body_char_limit=2_000,
        )
        _current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    targets = [
        str(memory_dir / 'memo_1.md'),
        str(memory_dir / 'memo_1364.md'),
    ]
    assert [memory['id'] for memory in loaded] == ['memo_1', 'memo_1364']
    assert reads == {'metadata': targets, 'body': targets}
    assert peak_bytes < 192 * 1_024


def test_direct_lookup_preserves_complete_store_precedence(direct_store):
    from lib.memory.storage import get_memory

    data_dir, primary, extra = direct_store
    _mark_migrations_complete(primary, extra)
    server_skills = data_dir / 'skills' / 'global'
    server_memories = data_dir / 'memories' / 'global'
    legacy_global = primary / '.tofu' / 'skills' / 'global'
    primary_memories = primary / '.tofu' / 'memories'
    primary_skills = primary / '.tofu' / 'skills'
    extra_memories = extra / '.tofu' / 'memories'

    server_skills_flat = _write_flat(
        server_skills, 'priority', 'server-skills-flat')
    server_skills_package = _write_package(
        server_skills, 'priority', 'server-skills-package')
    server_memory = _write_flat(
        server_memories, 'priority', 'server-memory')
    primary_legacy = _write_flat(
        legacy_global, 'priority', 'primary-legacy-global')
    primary_memory = _write_flat(
        primary_memories, 'priority', 'primary-memory')
    primary_package = _write_package(
        primary_skills, 'priority', 'primary-package')
    _write_flat(extra_memories, 'priority', 'extra-memory')

    def selected_body():
        memory = get_memory(
            'priority', project_path=str(primary), extra_paths=[str(extra)])
        assert memory is not None
        return memory['body'], memory['scope'], memory['is_package']

    assert selected_body() == ('server-skills-flat', 'global', False)
    server_skills_flat.unlink()
    assert selected_body() == ('server-skills-package', 'global', True)
    shutil.rmtree(server_skills_package)
    assert selected_body() == ('server-memory', 'global', False)
    server_memory.unlink()
    assert selected_body() == ('primary-legacy-global', 'global', False)
    primary_legacy.unlink()
    assert selected_body() == ('primary-memory', 'project', False)
    primary_memory.unlink()
    assert selected_body() == ('primary-package', 'project', True)
    shutil.rmtree(primary_package)
    assert selected_body() == ('extra-memory', 'project', False)


@pytest.mark.parametrize('memory_id', [
    '../escape',
    'nested/name',
    'windows\\name',
    '\x00invalid',
    '.',
    '..',
])
def test_invalid_memory_basename_rejected_before_storage_probe(
        memory_id, monkeypatch):
    import lib.memory.storage._crud as crud

    def unexpected_probe(*_args, **_kwargs):
        raise AssertionError('invalid memory ID reached storage discovery')

    monkeypatch.setattr(crud, '_iter_memory_store_dirs', unexpected_probe)
    assert crud.get_memory(memory_id) is None
    with pytest.raises(ValueError, match='basename'):
        crud.update_memory(memory_id, {})
    with pytest.raises(ValueError, match='basename'):
        crud.delete_memory(memory_id)
    with pytest.raises(ValueError, match='basename'):
        crud.toggle_memory(memory_id)


def test_direct_lookup_runs_legacy_migrations_without_corpus_listing(
        direct_store, monkeypatch):
    import lib.memory.storage._crud as crud

    data_dir, primary, _extra = direct_store
    legacy_skills = primary / '.tofu' / 'skills'
    _write_flat(legacy_skills, 'legacy_project', 'legacy-project-body')
    _write_flat(
        legacy_skills / 'global', 'legacy_global', 'legacy-global-body')

    def unexpected_listing(*_args, **_kwargs):
        raise AssertionError('direct lookup called list_all_memories')

    monkeypatch.setattr(crud, 'list_all_memories', unexpected_listing)
    project_memory = crud.get_memory(
        'legacy_project', project_path=str(primary))
    global_memory = crud.get_memory(
        'legacy_global', project_path=str(primary))

    assert project_memory is not None
    assert project_memory['body'] == 'legacy-project-body'
    assert global_memory is not None
    assert global_memory['body'] == 'legacy-global-body'
    assert (primary / '.tofu' / 'memories' / 'legacy_project.md').is_file()
    assert (data_dir / 'memories' / 'global' / 'legacy_global.md').is_file()


def test_missing_id_probes_roots_without_reading_documents(
        direct_store, monkeypatch):
    import lib.memory.storage._crud as crud
    import lib.memory.storage._files as memory_files

    _data_dir, primary, extra = direct_store
    _write_flat(primary / '.tofu' / 'memories', 'present', 'body')
    _write_flat(extra / '.tofu' / 'memories', 'other', 'body')
    _mark_migrations_complete(primary, extra)

    def unexpected_read(*_args, **_kwargs):
        raise AssertionError('missing ID read an unrelated document')

    def unexpected_list(*_args, **_kwargs):
        raise AssertionError('missing ID enumerated a directory')

    monkeypatch.setattr(memory_files, '_read_memory_source', unexpected_read)
    monkeypatch.setattr(crud.os, 'listdir', unexpected_list)
    assert crud.get_memory(
        'absent', project_path=str(primary), extra_paths=[str(extra)]) is None
