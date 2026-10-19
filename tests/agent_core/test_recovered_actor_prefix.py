"""Paid actor prefixes move once; replay never adds them to global spend again."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading

import pytest

from lib.agent_core.account_frontier import validate_frontier, validate_recovery_transition
from lib.agent_core.execution_budget import (
    _ensure_execution_budget, inherit_execution_budget, close_execution_budget, ModelBudgetExceeded,
)
from lib.goal_runs.account_journal import read_account_frontier
from lib.orchestration.execution_journal import RecoveryRefused

pytestmark = pytest.mark.unit
LIMITS = dict(max_tokens=80, max_tool_calls=8, deadline=None)


def root(identity, limits=None):
    task = dict(id=identity, _userId=7, status='running', config={'executionLimits': dict(limits or LIMITS)})
    return task, _ensure_execution_budget(task)


def child(parent, identity):
    task = dict(id=identity, _userId=7, _parentTaskId=parent['id'], status='running', config={})
    inherit_execution_budget(parent, task)
    return task


def spend(task, amount, tools=0):
    account = task['_executionBudget']
    if amount:
        lease = account.reserve_model(task, input_tokens=0, max_output_tokens=amount)
        lease.mark_dispatched(); lease.settle(dict(prompt_tokens=0, completion_tokens=amount))
    for _ in range(tools): assert account.reserve_tool(task) is None


def source():
    task, account = root('source'); cuts = []
    account.bind_account_frontier_observer(lambda cut: cuts.append(deepcopy(cut)))
    spend(task, 5, 1)
    closed = child(task, 'closed'); spend(closed, 7, 1); close_execution_budget(closed)
    a = child(task, 'source-a'); b = child(task, 'source-b')
    spend(a, 11, 2); spend(b, 13, 1)
    return cuts[-1]


def restored(frontier=None):
    frontier = source() if frontier is None else frontier
    task, account = root('target', frontier['snapshot']['limits'])
    account.restore_pending_frontier(task, frontier, owner=7)
    cuts = []; account.bind_account_frontier_observer(lambda cut: cuts.append(deepcopy(cut)))
    return task, account, cuts


def test_rebind_moves_paid_prefix_without_changing_total_and_preserves_new_spending():
    saved = source(); task, account, cuts = restored(saved)
    assert account.consumption() == (36, 5) and not account.actuals_complete()
    assert cuts[-1]['recovery']['pending_actors'] == {key: saved['actors'][key] for key in ('source-a','source-b')}
    assert cuts[-1]['closed_tokens'] == 12
    a = child(task, 'target-a'); account.rebind_actor_prefix(a, source_task_id='source-a', owner=7)
    assert account.consumption() == (36, 5) and account.tool_calls_for_control(a) == 2
    local = account.capture_actor_account(owner=7, task=a,
        counter_policy=dict(encoding='cl100k_base', output_field='max_tokens', wire_model='fixture'))
    assert local['tokens'] == 11 and local['tool_calls'] == 2
    assert local['model_ordinal'] == saved['actors']['source-a']['model_ordinal']
    spend(a, 3, 1); spend(task, 4)
    account.restore_pending_frontier(task, saved, owner=7)
    account.rebind_actor_prefix(a, source_task_id='source-a', owner=7)
    assert account.consumption() == (43, 6)
    close_execution_budget(a)
    assert not account.actuals_complete()  # Unbound source-b is still paid unresolved debt.
    b = child(task, 'target-b'); account.rebind_actor_prefix(b, source_task_id='source-b', owner=7)
    assert account.consumption() == (43, 6)
    close_execution_budget(b)
    assert account.actuals_complete()
    assert account.reserve_tool(task) is None and account.reserve_tool(task) is None
    assert account.reserve_tool(task) == 'execution_tool_budget_exhausted'
    lease = account.reserve_model(task, input_tokens=0, max_output_tokens=100)
    assert lease.max_output_tokens == 37
    lease.settle(unsent=True)
    for cut in cuts: validate_frontier(cut)
    for previous, current in zip(cuts,cuts[1:]): validate_recovery_transition(previous,current)
    metadata = dict(account_frontier_format=cuts[0]['format'],launch_id='launch',owner_user_id=7,task_id='target')
    events = [dict(type='execution_account_frontier',launch_id='launch',frontier=cut) for cut in cuts]
    assert read_account_frontier(events,metadata) == cuts[-1]
    successor, next_account = root('next')
    next_account.restore_replay_snapshot(successor, account.capture_replay_snapshot(task,owner=7), owner=7)
    assert next_account.consumption() == (43,8)


@pytest.mark.parametrize('damage', ['owner','limits','same_root','nested','unsettled','unknown',
    'missing_actors','observed','spent','hydrated_descendants','changed_retry'])
def test_invalid_restore_cannot_create_new_allowance(damage):
    saved = source(); task, account = root('target'); owner = 7
    if damage == 'owner': owner = 8
    elif damage == 'limits': saved['snapshot']['limits']['max_tokens'] += 1
    elif damage == 'same_root': task, account = root('source')
    elif damage == 'nested': saved['actors']['source-a']['parent_task_id'] = 'source-b'
    elif damage == 'unsettled': saved['snapshot']['settled'] = False
    elif damage == 'unknown': saved['snapshot']['usage_known'] = False
    elif damage == 'missing_actors':
        for key in ('source-a','source-b'):
            row = saved['actors'].pop(key); saved['closed_tokens'] += row['tokens']; saved['closed_tool_calls'] += row['tool_calls']
    elif damage == 'observed': account.bind_account_frontier_observer(lambda cut: None)
    elif damage == 'spent': spend(task,1)
    elif damage == 'hydrated_descendants':
        task['usage'] = task['_checkpointUsage'] = dict(prompt_tokens=0,completion_tokens=36)
    elif damage == 'changed_retry':
        account.restore_pending_frontier(task,saved,owner=owner); saved['version'] += 1
    before = account.consumption()
    with pytest.raises(ValueError): account.restore_pending_frontier(task,saved,owner=owner)
    assert account.consumption() == before


@pytest.mark.parametrize('damage', ['source','owner','object','parent','control','spent','reserved','tools','old_identity','changed_retry','duplicate_source'])
def test_actor_recipient_must_be_pristine_and_bound_once(damage):
    task, account, _ = restored()
    if damage == 'old_identity':
        with pytest.raises(ValueError): child(task,'source-a')
        assert account.consumption() == (36,5)
        return
    a = child(task,'target-a')
    source_id, owner = 'source-a', 7
    if damage == 'source': source_id = 'foreign'
    elif damage == 'owner': owner = 8
    elif damage == 'object': a = dict(a)
    elif damage == 'parent': a['_parentTaskId'] = 'foreign'
    elif damage == 'control': a['_executionBudgetControlTaskId'] = task['id']
    elif damage == 'spent': spend(a,1)
    elif damage == 'reserved': account.reserve_model(a,input_tokens=0,max_output_tokens=1)
    elif damage == 'tools': spend(a,0,1)
    elif damage in ('changed_retry','duplicate_source'):
        account.rebind_actor_prefix(a,source_task_id=source_id,owner=owner)
        if damage == 'changed_retry': source_id = 'source-b'
        else: a = child(task,'another')
    before = account.consumption()
    with pytest.raises(ValueError): account.rebind_actor_prefix(a,source_task_id=source_id,owner=owner)
    assert account.consumption() == before


def test_parallel_rebinds_and_duplicate_contenders_keep_one_paid_total():
    task, account, cuts = restored()
    a = child(task,'target-a'); b = child(task,'target-b'); start = threading.Barrier(3)
    def bind(target,source_id):
        start.wait(); account.rebind_actor_prefix(target,source_task_id=source_id,owner=7)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(bind,a,'source-a'),pool.submit(bind,a,'source-a'),pool.submit(bind,b,'source-b')]
        for future in futures: future.result()
    assert account.consumption() == (36,5)
    assert len([cut for cut in cuts if cut['reason']=='actor_rebound']) == 2
    assert cuts[-1]['recovery']['pending_actors'] == {}


def test_rebind_during_another_actors_request_preserves_reservation():
    task, account, cuts = restored(); a = child(task,'target-a')
    lease = account.reserve_model(task,input_tokens=0,max_output_tokens=10)
    lease.mark_dispatched()
    account.rebind_actor_prefix(a,source_task_id='source-a',owner=7)
    assert len(cuts[-1]['model_requests']) == 1 and not cuts[-1]['snapshot']['settled']
    lease.settle(dict(prompt_tokens=0,completion_tokens=10))
    assert account.consumption() == (46,5)


def test_failed_rebind_ack_keeps_paid_debt_and_fences_unbounded_admission():
    saved = source(); saved['snapshot']['limits'] = dict(max_tokens=None,max_tool_calls=None,deadline=None)
    task, account = root('target',saved['snapshot']['limits'])
    account.restore_pending_frontier(task,saved,owner=7)
    def observe(cut):
        if cut['reason']=='actor_rebound': raise OSError('lost ACK')
    account.bind_account_frontier_observer(observe)
    a = child(task,'target-a')
    with pytest.raises(OSError): account.rebind_actor_prefix(a,source_task_id='source-a',owner=7)
    assert account.consumption() == (36,5)
    with pytest.raises(ModelBudgetExceeded): account.reserve_model(a,input_tokens=0,max_output_tokens=1)
    assert account.reserve_tool(a) == 'execution_account_frontier_unavailable'
    with pytest.raises(ValueError): account.rebind_actor_prefix(a,source_task_id='source-a',owner=7)
    close_execution_budget(a)
    assert account.consumption() == (36,5) and not account.actuals_complete()


@pytest.mark.parametrize('damage', ['remove_recovery','drop_pending','alter_pending','resurrect','wrong_reason','source'])
def test_durable_history_cannot_erase_unbound_paid_actors(damage):
    _, _, cuts = restored(); before = cuts[-1]; after = deepcopy(before)
    after['version'] += 1; after['reason'] = 'tool_reserved'
    if damage == 'remove_recovery': after.pop('recovery')
    elif damage == 'drop_pending': after['recovery']['pending_actors'].pop('source-a')
    elif damage == 'alter_pending': after['recovery']['pending_actors']['source-a']['tokens'] += 1
    elif damage == 'resurrect': after['recovery']['pending_actors']['extra'] = deepcopy(after['recovery']['pending_actors']['source-a'])
    elif damage == 'source': after['recovery']['source_frontier_version'] += 1
    else: after['reason'] = 'actor_rebound'
    with pytest.raises(ValueError): validate_recovery_transition(before,after)
    metadata = dict(account_frontier_format=before['format'],launch_id='launch',owner_user_id=7,task_id='target')
    events = [dict(type='execution_account_frontier',launch_id='launch',frontier=cut) for cut in (before,after)]
    with pytest.raises(RecoveryRefused): read_account_frontier(events,metadata)


@pytest.mark.parametrize('limit', ['zero_tokens','zero_tools','expired'])
def test_original_zero_caps_and_expiry_remain_binding_after_prefix_restore(limit):
    saved = source()
    if limit == 'zero_tokens': saved['snapshot']['limits']['max_tokens'] = 0
    elif limit == 'zero_tools': saved['snapshot']['limits']['max_tool_calls'] = 0
    else: saved['snapshot']['limits']['deadline'] = 1.0
    task, account, _ = restored(saved); a = child(task,'target-a')
    account.rebind_actor_prefix(a,source_task_id='source-a',owner=7)
    assert account.consumption() == (36,5)
    if limit != 'zero_tools':
        with pytest.raises(ModelBudgetExceeded): account.reserve_model(a,input_tokens=0,max_output_tokens=1)
    assert account.reserve_tool(a) is not None


def test_zero_cost_pending_actor_still_prevents_a_complete_account():
    saved = source()
    for key in ('source-a','source-b'):
        saved['snapshot']['tokens'] -= saved['actors'][key]['tokens']
        saved['snapshot']['tool_calls'] -= saved['actors'][key]['tool_calls']
        saved['actors'][key]['tokens'] = saved['actors'][key]['tool_calls'] = 0
    task, account, cuts = restored(saved)
    assert account.consumption() == (12,2) and not account.actuals_complete()
    for source_id in ('source-a','source-b'):
        target = child(task,'recipient-'+source_id)
        account.rebind_actor_prefix(target,source_task_id=source_id,owner=7)
        close_execution_budget(target)
    assert account.actuals_complete() and account.consumption() == (12,2)
    for before, after in zip(cuts,cuts[1:]): validate_recovery_transition(before,after)


def test_rebind_receipt_cannot_move_unknown_work_into_closed_counters():
    task, account, cuts = restored(); a = child(task,'target-a')
    account.rebind_actor_prefix(a,source_task_id='source-a',owner=7)
    before, after = deepcopy(cuts[-2]), deepcopy(cuts[-1])
    after['closed_tokens'] += after['actors']['target-a']['tokens']
    after['closed_tool_calls'] += after['actors']['target-a']['tool_calls']
    after['actors']['target-a'] = deepcopy(before['actors']['target-a'])
    validate_frontier(after)  # Totals alone look valid; the transition must refuse.
    with pytest.raises(ValueError,match='pristine recipient'): validate_recovery_transition(before,after)


def test_pending_native_preflight_cannot_omit_unbound_recovery_debt():
    from lib.orchestration.invocation_frontier import preflight_registered_actor_accounts
    task, account, cuts = restored()
    with pytest.raises(RecoveryRefused,match='settled direct actor set'):
        preflight_registered_actor_accounts(None,cuts[-1],[],owner_user_id=7,
            parent_task_id='target',conversation_id='conversation',header_bytes=4096,
            snapshot_bytes=1048576,identity_bytes=1024,io_timeout_ms=1000)


@pytest.mark.parametrize('damage', ['empty_id','long_id','integer_id','negative_tools','changed_live_id'])
def test_hot_membership_checks_keep_malformed_actor_cuts_out_of_durable_history(damage):
    task, account = root('ordinary'); cuts=[]
    account.bind_account_frontier_observer(lambda cut:cuts.append(deepcopy(cut)))
    identity={'empty_id':'','long_id':'x'*513,'integer_id':7}.get(damage,'child')
    actor=dict(id=identity,_userId=7,_parentTaskId=task['id'],status='running',config={})
    if damage=='negative_tools':actor['_executionToolsAdmitted']=-1
    if damage=='changed_live_id':
        inherit_execution_budget(task,actor);actor['id']='tampered'
        with pytest.raises(ValueError):account.reserve_tool(task)
    else:
        with pytest.raises(ValueError):inherit_execution_budget(task,actor)
    assert account.reason()=='execution_account_frontier_unavailable'
    for cut in cuts:validate_frontier(cut)
    assert all(set(cut['actors'])<= {'ordinary','child'} for cut in cuts)


def test_two_recipients_cannot_claim_the_same_paid_source_concurrently():
    task, account, cuts = restored()
    candidates=[child(task,'candidate-a'),child(task,'candidate-b')];start=threading.Barrier(2)
    def bind(target):
        start.wait()
        try: account.rebind_actor_prefix(target,source_task_id='source-a',owner=7)
        except ValueError: return False
        return True
    with ThreadPoolExecutor(max_workers=2) as pool: outcomes=list(pool.map(bind,candidates))
    assert sorted(outcomes)==[False,True] and account.consumption()==(36,5)
    assert len([cut for cut in cuts if cut['reason']=='actor_rebound'])==1
