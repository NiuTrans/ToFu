"""Shared admission cannot multiply a parent's budget through delegation."""
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from lib.agent_core.execution_budget import (
    ModelBudgetExceeded, inherit_execution_budget, close_execution_budget)
from lib.agent_core.execution_limits import admit_tool_execution, execution_limit_reason
from lib.evolution.bridge import derive_actuals

pytestmark = pytest.mark.unit


def root():
    return {'id': 'root', 'config': {'executionLimits': {
        'max_tokens': 1000, 'max_tool_calls': 9, 'deadline': 4_000_000_000}},
        'usage': {'prompt_tokens': 100}, 'abort_event': threading.Event()}


def child(parent, name):
    value = {'id': name, 'config': {}, 'abort_event': threading.Event()}
    inherit_execution_budget(parent, value)
    return value


def test_overlapping_waves_share_atomic_tool_admission():
    parent = root()
    first = [child(parent, f'a-{i}') for i in range(5)]
    second = [child(parent, f'b-{i}') for i in range(5)]
    with ThreadPoolExecutor(max_workers=10) as pool:
        reasons = list(pool.map(admit_tool_execution, first + second))
    assert reasons.count(None) == 9
    assert reasons.count('execution_tool_budget_exhausted') == 1
    assert parent['_executionBudget'].consumption() == (100, 9)


def test_restored_child_usage_and_tool_slots_charge_shared_admission_once():
    parent = root()
    restored = {'id': 'resumed', 'config': {},
        'usage': {'prompt_tokens': 850, 'completion_tokens': 50},
        '_executionToolsAdmitted': 8}
    inherit_execution_budget(parent, restored)
    budget = parent['_executionBudget']
    assert budget.consumption() == (1000, 8)
    with pytest.raises(ValueError, match='already active'):
        budget.attach(restored)
    assert budget.consumption() == (1000, 8)
    assert execution_limit_reason(parent) == 'execution_token_budget_exhausted'
    close_execution_budget(restored)
    close_execution_budget(restored)
    assert budget.consumption() == (1000, 8)


def test_restored_unknown_consumption_fences_parent_after_child_closes():
    parent = root()
    restored = {'id': 'resumed', 'config': {},
        'usage': {'prompt_tokens': 200}, '_executionActualsComplete': False}
    inherit_execution_budget(parent, restored)
    assert execution_limit_reason(parent) == 'execution_token_usage_unknown'
    close_execution_budget(restored)
    assert execution_limit_reason(parent) == 'execution_token_usage_unknown'


def test_nested_flow_and_swarm_descendants_retain_usage_after_close():
    parent = root()
    worker = child(parent, 'flow-worker')
    worker['usage'] = {'prompt_tokens': 200}
    delegate = child(worker, 'swarm-delegate')
    delegate['usage'] = {'prompt_tokens': 750}
    assert execution_limit_reason(worker) == 'execution_token_budget_exhausted'
    close_execution_budget(delegate)
    close_execution_budget(delegate)
    close_execution_budget(worker)
    assert parent['_executionBudget'].consumption() == (1050, 0)
    assert derive_actuals(parent)['tokens'] == 1050


def test_local_slice_can_restrict_but_not_expand_tree_budget():
    parent = root()
    worker = child(parent, 'worker')
    worker['config']['executionLimits']['max_tool_calls'] = 1
    assert admit_tool_execution(worker) is None
    assert admit_tool_execution(worker) == 'execution_tool_budget_exhausted'
    assert parent['_executionBudget'].consumption()[1] == 1


def test_unbounded_invocations_allocate_no_account():
    value = child({'id': 'root', 'config': {}}, 'child')
    assert '_executionBudget' not in value
    assert value['config'] == {}


@pytest.mark.parametrize('limits', [{}, {'max_tokens': True}, {'max_tokens': 1, 'max_tool_calls': 1, 'deadline': float('nan')}, 'unbounded'])
def test_malformed_supplied_budget_is_rejected(limits):
    from lib.agent_core.execution_limits import validate_execution_limits
    with pytest.raises(ValueError):
        validate_execution_limits(limits)


