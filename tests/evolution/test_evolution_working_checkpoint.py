"""Working contexts survive loss without becoming replay or verdict authority."""
from types import SimpleNamespace
import pytest
from tests.evolution.test_evolution_runs import OWNER,runtime as runtime,finish

pytestmark=pytest.mark.unit


def launch(runtime):
    run=runtime[0].submit_run({'objective':'Retain long experiment context.'},user_id=OWNER)
    return run,runtime[2][0]


def save(task,phase='ready',round_num=1,messages=None):
    task['_invocationCheckpointPhase']=phase
    task['_invocationCheckpointSink'](task,messages or task['messages'],round_num)


@pytest.mark.parametrize('phase',['ready','model_in_flight','tools_in_flight','terminal_delivery'])
def test_owned_context_preserves_phase_messages_todos_and_lower_bound(runtime,phase):
    from lib.evolution.stage_inputs import read_working_checkpoint
    run,task=launch(runtime);task['_todoState']={'items':[{'title':'Frozen experiment','status':'pending'}]}
    task['usage']={'input_tokens':20,'output_tokens':5}
    save(task,phase)
    restored=read_working_checkpoint(run,user_id=OWNER)
    assert restored['context']['messages']==task['messages']
    assert restored['context']['task_state']['_todoState']==task['_todoState']
    assert restored['checkpoint']['phase']==phase
    assert restored['checkpoint']['binding']['actuals']['tokens']==25
    assert run['status']=='executing' and len(runtime[2])==1 and not run['receipts']


def test_large_utf8_context_rotates_without_round_count_growth_and_shrink_reclaims(runtime):
    from lib.evolution import stage_inputs
    run,task=launch(runtime)
    large=[*task['messages'],{'role':'assistant','tool_calls':[{'id':'measurements','type':'function','function':{'name':'run_command','arguments':'{}'}}]},
           {'role':'tool','tool_call_id':'measurements','content':'实验结果 🧪'*160000}]
    for number in range(12):save(task,round_num=number,messages=large)
    restored=stage_inputs.read_working_checkpoint(run,user_id=OWNER)
    assert restored['context']['messages']==large and restored['checkpoint']['byte_count']>1024*1024
    rows=runtime[1].rows
    count=restored['checkpoint']['part_count']
    assert sum(namespace==stage_inputs.WORKING_PARTS_NAMESPACE for namespace,_ in rows)<=2*count+2
    save(task,round_num=13);save(task,round_num=14)
    assert sum(namespace==stage_inputs.WORKING_PARTS_NAMESPACE for namespace,_ in rows)==4
    assert stage_inputs.read_working_checkpoint(run,user_id=OWNER)['context']['messages']==task['messages']


@pytest.mark.parametrize('change',['digest','missing','binding','input','framing','advanced'])
def test_corrupt_or_changed_prefix_is_never_accepted(runtime,monkeypatch,change):
    from lib.evolution import stage_inputs,records
    from lib.evolution.contracts import EvolutionStateError,EvolutionContractError
    run,task=launch(runtime);save(task)
    key=stage_inputs._working_key(task['id'],OWNER)
    header=runtime[1].rows[(stage_inputs.WORKING_NAMESPACE,key)]
    if change in ('digest','missing'):
        part_key=(stage_inputs.WORKING_PARTS_NAMESPACE,stage_inputs._working_key(task['id'],OWNER,f"-bank-{header['bank']}-part-0"))
        if change=='missing':runtime[1].rows.pop(part_key)
        else:runtime[1].rows[part_key]['data']='AAAA'
    elif change=='binding':header['binding']['plan_id']='foreign-plan'
    elif change=='input':header['input_sha256']='0'*64
    elif change=='framing':header['part_count']+=1
    else:
        original=records.get_versioned_record;seen=[0]
        def advanced(namespace,entity):
            value,version=original(namespace,entity)
            if namespace==stage_inputs.WORKING_NAMESPACE:
                seen[0]+=1
                if seen[0]>1:version+=1
            return value,version
        monkeypatch.setattr(records,'get_versioned_record',advanced)
    with pytest.raises((EvolutionStateError,EvolutionContractError)):
        stage_inputs.read_working_checkpoint(run,user_id=OWNER)
    assert len(runtime[2])==1


@pytest.mark.parametrize('failure',['parts','pointer'])
def test_failed_write_ahead_prevents_tools_and_previous_pointer_survives(runtime,monkeypatch,failure):
    from lib.evolution import stage_inputs,records
    from lib.tasks_pkg.orchestrator import _root_agent_loop
    run,task=launch(runtime);save(task);put=records.put_record;effects=[]
    def refuse(namespace,*args,**kwargs):
        target=stage_inputs.WORKING_PARTS_NAMESPACE if failure=='parts' else stage_inputs.WORKING_NAMESPACE
        if namespace==target:raise RuntimeError('checkpoint storage refused')
        return put(namespace,*args,**kwargs)
    monkeypatch.setattr(records,'put_record',refuse)
    monkeypatch.setattr(_root_agent_loop,'run_tool_dispatch',lambda *a,**kw:effects.append('mutated'))
    messages=[*task['messages'],{'role':'assistant','tool_calls':[{'id':'pending','type':'function','function':{'name':'run_command','arguments':'{}'}}]}]
    hooks=_root_agent_loop._RootLoopHooks(SimpleNamespace(task=task,messages=messages))
    with pytest.raises(RuntimeError,match='checkpoint storage refused'):hooks.execute_tools(1,[])
    assert not effects
    assert stage_inputs.read_working_checkpoint(run,user_id=OWNER)['checkpoint']['phase']=='ready'


def test_terminal_capture_reclaims_working_cache_and_preserves_originals(runtime):
    from lib.evolution import stage_inputs
    run,task=launch(runtime);save(task)
    assert stage_inputs.read_working_checkpoint(run,user_id=OWNER)
    reviewing=finish(runtime,task,'Observed retained experiment output.')
    assert reviewing['status']=='reviewing'
    assert stage_inputs.read_working_checkpoint(run,user_id=OWNER) is None
    assert stage_inputs.read_input(run,user_id=OWNER)['task_id']==task['id']


