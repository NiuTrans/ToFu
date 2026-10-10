"""Restart recovery never turns a settled/cancelled session into fresh execution."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from lib.swarm.integration import _rehydrate

pytestmark = pytest.mark.unit


@pytest.fixture
def recovery(monkeypatch):
    row = {
        'swarm_key': 'restart-conv', 'conv_id': 'restart-conv',
        'task_id': 'restart-launch', 'status': 'running',
        'config': {'user_id': 7},
        'specs': [{'id': 'child', 'role': 'coder', 'objective': 'work'}],
        'agents': [{'agent_id': 'child', 'status': 'running', 'messages': []}],
    }
    session = SimpleNamespace(rehydrate_in_background=Mock())
    factory = Mock(return_value=session)
    monkeypatch.setattr(_rehydrate, '_get_session', lambda key: None)
    monkeypatch.setattr(_rehydrate, '_set_session', lambda *a, **kw: None)
    monkeypatch.setattr(_rehydrate, '_rebuild_tool_list', lambda config: [])
    monkeypatch.setattr(_rehydrate, '_resolve_output_dir', lambda key: '')
    monkeypatch.setattr(_rehydrate, 'MasterOrchestrator', factory)
    monkeypatch.setattr('lib.agent_inbox.untombstone', Mock())
    monkeypatch.setattr('lib.swarm.restart_recovery.read_recovery_turns', lambda *a, **kw: {
        'turns': [{'turnId': 'last', 'status': 'succeeded'}]})
    return row, session, factory


def test_running_session_after_natural_parent_end_can_resume(recovery):
    row, session, factory = recovery
    assert _rehydrate._rehydrate_one(row)
    factory.assert_called_once()
    session.rehydrate_in_background.assert_called_once_with(row['agents'])


@pytest.mark.parametrize('status', ['cancelled', 'aborted', 'interrupted'])
def test_latest_committed_stop_blocks_recovery_without_deleting_evidence(recovery, monkeypatch, status):
    row, session, factory = recovery
    monkeypatch.setattr('lib.swarm.restart_recovery.read_recovery_turns', lambda *a, **kw: {
        'turns': [{'turnId': 'last', 'status': status}]})
    delete = Mock(side_effect=AssertionError('Stop must preserve result evidence'))
    monkeypatch.setattr('lib.swarm.persistence.delete_session', delete)
    assert not _rehydrate._rehydrate_one(row)
    factory.assert_not_called()
    assert row['agents'][0]['status'] == 'running'
    delete.assert_not_called()


def test_settled_session_with_unfinished_checkpoint_does_not_respawn(recovery):
    row, session, factory = recovery
    row['status'] = 'terminated'
    assert not _rehydrate._rehydrate_one(row)
    factory.assert_not_called()
    assert row['agents'][0]['status'] == 'running'


def test_recovery_authority_read_failure_never_launches(recovery, monkeypatch):
    row, session, factory = recovery
    monkeypatch.setattr('lib.swarm.restart_recovery.read_recovery_turns', Mock(
        side_effect=RuntimeError('authority unavailable')))
    assert not _rehydrate._rehydrate_one(row)
    factory.assert_not_called()


def test_missing_authority_does_not_authorize_resume(recovery, monkeypatch):
    row, session, factory = recovery
    reader = Mock(return_value={'turns': []})
    monkeypatch.setattr('lib.swarm.restart_recovery.read_recovery_turns', reader)
    assert not _rehydrate._rehydrate_one(row)
    reader.assert_called_once_with('restart-conv', user_id=7, lane_id='main',
                                   limit=1)
    factory.assert_not_called()


def test_terminal_session_does_not_invent_work_for_missing_checkpoint(recovery):
    row, session, factory = recovery
    row.update(status='terminated', agents=[])
    assert not _rehydrate._rehydrate_one(row)
    factory.assert_not_called()


def test_settled_session_with_only_unread_success_can_deliver(recovery):
    row, session, factory = recovery
    row['status'] = 'terminated'
    row['agents'][0].update(status='completed', delivered=False,
                           result={'status': 'completed', 'final_answer': 'evidence'})
    assert _rehydrate._rehydrate_one(row)
    session.rehydrate_in_background.assert_called_once_with(row['agents'])
