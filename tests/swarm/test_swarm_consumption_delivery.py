"""Swarm results survive provider failure and uncertain durable acknowledgement."""
from unittest.mock import Mock

import pytest

from lib import agent_inbox
from lib.swarm.delivery import acknowledge_presented_results, restore_unconsumed_results

pytestmark = pytest.mark.unit


def carrier():
    return {'id': 'consumption-test', '_userId': 7, '_swarm_inject_pending': [
        {'agent_id': 'branch', 'value': 'evidence', 'priority': 'next', 'status': 'completed'}]}


def test_durable_ack_failure_keeps_result_unread_and_retryable(monkeypatch):
    task = carrier()
    session = Mock(user_id=7)
    monkeypatch.setattr('lib.swarm.integration.get_active_session', lambda _: session)
    ack = Mock(side_effect=[False, True])
    monkeypatch.setattr('lib.swarm.persistence.mark_delivered', ack)
    assert acknowledge_presented_results(task) is False
    session.mark_results_delivered.assert_not_called()
    assert task['_swarm_inject_pending']
    assert acknowledge_presented_results(task) is True
    session.mark_results_delivered.assert_called_once_with(['branch'])
    assert '_swarm_inject_pending' not in task
    assert acknowledge_presented_results(task) is True
    assert ack.call_count == 2


def test_unconsumed_result_restored_once_with_original_priority():
    task = carrier()
    try:
        restore_unconsumed_results(task)
        restore_unconsumed_results(task)
        items = agent_inbox.drain(task['id'], modes={'swarm-update'})
        assert len(items) == 1
        assert items[0]['value'] == 'evidence'
        assert items[0]['priority'] == 'next'
        assert items[0]['agent_id'] == 'branch'
    finally:
        agent_inbox.clear(task['id'])


def test_owner_mismatch_cannot_acknowledge_another_session(monkeypatch):
    task = carrier()
    monkeypatch.setattr('lib.swarm.integration.get_active_session', lambda _: Mock(user_id=8))
    ack = Mock()
    monkeypatch.setattr('lib.swarm.persistence.mark_delivered', ack)
    with pytest.raises(ValueError, match='another owner'):
        acknowledge_presented_results(task)
    ack.assert_not_called()
    assert task['_swarm_inject_pending']


def test_tool_return_waits_for_provider_and_restores_live_reservation(monkeypatch):
    from lib.swarm.delivery import stage_tool_results
    task = {'id': 'tool-consumption', '_userId': 7}
    session = Mock(user_id=7, task_id='wave-1')
    monkeypatch.setattr('lib.swarm.integration.get_active_session', lambda _: session)
    ack = Mock(return_value=True)
    monkeypatch.setattr('lib.swarm.persistence.mark_delivered', ack)
    stage_tool_results(task, session, [{'agent_id': 'branch', 'role': 'general',
        'status': 'completed', 'final_answer': 'full evidence'}])
    ack.assert_not_called()
    assert 'full evidence' in task['_swarm_inject_pending'][0]['value']
    try:
        restore_unconsumed_results(task)
        session.restore_unconsumed_results.assert_called_once_with(['branch'])
        items = agent_inbox.drain(task['id'])
        assert len(items) == 1
        assert items[0]['swarm_run_id'] == 'wave-1'
    finally:
        agent_inbox.clear(task['id'])


def test_reused_agent_id_in_new_session_is_not_acknowledged(monkeypatch):
    task = carrier()
    task['_swarm_inject_pending'][0]['swarm_run_id'] = 'previous'
    session = Mock(user_id=7, task_id='current')
    monkeypatch.setattr('lib.swarm.integration.get_active_session', lambda _: session)
    ack = Mock(return_value=True)
    monkeypatch.setattr('lib.swarm.persistence.mark_delivered', ack)
    assert acknowledge_presented_results(task) is True
    assert not task.get('_swarm_inject_pending')
    ack.assert_not_called()
    session.mark_results_delivered.assert_not_called()


def test_real_master_await_stages_receipt_without_durable_ack(monkeypatch):
    from lib.swarm.master import MasterOrchestrator
    from lib.swarm.types import SubTaskSpec, SubAgentResult
    from lib.swarm.delivery import stage_tool_results
    spec = SubTaskSpec(id='ready', objective='evidence')
    master = MasterOrchestrator('master-receipt', '', [spec], user_id=7)
    master._results_by_id['ready'] = (spec, SubAgentResult(status='completed', final_answer='result'))
    task = {'id': 'master-receipt', '_userId': 7}
    ack = Mock(return_value=True)
    monkeypatch.setattr('lib.swarm.persistence.mark_delivered', ack)
    result = master.await_agents(ids=['ready'], timeout_seconds=0,
        delivery_ack=lambda rows: stage_tool_results(task, master, rows))
    assert result['completed'][0]['result'] == 'result'
    assert task['_swarm_inject_pending'][0]['swarm_run_id'] == master.task_id
    ack.assert_not_called()


def test_failed_staging_leaves_live_result_unread():
    from lib.swarm.master import MasterOrchestrator
    from lib.swarm.types import SubTaskSpec, SubAgentResult
    spec = SubTaskSpec(id='ready', objective='evidence')
    master = MasterOrchestrator('master-receipt-failed', '', [spec], user_id=7)
    master._results_by_id['ready'] = (spec, SubAgentResult(status='completed', final_answer='result'))
    def reject(_rows):
        raise RuntimeError('receipt unavailable')
    with pytest.raises(RuntimeError, match='receipt unavailable'):
        master.await_agents(ids=['ready'], timeout_seconds=0, delivery_ack=reject)
    assert 'ready' not in master._delivered_result_ids
