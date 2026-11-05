"""Rebuilt attempts retain conversation affinity and transport evidence identity."""
import copy

import pytest

import lib.tasks_pkg.llm_fallback._recovery as recovery
from lib.llm.llm_errors import PromptTooLongError
from lib.llm.cache_affinity import cache_affinity_key
from lib.tasks_pkg.llm_fallback import _request_metadata, _requests
from lib.tasks_pkg.context_composer import checklist, turn_requirements
from lib.tasks_pkg.compaction import api as compaction_api

pytestmark = pytest.mark.unit


def _metadata():
    return {
        '_conv_id': 'conversation-stable',
        '_safety_identifier': 'tofu_hash',
        '_raw_archive_context': {
            'userId': 7,
            'conversationId': 'conversation-stable',
            'turnId': 'turn-stable',
            'attemptId': 'attempt-stable',
            'taskId': 'task-stable',
            'roundNum': 4,
            'model': 'source-model',
        },
    }


def test_fallback_preserves_stable_identity_not_source_transport_identity(monkeypatch):
    monkeypatch.setattr(turn_requirements, 'refresh_turn_requirements', lambda *a, **k: None)
    task = {'id': 'task-stable', 'convId': 'conversation-stable',
            '_userId': 7, '_turnId': 'turn-stable', '_attemptId': 'attempt-stable'}
    body = {'model': 'source-model', '_thinking_depth': 'max',
            '_reasoning_intent': {'version': 1, 'mode': 'enabled', 'value': None,
                              'strict': True, 'sampling': {}, 'source': 'product'},
            **_metadata()}
    rebuilt, _ = _requests.prepare_fallback_body(
        task, body, 'source-model', 'destination-model', 4096, [],
        [{'role': 'user', 'content': 'continue'}], round_num=3,
    )
    assert rebuilt['_reasoning_intent'] == body['_reasoning_intent']
    assert rebuilt['_thinking_depth'] == body['_thinking_depth']
    assert rebuilt['_conv_id'] == body['_conv_id']
    assert rebuilt['_safety_identifier'] == body['_safety_identifier']
    assert rebuilt['_raw_archive_context'] == {
        **body['_raw_archive_context'], 'model': 'destination-model'}
    assert rebuilt['_raw_archive_context'] is not body['_raw_archive_context']
    assert cache_affinity_key(rebuilt) == cache_affinity_key(body)


def test_rebuilt_child_attempt_keeps_its_own_cache_lineage():
    """A child's fallback stays on the child's lineage, not the parent's."""
    from lib.llm.cache_affinity import CACHE_AFFINITY_SCOPE_KEY
    from lib.tasks_pkg.invocation import InvocationPolicy, invocation_affinity_id
    task = {'id': 'child-task', 'convId': 'conversation-stable', '_userId': 7,
            '_invocationPolicy': InvocationPolicy.child('root-task')}
    assert invocation_affinity_id(task) != 'conversation-stable'
    source = {'model': 'source-model', '_conv_id': 'conversation-stable',
              CACHE_AFFINITY_SCOPE_KEY: invocation_affinity_id(task)}
    rebuilt = _request_metadata.preserve_rebuilt_request_context(
        source, {'model': 'destination-model'}, task=task)
    assert cache_affinity_key(rebuilt) == cache_affinity_key(source)
    fresh = _request_metadata.preserve_rebuilt_request_context(
        {}, {'model': 'destination-model'}, task=task)
    assert fresh[CACHE_AFFINITY_SCOPE_KEY] == invocation_affinity_id(task)


def test_reactive_rebuild_preserves_stable_identity(monkeypatch):
    monkeypatch.setattr(compaction_api, 'reactive_compact', lambda *a, **k: False)
    monkeypatch.setattr(checklist, 'restore_checklist_context', lambda *a, **k: None)
    monkeypatch.setattr(turn_requirements, 'refresh_turn_requirements', lambda *a, **k: None)
    task = {'id': 'task-stable', 'convId': 'conversation-stable',
            '_userId': 7, 'config': {}}
    source = {'model': 'source-model', **_metadata()}
    result = recovery._prepare_reactive_retry_body(
        task, source, 'source-model', [{'role': 'user', 'content': 'continue'}], [],
        'medium', False, PromptTooLongError('too long'), 'task',
    )
    assert result is not None
    _request_metadata.attach_rebuilt_request_metadata(
        task, source, result, [{'role': 'user', 'content': 'continue'}], [],
        round_num=3, tid='task')
    assert result['_conv_id'] == source['_conv_id']
    assert result['_safety_identifier'] == source['_safety_identifier']
    assert result['_raw_archive_context'] == source['_raw_archive_context']
    assert result['_raw_archive_context'] is not source['_raw_archive_context']
    assert source == {'model': 'source-model', **copy.deepcopy(_metadata())}
