"""Mixed completed/pending Goal fees remain evidence, with exact source versions."""
from copy import deepcopy
from dataclasses import replace

import pytest

from lib.billing.cost import transfer_native_child_fees
from lib.billing.native_actor_fees import capture
from lib.goal_runs.pending_recovery import preflight_pending_actors
from lib.goal_runs.pending_fee_evidence import preflight_pending_fees
from lib.orchestration.execution_journal import RecoveryRefused, encode_agent_result
from lib.orchestration.invocation_recovery import FIELD
from tests.billing.test_recovered_actor_fee_transfer import root, child
from tests.billing.test_native_actor_fee_evidence import paid
from tests.goals.test_goal_pending_recovery import evidence_fixture

pytestmark = pytest.mark.unit


def mixed():
    evidence, storage, bounds, response, calls = evidence_fixture()
    parent, account = root('flow-root'); cuts = []
    account.bind_account_frontier_observer(lambda cut: cuts.append(deepcopy(cut)))
    active = child(parent, 'task'); paid(active)
    closed = child(parent, 'finished'); paid(closed); closed['status'] = 'done'
    transfer_native_child_fees(parent, closed); account.close(closed)
    evidence = replace(evidence, account_frontier=cuts[-1])
    evidence.metadata['launch'] = dict(model='alpha')
    admission = deepcopy(next(iter(evidence.journal.pending_agents.values())))
    admission.update(key='root:agent:finished:0:0', invocation_id='finished')
    binding = admission['invocation_completion']; binding['nonce'] = 'b'*32
    binding['identity'].update(child_task_id='finished', archive_scope='invocation:7:finished')
    receipt = {key: deepcopy(admission[key]) for key in ('kind', 'input_hash', 'invocation_id', 'invocation_completion')}
    receipt.update(result=encode_agent_result({'output':'kept'}), execution_account=deepcopy(cuts[-1]['snapshot']))
    evidence.journal.records[admission['key']] = receipt
    record = dict(version=4, value=dict(task_id='finished', user_id=7, conv_id='conversation',
        child_invocation=deepcopy(binding['identity'])))
    record['value'][FIELD] = dict(admission=admission, receipt=deepcopy(receipt), native_fees=capture(closed))
    query = storage.query
    def read(op, payload):
        if op == 'record.get' and payload['key'] == 'finished': return deepcopy(record)
        return query(op, payload)
    storage.query = read
    pending = preflight_pending_actors(evidence, storage=storage, frontier_bounds=bounds)
    return evidence, pending, storage, record, calls


def candidate(evidence, pending, storage):
    return preflight_pending_fees(evidence, pending, storage=storage,
        wallet_user_id='original-wallet', reserved_micro=100000)


def test_mixed_goal_fees_preserve_source_versions_and_do_not_sum_receipt_accounts():
    evidence, pending, storage, record, calls = mixed()
    before = deepcopy((pending, record))
    result = candidate(evidence, pending, storage)
    assert result == candidate(evidence, pending, storage)
    assert result.invoice['componentsMicro']['totalMicro'] == 15186
    assert result.invoice['roundCount'] == 2 and result.account_frontier['snapshot']['tokens'] == 20400
    assert result.actors['root:agent:finished:0:0']['registration_version'] == 4
    assert result.actors['root:agent:worker:0:0']['checkpoint_version'] == 3
    assert all(call[0] == 'frontier' for call in calls) and (pending, record) == before
    result.actors['root:agent:finished:0:0']['evidence']['rounds'].clear()
    assert candidate(evidence, pending, storage).invoice['roundCount'] == 2
    from lib.goal_runs.recovery import GoalRecoverySeed
    with pytest.raises(RecoveryRefused, match='atomic takeover'):
        GoalRecoverySeed('goal_flow-root', {}, evidence.metadata, evidence.journal.records,
            pending_actors=pending).require_settled()


@pytest.mark.parametrize('damage', ['missing_fees', 'receipt', 'admission', 'launch', 'frontier',
    'pending_admission', 'missing_completed', 'missing_pending', 'root_spend', 'foreign_conversation',
    'registration_version', 'checkpoint_version'])
def test_goal_candidate_requires_exact_receipts_launch_and_total_coverage(damage):
    evidence, pending, storage, record, calls = mixed()
    if damage == 'missing_fees': record['value'][FIELD].pop('native_fees')
    elif damage == 'receipt': record['value'][FIELD]['receipt']['result']['output'] = 'changed'
    elif damage == 'admission': record['value'][FIELD]['admission']['key'] = 'other'
    elif damage == 'launch': evidence.metadata['launch_id'] = 'other'
    elif damage == 'frontier': evidence.account_frontier['version'] += 1
    elif damage == 'pending_admission': next(iter(pending.actors.values()))['admission']['input_hash'] = 'b'*64
    elif damage == 'missing_completed': evidence.journal.records.clear()
    elif damage == 'missing_pending': pending.actors.clear()
    elif damage == 'root_spend':
        for cut in (pending.account_frontier, evidence.account_frontier):
            cut['snapshot']['root_tokens'] = 1; cut['snapshot']['tokens'] += 1
            cut['actors']['flow-root']['tokens'] = 1
    elif damage == 'foreign_conversation': evidence.metadata['conversation_id'] = 'foreign'
    elif damage == 'registration_version': next(iter(pending.actors.values()))['registration_version'] = True
    else: next(iter(pending.actors.values()))['frontier']['version'] = 0
    with pytest.raises(RecoveryRefused): candidate(evidence, pending, storage)