def test_child_monitor_observes_inherited_deadline_and_progress():
    from lib.tasks_pkg.invocation import invocation_abort_requested
    from lib.swarm.liveness import ProgressBeacon
    task = root()
    task['config']['executionLimits']['deadline'] = 1
    assert invocation_abort_requested(task)
    assert task['_invocationStopReason'] == 'deadline'
    task = root()
    task['_invocationProgressBeacon'] = ProgressBeacon(stall_timeout=0)
    task['_invocationProgressBeacon'].touch(task['id'])
    assert invocation_abort_requested(task)
    assert task['_invocationStopReason'] == 'stalled'


def test_real_swarm_detached_carrier_shares_the_flow_budget():
    from lib.swarm.master import MasterOrchestrator
    parent = root()
    parent['_userId'] = 7
    master = MasterOrchestrator('session', '', [], user_id=7, parent_task=parent)
    proxy = master._parent_task_proxy
    assert proxy is not parent
    assert proxy['_executionBudget'] is parent['_executionBudget']
    delegated = child(proxy, 'swarm-child')
    for _ in range(9):
        assert admit_tool_execution(delegated) is None
    assert admit_tool_execution(parent) == 'execution_tool_budget_exhausted'
    close_execution_budget(delegated)
    assert len(parent['_executionBudget']._active) == 1


def test_swarm_session_root_outlives_spawning_turn_for_blocked_dependents():
    """A fire-and-forget swarm keeps admitting dependency-blocked agents long
    after the spawning chat task settled 'done'.

    Regression for the bug where a spec that only unblocked after its
    dependency finished (e.g. ``tls`` waiting on ``nsb``) was killed with
    'execution_owner_cancelled' at admission, because the shared budget
    account's root was the short-lived spawning chat task (already terminal).
    The session proxy is now the budget root and stays 'running' until the
    swarm is explicitly aborted/suspended.
    """
    from lib.swarm.master import MasterOrchestrator
    parent = root()
    parent['_userId'] = 7
    master = MasterOrchestrator('session', '', [], user_id=7, parent_task=parent)
    proxy = master._parent_task_proxy
    account = parent['_executionBudget']
    # The budget root is now the long-lived session proxy, not the chat task.
    assert account.root_id == proxy['id']
    assert proxy['status'] == 'running'
    # The spawning chat task settles 'done' while a dependent is still queued.
    parent['status'] = 'done'
    # A dependency-blocked child admitted later still attaches and admits.
    blocked = child(proxy, 'blocked-dependent')
    assert admit_tool_execution(blocked) is None
    lease = account.reserve_model(blocked, input_tokens=10, max_output_tokens=5)
    lease.mark_dispatched()
    lease.settle({'input_tokens': 10, 'output_tokens': 2})
    close_execution_budget(blocked)
    assert account.consumption() == (112, 1)
    # An explicit whole-swarm abort still fences every later admission.
    master.abort()
    with pytest.raises(ModelBudgetExceeded, match='owner_cancelled'):
        child(proxy, 'after-abort')
    assert admit_tool_execution(blocked) == 'execution_owner_cancelled'


def test_nullable_limits_keep_usage_accounting_without_old_cumulative_caps():
    from lib.agent_core.execution_limits import validate_execution_limits
    from lib.swarm.integration._tools import plan_child_execution_limits
    from lib.tasks_pkg.invocation import invocation_abort_requested
    parent = root()
    parent['config']['executionLimits'] = {
        'max_tokens': None, 'max_tool_calls': None, 'deadline': None}
    validate_execution_limits(parent['config']['executionLimits'])
    worker = child(parent, 'unlimited-worker')
    worker['usage'] = {'prompt_tokens': 211730}
    for _ in range(150):
        assert admit_tool_execution(worker) is None
    assert execution_limit_reason(parent) is None
    assert not invocation_abort_requested(worker)
    assert plan_child_execution_limits(parent, 3) == (
        {'max_tokens': None, 'max_tool_calls': None, 'deadline': None}, None)
    close_execution_budget(worker)
    assert parent['_executionBudget'].consumption() == (211830, 150)
    assert len(parent['_executionBudget']._active) == 1
    assert derive_actuals(parent)['tokens'] == 211830
    parent['aborted'] = True
    assert invocation_abort_requested(parent)


