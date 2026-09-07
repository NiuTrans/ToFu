"""Executable resource and wire contracts for durable memory operations.

The tests prove invalid model/API payloads fail before corpus scans or writes,
legacy direct-call search clamping remains compatible, generated Unicode IDs
fit filesystem components, and the five resident schemas stay bounded without
losing their task-selection semantics.
"""

from __future__ import annotations

import copy
from pathlib import Path

from jsonschema import Draft7Validator
import pytest

from lib.context_telemetry import tool_schema_tokens
from lib.memory.contracts import (
    MEMORY_BODY_MAX_CHARS,
    MEMORY_DESCRIPTION_MAX_CHARS,
    MEMORY_GENERATED_ID_MAX_BYTES,
    MEMORY_ID_MAX_CHARS,
    MEMORY_MERGE_MAX_ITEMS,
    MEMORY_NAME_MAX_CHARS,
    MEMORY_SEARCH_QUERY_MAX_CHARS,
    MEMORY_SEARCH_TOP_K_DEFAULT,
    MEMORY_SEARCH_TOP_K_MAX,
    MEMORY_SEARCH_TOP_K_MIN,
    MEMORY_TAG_MAX_CHARS,
    MEMORY_TAG_MAX_ITEMS,
)
from lib.memory.tools import ALL_MEMORY_TOOLS
from lib.tools.gateway import sanitize_wire_tools


pytestmark = pytest.mark.unit


@pytest.fixture()
def isolated_memory_store(tmp_path, monkeypatch):
    """Keep global migration scans and all writes inside one temporary root."""
    import lib.memory.storage._dirs as storage_dirs

    data_dir = tmp_path / 'data'
    project_dir = tmp_path / 'project'
    (project_dir / '.tofu' / 'skills').mkdir(parents=True)
    monkeypatch.setenv('TOFU_DATA_DIR', str(data_dir))
    monkeypatch.setattr(
        storage_dirs, '_server_data_dir', lambda: str(data_dir))
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False
    yield project_dir
    storage_dirs._migrated_roots.clear()
    storage_dirs._server_store_migrated = False