def test_inspection_after_loss_stays_read_only(runtime,monkeypatch):
    from lib.evolution import stage_inputs
    from tests.evolution.test_evolution_review_recovery import hide,advance
    run,task=launch(runtime);save(task,'tools_in_flight')
    hide(runtime,monkeypatch,task['id'])
    lost=advance(runtime,run)
    assert lost['status']=='needs_attention'
    inspected=stage_inputs.read_working_checkpoint(lost,user_id=OWNER)
    assert inspected['checkpoint']['phase']=='tools_in_flight' and len(runtime[2])==1


def test_terminal_memory_release_drops_checkpoint_closure_after_persistence_ack(runtime):
    from lib.tasks_pkg.manager._persist import _release_heavy_task_state
    _,task=launch(runtime)
    task.update(status='done',_terminalPersistencePending=True)
    assert _release_heavy_task_state(task)==0 and task['_evolutionWorkingCheckpointSinkInstalled']
    task['_terminalPersistencePending']=False
    assert _release_heavy_task_state(task)>=1
    assert '_invocationCheckpointSink' not in task and '_evolutionWorkingCheckpointPreviousSink' not in task


def test_latest_context_cost_survives_loss_before_diagnostic_checkpoint(runtime,monkeypatch):
    from lib.evolution.bridge import read_checkpoint_lower_bound
    from tests.evolution.test_evolution_review_recovery import hide,advance
    from lib.tasks_pkg.manager import checkpoint_task_partial
    run,task=launch(runtime)
    task['usage']={'input_tokens':20,'output_tokens':5}
    checkpoint_task_partial(task,force=True)
    task['usage']={'input_tokens':30,'output_tokens':7}
    save(task,'tools_in_flight')
    receipt=read_checkpoint_lower_bound(run,user_id=OWNER)
    assert receipt['actuals']['tokens']==37 and receipt['checkpoint_source']=='task_result_and_working_context'
    hide(runtime,monkeypatch,task['id'])
    lost=advance(runtime,run)
    assert lost['actuals']['tokens']==37 and lost['actuals_complete'] is False
    assert len(runtime[2])==1 and len(lost['receipts'])==1


def test_existing_observer_runs_after_publication_and_is_restored_on_release(runtime):
    from lib.evolution import stage_inputs
    from lib.tasks_pkg.manager._persist import _release_heavy_task_state
    run,task=launch(runtime);observed=[]
    task.pop('_evolutionWorkingCheckpointSinkInstalled')
    def previous(carrier,messages,number):
        assert stage_inputs.read_working_checkpoint(run,user_id=OWNER)['context']['messages']==messages
        observed.append(number)
    task['_invocationCheckpointSink']=previous
    stage_inputs.install_working_checkpoint_sink(task)
    save(task,round_num=2)
    assert observed==[2]
    task['status']='done';_release_heavy_task_state(task)
    assert task['_invocationCheckpointSink'] is previous


def test_deferred_cleanup_keeps_prefix_valid_and_retries_without_address_growth(runtime,monkeypatch):
    from lib.evolution import stage_inputs,records
    from lib.storage.errors import StorageError
    run,task=launch(runtime);large=[*task['messages'],{'role':'assistant','content':'测量'*200000}]
    save(task,messages=large);save(task,messages=large)
    store=runtime[1];original=store.command
    def refuse_delete(operation,payload,command_id):
        if operation=='record.delete':raise StorageError('database_busy','cleanup delayed')
        return original(operation,payload,command_id)
    with monkeypatch.context() as patch:
        patch.setattr(store,'command',refuse_delete)
        save(task,round_num=3)
    assert stage_inputs.read_working_checkpoint(run,user_id=OWNER)['context']['messages']==task['messages']
    save(task,round_num=4);save(task,round_num=5)
    assert sum(namespace==stage_inputs.WORKING_PARTS_NAMESPACE for namespace,_ in store.rows)==4


def test_identical_live_context_reuses_frames_but_publishes_new_phase(runtime):
    from lib.evolution import stage_inputs
    run,task=launch(runtime);save(task)
    first=stage_inputs.read_working_checkpoint(run,user_id=OWNER)
    versions={key:value for key,value in runtime[1].versions.items() if key[0]==stage_inputs.WORKING_PARTS_NAMESPACE}
    save(task,'model_in_flight',round_num=2)
    second=stage_inputs.read_working_checkpoint(run,user_id=OWNER)
    assert second['version']>first['version'] and second['checkpoint']['phase']=='model_in_flight'
    assert second['checkpoint']['bank']==first['checkpoint']['bank']
    assert {key:value for key,value in runtime[1].versions.items() if key[0]==stage_inputs.WORKING_PARTS_NAMESPACE}==versions


@pytest.mark.parametrize('field,value',[('id','unrelated-carrier'),('_userId',OWNER+1),('_preparedInputDigest','0'*64)])
def test_publication_refuses_changed_carrier_or_immutable_input(runtime,monkeypatch,field,value):
    from lib.evolution import stage_inputs
    from lib.evolution.contracts import EvolutionStateError
    run,task=launch(runtime);save(task)
    with monkeypatch.context() as patch:
        patch.setitem(task,field,value)
        with pytest.raises(EvolutionStateError):save(task,'tools_in_flight')
    assert stage_inputs.read_working_checkpoint(run,user_id=OWNER)['checkpoint']['phase']=='ready'