@pytest.mark.parametrize(('dimension', 'limit', 'expected'), [
    ('max_tokens', 100, 'execution_token_budget_exhausted'),
    ('max_tool_calls', 0, 'execution_tool_budget_exhausted'),
    ('deadline', 1, 'execution_deadline_exceeded'),
])
def test_independent_explicit_dimension_is_enforced_in_shared_tree(dimension, limit, expected):
    parent = root()
    parent['config']['executionLimits'] = {
        'max_tokens': None, 'max_tool_calls': None, 'deadline': None, dimension: limit}
    worker = child(parent, 'worker')
    assert execution_limit_reason(worker, pending_tools=1) == expected
    assert admit_tool_execution(worker) == expected
    assert worker['aborted']
    assert worker['abort_event'].is_set()
    close_execution_budget(worker)


def test_unlimited_token_dimension_does_not_hide_explicit_child_tool_limit():
    from lib.swarm.integration._tools import plan_child_execution_limits
    parent = root()
    parent['config']['executionLimits'] = {
        'max_tokens': None, 'max_tool_calls': 2, 'deadline': None}
    limits, error = plan_child_execution_limits(parent, 2)
    assert error is None
    assert limits == {'max_tokens': None, 'max_tool_calls': 1, 'deadline': None}
    first = child(parent, 'first')
    second = child(parent, 'second')
    assert admit_tool_execution(first) is None
    assert admit_tool_execution(second) is None
    assert admit_tool_execution(parent) == 'execution_tool_budget_exhausted'


def test_continuity_attempts_distinguish_admission_dispatch_and_late_settlement():
    parent = root()
    worker = child(parent, 'worker')
    account = parent['_executionBudget']
    first = account.reserve_model(parent, input_tokens=20, max_output_tokens=30)
    second = account.reserve_model(worker, input_tokens=10, max_output_tokens=15)
    try:
        snapshot = account.continuity_snapshot()
        assert snapshot['model_attempts'] == [
            {'attempt_ordinal':first.ordinal, 'task_id':parent['id'], 'dispatched':False, 'reserved_tokens':50},
            {'attempt_ordinal':second.ordinal, 'task_id':worker['id'], 'dispatched':False, 'reserved_tokens':25}]
        assert snapshot['pending_model_requests'] == 2
        snapshot['model_attempts'][0]['dispatched'] = True
        assert account.continuity_snapshot()['model_attempts'][0]['dispatched'] is False
        second.mark_dispatched()
        close_execution_budget(worker)
        observed = account.continuity_snapshot()
        assert not observed['active_child_task_ids']
        assert observed['model_attempts'][1]['dispatched'] is True
        # A closed child may still have a physically dispatched model request.
        assert observed['model_attempts'][1]['task_id'] == worker['id']
        assert account.consumption() == (100, 0)
        assert account.actuals_complete() is False
        first.settle(unsent=True)
        second.settle({'input_tokens':10,'output_tokens':2})
        settled = account.continuity_snapshot()
        assert settled['model_attempts'] == [] and settled['pending_model_requests'] == 0
        assert not settled['unknown_usage'] and account.consumption() == (112, 0)
        assert account.actuals_complete()
        next_attempt = account.reserve_model(parent,input_tokens=1,max_output_tokens=1)
        try:
            assert next_attempt.ordinal > second.ordinal
        finally:
            next_attempt.settle(unsent=True)
    finally:
        first.settle(unsent=True)
        second.settle(unsent=True)
        close_execution_budget(worker)


