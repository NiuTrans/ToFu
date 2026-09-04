"""Native batch continuation for spilled tool results."""

from __future__ import annotations

import json
import time

import pytest


pytestmark = pytest.mark.unit


def _invoke(monkeypatch, fn_name, arguments, repository):
    import lib.tasks_pkg.handlers.tool_result_artifacts as handler

    monkeypatch.setattr(
        handler, 'ToolResultArtifactRepository', lambda: repository)
    monkeypatch.setattr(handler, 'task_user_id', lambda _task: 17)
    monkeypatch.setattr(handler, '_finalize_tool_round', lambda *_args: None)
    _tc_id, content, is_search = handler._handle_tool_result_artifact(
        {'model': ''}, {}, fn_name, 'tc-batch', arguments, 1, {}, {}, '', False)
    assert is_search is False
    return json.loads(content)


def test_contracts_keep_single_shape_and_add_bounded_batches():
    from lib.tools.tool_result_artifacts import (
        READ_TOOL_ARTIFACT, SEARCH_TOOL_ARTIFACT)

    read = READ_TOOL_ARTIFACT.parameters['properties']
    search = SEARCH_TOOL_ARTIFACT.parameters['properties']
    assert {'artifact_ref', 'cursor', 'limit', 'reads'} <= set(read)
    assert {'artifact_ref', 'query', 'cursor', 'limit', 'searches'} <= set(search)
    assert read['reads']['maxItems'] == 16
    assert search['searches']['maxItems'] == 16

    assert READ_TOOL_ARTIFACT.validate_arguments({
        'artifact_ref': 'tool-result:legacy'})['limit'] == 8192
    assert SEARCH_TOOL_ARTIFACT.validate_arguments({
        'artifact_ref': 'tool-result:legacy', 'query': 'needle'})['limit'] == 8
    with pytest.raises(Exception) as exc_info:
        READ_TOOL_ARTIFACT.validate_arguments({
            'reads': [{'artifact_ref': f'tool-result:{index}'}
                      for index in range(17)]})
    assert getattr(exc_info.value, 'code', '') == 'too_many_items'


def test_read_batch_is_concurrent_input_ordered_and_failure_isolated(monkeypatch):
    completed = []

    class Repository:
        def read_range(self, **kwargs):
            ref = kwargs['artifact_ref']
            if ref.endswith('slow'):
                time.sleep(0.04)
            if ref.endswith('missing'):
                completed.append(ref)
                return None
            completed.append(ref)
            return {
                'artifactRef': ref,
                'content': ref.rsplit(':', 1)[-1],
                'offset': kwargs['offset'],
                'nextCursor': None,
                'truncated': False,
            }

    value = _invoke(monkeypatch, 'read_tool_artifact', {'reads': [
        {'artifact_ref': 'tool-result:slow'},
        {'artifact_ref': 'tool-result:fast'},
        {'artifact_ref': 'tool-result:missing'},
    ]}, Repository())

    assert completed[0] == 'tool-result:fast'
    assert value['status'] == 'partial_failure'
    assert [item['index'] for item in value['items']] == [0, 1, 2]
    assert [item['artifactRef'] for item in value['items']] == [
        'tool-result:slow', 'tool-result:fast', 'tool-result:missing']
    assert value['items'][2]['error']['code'] == 'artifact_unavailable'


def test_search_batch_preserves_each_query_and_next_cursor(monkeypatch):
    class Repository:
        def search(self, **kwargs):
            if kwargs['query'] == 'boom':
                raise RuntimeError('one broken storage request')
            return {
                'artifactRef': kwargs['artifact_ref'],
                'query': kwargs['query'],
                'items': [{'offset': kwargs['cursor'], 'text': 'match'}],
                'nextCursor': str(kwargs['cursor'] + 10),
                'truncated': True,
            }

    value = _invoke(monkeypatch, 'search_tool_artifact', {'searches': [
        {'artifact_ref': 'tool-result:a', 'query': 'alpha', 'cursor': '3'},
        {'artifact_ref': 'tool-result:b', 'query': 'boom'},
        {'artifact_ref': 'tool-result:c', 'query': 'gamma', 'cursor': '7'},
    ]}, Repository())

    assert [(item.get('query'), item.get('nextCursor'))
            for item in value['items']] == [
                ('alpha', '13'), ('boom', None), ('gamma', '17')]
    assert value['items'][1]['error']['code'] == 'artifact_store_unavailable'


def test_batch_budget_keeps_all_identities_fair_bodies_and_resume_cursors(
        monkeypatch):
    import lib.tasks_pkg.handlers.tool_result_artifacts as handler

    monkeypatch.setattr(handler, '_RESPONSE_TOKEN_BUDGET', 1_200)
    monkeypatch.setattr(handler, '_count_tokens', lambda text, _model: len(text))
    results = [
        {
            'index': index,
            'status': 'ok',
            'artifactRef': f'tool-result:{index}',
            'content': chr(65 + index) * 2_000,
            'offset': index * 100,
            'nextCursor': str(index * 100 + 2_000),
            'truncated': True,
        }
        for index in range(3)
    ]
    value = json.loads(handler._fit_batch_to_budget(
        'read_tool_artifact', results, ''))

    assert len(json.dumps(value, separators=(',', ':'))) <= 1_200
    assert [item['artifactRef'] for item in value['items']] == [
        'tool-result:0', 'tool-result:1', 'tool-result:2']
    assert all(item['content'] for item in value['items'])
    assert all(item['outputTruncated'] is True for item in value['items'])
    assert [item['nextCursor'] for item in value['items']] == [
        str(item['offset'] + len(item['content'].encode()))
        for item in value['items']]
    assert max(map(lambda item: len(item['content']), value['items'])) - min(
        map(lambda item: len(item['content']), value['items'])) <= 4


def test_legacy_single_read_response_shape_is_unchanged(monkeypatch):
    class Repository:
        def read_range(self, **kwargs):
            return {
                'artifactRef': kwargs['artifact_ref'],
                'content': 'legacy-content',
                'offset': kwargs['offset'],
                'nextCursor': None,
                'truncated': False,
            }

    value = _invoke(monkeypatch, 'read_tool_artifact', {
        'artifact_ref': 'tool-result:legacy', 'cursor': '4', 'limit': 32,
    }, Repository())
    assert value == {
        'artifactRef': 'tool-result:legacy',
        'content': 'legacy-content',
        'nextCursor': None,
        'offset': 4,
        'status': 'ok',
        'truncated': False,
    }