def test_ready_context_retains_unfinished_children_model_leases_and_background(runtime,monkeypatch):
    import threading
    import uuid
    from lib.agent_core.execution_budget import inherit_execution_budget,close_execution_budget
    from lib.tasks_pkg import background_commands as commands
    from lib.evolution import stage_inputs
    run,task=launch(runtime)
    child={'id':'unfinished-child','config':{},'usage':{}}
    inherit_execution_budget(task,child)
    lease=task['_executionBudget'].reserve_model(child,input_tokens=10,max_output_tokens=5)
    handle='bg_'+uuid.uuid4().hex[:12]
    assert commands.register_handed_off_command(command_id=handle,task=task,command='experiment',done=threading.Event())
    try:
        save(task,'ready')
        effects=stage_inputs.read_working_checkpoint(run,user_id=OWNER)['context']['effects']
        assert effects['account']['active_child_task_ids']==[child['id']]
        assert effects['account']['pending_model_requests']==1
        assert effects['account']['model_attempts']==[{'attempt_ordinal':lease.ordinal,
            'task_id':child['id'],'dispatched':False,'reserved_tokens':15}]
        assert effects['background']['commands']==[{'command_id':handle,'status':'running'}]
        assert task['_executionBudget'].continuity_snapshot()['pending_model_requests']==1
        lease.mark_dispatched()
        save(task,'ready',round_num=1)
        dispatched=stage_inputs.read_working_checkpoint(run,user_id=OWNER)['context']['effects']['account']
        assert dispatched['model_attempts'][0]['dispatched'] is True
        assert dispatched['pending_model_requests']==1
        lease.settle({'input_tokens':10,'output_tokens':5});close_execution_budget(child)
        commands.finish_handed_off_command(command_id=handle,result='$ experiment\n[exit code: 0]',error=None)
        save(task,'ready',round_num=2)
        closed=stage_inputs.read_working_checkpoint(run,user_id=OWNER)['context']['effects']
        assert not closed['account']['active_child_task_ids'] and closed['account']['pending_model_requests']==0
        assert closed['account']['model_attempts']==[]
        assert closed['background']['commands'][0]['status']=='completed'
    finally:
        lease.settle(unsent=True)
        close_execution_budget(child)
        with commands._lock:commands._registry.pop(handle,None)


def test_missing_account_and_recovering_registry_are_explicit_unknowns(runtime,monkeypatch):
    from lib.evolution import stage_inputs
    from lib.tasks_pkg import background_commands as commands
    run,task=launch(runtime)
    monkeypatch.setattr(commands,'_recovery_scan_failed',True)
    save(task,'ready')
    effects=stage_inputs.read_working_checkpoint(run,user_id=OWNER)['context']['effects']
    assert effects['account'] is None and effects['background']['recovery_pending']
    assert effects['background']['commands']==[] and len(runtime[2])==1


def test_effect_inventory_excludes_same_user_other_task_and_keeps_completed_identity(runtime):
    import threading
    import uuid
    from lib.tasks_pkg import background_commands as commands
    from lib.evolution import stage_inputs
    run,task=launch(runtime)
    own='bg_'+uuid.uuid4().hex[:12];foreign='bg_'+uuid.uuid4().hex[:12]
    other={'id':'other-experiment','_userId':OWNER}
    assert commands.register_handed_off_command(command_id=own,task=task,command='private command',done=threading.Event())
    assert commands.register_handed_off_command(command_id=foreign,task=other,command='unrelated',done=threading.Event())
    try:
        commands.finish_handed_off_command(command_id=own,result='private result',error=None)
        save(task)
        background=stage_inputs.read_working_checkpoint(run,user_id=OWNER)['context']['effects']['background']
        assert background['commands']==[{'command_id':own,'status':'completed'}]
        assert 'private command' not in str(background) and 'private result' not in str(background)
        with commands._lock:assert commands._registry[own]['task'] is None
    finally:
        with commands._lock:
            commands._registry.pop(own,None);commands._registry.pop(foreign,None)


def test_settled_request_with_unknown_usage_stays_explicit_in_context(runtime):
    from lib.agent_core.execution_budget import inherit_execution_budget,close_execution_budget
    from lib.evolution import stage_inputs
    run,task=launch(runtime);child={'id':'unknown-consumption-child','config':{},'usage':{}}
    inherit_execution_budget(task,child)
    lease=task['_executionBudget'].reserve_model(child,input_tokens=10,max_output_tokens=5)
    lease.mark_dispatched();lease.settle(None);close_execution_budget(child)
    save(task)
    account=stage_inputs.read_working_checkpoint(run,user_id=OWNER)['context']['effects']['account']
    assert account['pending_model_requests']==0 and not account['active_child_task_ids']
    assert account['unknown_usage'] and len(runtime[2])==1


@pytest.mark.parametrize('old_phase,old_round,new_phase,new_round',[
    ('ready',3,'tools_in_flight',2),('tools_in_flight',3,'ready',3),
    ('tools_in_flight',3,'model_in_flight',3),('model_in_flight',3,'ready',3),
    ('terminal_delivery',3,'ready',4),('terminal_delivery',3,'model_in_flight',4)])
def test_late_checkpoint_cannot_replace_newer_or_unresolved_progress(runtime,old_phase,old_round,new_phase,new_round):
    from lib.evolution import stage_inputs
    from lib.evolution.contracts import EvolutionStateError
    run,task=launch(runtime);save(task,old_phase,round_num=old_round)
    before=stage_inputs.read_working_checkpoint(run,user_id=OWNER)
    versions=dict(runtime[1].versions);writes=runtime[1].writes
    with pytest.raises(EvolutionStateError,match='progress regressed'):
        save(task,new_phase,round_num=new_round)
    assert runtime[1].versions==versions and runtime[1].writes==writes
    assert stage_inputs.read_working_checkpoint(run,user_id=OWNER)==before


def test_checkpoint_order_accepts_shared_loop_progress_and_same_phase_retries(runtime):
    from lib.evolution import stage_inputs
    run,task=launch(runtime)
    for phase,number in [('model_in_flight',0),('model_in_flight',0),('tools_in_flight',0),
        ('ready',1),('model_in_flight',1),('tools_in_flight',1),('terminal_delivery',2),('terminal_delivery',2)]:
        save(task,phase,round_num=number)
    assert stage_inputs.read_working_checkpoint(run,user_id=OWNER)['checkpoint']['phase']=='terminal_delivery'