def test_model_send_observation_failure_refuses_dispatch_but_can_void():
    parent=root();worker=child(parent,'worker');account=parent['_executionBudget'];events=[]
    def observe(event):
        events.append(dict(event))
        if event['phase']=='dispatch_intent':raise RuntimeError('intent not acknowledged')
    account.bind_model_attempt_observer(observe)
    lease=account.reserve_model(worker,input_tokens=10,max_output_tokens=5)
    with pytest.raises(RuntimeError,match='not acknowledged'):lease.mark_dispatched()
    assert not lease.dispatched and not account.continuity_snapshot()['model_attempts'][0]['dispatched']
    lease.settle(unsent=True);close_execution_budget(worker)
    assert [event['phase'] for event in events]==['dispatch_intent','voided']
    assert events[-1]['reported_tokens']==0 and events[-1]['usage_complete'] is True
    assert account.consumption()==(100,0) and account.actuals_complete()


def test_model_settlement_observation_failure_keeps_paid_usage_and_releases_waiters():
    from lib.agent_core.execution_budget import ModelBudgetExceeded
    parent=root();worker=child(parent,'worker');account=parent['_executionBudget']
    def observe(event):
        if event['phase']=='settled':raise RuntimeError('terminal observation lost')
    account.bind_model_attempt_observer(observe)
    lease=account.reserve_model(worker,input_tokens=10,max_output_tokens=5)
    lease.mark_dispatched();lease.settle({'input_tokens':10,'output_tokens':2});lease.settle(unsent=True)
    close_execution_budget(worker)
    assert account.consumption()==(112,0)
    assert account.continuity_snapshot()['model_attempts']==[]
    assert not account.actuals_complete() and parent['_executionActualsComplete'] is False
    with pytest.raises(ModelBudgetExceeded,match='usage_unknown'):
        account.reserve_model(parent,input_tokens=1,max_output_tokens=1)


def test_model_observer_cannot_replace_an_existing_admission_authority():
    parent=root();worker=child(parent,'worker');account=parent['_executionBudget']
    lease=account.reserve_model(worker,input_tokens=10,max_output_tokens=5)
    try:
        with pytest.raises(ValueError,match='active account'):
            account.bind_model_attempt_observer(lambda event:None)
    finally:
        lease.settle(unsent=True);close_execution_budget(worker)


@pytest.mark.parametrize('change',['cancel','deadline'])
def test_model_observation_delay_cannot_send_after_owner_cancellation(monkeypatch,change):
    from lib.llm.llm_errors import AbortedError
    parent=root();worker=child(parent,'worker');account=parent['_executionBudget']
    def observe(event):
        if event['phase']=='dispatch_intent':
            if change=='cancel':worker['abort_event'].set()
            else:monkeypatch.setattr('lib.agent_core.execution_budget.time.time',lambda:4_000_000_001)
    account.bind_model_attempt_observer(observe)
    lease=account.reserve_model(worker,input_tokens=10,max_output_tokens=5,
                                cancelled=worker['abort_event'].is_set)
    expected='owner_cancelled' if change=='cancel' else 'deadline_exceeded'
    with pytest.raises(AbortedError,match=expected):lease.mark_dispatched()
    assert not lease.dispatched
    lease.settle(unsent=True);close_execution_budget(worker)
    assert account.consumption()==(100,0) and account.actuals_complete()


def test_child_witnesses_refuse_unacknowledged_attachment_and_preserve_late_requests():
    from lib.agent_core.execution_budget import ExecutionBudget
    parent=root();events=[];failed=[]
    def observe(event):
        events.append(dict(event))
        if event['phase']=='attach_intent' and not failed:
            failed.append(True);raise RuntimeError('attachment acknowledgement lost')
    parent['_executionChildObserver']=observe;parent['_executionBudget']=ExecutionBudget(parent)
    with pytest.raises(RuntimeError,match='acknowledgement lost'):child(parent,'rejected')
    account=parent['_executionBudget']
    assert 'rejected' not in account._active and events[-1]['phase']=='attachment_voided'
    worker=child(parent,'admitted')
    assert events[-1]['child_ordinal']>events[0]['child_ordinal']
    lease=account.reserve_model(worker,input_tokens=10,max_output_tokens=5)
    lease.mark_dispatched();close_execution_budget(worker)
    assert events[-1]['phase']=='detached' and events[-1]['pending_model_requests']==1
    assert not account._child_records
    lease.settle({'input_tokens':10,'output_tokens':2})
    assert account.consumption()==(112,0) and account.actuals_complete()


