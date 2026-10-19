"""Goal pending seeds retain exact account/actor evidence but cannot dispatch."""
from copy import deepcopy

import pytest

from lib.agent_core.execution_budget import _ensure_execution_budget, inherit_execution_budget
from lib.goal_runs.execution_journal import GoalExecutionEvidence, create_goal_execution_journal
from lib.goal_runs.pending_recovery import preflight_pending_actors
from lib.goal_runs.recovery import GoalRecoverySeed
from lib.orchestration.execution_journal import JournalRecovery, RecoveryRefused
from tests.orchestration.test_invocation_frontier import fixture

pytestmark = pytest.mark.unit


def evidence_fixture():
    storage, admission, registration, response, calls, options = fixture()
    root = dict(id='flow-root', _userId=7, status='running', config={})
    child = dict(id='task', _userId=7, status='running', config={})
    account = _ensure_execution_budget(root); cuts = []
    account.bind_account_frontier_observer(lambda row: cuts.append(deepcopy(row)))
    inherit_execution_budget(root, child)
    lease = account.reserve_model(child, input_tokens=10000, max_output_tokens=200)
    lease.mark_dispatched(); lease.settle(response['state']['snapshot']['usage'][0])
    admission.update(type='execution_effect_started', key='root:agent:worker:0:0')
    evidence = GoalExecutionEvidence(dict(run_id='goal_flow-root', launch_id='launch',
        owner_user_id=7, task_id='flow-root', conversation_id='conversation'),
        JournalRecovery({}, {admission['key']: admission}), cuts[-1])
    bounds = {key: options[key] for key in ('header_bytes', 'snapshot_bytes', 'identity_bytes', 'io_timeout_ms')}
    return evidence, storage, bounds, response, calls


def test_pending_seed_preserves_paid_source_and_blocks_ordinary_goal_launch_before_writes():
    evidence, storage, bounds, response, calls = evidence_fixture()
    pending = preflight_pending_actors(evidence, storage=storage, frontier_bounds=bounds)
    actor = next(iter(pending.actors.values()))
    assert pending.source_run_id == 'goal_flow-root' and pending.source_launch_id == 'launch'
    assert pending.account_frontier['snapshot']['tokens'] == 10200
    assert pending.account_frontier['snapshot']['complete'] is False
    assert actor['frontier'] == response['state'] and actor['registration_version'] == 1
    assert actor['admission'] == next(iter(evidence.journal.pending_agents.values()))
    assert 'result' not in actor['admission']
    seed = GoalRecoverySeed('goal_flow-root', {}, evidence.metadata, {}, pending_actors=pending)
    assert seed.records == {} and seed.account_snapshot is None
    class NoWrites:
        def __getattr__(self, key): raise AssertionError('Pending seed reached admission')
    task = dict(id='successor', _userId=7, _goalRecoverySeed=seed)
    with pytest.raises(RecoveryRefused, match='accepted atomic takeover'):
        create_goal_execution_journal(task, {}, None, runs=NoWrites())
    assert '_executionBudget' not in task
    assert all(row[0] == 'frontier' for row in calls)
    actor['frontier']['snapshot']['host_fee_projection']['rounds'].clear()
    assert response['state']['snapshot']['host_fee_projection']['rounds']


@pytest.mark.parametrize('damage', ['bounds', 'no_cut', 'missing_actor', 'duplicate',
    'unknown', 'source', 'receipt_missing_account', 'receipt_newer', 'receipt_foreign'])
def test_pending_preflight_requires_one_complete_matching_inventory(damage):
    evidence, storage, bounds, response, calls = evidence_fixture()
    if damage == 'bounds': bounds['owner_user_id'] = 8
    elif damage == 'no_cut': evidence = GoalExecutionEvidence(evidence.metadata, evidence.journal, None)
    elif damage == 'missing_actor': evidence.journal.pending_agents.clear()
    elif damage == 'duplicate': evidence.journal.pending_agents['another'] = next(iter(evidence.journal.pending_agents.values()))
    elif damage == 'unknown': evidence.account_frontier['snapshot']['usage_known'] = False
    elif damage == 'source': evidence.metadata['task_id'] = 'other'
    else:
        admission = next(iter(evidence.journal.pending_agents.values()))
        receipt = {key: deepcopy(admission[key]) for key in ('kind','invocation_id','input_hash','invocation_completion')}
        receipt['result'] = {}
        if damage != 'receipt_missing_account':
            receipt['execution_account'] = deepcopy(evidence.account_frontier['snapshot'])
            if damage == 'receipt_newer': receipt['execution_account']['tokens'] += 1
            else: receipt['execution_account']['owner_user_id'] = 8
        evidence.journal.records['finished'] = receipt
    with pytest.raises(RecoveryRefused):
        preflight_pending_actors(evidence, storage=storage, frontier_bounds=bounds)
    assert not calls