def test_delta_publication_keeps_unchanged_frame_versions_and_full_digest(runtime):
    from lib.evolution import stage_inputs
    run,task=launch(runtime)
    messages=[*task['messages'],{'role':'assistant','content':'x'*900000}]
    save(task,round_num=1,messages=messages)
    messages[-1]['content']+='a';save(task,round_num=2,messages=messages)
    bank0={key:version for key,version in runtime[1].versions.items() if key[0]==stage_inputs.WORKING_PARTS_NAMESPACE and '-bank-0-part-' in key[1]}
    messages[-1]['content']+='b';save(task,round_num=3,messages=messages)
    after={key:version for key,version in runtime[1].versions.items() if key in bank0}
    changed=[key for key in bank0 if bank0[key]!=after[key]]
    assert len(changed)==1 and changed[0][1].endswith('-part-3')
    assert stage_inputs.read_working_checkpoint(run,user_id=OWNER)['context']['messages']==messages


def test_legacy_body_digest_frames_remain_readable(runtime):
    from lib.evolution import stage_inputs
    run,task=launch(runtime);save(task)
    header=stage_inputs.read_working_checkpoint(run,user_id=OWNER)['checkpoint']
    for (namespace,_),part in runtime[1].rows.items():
        if namespace==stage_inputs.WORKING_PARTS_NAMESPACE and 'data' in part:
            part.pop('schema_version');part['sha256']=header['sha256']
    assert stage_inputs.read_working_checkpoint(run,user_id=OWNER)['context']['messages']==task['messages']


def test_valid_frame_hash_cannot_override_full_context_digest(runtime):
    import base64
    import hashlib
    from lib.evolution import stage_inputs
    from lib.evolution.contracts import EvolutionStateError
    run,task=launch(runtime);save(task)
    for (namespace,_),part in runtime[1].rows.items():
        if namespace==stage_inputs.WORKING_PARTS_NAMESPACE and 'data' in part:
            raw=base64.b64decode(part['data']);raw=bytes([raw[0]^1])+raw[1:]
            part['data']=base64.b64encode(raw).decode();part['sha256']=hashlib.sha256(raw).hexdigest()
    with pytest.raises(EvolutionStateError,match='checkpoint digest changed'):
        stage_inputs.read_working_checkpoint(run,user_id=OWNER)


def test_ambiguous_frame_write_is_not_reused_as_an_old_acknowledged_hash(runtime,monkeypatch):
    from lib.evolution import stage_inputs,records
    from lib.storage.errors import StorageError
    run,task=launch(runtime);messages=[*task['messages'],{'role':'assistant','content':'x'*900000}]
    save(task,round_num=1,messages=messages)
    messages[-1]['content']+='a';save(task,round_num=2,messages=messages)
    put=records.put_record;failed=[False]
    def lost_ack(namespace,key,value,**kwargs):
        version=put(namespace,key,value,**kwargs)
        if namespace==stage_inputs.WORKING_PARTS_NAMESPACE and key.endswith('-bank-0-part-3') and not failed[0]:
            failed[0]=True;raise StorageError('database_timeout','lost part acknowledgement')
        return version
    messages[-1]['content']+='b'
    with monkeypatch.context() as patch:
        patch.setattr(records,'put_record',lost_ack)
        with pytest.raises(StorageError):save(task,round_num=3,messages=messages)
    # The original bank still supplies the last acknowledged working context.
    assert stage_inputs.read_working_checkpoint(run,user_id=OWNER)['context']['messages'][-1]['content'].endswith('a')
    save(task,round_num=3,messages=messages)
    assert stage_inputs.read_working_checkpoint(run,user_id=OWNER)['context']['messages']==messages


def test_child_send_journal_survives_an_older_parent_working_context(runtime):
    from lib.evolution import stage_inputs
    from lib.agent_core.execution_budget import inherit_execution_budget,close_execution_budget
    run,task=launch(runtime);save(task,'ready')
    original=stage_inputs.read_working_checkpoint(run,user_id=OWNER)
    child={'id':'later-provider-child','config':{},'usage':{}}
    inherit_execution_budget(task,child)
    lease=task['_executionBudget'].reserve_model(child,input_tokens=10,max_output_tokens=5)
    try:
        lease.mark_dispatched()
        event=stage_inputs.read_model_attempt_event(run,lease.ordinal,'dispatch_intent',user_id=OWNER)
        assert event['attempt_task_id']==child['id'] and event['reserved_tokens']==15
        assert event['reported_tokens']==0 and event['usage_complete'] is False
        assert stage_inputs.read_working_checkpoint(run,user_id=OWNER)==original
        lease.settle({'input_tokens':10,'output_tokens':2})
        terminal=stage_inputs.read_model_attempt_event(run,lease.ordinal,'terminal',user_id=OWNER)
        assert terminal['phase']=='settled' and terminal['reported_tokens']==12 and terminal['usage_complete']
        assert stage_inputs.read_model_attempt_event(run,lease.ordinal,'terminal',user_id=OWNER+1) is None
        changed=dict(run,plan_id='foreign-plan')
        with pytest.raises(RuntimeError,match='identity changed'):
            stage_inputs.read_model_attempt_event(changed,lease.ordinal,'terminal',user_id=OWNER)
        stage_inputs.release_working_checkpoint(task['id'],user_id=OWNER)
        assert stage_inputs.read_model_attempt_event(run,lease.ordinal,'terminal',user_id=OWNER)==terminal
    finally:
        lease.settle(unsent=True);close_execution_budget(child)