def test_child_detachment_failure_releases_membership_and_retains_incompleteness():
    from lib.agent_core.execution_budget import ExecutionBudget
    parent=root()
    def observe(event):
        if event['phase']=='detached':raise RuntimeError('detachment not acknowledged')
    parent['_executionChildObserver']=observe;parent['_executionBudget']=ExecutionBudget(parent)
    worker=child(parent,'worker');worker['usage']={'input_tokens':20}
    close_execution_budget(worker);close_execution_budget(worker)
    assert parent['_executionBudget'].consumption()==(120,0)
    assert not parent['_executionBudget']._child_records
    assert not parent['_executionBudget'].actuals_complete()


def test_closed_child_control_identity_cannot_admit_a_new_descendant():
    from lib.agent_core.execution_budget import ExecutionBudget,ModelBudgetExceeded
    parent=root();parent['_executionChildObserver']=lambda event:None
    parent['_executionBudget']=ExecutionBudget(parent)
    worker=child(parent,'worker');proxy={'id':'proxy','config':{}}
    from lib.agent_core.execution_budget import share_execution_budget
    share_execution_budget(worker,proxy)
    close_execution_budget(worker)
    with pytest.raises(ModelBudgetExceeded,match='owner_cancelled'):child(proxy,'late')
    assert 'late' not in parent['_executionBudget']._active


def test_detached_child_cannot_receive_a_tool_slot_or_change_shared_usage():
    parent=root();worker=child(parent,'closed-worker')
    close_execution_budget(worker)
    before=parent['_executionBudget'].consumption()
    assert admit_tool_execution(worker)=='execution_owner_cancelled'
    assert parent['_executionBudget'].consumption()==before
    assert not worker.get('_executionToolsAdmitted') and worker['abort_event'].is_set()


def test_missing_local_limits_cannot_bypass_shared_tool_budget():
    parent=root();worker=child(parent,'worker')
    worker['config'].pop('executionLimits')
    for _ in range(9):assert admit_tool_execution(worker) is None
    assert admit_tool_execution(worker)=='execution_tool_budget_exhausted'
    assert parent['_executionBudget'].consumption()==(100,9)
    close_execution_budget(worker)


def test_detached_control_carrier_cannot_execute_after_its_owner_closes():
    from lib.agent_core.execution_budget import share_execution_budget
    parent=root();worker=child(parent,'worker');proxy={'id':'proxy','config':{}}
    share_execution_budget(worker,proxy)
    assert admit_tool_execution(proxy) is None
    close_execution_budget(worker)
    assert admit_tool_execution(proxy)=='execution_owner_cancelled'
    assert parent['_executionBudget'].consumption()==(100,1)


def test_live_control_carrier_cannot_execute_after_owner_cancellation():
    from lib.agent_core.execution_budget import share_execution_budget
    parent=root();proxy={'id':'proxy','config':{}}
    share_execution_budget(parent,proxy)
    parent['abort_event'].set()
    assert admit_tool_execution(proxy)=='execution_owner_cancelled'
    assert parent['_executionBudget'].consumption()==(100,0)


def test_closed_child_is_refused_by_the_real_tool_executor_before_handler_resolution(tmp_path):
    from types import SimpleNamespace
    from lib.tasks_pkg.executor import _execute_tool_one
    parent=root();worker=child(parent,'closed-worker');marker=tmp_path/'unexpected-effect'
    def handler(*args):
        marker.write_text('handler entered')
        return 'call-1','done',False
    worker['_tool_env']=SimpleNamespace(resolve=lambda name:handler)
    close_execution_budget(worker)
    result=_execute_tool_one(worker,{'id':'call-1'},'write_file','call-1',{},1,{},worker['config'],None,False)
    assert result==('call-1','execution_owner_cancelled',False)
    assert not marker.exists() and parent['_executionBudget'].consumption()==(100,0)


