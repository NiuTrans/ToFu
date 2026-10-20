"""Tool Search cache replay preserves UI projection without changing model JSON.

The harness follows test_web_search_cache_hit_diag.py: execute the actual
pipeline and capture its finalized events, never substitute a display helper.
"""

from __future__ import annotations

import json
import threading
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.unit

_ARGS = {'query': 'read_files'}
_SCHEMA = {
    'type': 'object',
    'properties': {
        'path': {'type': 'string', 'description': 'Exact file path'},
        'limit': {'type': 'integer', 'default': 5, 'minimum': 1},
    },
    'required': ['path'],
    'additionalProperties': False,
}
_ITEM = {
    'name': 'read_files', 'namespace': 'project',
    'description': 'Read project files.', 'arguments_schema': _SCHEMA,
    'score': 0.9,
}
_MATCH = {
    'type': 'tool_catalog_match', 'toolName': 'read_files',
    'title': 'read_files', 'namespace': 'project',
    'snippet': 'Read project files.', 'score': 0.9,
    'arguments': [
        {'name': 'path', 'type': 'string', 'required': True},
        {'name': 'limit', 'type': 'integer', 'required': False},
    ],
}


def _payload(**overrides):
    result = {'status': 'ok', 'query': _ARGS['query'], 'items': [_ITEM],
              'total': 1, 'next_cursor': None, 'execute_with': 'execute_tools'}
    result.update(overrides)
    return result


def _task(**overrides):
    task = {
        'id': 'tool-search-cache-task', 'convId': 'tool-search-cache-conv',
        '_userId': 1, 'status': 'running', 'aborted': False,
        'model': 'test-model', 'events': [], 'events_lock': threading.Lock(),
        '_dispatch_heartbeat': 0.0, '_t_last_event': 0.0, '_attended': False,
    }
    task.update(overrides)
    return task


def _cached_task(body, *, display=None, source=None):
    from lib.tasks_pkg.tool_dispatch._flags import _make_cache_key

    entry = ((body, False) if source is None else
             (body, False, source, display, None, None, None))
    return _task(_tool_result_cache={
        _make_cache_key('search_tools', _ARGS): entry})


@pytest.fixture
def pipeline(monkeypatch):
    from lib.tasks_pkg.executor import _finalize
    from lib.tasks_pkg.tool_dispatch import _heartbeat, _pipeline
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    from lib.tasks_pkg.tool_display import _build_tool_round_entry

    events = []
    lock = threading.Lock()

    def record(task, event):
        with lock:
            events.append(dict(event))

    for owner in (_finalize, _heartbeat, _pipeline):
        monkeypatch.setattr(owner, 'append_event', record)

    def run(task, call_id='cache-hit', seq=1):
        arguments = json.dumps(_ARGS)
        tc = {'id': call_id, 'type': 'function',
              'function': {'name': 'search_tools', 'arguments': arguments}}
        _, row, _ = _build_tool_round_entry(
            'search_tools', dict(_ARGS), call_id, arguments, seq, False)
        parsed = [(tc, 'search_tools', call_id, dict(_ARGS),
                   row['roundNum'], row, None)]
        messages = [{'role': 'assistant', 'content': None, 'tool_calls': [tc]}]
        execute_tool_pipeline(
            task, parsed, cfg={'autoApply': True}, project_path=None,
            project_enabled=False, tool_list=[], messages=messages,
            all_search_results_text=[], round_num=seq - 1, model='test-model')
        result_events = [event for event in events
                         if event.get('toolCallId') == call_id
                         and event.get('type') == 'tool_result']
        assert len(result_events) == 1
        tool_messages = [message for message in messages
                         if message.get('role') == 'tool'
                         and message.get('tool_call_id') == call_id]
        assert len(tool_messages) == 1
        assert tool_messages[0]['content'] == row['toolContent']
        return row, result_events[0]

    return run


@pytest.fixture
def cached(monkeypatch):
    from lib.tasks_pkg.handlers import tool_gateway
    from lib.tasks_pkg.tool_dispatch import _heartbeat, _pipeline

    execute = Mock(side_effect=AssertionError('cache hit executed a fresh tool'))
    search = Mock(side_effect=AssertionError('cache hit reran catalog search'))
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', execute)
    monkeypatch.setattr(_pipeline, '_execute_tool_one', execute)
    monkeypatch.setattr(tool_gateway, 'search_executable_catalog', search)

    yield _cached_task
    execute.assert_not_called()
    search.assert_not_called()