@pytest.mark.parametrize('phase',['dispatch_intent','settled'])
@pytest.mark.parametrize('committed',[False,True])
def test_model_journal_ack_loss_never_grants_send_or_complete_accounting(runtime,monkeypatch,phase,committed):
    from lib.evolution import stage_inputs,records
    from lib.agent_core.execution_budget import inherit_execution_budget,close_execution_budget
    from lib.storage.errors import StorageError
    run,task=launch(runtime)
    child={'id':'ack-loss-child','config':{},'usage':{}}
    inherit_execution_budget(task,child)
    account=task['_executionBudget'];lease=account.reserve_model(child,input_tokens=10,max_output_tokens=5)
    put=records.put_record;failures=[]
    def lose(namespace,key,value,**kwargs):
        if namespace==stage_inputs.MODEL_ATTEMPTS_NAMESPACE and value['phase']==phase and not failures:
            failures.append(key)
            if committed:put(namespace,key,value,**kwargs)
            raise StorageError('database_timeout','model observation acknowledgement lost')
        return put(namespace,key,value,**kwargs)
    monkeypatch.setattr(records,'put_record',lose)
    try:
        if phase=='dispatch_intent':
            with pytest.raises(StorageError):lease.mark_dispatched()
            assert not lease.dispatched
            lease.settle(unsent=True)
            event=stage_inputs.read_model_attempt_event(run,lease.ordinal,'terminal',user_id=OWNER)
            assert event['phase']=='voided' and event['reported_tokens']==0 and event['usage_complete']
            assert account.consumption()[0]==0
        else:
            lease.mark_dispatched();lease.settle({'input_tokens':10,'output_tokens':2})
            assert not account.actuals_complete() and task['_executionActualsComplete'] is False
            assert account.consumption()[0]==12
            event=stage_inputs.read_model_attempt_event(run,lease.ordinal,'terminal',user_id=OWNER)
            assert (event is not None)==committed
            if event:assert event['reported_tokens']==12
        assert failures
    finally:
        lease.settle(unsent=True);close_execution_budget(child)


def test_model_journal_cannot_void_an_attempt_after_send_admission(runtime):
    from lib.evolution import stage_inputs
    from lib.agent_core.execution_budget import inherit_execution_budget,close_execution_budget
    run,task=launch(runtime);child={'id':'paid-child','config':{},'usage':{}}
    inherit_execution_budget(task,child)
    lease=task['_executionBudget'].reserve_model(child,input_tokens=10,max_output_tokens=5)
    try:
        lease.mark_dispatched();lease.settle(unsent=True)
        terminal=stage_inputs.read_model_attempt_event(run,lease.ordinal,'terminal',user_id=OWNER)
        assert terminal['phase']=='settled' and terminal['usage_complete'] is False
        assert task['_executionBudget'].actuals_complete() is False
    finally:
        lease.settle(unsent=True);close_execution_budget(child)


def test_observer_binding_refusal_preserves_the_existing_checkpoint_port(runtime):
    from lib.evolution import stage_inputs
    from lib.agent_core.execution_budget import ExecutionBudget
    run,task=launch(runtime);carrier=dict(task)
    carrier.pop('_evolutionWorkingCheckpointSinkInstalled')
    carrier.pop('_executionModelAttemptObserver')
    previous=lambda *args:None
    carrier['_invocationCheckpointSink']=previous
    carrier['_executionBudget']=ExecutionBudget(carrier)
    lease=carrier['_executionBudget'].reserve_model(carrier,input_tokens=10,max_output_tokens=5)
    try:
        with pytest.raises(ValueError,match='active account'):
            stage_inputs.install_working_checkpoint_sink(carrier)
        assert carrier['_invocationCheckpointSink'] is previous
        assert '_executionModelAttemptObserver' not in carrier
        assert not carrier.get('_evolutionWorkingCheckpointSinkInstalled')
    finally:
        lease.settle(unsent=True)


def test_model_journal_refuses_a_voided_event_that_claims_paid_tokens(runtime):
    from lib.evolution import stage_inputs
    from lib.agent_core.execution_budget import inherit_execution_budget,close_execution_budget
    run,task=launch(runtime);child={'id':'unsubmitted-child','config':{},'usage':{}}
    inherit_execution_budget(task,child)
    lease=task['_executionBudget'].reserve_model(child,input_tokens=10,max_output_tokens=5)
    lease.settle(unsent=True);close_execution_budget(child)
    key=next(key for key in runtime[1].rows if key[0]==stage_inputs.MODEL_ATTEMPTS_NAMESPACE)
    runtime[1].rows[key]['reported_tokens']=1
    with pytest.raises(ValueError):
        stage_inputs.read_model_attempt_event(run,lease.ordinal,'terminal',user_id=OWNER)


def test_child_lifecycle_witnesses_bind_real_swarm_control_and_nested_children(runtime):
    from lib.evolution import stage_inputs
    from lib.agent_core.execution_budget import inherit_execution_budget,close_execution_budget
    from lib.swarm.master import MasterOrchestrator
    run,task=launch(runtime)
    proxy=MasterOrchestrator('witness-session','',[],user_id=OWNER,parent_task=task)._parent_task_proxy
    first={'id':'owned-child','config':{},'usage':{}}
    second={'id':'owned-grandchild','config':{},'usage':{}}
    inherit_execution_budget(proxy,first);inherit_execution_budget(first,second)
    close_execution_budget(second);close_execution_budget(first)
    rows=[value for (namespace,key),value in runtime[1].rows.items()
          if namespace==stage_inputs.CHILD_LIFECYCLES_NAMESPACE]
    assert len(rows)==4 and {row['binding']['task_id'] for row in rows}=={task['id']}
    assert next(row for row in rows if row['child_task_id']==first['id'] and row['phase']=='attach_intent')['parent_task_id']==task['id']
    assert next(row for row in rows if row['child_task_id']==second['id'] and row['phase']=='attach_intent')['parent_task_id']==first['id']
    assert not task['_executionBudget']._child_records
    assert stage_inputs.read_child_lifecycle_event(run,1,'terminal',user_id=OWNER)['phase']=='detached'
    assert stage_inputs.read_child_lifecycle_event(run,1,'terminal',user_id=OWNER+1) is None
    with pytest.raises(RuntimeError,match='identity changed'):
        stage_inputs.read_child_lifecycle_event(dict(run,plan_id='foreign-plan'),1,'terminal',user_id=OWNER)


