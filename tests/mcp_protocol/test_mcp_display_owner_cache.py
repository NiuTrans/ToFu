"""MCP presentation metadata stays within its explicit execution owner."""
import json
import pytest

from lib.mcp import project_names as names
from lib.tasks_pkg.tool_display import _build_tool_round_entry

pytestmark = pytest.mark.unit
PID = '6a1e782e9ba0ae3d7727a668'


@pytest.fixture(autouse=True)
def clear_names():
    names.clear_cache()
    yield
    names.clear_cache()


def _round(owner):
    return _build_tool_round_entry('mcp__overleaf__list_files', {'project_id':PID},
                                  'call', '{}', 0, False, task={'_userId':owner})[1]


def test_other_owner_does_not_receive_cached_project_name():
    names.ingest_tool_result('mcp__overleaf__list_projects', {},
                             json.dumps([{'id':PID, 'name':'Private owner A project'}]), user_id=1)
    assert 'Private owner A project' not in _round(2)['query']

    assert 'Private owner A project' in _round(1)['query']


def test_private_urls_and_doc_titles_stay_with_owner():
    names.ingest_tool_result('mcp__xuecheng__read_doc', {'doc': '1234567'},
        '{"title":"Private document","url":"https://private.example/page/1234567"}', user_id=1)
    assert names.get_doc_title('1234567', user_id=1) == 'Private document'
    assert names.get_doc_url('1234567', user_id=1) == 'https://private.example/page/1234567'
    assert names.get_doc_url('7654321', user_id=1).startswith('https://private.example/')
    for owner in (None, 2):
        assert names.get_doc_title('1234567', user_id=owner) == ''
        assert names.get_doc_url('1234567', user_id=owner).startswith('https://km.internal.example.com/')


def test_ingestion_requires_valid_explicit_owner():
    for owner in (None, 0, True, -1):
        with pytest.raises(ValueError):
            names.ingest_tool_result('mcp__overleaf__list_projects', {}, [], user_id=owner)


def test_cache_capacity_is_shared_across_owners_and_value_sizes(monkeypatch):
    import tracemalloc
    monkeypatch.setattr(names, '_MAX_ENTRIES', 256)
    title = '私' * 1_000_000
    tracemalloc.start()
    try:
        for owner in range(1, 3001):
            names.set_project_name(PID, title, user_id=owner)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert names.get_project_name(PID, user_id=1) == ''
    assert names.get_project_name(PID, user_id=3000) == title[:512]
    assert peak < 2 * 1024 * 1024


def test_structured_results_bound_traversal_and_do_not_cross_namespaces():
    cyclic = []
    cyclic.append(cyclic)
    names.ingest_tool_result('mcp__overleaf__list_projects', {}, cyclic, user_id=1)
    result = [{'irrelevant': i} for i in range(300)]
    result.append({'id': PID, 'name': 'Beyond traversal budget'})
    names.ingest_tool_result('mcp__overleaf__list_projects', {}, result, user_id=1)
    assert names.get_project_name(PID, user_id=1) == ''
    names.ingest_tool_result('mcp__xuecheng__read_doc', {}, {'id': PID, 'name': 'Wrong namespace'}, user_id=1)
    assert names.get_project_name(PID, user_id=1) == ''


@pytest.mark.parametrize('base', ['https://user:secret@private.example', 'https://private.example:99999', 'https://private.example\"onclick=\"bad'])
def test_unsafe_learned_url_is_not_rendered(base):
    names.ingest_tool_result('mcp__overleaf__create_project', {}, f'{base}/project/{PID}', user_id=1)
    assert names.get_project_url(PID, user_id=1) == f'https://www.overleaf.com/project/{PID}'


def test_successful_config_update_invalidates_only_its_owner(tmp_path, monkeypatch):
    from lib.mcp import config
    monkeypatch.setattr(config, '_CONFIG_DIR', str(tmp_path))
    for owner in (1, 2):
        names.set_project_name(PID, 'Private cached name', user_id=owner)
    config.upsert_server('overleaf', {'enabled': False}, user_id=1)
    assert names.get_project_name(PID, user_id=1) == ''
    assert names.get_project_name(PID, user_id=2) == 'Private cached name'


def test_failed_config_update_does_not_invalidate_cache(tmp_path, monkeypatch):
    from lib.mcp import config
    monkeypatch.setattr(config, '_CONFIG_DIR', str(tmp_path))
    names.set_project_name(PID, 'Preserved', user_id=1)
    def fail(*args, **kwargs):
        raise OSError('injected write failure')
    monkeypatch.setattr(config, 'update_json_atomic', fail)
    with pytest.raises(OSError):
        config.upsert_server('overleaf', {'enabled': False}, user_id=1)
    assert names.get_project_name(PID, user_id=1) == 'Preserved'
