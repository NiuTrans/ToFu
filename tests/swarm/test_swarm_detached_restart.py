"""Completed spawning turns retain detached children across process restart."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib.swarm.integration import _rehydrate
from lib.swarm.restart_recovery import detached_recovery_allowed
from lib.llm.llm_errors import AbortedError
from tests.storage.test_storage_authority_sql_contract import storage as storage  # noqa: F401

pytestmark = pytest.mark.unit


@pytest.fixture
def detached(monkeypatch):
    row = {'swarm_key': 'conv', 'conv_id': 'conv', 'task_id': 'original-task',
        'status': 'running', 'config': {'user_id': 7, 'tool_catalog': [],
            'parent_cfg': {'_swarmParentTurnId': 'turn',
                '_swarmParentAttemptId': 'original', '_swarmParentTaskId': 'original-task'}},
        'specs': [{'id': 'child', 'role': 'coder', 'objective': 'audit'}],
        'agents': [{'agent_id': 'child', 'status': 'running',
                    'messages': [{'role': 'user', 'content': 'audit'}], 'rounds_used': 15}]}
    source = {'attemptId': 'original', 'conversationId': 'conv', 'turnId': 'turn',
              'taskId': 'original-task', 'status': 'completed'}
    turn = {'turnId': 'turn', 'laneId': 'main', 'currentAttemptId': 'original',
            'status': 'completed', 'ordinal': 1, 'settlement': {'cause': 'provider_finished'}}
    page = {'turns': []}
    monkeypatch.setattr('lib.turn_lifecycle.get_attempt', lambda *a, **kw: source)
    monkeypatch.setattr('lib.turn_lifecycle.get_turn', lambda *a, **kw: turn)
    monkeypatch.setattr('lib.swarm.restart_recovery.read_recovery_turns', lambda *a, **kw: page)
    session = SimpleNamespace(rehydrate_in_background=Mock(), abort=Mock())
    factory = Mock(return_value=session)
    published = {}
    monkeypatch.setattr(_rehydrate, 'MasterOrchestrator', factory)
    monkeypatch.setattr(_rehydrate, '_get_session', lambda key: published.get(key))
    monkeypatch.setattr(_rehydrate, '_set_session', lambda key, value, **kw: published.setdefault(key, value))
    monkeypatch.setattr(_rehydrate, '_remove_session', lambda key, **kw: published.pop(key, None))
    monkeypatch.setattr(_rehydrate, '_resolve_output_dir', lambda key: '')
    monkeypatch.setattr('lib.agent_inbox.untombstone', Mock())
    return row, source, turn, page, session, factory, published


def test_startup_restores_detached_children_without_reopening_completed_parent(detached):
    row, source, turn, _, session, factory, _ = detached
    assert _rehydrate._rehydrate_one(row)
    session.rehydrate_in_background.assert_called_once_with(row['agents'])
    assert not _rehydrate._rehydrate_one(row)
    factory.assert_called_once()
    assert source['status'] == turn['status'] == 'completed'


@pytest.mark.parametrize('mutation', [
    'owner', 'attempt', 'task', 'conversation', 'source_status', 'replacement',
    'turn_status', 'cause', 'ordinal', 'lane', 'terminated', 'later_stop',
    'later_interrupt', 'missing_page',
])
def test_detached_restart_never_crosses_cancel_or_foreign_authority(detached, mutation):
    row, source, turn, page, session, factory, _ = detached
    if mutation == 'owner': row['config']['user_id'] = 8
    elif mutation == 'attempt': source['attemptId'] = 'foreign'
    elif mutation == 'task': source['taskId'] = 'foreign'
    elif mutation == 'conversation': source['conversationId'] = 'foreign'
    elif mutation == 'source_status': source['status'] = 'interrupted'
    elif mutation == 'replacement': turn['currentAttemptId'] = 'replacement'
    elif mutation == 'turn_status': turn['status'] = 'interrupted'
    elif mutation == 'cause': turn['settlement']['cause'] = 'user_abort'
    elif mutation == 'ordinal': turn['ordinal'] = True
    elif mutation == 'lane': turn['laneId'] = 'other'
    elif mutation == 'terminated': row['status'] = 'terminated'
    elif mutation == 'later_stop': page['turns'] = [{'status': 'cancelled'}, {'status': 'completed'}]
    elif mutation == 'later_interrupt': page['turns'] = [{'status': 'interrupted'}]
    elif mutation == 'missing_page': page.clear()
    assert not detached_recovery_allowed(row, user_id=7)
    # Owner mismatch alone is tested at the owner-scoped admission function.
    if mutation != 'owner':
        assert not _rehydrate._rehydrate_one(row)
        factory.assert_not_called()
    session.rehydrate_in_background.assert_not_called()


def test_later_ordinary_turn_keeps_detached_assignment(detached):
    row, _, _, page, *_ = detached
    page['turns'] = [{'status': 'completed'}, {'status': 'running'}]
    assert detached_recovery_allowed(row, user_id=7)


@pytest.mark.parametrize('boundary', ['catalog', 'publication', 'admission'])
def test_stop_during_detached_activation_fences_worker_start(detached, monkeypatch, boundary):
    row, _, turn, _, session, factory, published = detached
    def stop():
        turn['currentAttemptId'] = 'replacement'
    if boundary == 'catalog':
        def catalog(config):
            stop()
            return []
        monkeypatch.setattr(_rehydrate, '_rebuild_tool_list', catalog)
    elif boundary == 'publication':
        def publish(key, value, **kw):
            published[key] = value
            stop()
        monkeypatch.setattr(_rehydrate, '_set_session', publish)
    if boundary == 'admission':
        assert _rehydrate._rehydrate_one(row)
        stop()
        with pytest.raises(AbortedError):
            factory.call_args.kwargs['before_agent_admission']()
    else:
        assert not _rehydrate._rehydrate_one(row)
        assert not published
        session.rehydrate_in_background.assert_not_called()


def test_detached_authority_unavailable_preserves_checkpoint(detached, monkeypatch):
    row, _, _, _, session, factory, _ = detached
    monkeypatch.setattr('lib.turn_lifecycle.get_attempt', Mock(side_effect=RuntimeError('offline')))
    assert not _rehydrate._rehydrate_one(row)
    factory.assert_not_called()
    assert row['agents'][0]['rounds_used'] == 15


def test_real_completed_turn_authorizes_detached_checkpoint_until_replacement(storage, monkeypatch):
    from uuid import uuid4
    from lib.turn_lifecycle import (
        bind_task, claim_attempt_start, create_attempt, create_turn_pair,
        get_turn, mark_task_started, record_task_event,
    )
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kw: storage.client)
    conv = 'detached-restart-' + uuid4().hex
    pair = create_turn_pair(conv, command_id='create-' + conv,
        input_projection={'content': 'Audit branches'}, config={}, user_id=7,
        conversation_defaults={'allowCreate': True, 'title': 'Detached restart fixture'})
    attempt_id, turn_id = pair['attempt']['attemptId'], pair['turn']['turnId']
    assert claim_attempt_start(attempt_id, user_id=7)
    bind_task(attempt_id, 'spawning-task', user_id=7)
    mark_task_started(attempt_id, 'spawning-task', user_id=7)
    task = {'id': 'spawning-task', '_attemptId': attempt_id, '_userId': 7,
            'status': 'done', 'finishReason': 'stop', 'content': 'Audit continues in background',
            'thinking': '', 'toolRounds': [], 'config': {'model': 'fixture'}}
    assert record_task_event(task, {'type': 'done', 'finishReason': 'stop'})
    row = {'conv_id': conv, 'task_id': 'spawning-task', 'status': 'running',
           'config': {'user_id': 7, 'parent_cfg': {'_swarmParentTurnId': turn_id,
                      '_swarmParentAttemptId': attempt_id, '_swarmParentTaskId': 'spawning-task'}}}
    assert detached_recovery_allowed(row, user_id=7)
    current = get_turn(conv, turn_id, user_id=7)
    create_attempt(conv, turn_id, command_id='replace-' + conv, operation='regenerate',
                   expected_projection_revision=current['projectionRevision'], config={}, user_id=7)
    assert not detached_recovery_allowed(row, user_id=7)


def test_long_completed_history_preserves_detached_authority_and_early_stop(detached):
    row, _, _, page, *_ = detached
    page['turns'] = [{'status': 'completed'} for _ in range(5001)]
    assert detached_recovery_allowed(row, user_id=7)
    page['turns'][0]['status'] = 'cancelled'
    assert not detached_recovery_allowed(row, user_id=7)
