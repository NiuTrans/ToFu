"""Direct-summary and revision contracts for memory CRUD operations.

Single-record operations resolve only their exact canonical path and never
materialize unrelated frontmatter or bodies. Merge resolves only its bounded
source set, and revision changes preserve the newer source instead of
overwriting or deleting it.
"""

from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.unit


@pytest.fixture()
def isolated_memory_store(tmp_path, monkeypatch):
    import lib.memory.storage._dirs as storage_dirs
    import lib.memory.storage._files as memory_files

    data_dir = tmp_path / 'data'
    project_dir = tmp_path / 'project'
    (project_dir / '.tofu' / 'skills').mkdir(parents=True)
    monkeypatch.setenv('TOFU_DATA_DIR', str(data_dir))
    monkeypatch.setattr(
        storage_dirs, '_server_data_dir', lambda: str(data_dir))
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False
    memory_files._metadata_cache.clear()
    yield project_dir
    memory_files._metadata_cache.clear()
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False


def _populate(project_path: str, count: int = 12):
    from lib.memory.storage import create_memory

    memories = []
    for index in range(count):
        memories.append(create_memory(
            name=f'lesson-{index}',
            description=f'bounded CRUD lesson number {index}',
            body=(f'body-{index}-' + 'x' * 10_000),
            tags=[f'tag-{index}'],
            scope='project',
            project_path=project_path,
        ))
    return memories


def _record_memory_reads(monkeypatch):
    import lib.memory.storage._files as memory_files

    original = memory_files._read_memory_source
    reads = {'metadata': [], 'full': [], 'bounded': []}

    def recorded(filepath, *, include_body=True, body_char_limit=None):
        if not include_body:
            reads['metadata'].append(filepath)
        elif body_char_limit is None:
            reads['full'].append(filepath)
        else:
            reads['bounded'].append((filepath, body_char_limit))
        return original(
            filepath,
            include_body=include_body,
            body_char_limit=body_char_limit,
        )

    memory_files._metadata_cache.clear()
    monkeypatch.setattr(
        memory_files._metadata_cache, '_clock_ns', lambda: 10**30)
    monkeypatch.setattr(memory_files, '_read_memory_source', recorded)
    return reads


@pytest.mark.parametrize('operation,expects_target_body', [
    ('get', True),
    ('update', True),
    ('delete', False),
    ('toggle', True),
    ('clear', False),
])
def test_crud_reads_only_target_metadata_and_at_most_one_full_body(
        isolated_memory_store, monkeypatch, operation, expects_target_body):
    from lib.memory.storage import (
        clear_memories,
        delete_memory,
        get_memory,
        toggle_memory,
        update_memory,
    )

    project_path = str(isolated_memory_store)
    memories = _populate(project_path)
    target = memories[5]
    reads = _record_memory_reads(monkeypatch)

    if operation == 'get':
        assert get_memory(target['id'], project_path=project_path)['id'] == target['id']
    elif operation == 'update':
        assert update_memory(
            target['id'], {'description': 'updated bounded CRUD lesson'},
            project_path=project_path,
        )['description'] == 'updated bounded CRUD lesson'
    elif operation == 'delete':
        assert delete_memory(target['id'], project_path=project_path)
    elif operation == 'toggle':
        assert toggle_memory(
            target['id'], enabled=False,
            project_path=project_path,
        )['enabled'] is False
    else:
        assert clear_memories(project_path=project_path)['total'] == len(memories)

    if operation == 'clear':
        assert len(reads['metadata']) == len(memories)
        assert set(reads['metadata']) == {
            memory['filepath'] for memory in memories}
    else:
        assert reads['metadata'] == [target['filepath']]
    expected_full = [target['filepath']] if expects_target_body else []
    assert reads['full'] == expected_full
    assert reads['bounded'] == []