def _project_payload(**overrides):
    payload = {
        'name': 'Bounded lesson',
        'description': 'reproduced boundary lesson with concrete evidence',
        'body': 'Verified body.',
        'tags': ['memory', 'bounds'],
        'scope': 'project',
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize('override,error_fragment', [
    ({'name': 'n' * (MEMORY_NAME_MAX_CHARS + 1)}, 'name'),
    ({'description': 'd' * (MEMORY_DESCRIPTION_MAX_CHARS + 1)},
     'description'),
    ({'body': 'b' * (MEMORY_BODY_MAX_CHARS + 1)}, 'body'),
    ({'tags': ['t'] * (MEMORY_TAG_MAX_ITEMS + 1)}, 'tags'),
    ({'tags': ['t' * (MEMORY_TAG_MAX_CHARS + 1)]}, r'tags\[0\]'),
    ({'tags': ['same', 'same']}, 'duplicate'),
    ({'name': 'frontmatter\ninjection'}, 'single line'),
    ({'tags': ['one,two']}, 'comma'),
    ({'scope': 'tenant-global'}, 'scope'),
])
def test_create_rejects_oversized_or_ambiguous_payload_before_writing(
        isolated_memory_store, override, error_fragment):
    from lib.memory.storage import create_memory

    with pytest.raises(ValueError, match=error_fragment):
        create_memory(
            project_path=str(isolated_memory_store),
            **_project_payload(**override),
        )

    assert not list(isolated_memory_store.rglob('*.md'))


def test_create_accepts_exact_limits_and_caps_unicode_filename_bytes(
        isolated_memory_store):
    from lib.memory.storage import create_memory

    memory = create_memory(
        project_path=str(isolated_memory_store),
        **_project_payload(
            name='记' * MEMORY_NAME_MAX_CHARS,
            description='d' * MEMORY_DESCRIPTION_MAX_CHARS,
            body='b' * MEMORY_BODY_MAX_CHARS,
            tags=['t' * MEMORY_TAG_MAX_CHARS],
        ),
    )

    assert memory['name'] == '记' * MEMORY_NAME_MAX_CHARS
    assert len(memory['id'].encode('utf-8')) <= MEMORY_GENERATED_ID_MAX_BYTES
    assert Path(memory['filepath']).is_file()


def test_oversized_update_preserves_existing_file_exactly(
        isolated_memory_store):
    from lib.memory.storage import create_memory, update_memory

    memory = create_memory(
        project_path=str(isolated_memory_store), **_project_payload())
    path = Path(memory['filepath'])
    before = path.read_bytes()

    with pytest.raises(ValueError, match='body'):
        update_memory(
            memory['id'], {'body': 'x' * (MEMORY_BODY_MAX_CHARS + 1)},
            project_path=str(isolated_memory_store),
        )

    assert path.read_bytes() == before


@pytest.mark.api
def test_create_api_surfaces_resource_rejection_as_bad_request(flask_client):
    response = flask_client.post('/api/v1/memory', json={
        'name': 'n' * (MEMORY_NAME_MAX_CHARS + 1),
        'description': 'bounded API failure',
        'body': 'must not be written',
        'scope': 'global',
    })

    assert response.status_code == 400
    assert 'name exceeds' in str(response.get_json().get('error', ''))


def test_invalid_mutators_fail_before_full_corpus_scan(monkeypatch):
    import lib.memory.storage._crud as crud

    def unexpected_scan(*_args, **_kwargs):
        raise AssertionError('invalid payload reached list_all_memories')

    monkeypatch.setattr(crud, 'list_all_memories', unexpected_scan)

    with pytest.raises(ValueError, match='body'):
        crud.update_memory(
            'valid-id', {'body': 'x' * (MEMORY_BODY_MAX_CHARS + 1)})
    with pytest.raises(ValueError, match='unique'):
        crud.merge_memories(
            ['same', 'same'], **_project_payload())
    with pytest.raises(ValueError, match='at most'):
        crud.merge_memories(
            [f'm{i}' for i in range(MEMORY_MERGE_MAX_ITEMS + 1)],
            **_project_payload())
    with pytest.raises(ValueError, match='memory_id'):
        crud.delete_memory('x' * (MEMORY_ID_MAX_CHARS + 1))


def test_merge_omitted_tags_unions_sources_and_deletes_them(
        isolated_memory_store):
    from lib.memory.storage import create_memory, get_memory, merge_memories

    project_path = str(isolated_memory_store)
    first = create_memory(
        project_path=project_path,
        **_project_payload(name='First', tags=['alpha']))
    second = create_memory(
        project_path=project_path,
        **_project_payload(name='Second', tags=['beta']))
    # Legacy hand-written single tags parse as a scalar. The merge must treat
    # it as one tag rather than unioning the string's individual characters.
    first_path = Path(first['filepath'])
    first_path.write_text(
        first_path.read_text(encoding='utf-8').replace(
            'tags: [alpha]', 'tags: alpha'),
        encoding='utf-8',
    )

    result = merge_memories(
        [first['id'], second['id']],
        project_path=project_path,
        **_project_payload(name='Merged', tags=None),
    )

    assert result['merged_memory']['tags'] == ['alpha', 'beta']
    assert result['deleted_ids'] == [first['id'], second['id']]
    assert result['failed_ids'] == []
    assert get_memory(first['id'], project_path=project_path) is None
    assert get_memory(second['id'], project_path=project_path) is None


def test_invalid_or_empty_search_skips_corpus_loading(monkeypatch):
    import lib.memory.storage as storage
    from lib.memory.relevance import search_memories
    from lib.memory.relevance._search import search_memories_scored

    def unexpected_load(*_args, **_kwargs):
        raise AssertionError('invalid search loaded the memory corpus')

    monkeypatch.setattr(storage, 'get_eligible_memories', unexpected_load)
    monkeypatch.setattr(storage, 'iter_eligible_memories', unexpected_load)

    assert search_memories('') == 'Please provide search keywords.'
    assert search_memories('the and this') == (
        'No valid search terms after tokenization.')
    assert search_memories_scored('   ') == []
    with pytest.raises(ValueError, match='query'):
        search_memories('q' * (MEMORY_SEARCH_QUERY_MAX_CHARS + 1))
    with pytest.raises(ValueError, match='top_k'):
        search_memories('bounded query', top_k='50')


def test_direct_search_top_k_keeps_legacy_clamp(monkeypatch):
    import lib.memory.storage as storage
    from lib.memory.relevance._search import search_memories_scored

    memories = [
        {
            'name': f'needle {index}',
            'description': 'needle search result',
            'tags': [],
            'body': '',
            'scope': 'project',
        }
        for index in range(MEMORY_SEARCH_TOP_K_MAX + 5)
    ]
    monkeypatch.setattr(
        storage, 'iter_eligible_memories',
        lambda *_args, **_kwargs: iter(memories))

    assert len(search_memories_scored('needle', top_k=0)) == 1
    assert len(search_memories_scored('needle', top_k=10_000)) == 50


def test_wire_schemas_share_limits_and_reject_costly_shapes():
    by_name = {tool['function']['name']: tool for tool in ALL_MEMORY_TOOLS}
    schemas = {
        name: tool['function']['parameters']
        for name, tool in by_name.items()
    }
    for schema in schemas.values():
        Draft7Validator.check_schema(schema)
        assert schema['additionalProperties'] is False

    create = schemas['create_memory']['properties']
    assert create['name']['maxLength'] == MEMORY_NAME_MAX_CHARS
    assert create['description']['maxLength'] == MEMORY_DESCRIPTION_MAX_CHARS
    assert create['body']['maxLength'] == MEMORY_BODY_MAX_CHARS
    assert create['tags']['maxItems'] == MEMORY_TAG_MAX_ITEMS
    assert create['tags']['items']['maxLength'] == MEMORY_TAG_MAX_CHARS

    merge_ids = schemas['merge_memories']['properties']['memory_ids']
    assert merge_ids['minItems'] == 2
    assert merge_ids['maxItems'] == MEMORY_MERGE_MAX_ITEMS
    assert merge_ids['uniqueItems'] is True

    search = schemas['search_memories']['properties']
    assert search['query']['maxLength'] == MEMORY_SEARCH_QUERY_MAX_CHARS
    assert search['top_k'] == {
        'type': 'integer',
        'minimum': MEMORY_SEARCH_TOP_K_MIN,
        'maximum': MEMORY_SEARCH_TOP_K_MAX,
        'default': MEMORY_SEARCH_TOP_K_DEFAULT,
        'description': 'Maximum results.',
    }

    create_validator = Draft7Validator(schemas['create_memory'])
    assert create_validator.is_valid({
        'name': 'lesson', 'description': 'dense trigger', 'body': 'evidence'})
    assert not create_validator.is_valid({
        'name': 'lesson', 'description': '', 'body': 'evidence'})
    assert not create_validator.is_valid({
        'name': 'lesson', 'description': 'dense trigger', 'body': 'evidence',
        'unknown': True})
    merge_validator = Draft7Validator(schemas['merge_memories'])
    assert not merge_validator.is_valid({
        'memory_ids': ['same', 'same'], 'name': 'merged',
        'description': 'dense trigger', 'body': 'evidence'})


def test_memory_family_is_provider_stable_and_below_resident_budget():
    schemas = copy.deepcopy(ALL_MEMORY_TOOLS)

    assert sanitize_wire_tools(schemas) is schemas
    assert tool_schema_tokens(ALL_MEMORY_TOOLS) <= 910

    descriptions = {
        tool['function']['name']: tool['function']['description']
        for tool in ALL_MEMORY_TOOLS
    }
    assert 'My Context' in descriptions['create_memory']
    assert 'delete originals' in descriptions['merge_memories']
    assert 'local files' in descriptions['search_memories']
    assert 'load_skill' in descriptions['search_memories']