def test_detachment_retains_pending_tools_of_a_declared_control_carrier():
    from lib.agent_core.execution_budget import ExecutionBudget,share_execution_budget
    parent=root();child_events=[];tool_events=[]
    parent['_executionChildObserver']=child_events.append
    parent['_executionToolObserver']=tool_events.append
    parent['_executionBudget']=ExecutionBudget(parent)
    worker=child(parent,'worker');proxy={'id':'proxy','config':{}}
    share_execution_budget(worker,proxy)
    account=parent['_executionBudget']
    event=account.begin_tool_attempt(proxy,'call','fixture-tool',1)
    assert event['task_id']=='proxy' and event['control_task_id']=='worker'
    close_execution_budget(worker)
    assert child_events[-1]['phase']=='detached'
    assert child_events[-1]['pending_tool_attempts']==1
    assert account.actuals_complete() is False
    snapshot=account.continuity_snapshot()['pending_tool_attempts']
    assert len(snapshot)==1 and snapshot[0]['control_task_id']=='worker'
    account.finish_tool_attempt(event,phase='executor_returned')
    assert account.actuals_complete() and not account._tool_attempts
    assert tool_events[-1]['control_task_id']=='worker'


def test_observer_bundle_refuses_tool_history_without_installing_partial_ports():
    from lib.agent_core.execution_budget import ExecutionBudget
    parent=root();account=ExecutionBudget(parent)
    assert account.reserve_tool(parent) is None
    with pytest.raises(ValueError,match='tool observation'):
        account.bind_model_attempt_observer(lambda event:None,
            child_observer=lambda event:None,tool_observer=lambda event:None)
    assert account._model_attempt_observer is None and account._child_observer is None
    assert account._tool_observer is None
    assert account.consumption()==(100,1)


def test_restored_tool_charges_cannot_be_relabelled_as_fresh_observation_history():
    from lib.agent_core.execution_budget import ExecutionBudget
    parent=root();parent['_executionToolsAdmitted']=3
    account=ExecutionBudget(parent)
    with pytest.raises(ValueError,match='tool observation'):
        account.bind_model_attempt_observer(lambda event:None,tool_observer=lambda event:None)
    assert account._model_attempt_observer is None and account.consumption()==(100,3)


@pytest.mark.parametrize('status',['done','error','aborted','interrupted'])
def test_terminal_root_rejects_new_model_tool_and_child_work_without_cancellation_flag(status):
    from lib.agent_core.execution_budget import ExecutionBudget,ModelBudgetExceeded,share_execution_budget
    parent=root();account=ExecutionBudget(parent);parent['_executionBudget']=account
    proxy={'id':'proxy','config':{}}
    share_execution_budget(parent,proxy)
    parent['status']=status
    with pytest.raises(ModelBudgetExceeded,match='owner_cancelled'):
        account.reserve_model(parent,input_tokens=10,max_output_tokens=5)
    assert admit_tool_execution(proxy)=='execution_owner_cancelled'
    with pytest.raises(ModelBudgetExceeded,match='owner_cancelled'):child(proxy,'late-child')
    assert account.consumption()==(100,0) and not account._model_leases


def test_terminal_transition_between_model_reservation_and_send_refuses_dispatch_but_settles_lease():
    from lib.agent_core.execution_budget import ExecutionBudget
    from lib.llm.llm_errors import AbortedError
    parent=root();account=ExecutionBudget(parent)
    lease=account.reserve_model(parent,input_tokens=10,max_output_tokens=5)
    parent['status']='done'
    with pytest.raises(AbortedError,match='owner_cancelled'):lease.mark_dispatched()
    lease.settle(unsent=True)
    assert not lease.dispatched and account.consumption()==(100,0) and account.actuals_complete()


def test_reported_usage_of_already_dispatched_request_survives_terminal_owner():
    from lib.agent_core.execution_budget import ExecutionBudget
    parent=root();account=ExecutionBudget(parent)
    lease=account.reserve_model(parent,input_tokens=10,max_output_tokens=5)
    lease.mark_dispatched();parent['status']='done'
    lease.settle({'input_tokens':10,'output_tokens':2})
    assert account.consumption()==(112,0) and account.actuals_complete()