def test_unacknowledged_child_construction_is_rejected_and_unregistered(runtime,monkeypatch):
    from lib.evolution import stage_inputs,records
    from lib.tasks_pkg import manager
    from lib.tasks_pkg.child_invocation import create_child_invocation
    from lib.storage.errors import StorageError
    run,task=launch(runtime);put=records.put_record;allocated=[];create=manager.create_task
    def capture(*args,**kwargs):
        child=create(*args,**kwargs);allocated.append(child);return child
    def lose(namespace,key,value,**kwargs):
        if namespace==stage_inputs.CHILD_LIFECYCLES_NAMESPACE and value['phase']=='attach_intent':
            put(namespace,key,value,**kwargs)
            raise StorageError('database_timeout','attachment acknowledgement lost')
        return put(namespace,key,value,**kwargs)
    monkeypatch.setattr(records,'put_record',lose);monkeypatch.setattr(manager,'create_task',capture)
    with pytest.raises(StorageError):
        create_child_invocation(task,model='fixture-model',tools=[],objective='Never execute rejected child',
                               event_sink=lambda event:None,abort_check=lambda:False)
    child=allocated[0]
    assert manager.chat_task_runtime.get(child['id']) is None
    assert child['_executionSession'].is_terminal and child['_invocationPublicationClosed']
    assert child['id'] not in task['_executionBudget']._active
    terminal=[value for (namespace,key),value in runtime[1].rows.items()
              if namespace==stage_inputs.CHILD_LIFECYCLES_NAMESPACE and value['phase']=='attachment_voided']
    assert len(terminal)==1 and terminal[0]['child_task_id']==child['id']


def test_registration_failure_retains_terminal_retry_when_rejection_is_not_durable(runtime,monkeypatch):
    from lib.tasks_pkg import manager
    from lib.tasks_pkg.child_invocation import create_child_invocation
    from lib.storage.errors import StorageError
    run,task=launch(runtime);allocated=[];create=manager.create_task
    def capture(*args,**kwargs):
        child=create(*args,**kwargs);allocated.append(child);return child
    def fail(*args,**kwargs):raise StorageError('database_timeout','registration failed')
    monkeypatch.setattr(manager,'create_task',capture)
    monkeypatch.setattr('lib.tasks_pkg.invocation_registration.register_child_invocation',fail)
    monkeypatch.setattr('lib.tasks_pkg.manager._persist.persist_task_result',lambda task:False)
    try:
        with pytest.raises(StorageError,match='registration failed'):
            create_child_invocation(task,model='fixture-model',tools=[],objective='Reject safely',
                                   event_sink=lambda event:None,abort_check=lambda:False)
        child=allocated[0]
        assert child['_executionSession'].is_terminal and child['status']=='error'
        assert child['_terminalPersistencePending'] and child['_terminalPersistenceRetryReady']
        assert manager.chat_task_runtime.get(child['id']) is child
        assert child['id'] not in task['_executionBudget']._active
    finally:
        for child in allocated:manager.discard_task(child['id'])


def tool_call(task,handler,*,call_id='observed-call',name='fixture-tool'):
    from types import SimpleNamespace
    from lib.tasks_pkg.executor import _execute_tool_one
    task['_tool_env']=SimpleNamespace(resolve=lambda tool:handler)
    return _execute_tool_one(task,{'id':call_id},name,call_id,{},1,{},task['config'],None,False)


def test_tool_executor_witnesses_precede_effect_and_bind_full_return_digest(runtime):
    import hashlib
    from lib.evolution import stage_inputs
    run,task=launch(runtime);entered=[]
    def handler(*args):
        entered.append(True)
        rows=[v for (ns,key),v in runtime[1].rows.items() if ns==stage_inputs.TOOL_ATTEMPTS_NAMESPACE]
        assert len(rows)==1 and rows[0]['phase']=='intent'
        assert task['_executionBudget'].actuals_complete() is False
        assert task['_executionBudget'].continuity_snapshot()['pending_tool_attempts'][0]['tool_call_id']=='observed-call'
        return 'observed-call','complete original 🧪',False
    assert tool_call(task,handler)[1]=='complete original 🧪'
    rows=[v for (ns,key),v in runtime[1].rows.items() if ns==stage_inputs.TOOL_ATTEMPTS_NAMESPACE]
    terminal=next(v for v in rows if v['phase']=='executor_returned')
    assert terminal['result_sha256']==hashlib.sha256('complete original 🧪'.encode()).hexdigest()
    assert stage_inputs.read_tool_attempt_event(run,1,'terminal',user_id=OWNER)==terminal
    assert stage_inputs.read_tool_attempt_event(run,1,'terminal',user_id=OWNER+1) is None
    with pytest.raises(RuntimeError,match='identity changed'):
        stage_inputs.read_tool_attempt_event(dict(run,plan_id='foreign-plan'),1,'terminal',user_id=OWNER)
    assert entered and not task['_executionBudget']._tool_attempts
    assert task['_executionBudget'].actuals_complete()


