"""Cold delegation preserves requirement identity without native replay fallback."""
from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

from lib.swarm.integration import _config, _rehydrate
from lib.tasks_pkg.requirement_source import requirement_source, snapshot_requirement_source
from tests.swarm.test_swarm_restart_continue import authority  # noqa: F401
from tests.swarm.test_swarm_recovery_activation_fence import activation as activation
from tests.tasks_pkg.test_native_parent import native_parent as native_parent

pytestmark = pytest.mark.unit


def test_durable_snapshot_keeps_source_but_never_execution_port(native_parent):
    parent, _ = native_parent
    snapshot = _config._persist_config({}, 'alpha', None, '', parent['config'],
        user_id=7, parent_task=parent)
    assert snapshot['native_execution_required'] is True
    assert snapshot['requirement_source'] == snapshot_requirement_source(parent)
    decoded = json.loads(json.dumps(snapshot))
    assert decoded == snapshot
    assert '_nativePreparedInvocation' not in json.dumps(decoded)
    parent['_turnId'] = 'later'
    assert snapshot['requirement_source']['turn'] == 'turn'


@pytest.mark.parametrize('resume', [False, True])
@pytest.mark.parametrize('status', ['running', 'pending', 'missing'])
def test_unacknowledged_native_children_never_resume_as_python(activation, monkeypatch, resume, status):
    row, task, _, _, created, published, start = activation
    row['config']['native_execution_required'] = True
    row['agents'][0]['status'] = status
    if status == 'missing': row['agents'] = []
    before = deepcopy(row)
    monkeypatch.setattr(_rehydrate, '_recovery_allowed', lambda *a, **kw: True)
    rebuild = Mock(side_effect=AssertionError('worker tools acquired before refusal'))
    monkeypatch.setattr(_rehydrate, '_rebuild_tool_list', rebuild)
    if resume:
        with pytest.raises(RuntimeError, match='acknowledged actor checkpoints') as caught:
            _rehydrate._rehydrate_one(row, resume_task=task)
        assert caught.value._user_message['kind'] == 'execution_token_usage_unknown'
        assert task['_executionActualsComplete'] is False
        from lib.agent_core.execution_budget import model_budget_admission, ModelBudgetAdmissionDenied
        with pytest.raises(ModelBudgetAdmissionDenied, match='execution_token_usage_unknown'):
            model_budget_admission(task)(input_tokens=1, max_output_tokens=1)
        assert not task['_executionBudget'].actuals_complete()
    else:
        assert not _rehydrate._rehydrate_one(row)
    assert not created and not published
    assert row == before
    rebuild.assert_not_called()
    start.assert_not_called()


@pytest.mark.parametrize('invalid', ['true', 1, None])
def test_malformed_native_recovery_marker_refuses(activation, invalid):
    row, task, _, _, created, published, start = activation
    row['config']['native_execution_required'] = invalid
    with pytest.raises(ValueError, match='native delegation binding'):
        _rehydrate._rehydrate_one(row, resume_task=task)
    assert not created and not published
    start.assert_not_called()


def test_native_parent_stop_is_not_a_terminal_replay_exemption(activation):
    row, task, _, _, created, published, start = activation
    row['config']['native_execution_required'] = True
    row['agents'][0].update(status='cancelled', result={'recovery_reason': 'parent_stop'})
    with pytest.raises(RuntimeError, match='acknowledged actor checkpoints'):
        _rehydrate._rehydrate_one(row, resume_task=task)
    assert task['_executionActualsComplete'] is False
    assert not created and not published
    start.assert_not_called()


@pytest.mark.parametrize('legacy', [False, True])
def test_cold_terminal_evidence_keeps_original_requirement_source(activation, monkeypatch, legacy):
    row, _, _, _, created, _, start = activation
    row['config']['native_execution_required'] = True
    row['agents'][0].update(status='completed', result={'status': 'completed', 'final_answer': 'durable'})
    if not legacy:
        row['config']['requirement_source'] = dict(owner=7, tenant='', conversation='conv',
            turn='turn', attempt='original', task='original-task')
    monkeypatch.setattr(_rehydrate, '_recovery_allowed', lambda *a, **kw: True)
    monkeypatch.setattr(_rehydrate, '_rebuild_tool_list', lambda cfg: [])
    assert _rehydrate._rehydrate_one(row)
    parent = created[0]._parent_task_proxy
    assert requirement_source(parent).identity == ('conv', 'turn', 'original', 'original-task')
    assert '_turnId' not in parent and '_attemptId' not in parent
    assert not parent.get('_nativePreparedInvocation')
    start.assert_called_once()


def test_foreign_cold_requirement_source_refuses_before_publication(activation, monkeypatch):
    row, _, _, _, created, published, start = activation
    row['config']['requirement_source'] = dict(owner=8, tenant='', conversation='conv',
        turn='turn', attempt='original', task='original-task')
    monkeypatch.setattr(_rehydrate, '_recovery_allowed', lambda *a, **kw: True)
    monkeypatch.setattr(_rehydrate, '_rebuild_tool_list', lambda cfg: [])
    with pytest.raises(PermissionError, match='source scope'):
        _rehydrate._rehydrate_one(row)
    assert not created and not published
    start.assert_not_called()