def _assert_projection(row, event, results, *, total=1, cursor=None,
                       fail_open=False):
    for surface in (row, event):
        assert surface['results'] == results
        assert surface['toolSearchTotal'] == total
        assert surface['toolSearchNextCursor'] == cursor
        assert surface['toolSearchFailOpen'] is fail_open


@pytest.mark.parametrize('source', [None, 'dedup', 'prefetch'],
                         ids=['legacy-no-display', 'cached-display', 'prefetched-display'])
def test_cached_matches_keep_compact_display_and_full_model_schema(pipeline, cached, source):
    body = json.dumps(_payload(total=7, next_cursor='page-2', fail_open=True), indent=2)
    task = cached(body, source=source, display=[_MATCH] if source else None)
    row, event = pipeline(task)

    assert json.loads(row['toolContent']) == json.loads(body)
    assert json.loads(row['toolContent'])['items'][0]['arguments_schema'] == _SCHEMA
    _assert_projection(row, event, [_MATCH], total=7, cursor='page-2', fail_open=True)
    assert 'arguments_schema' not in event['results'][0]


def test_cached_zero_matches_do_not_fabricate_a_generic_result(pipeline, cached):
    body = json.dumps(_payload(items=[], total=0))
    row, event = pipeline(cached(body))

    assert json.loads(row['toolContent']) == json.loads(body)
    _assert_projection(row, event, [], total=0)


def test_cached_capability_availability_is_projected_without_search(pipeline, cached):
    availability = {
        'namespace': 'browser', 'reason_code': 'browser_extension_disconnected',
        'message': 'Browser extension is disconnected.',
        'recovery_hint': 'Connect the browser extension.',
    }
    body = json.dumps(_payload(items=[], total=0,
                               capability_availability=[availability]))
    row, event = pipeline(cached(body))

    assert json.loads(row['toolContent']) == json.loads(body)
    _assert_projection(row, event, [{
        'type': 'tool_catalog_availability', 'namespace': 'browser',
        'reasonCode': availability['reason_code'], 'message': availability['message'],
        'recoveryHint': availability['recovery_hint'],
    }], total=0)


def test_real_search_then_duplicate_pipeline_call_reuses_display_and_model_json(pipeline, monkeypatch):
    from lib.tasks_pkg.handlers import tool_gateway
    from lib.tasks_pkg.tool_dispatch._flags import _make_cache_key

    catalog = [{'type': 'function', 'function': {
        'name': 'read_files', 'description': _ITEM['description'],
        'parameters': _SCHEMA}}]
    task = _task(_executable_tool_catalog=catalog,
                 _executableToolNamespaceByName={'read_files': 'project'})
    search = Mock(wraps=tool_gateway.search_executable_catalog)
    monkeypatch.setattr(tool_gateway, 'search_executable_catalog', search)
    fresh, fresh_event = pipeline(task, 'fresh', 1)
    assert search.call_count == 1
    assert _make_cache_key('search_tools', _ARGS) in task['_tool_result_cache']
    replay, replay_event = pipeline(task, 'replay', 2)

    assert search.call_count == 1, 'a duplicate pipeline call must not search again'
    assert replay['toolContent'] == fresh['toolContent']
    assert json.loads(replay['toolContent'])['items'][0]['arguments_schema'] == _SCHEMA
    assert fresh['results'][0]['type'] == 'tool_catalog_match'
    _assert_projection(fresh, fresh_event, fresh['results'])
    _assert_projection(replay, replay_event, fresh['results'])


@pytest.mark.parametrize('body', [
    '{not-json', '[]', 'null', _payload(),
    json.dumps(_payload(status='error', error='invalid_query')),
    json.dumps(_payload(items='not-an-array', total='invalid')),
    json.dumps(_payload(items=[], capability_availability=[{'namespace': 'browser'}])),
], ids=['malformed-json', 'array', 'null', 'non-json-carrier', 'error-with-items',
        'invalid-fields', 'incomplete-availability'])
def test_bad_cached_payload_is_discarded_before_fresh_search(pipeline, monkeypatch, body):
    from lib.tasks_pkg.handlers import tool_gateway

    fresh = _payload(items=[], total=0)
    search = Mock(return_value=fresh)
    monkeypatch.setattr(tool_gateway, 'search_executable_catalog', search)
    # Even an apparently valid cached display cannot legitimize bad model JSON.
    task = _cached_task(body, source='dedup', display=[_MATCH])
    row, event = pipeline(task)

    search.assert_called_once()
    assert json.loads(row['toolContent']) == fresh
    _assert_projection(row, event, [], total=0)
