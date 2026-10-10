"""Durable cancellation outranks delayed local notifications during recovery."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from lib.swarm.integration import _rehydrate
from lib.swarm.master import MasterOrchestrator
from lib.swarm.protocol import SubAgentResult, SubTaskSpec
from tests.swarm.test_swarm_restart_continue import authority  # noqa: F401

pytestmark = pytest.mark.unit


@pytest.fixture
def activation(authority, monkeypatch):  # noqa: F811 - pytest fixture injection
    row, task, source, current = authority
    created, published = [], {}

    def create(**kwargs):
        master = MasterOrchestrator(**kwargs)
        created.append(master)
        return master

    monkeypatch.setattr(_rehydrate, 'MasterOrchestrator', create)
    monkeypatch.setattr(_rehydrate, '_get_session', lambda key: published.get(key))
    monkeypatch.setattr(_rehydrate, '_set_session', lambda key, value, **kw: published.setdefault(key, value))
    monkeypatch.setattr(_rehydrate, '_remove_session', lambda key, **kw: published.pop(key, None))
    monkeypatch.setattr(_rehydrate, '_resolve_output_dir', lambda key: '')
    monkeypatch.setattr('lib.swarm.integration._state.add_session_alias', Mock())
    monkeypatch.setattr('lib.agent_inbox.untombstone', Mock())
    monkeypatch.setattr('lib.swarm.persistence.save_session', Mock())
    start = Mock()
    monkeypatch.setattr(MasterOrchestrator, 'rehydrate_in_background', start)
    return row, task, source, current, created, published, start


@pytest.mark.parametrize('boundary', ['binding', 'publication'])
@pytest.mark.parametrize('mutation', ['stop', 'supersede'])
def test_durable_cancel_during_activation_never_requests_workers(activation, monkeypatch, boundary, mutation):
    row, task, _, current, created, published, start = activation

    def cancel():
        if mutation == 'stop':
            current['status'] = 'interrupted'
        else:
            current['currentAttemptId'] = 'newer'
        assert not task.get('aborted'), 'local notification deliberately delayed'

    if boundary == 'binding':
        monkeypatch.setattr('lib.swarm.persistence.save_session', lambda *a, **kw: cancel())
    else:
        def publish(key, master, **kwargs):
            published[key] = master
            cancel()
        monkeypatch.setattr(_rehydrate, '_set_session', publish)

    assert not _rehydrate._rehydrate_one(row, resume_task=task, tool_list=[])
    start.assert_not_called()
    assert not published
    assert created[0]._aborted


@pytest.mark.parametrize('boundary', ['capacity_wait', 'start_observer'])
def test_recovered_worker_rechecks_durable_authority_at_admission(activation, monkeypatch, boundary):
    row, task, _, current, created, _, _ = activation
    assert _rehydrate._rehydrate_one(row, resume_task=task, tool_list=[])
    master = created[0]
    run = Mock(return_value=SubAgentResult(status='completed'))
    agent = Mock(agent_id='worker', run=run)
    monkeypatch.setattr(master, '_make_agent', lambda spec: agent)
    monkeypatch.setattr(master, '_on_agent_complete_callback', Mock())

    def cancel(spec=None):
        current['status'] = 'interrupted'
        assert not task.get('aborted')

    if boundary == 'capacity_wait':
        cancel()
        monkeypatch.setattr(master, '_on_agent_start_callback', Mock())
    else:
        monkeypatch.setattr(master, '_on_agent_start_callback', cancel)
    scheduler = master._build_scheduler()
    try:
        scheduler.add_specs([SubTaskSpec(id='worker', objective='must not dispatch', max_retries=0)])
        results = scheduler.run_until_idle(timeout=2)
        assert len(results) == 1
        assert results[0][1].status == 'cancelled'
        run.assert_not_called()
    finally:
        scheduler.shutdown(require_quiescence=True)


def test_normal_parent_completion_does_not_cancel_recovered_detached_admission(activation, monkeypatch):
    row, task, source, current, created, _, _ = activation
    assert _rehydrate._rehydrate_one(row, resume_task=task, tool_list=[])
    accepted = {'attemptId': 'resume', 'conversationId': 'conv', 'turnId': 'turn',
        'taskId': 'resume-task', 'operation': 'continue', 'status': 'completed',
        'config': {'_resumeSource': {'attemptId': 'original', 'taskId': 'original-task',
                                   'status': 'interrupted', 'cause': 'server_restart'}}}
    current['status'] = 'completed'
    task['status'] = 'done'
    monkeypatch.setattr('lib.turn_lifecycle.get_attempt', lambda aid, **kw:
                        deepcopy(accepted) if kw.get('include_execution_config') else source)
    master = created[0]
    run = Mock(return_value=SubAgentResult(status='completed'))
    agent = Mock(agent_id='worker', run=run)
    monkeypatch.setattr(master, '_make_agent', lambda spec: agent)
    monkeypatch.setattr(master, '_on_agent_start_callback', Mock())
    monkeypatch.setattr(master, '_on_agent_complete_callback', Mock())
    scheduler = master._build_scheduler()
    try:
        scheduler.add_specs([SubTaskSpec(id='worker', objective='detached authorized work', max_retries=0)])
        results = scheduler.run_until_idle(timeout=2)
        assert len(results) == 1 and results[0][1].status == 'completed'
        run.assert_called_once()
    finally:
        scheduler.shutdown(require_quiescence=True)