@pytest.mark.parametrize('phase',['intent','executor_returned'])
@pytest.mark.parametrize('committed',[False,True])
def test_tool_observation_ack_loss_never_enters_without_intent_or_claims_complete(runtime,monkeypatch,phase,committed):
    from lib.evolution import stage_inputs,records
    from lib.storage.errors import StorageError
    run,task=launch(runtime);put=records.put_record;failed=[];entered=[]
    def lose(namespace,key,value,**kwargs):
        if namespace==stage_inputs.TOOL_ATTEMPTS_NAMESPACE and value['phase']==phase and not failed:
            failed.append(True)
            if committed:put(namespace,key,value,**kwargs)
            raise StorageError('database_timeout','tool acknowledgement lost')
        return put(namespace,key,value,**kwargs)
    def handler(*args):entered.append(True);return 'observed-call','original result',False
    monkeypatch.setattr(records,'put_record',lose)
    if phase=='intent':
        with pytest.raises(StorageError):tool_call(task,handler)
        assert not entered
        assert any(v['phase']=='not_entered' for (ns,key),v in runtime[1].rows.items() if ns==stage_inputs.TOOL_ATTEMPTS_NAMESPACE)
    else:
        assert tool_call(task,handler)[1]=='original result'
        assert entered and not task['_executionBudget'].actuals_complete()
    assert not task['_executionBudget']._tool_attempts


def test_tool_intent_latency_cannot_bypass_cancellation(runtime,monkeypatch):
    from lib.evolution import stage_inputs,records
    from lib.agent_core.execution_budget import ModelBudgetExceeded
    run,task=launch(runtime);put=records.put_record;entered=[]
    def cancel(namespace,key,value,**kwargs):
        result=put(namespace,key,value,**kwargs)
        if namespace==stage_inputs.TOOL_ATTEMPTS_NAMESPACE and value['phase']=='intent':task['abort_event'].set()
        return result
    monkeypatch.setattr(records,'put_record',cancel)
    with pytest.raises(ModelBudgetExceeded,match='owner_cancelled'):
        tool_call(task,lambda *args:entered.append(True))
    assert not entered and not task['_executionBudget']._tool_attempts


def test_tool_witness_digest_preserves_large_unicode_original_without_whole_byte_copy(runtime):
    import hashlib
    import tracemalloc
    from lib.evolution import stage_inputs
    run,task=launch(runtime);content='原始 🧪 e\u0301\r\n'*200000
    expected=hashlib.sha256(content.encode()).hexdigest()
    tracemalloc.start()
    try:
        result=tool_call(task,lambda *args:('observed-call',content,False))
        peak=tracemalloc.get_traced_memory()[1]
    finally:tracemalloc.stop()
    assert result[1] is content
    rows=[v for (ns,key),v in runtime[1].rows.items() if ns==stage_inputs.TOOL_ATTEMPTS_NAMESPACE]
    assert next(v for v in rows if v['phase']=='executor_returned')['result_sha256']==expected
    assert peak<len(content.encode())


def test_executor_exception_releases_tool_observation_and_preserves_error(runtime,monkeypatch):
    from types import SimpleNamespace
    from lib.evolution import stage_inputs
    from lib.tasks_pkg.executor import _execute_tool_one
    run,task=launch(runtime)
    def fail(*args):raise RuntimeError('original resolver failure')
    monkeypatch.setattr('lib.tasks_pkg.executor._resolver.resolver_for',lambda task:SimpleNamespace(lookup=fail))
    with pytest.raises(RuntimeError,match='original resolver failure'):
        _execute_tool_one(task,{},'fixture-tool','failed-call',{},1,{},task['config'],None,False)
    terminal=stage_inputs.read_tool_attempt_event(run,1,'terminal',user_id=OWNER)
    assert terminal['phase']=='executor_exception' and 'result_sha256' not in terminal
    assert not task['_executionBudget']._tool_attempts


def test_nonportable_tool_return_keeps_original_without_inventing_digest(runtime):
    from lib.evolution import stage_inputs
    run,task=launch(runtime);value=object()
    result=tool_call(task,lambda *args:('observed-call',value,False))
    assert result[1] is value
    terminal=stage_inputs.read_tool_attempt_event(run,1,'terminal',user_id=OWNER)
    assert terminal['phase']=='executor_returned' and 'result_sha256' not in terminal


def test_child_detachment_archives_pending_proxy_tool_without_claiming_completion(runtime):
    from lib.evolution import stage_inputs
    from lib.agent_core.execution_budget import inherit_execution_budget,share_execution_budget,close_execution_budget
    run,task=launch(runtime);worker={'id':'owned-worker','config':{},'usage':{}}
    inherit_execution_budget(task,worker)
    proxy={'id':'owned-proxy','config':{}}
    share_execution_budget(worker,proxy)
    budget=task['_executionBudget'];event=budget.begin_tool_attempt(proxy,'owned-call','fixture-tool',1)
    close_execution_budget(worker)
    detached=stage_inputs.read_child_lifecycle_event(run,1,'terminal',user_id=OWNER)
    assert detached['pending_model_requests']==0 and detached['pending_tool_attempts']==1
    intent=stage_inputs.read_tool_attempt_event(run,1,'intent',user_id=OWNER)
    assert intent['task_id']=='owned-proxy' and intent['control_task_id']=='owned-worker'
    assert stage_inputs.read_tool_attempt_event(run,1,'terminal',user_id=OWNER) is None
    assert budget.actuals_complete() is False
    budget.finish_tool_attempt(event,phase='executor_exception')
    terminal=stage_inputs.read_tool_attempt_event(run,1,'terminal',user_id=OWNER)
    assert terminal['phase']=='executor_exception' and terminal['control_task_id']=='owned-worker'


def test_older_detachment_records_do_not_fabricate_zero_pending_tools(runtime):
    from lib.evolution import stage_inputs
    from lib.agent_core.execution_budget import inherit_execution_budget,close_execution_budget
    run,task=launch(runtime);worker={'id':'legacy-worker','config':{},'usage':{}}
    inherit_execution_budget(task,worker);close_execution_budget(worker)
    for (namespace,key),value in runtime[1].rows.items():
        if namespace==stage_inputs.CHILD_LIFECYCLES_NAMESPACE:value.pop('pending_tool_attempts',None)
    event=stage_inputs.read_child_lifecycle_event(run,1,'terminal',user_id=OWNER)
    assert 'pending_tool_attempts' not in event