def test_detached_same_identity_model_carrier_cannot_hide_terminal_owner_status():
    from lib.agent_core.execution_budget import ExecutionBudget,ModelBudgetExceeded,share_execution_budget
    parent=root();parent['_executionBudget']=ExecutionBudget(parent)
    carrier={'id':parent['id'],'config':{}}
    share_execution_budget(parent,carrier)
    parent['status']='done'
    assert 'status' not in carrier
    with pytest.raises(ModelBudgetExceeded,match='owner_cancelled'):
        parent['_executionBudget'].reserve_model(carrier,input_tokens=10,max_output_tokens=5)
    assert parent['_executionBudget'].consumption()==(100,0)


def test_multiple_detached_carriers_share_one_owner_local_tool_slice_atomically():
    from lib.agent_core.execution_budget import share_execution_budget
    parent=root();worker=child(parent,'worker')
    worker['config']['executionLimits']['max_tool_calls']=1
    proxies=[{'id':'proxy-'+str(number),'config':{}} for number in range(8)]
    for proxy in proxies:share_execution_budget(worker,proxy)
    with ThreadPoolExecutor(max_workers=8) as pool:
        reasons=list(pool.map(admit_tool_execution,proxies))
    assert reasons.count(None)==1 and reasons.count('execution_tool_budget_exhausted')==7
    account=parent['_executionBudget']
    assert account.consumption()==(100,1)
    assert account.allowance_snapshot(worker)['tool_calls_available']==0
    assert execution_limit_reason(worker,pending_tools=1)=='execution_tool_budget_exhausted'
    close_execution_budget(worker)
    assert 'worker' not in account._control_tool_calls


def test_missing_proxy_limits_cannot_expand_the_control_owner_slice():
    from lib.agent_core.execution_budget import share_execution_budget
    parent=root();worker=child(parent,'worker')
    worker['config']['executionLimits']['max_tool_calls']=1
    first={'id':'first-proxy','config':{}};second={'id':'second-proxy','config':{}}
    share_execution_budget(worker,first);share_execution_budget(worker,second)
    first['config'].pop('executionLimits');second['config'].pop('executionLimits')
    assert admit_tool_execution(first) is None
    assert admit_tool_execution(second)=='execution_tool_budget_exhausted'
    assert parent['_executionBudget'].consumption()==(100,1)
    close_execution_budget(worker)


def test_restored_control_owner_charges_are_not_reset_by_a_fresh_proxy():
    from lib.agent_core.execution_budget import inherit_execution_budget,share_execution_budget
    parent=root();worker={'id':'restored-owner','config':{},'_executionToolsAdmitted':2}
    inherit_execution_budget(parent,worker)
    worker['config']['executionLimits']['max_tool_calls']=2
    proxy={'id':'new-proxy','config':{}};share_execution_budget(worker,proxy)
    assert admit_tool_execution(proxy)=='execution_tool_budget_exhausted'
    assert parent['_executionBudget'].consumption()==(100,2)
    close_execution_budget(worker)


def test_same_identity_model_carrier_cannot_expand_the_active_owner_token_slice():
    from lib.agent_core.execution_budget import ExecutionBudget,share_execution_budget,ModelBudgetExceeded
    parent=root();parent['_executionBudget']=ExecutionBudget(parent)
    parent['config']['executionLimits']['max_tokens']=130
    carrier={'id':parent['id'],'config':{}}
    share_execution_budget(parent,carrier)
    carrier['config']['executionLimits']['max_tokens']=1000
    account=parent['_executionBudget']
    lease=account.reserve_model(carrier,input_tokens=10,max_output_tokens=50)
    assert lease.max_output_tokens==20
    lease.mark_dispatched();lease.settle({'input_tokens':10,'output_tokens':20})
    with pytest.raises(ModelBudgetExceeded,match='budget_exhausted'):
        account.reserve_model(carrier,input_tokens=1,max_output_tokens=1)
    assert account.allowance_snapshot(carrier)['tokens_available']==0