def test_merge_resolves_only_source_metadata_and_reads_no_source_bodies(
        isolated_memory_store, monkeypatch):
    import lib.memory.storage._crud as crud
    from lib.memory.contracts import MEMORY_MERGE_MAX_ITEMS

    project_path = str(isolated_memory_store)
    sources = _populate(project_path, count=MEMORY_MERGE_MAX_ITEMS)
    reads = _record_memory_reads(monkeypatch)
    original_list = crud.list_all_memories
    list_calls = 0

    def counted_list(*args, **kwargs):
        nonlocal list_calls
        list_calls += 1
        return original_list(*args, **kwargs)

    monkeypatch.setattr(crud, 'list_all_memories', counted_list)
    result = crud.merge_memories(
        [memory['id'] for memory in sources],
        name='merged lessons',
        description='consolidated bounded CRUD lessons',
        body='one replacement body',
        tags=None,
        scope='project',
        project_path=project_path,
    )

    assert list_calls == 0
    assert reads['metadata'] == [memory['filepath'] for memory in sources]
    assert reads['full'] == []
    assert reads['bounded'] == []
    assert result['deleted_ids'] == [memory['id'] for memory in sources]
    assert result['failed_ids'] == []
    assert result['merged_memory']['tags'] == sorted(
        f'tag-{index}' for index in range(len(sources)))
    assert Path(result['merged_memory']['filepath']).is_file()
    assert not any(Path(memory['filepath']).exists() for memory in sources)


def test_update_revision_conflict_preserves_external_change(
        isolated_memory_store, monkeypatch):
    import lib.memory.storage._crud as crud

    project_path = str(isolated_memory_store)
    memory = _populate(project_path, count=1)[0]
    original_hydrate = crud._hydrate_memory_at_revision

    def hydrate_then_change(summary, revision):
        hydrated = original_hydrate(summary, revision)
        path = Path(hydrated['filepath'])
        path.write_text(
            path.read_text(encoding='utf-8') + '\nexternal concurrent edit\n',
            encoding='utf-8',
        )
        return hydrated

    monkeypatch.setattr(
        crud, '_hydrate_memory_at_revision', hydrate_then_change)

    with pytest.raises(crud.MemoryRevisionConflict, match='retry'):
        crud.update_memory(
            memory['id'], {'body': 'stale overwrite'},
            project_path=project_path,
        )

    text = Path(memory['filepath']).read_text(encoding='utf-8')
    assert 'external concurrent edit' in text
    assert 'stale overwrite' not in text


def test_merge_preserves_source_changed_after_replacement_creation(
        isolated_memory_store, monkeypatch):
    import lib.memory.storage._crud as crud

    project_path = str(isolated_memory_store)
    first, second = _populate(project_path, count=2)
    original_create = crud.create_memory

    def create_then_change_source(**kwargs):
        merged = original_create(**kwargs)
        path = Path(first['filepath'])
        path.write_text(
            path.read_text(encoding='utf-8') + '\nnewer source revision\n',
            encoding='utf-8',
        )
        return merged

    monkeypatch.setattr(crud, 'create_memory', create_then_change_source)
    result = crud.merge_memories(
        [first['id'], second['id']],
        name='replacement',
        description='replacement after revision-safe merge',
        body='replacement body',
        scope='project',
        project_path=project_path,
    )

    assert result['failed_ids'] == [first['id']]
    assert result['deleted_ids'] == [second['id']]
    assert Path(first['filepath']).is_file()
    assert 'newer source revision' in Path(first['filepath']).read_text(
        encoding='utf-8')
    assert not Path(second['filepath']).exists()


@pytest.mark.api
def test_revision_conflict_is_http_409(flask_client, monkeypatch):
    import lib.memory.storage as storage

    def conflict(*_args, **_kwargs):
        raise storage.MemoryRevisionConflict('memory changed; retry')

    monkeypatch.setattr(storage, 'update_memory', conflict)
    response = flask_client.put(
        '/api/v1/memory/revision-test', json={'body': 'new body'})

    assert response.status_code == 409
    assert 'retry' in str(response.get_json().get('error', ''))


@pytest.mark.api
def test_toggle_rejects_non_boolean_enabled(flask_client):
    response = flask_client.post(
        '/api/v1/memory/missing/toggle', json={'enabled': 'false'})

    assert response.status_code == 400
    assert 'boolean' in str(response.get_json().get('error', ''))