def test_registered_hook_batches_are_observed_without_charging_tool_slots(runtime,monkeypatch):
    from lib.tasks_pkg import tool_hooks
    from lib.evolution import stage_inputs
    run,task=launch(runtime);entered=[]
    def before(name,args,carrier):
        entered.append('pre')
        rows=[v for (ns,key),v in runtime[1].rows.items() if ns==stage_inputs.TOOL_ATTEMPTS_NAMESPACE]
        assert any(v['scope']=='pre_hooks' and v['phase']=='intent' for v in rows)
        assert carrier['_executionBudget'].actuals_complete() is False
        return tool_hooks.HookResult(action='modify',modified_args={'after':'validated'})
    def after(name,args,text,carrier):entered.append('post');return text+' amended'
    monkeypatch.setattr(tool_hooks,'_pre_hooks',[before]);monkeypatch.setattr(tool_hooks,'_post_hooks',[after])
    args={};assert tool_hooks.run_pre_hooks('fixture-tool',args,task) is None
    assert args=={'after':'validated'}
    assert tool_hooks.run_post_hooks('fixture-tool',args,'original',task)=='original amended'
    rows=[v for (ns,key),v in runtime[1].rows.items() if ns==stage_inputs.TOOL_ATTEMPTS_NAMESPACE]
    assert len(rows)==4 and {v['scope'] for v in rows}=={'pre_hooks','post_hooks'}
    assert entered==['pre','post'] and task['_executionBudget'].consumption()[1]==0
    assert not task['_executionBudget']._tool_attempts


def test_unacknowledged_pre_hook_intent_refuses_callback_and_post_hook_retains_original(runtime,monkeypatch):
    from lib.tasks_pkg import tool_hooks
    from lib.evolution import stage_inputs,records
    from lib.storage.errors import StorageError
    run,task=launch(runtime);entered=[];put=records.put_record
    monkeypatch.setattr(tool_hooks,'_pre_hooks',[lambda *args:entered.append('pre')])
    monkeypatch.setattr(tool_hooks,'_post_hooks',[lambda *args:entered.append('post')])
    def refuse(namespace,key,value,**kwargs):
        if namespace==stage_inputs.TOOL_ATTEMPTS_NAMESPACE and value['phase']=='intent':
            raise StorageError('database_timeout','hook intent refused')
        return put(namespace,key,value,**kwargs)
    monkeypatch.setattr(records,'put_record',refuse)
    with pytest.raises(StorageError):tool_hooks.run_pre_hooks('fixture-tool',{},task)
    assert tool_hooks.run_post_hooks('fixture-tool',{},'original',task)=='original'
    assert not entered and not task['_executionBudget']._tool_attempts


def test_empty_tool_hook_registries_create_no_observation_or_account(runtime,monkeypatch):
    from lib.tasks_pkg import tool_hooks
    from lib.evolution import stage_inputs
    run,task=launch(runtime)
    monkeypatch.setattr(tool_hooks,'_pre_hooks',[]);monkeypatch.setattr(tool_hooks,'_post_hooks',[])
    assert tool_hooks.run_pre_hooks('fixture-tool',{},task) is None
    assert tool_hooks.run_post_hooks('fixture-tool',{},'original',task)=='original'
    assert '_executionBudget' not in task
    assert not any(ns==stage_inputs.TOOL_ATTEMPTS_NAMESPACE for ns,key in runtime[1].rows)


def test_hook_errors_survive_swallowing_without_changing_later_callbacks(runtime,monkeypatch):
    from lib.tasks_pkg import tool_hooks
    from lib.evolution import stage_inputs
    run,task=launch(runtime)
    def fail(*args):raise RuntimeError('callback failed')
    def modify(name,args,carrier):
        return tool_hooks.HookResult(action='modify',modified_args={'checked':True})
    def amend(name,args,text,carrier):return text+' amended'
    monkeypatch.setattr(tool_hooks,'_pre_hooks',[fail,modify,fail])
    monkeypatch.setattr(tool_hooks,'_post_hooks',[fail,amend])
    args={}
    assert tool_hooks.run_pre_hooks('fixture-tool',args,task) is None
    assert args=={'checked':True}
    assert tool_hooks.run_post_hooks('fixture-tool',args,'original',task)=='original amended'
    for ordinal,scope,count in [(1,'pre_hooks',2),(2,'post_hooks',1)]:
        terminal=stage_inputs.read_tool_attempt_event(run,ordinal,'terminal',user_id=OWNER)
        assert terminal['phase']=='executor_returned'
        assert terminal['scope']==scope and terminal['hook_errors']==count
        from lib.evolution.stage_inputs import _validate
        for changes in ({'hook_errors':-1},{'scope':'executor'},{'phase':'intent'}):
            with pytest.raises(ValueError):_validate({**terminal,**changes},'tool_attempt_event')
        intent=stage_inputs.read_tool_attempt_event(run,ordinal,'intent',user_id=OWNER)
        assert 'hook_errors' not in intent
    assert not task['_executionBudget']._tool_attempts
    assert task['_executionBudget'].consumption()[1]==0


def test_legacy_hook_receipt_does_not_invent_zero_errors(runtime,monkeypatch):
    from lib.tasks_pkg import tool_hooks
    from lib.evolution import stage_inputs
    run,task=launch(runtime)
    monkeypatch.setattr(tool_hooks,'_pre_hooks',[lambda *args:None])
    tool_hooks.run_pre_hooks('fixture-tool',{},task)
    assert stage_inputs.read_tool_attempt_event(run,1,'terminal',user_id=OWNER)['hook_errors']==0
    for (namespace,key),value in runtime[1].rows.items():
        if namespace==stage_inputs.TOOL_ATTEMPTS_NAMESPACE:value.pop('hook_errors',None)
    assert 'hook_errors' not in stage_inputs.read_tool_attempt_event(run,1,'terminal',user_id=OWNER)