def test_owner_usage_and_tightened_deadline_are_checked_at_model_send():
    from lib.agent_core.execution_budget import ExecutionBudget,share_execution_budget
    from lib.llm.llm_errors import AbortedError
    parent=root();parent['_executionBudget']=ExecutionBudget(parent)
    carrier={'id':parent['id'],'config':{}}
    share_execution_budget(parent,carrier)
    lease=parent['_executionBudget'].reserve_model(carrier,input_tokens=10,max_output_tokens=5)
    parent['config']['executionLimits']['deadline']=1
    with pytest.raises(AbortedError,match='deadline_exceeded'):lease.mark_dispatched()
    lease.settle(unsent=True)
    assert parent['_executionBudget'].consumption()==(100,0)


def test_carrier_token_headroom_uses_the_active_owner_latest_usage():
    from lib.agent_core.execution_budget import ExecutionBudget,share_execution_budget,ModelBudgetExceeded
    parent=root();parent['_executionBudget']=ExecutionBudget(parent)
    parent['config']['executionLimits']['max_tokens']=200
    carrier={'id':parent['id'],'config':{}}
    share_execution_budget(parent,carrier)
    parent['usage']={'input_tokens':195}
    with pytest.raises(ModelBudgetExceeded,match='budget_exhausted'):
        parent['_executionBudget'].reserve_model(carrier,input_tokens=5,max_output_tokens=1)
    assert parent['_executionBudget'].allowance_snapshot(carrier)['tokens_available']==5


def test_fresh_carrier_cannot_clear_unknown_usage_of_a_locally_bounded_owner():
    from lib.agent_core.execution_budget import ExecutionBudget,share_execution_budget,ModelBudgetExceeded
    parent=root();parent['config']['executionLimits']['max_tokens']=None
    parent['_executionBudget']=ExecutionBudget(parent)
    account=parent['_executionBudget'];request=account.reserve_model(parent,input_tokens=10,max_output_tokens=5)
    request.mark_dispatched();request.settle()
    parent['config']['executionLimits']['max_tokens']=200
    carrier={'id':parent['id'],'config':{}}
    share_execution_budget(parent,carrier)
    assert '_executionActualsComplete' not in carrier
    with pytest.raises(ModelBudgetExceeded,match='usage_unknown'):
        account.reserve_model(carrier,input_tokens=10,max_output_tokens=5)
    assert not account._model_leases and account.actuals_complete() is False


def test_owner_usage_becoming_unknown_between_reservation_and_send_refuses_carrier_dispatch():
    from lib.agent_core.execution_budget import ExecutionBudget,share_execution_budget
    from lib.llm.llm_errors import AbortedError
    parent=root();parent['config']['executionLimits']['max_tokens']=None
    parent['_executionBudget']=ExecutionBudget(parent)
    parent['config']['executionLimits']['max_tokens']=200
    carrier={'id':parent['id'],'config':{}}
    share_execution_budget(parent,carrier)
    lease=parent['_executionBudget'].reserve_model(carrier,input_tokens=10,max_output_tokens=5)
    parent['_executionActualsComplete']=False
    with pytest.raises(AbortedError,match='usage_unknown'):lease.mark_dispatched()
    lease.settle(unsent=True)
    assert not lease.dispatched and not parent['_executionBudget']._model_leases


def test_unbounded_tree_unknown_usage_does_not_erase_an_independent_known_child_slice():
    parent=root();parent['config']['executionLimits']['max_tokens']=None
    unknown=child(parent,'unknown-child');known=child(parent,'known-child')
    known['config']['executionLimits']['max_tokens']=100
    account=parent['_executionBudget']
    request=account.reserve_model(unknown,input_tokens=10,max_output_tokens=5)
    request.mark_dispatched();request.settle();close_execution_budget(unknown)
    lease=account.reserve_model(known,input_tokens=10,max_output_tokens=5)
    assert lease.max_output_tokens==5
    lease.settle(unsent=True);close_execution_budget(known)
    assert account.actuals_complete() is False
