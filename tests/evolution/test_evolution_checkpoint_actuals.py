"""Real running checkpoints retain cost lower bounds without admitting replay."""
from copy import deepcopy
import json
import pytest
from tests.evolution.test_evolution_runs import OWNER,runtime as runtime,finish,review
from tests.evolution.test_evolution_review_recovery import hide,advance,reviewing

pytestmark=pytest.mark.unit


def save_progress(task,*,tools=3):
    from lib.tasks_pkg.manager import checkpoint_task_partial
    from lib.storage import get_storage_client
    task['usage']={'input_tokens':70,'output_tokens':10}
    from lib.agent_core.execution_limits import admit_tool_execution
    for _ in range(tools):assert admit_tool_execution(task) is None
    checkpoint_task_partial(task,force=True)
    row=get_storage_client().query('task_results.replay_get',
        {'key':task['id'],'user_id':OWNER,'include_metadata':True})
    return row


def test_lost_executor_consumes_observed_lower_bound_without_replay_or_verdict(runtime,monkeypatch):
    from lib.evolution import get_episode
    run=runtime[0].submit_run({'objective':'Retain the cost of interrupted experiments.'},user_id=OWNER)
    task=runtime[2][0];row=save_progress(task)
    snapshot=row['metadata']['evolutionConsumption']
    assert snapshot['actuals']['tokens']==80 and snapshot['actuals']['tool_calls']==3
    assert snapshot['intent_id']==run['stage_dispatch']['intent_id']
    hide(runtime,monkeypatch,task['id'])
    lost=advance(runtime,run)
    assert len(runtime[2])==1 and lost['status']=='needs_attention'
    assert lost['actuals']['tokens']==80 and lost['actuals']['tool_calls']==3
    assert lost['actuals_complete'] is False and lost['review'] is None
    assert lost['receipts'][0]['checkpoint_lower_bound'] and 'terminal_verdict' not in lost['receipts'][0]
    episode=get_episode(run['episode_id'],user_id=OWNER)
    assert episode['status']=='aborted' and episode['actuals_complete'] is False
    assert episode['actuals']['tokens']==80
    assert advance(runtime,lost)['actuals']==lost['actuals']


def test_lost_review_keeps_partial_cost_separate_from_successor(runtime,monkeypatch):
    run=reviewing(runtime);task=runtime[2][-1];save_progress(task,tools=0)
    hide(runtime,monkeypatch,task['id'])
    prepared=advance(runtime,run)
    assert prepared['actuals']['tokens']==95 and prepared['actuals_complete'] is False
    successor=advance(runtime,prepared)
    completed=finish(runtime,runtime[2][-1],review())
    assert successor['status']=='reviewing' and completed['status']=='completed'
    assert completed['actuals']['tokens']==110 and completed['actuals_complete'] is False
    assert sum(row.get('checkpoint_lower_bound',False) for row in completed['receipts'])==1


@pytest.mark.parametrize('change',['foreign','task','intent','stage','birth','negative','complete','missing'])
def test_unbound_or_invalid_checkpoint_never_fabricates_usage(runtime,monkeypatch,change):
    from lib.storage import get_storage_client
    run=runtime[0].submit_run({'objective':'Validate interrupted cost authority.'},user_id=OWNER)
    task=runtime[2][0];save_progress(task)
    original=get_storage_client
    class Client:
        def query(self,operation,payload,**kwargs):
            result=original().query(operation,payload,**kwargs)
            if operation=='task_results.replay_get' and payload.get('include_metadata') and result:
                result=deepcopy(result);snapshot=result['metadata']['evolutionConsumption']
                if change=='foreign':result['user_id']=OWNER+1
                elif change=='missing':result['metadata'].pop('evolutionConsumption')
                elif change=='negative':snapshot['actuals']['tokens']=-1
                elif change=='complete':snapshot['complete']=True
                else:snapshot[{'task':'task_id','intent':'intent_id','stage':'stage','birth':'birth_version'}[change]]=0 if change=='birth' else 'changed'
            return result
    monkeypatch.setattr('lib.storage.get_storage_client',lambda **_:Client())
    hide(runtime,monkeypatch,task['id'])
    lost=advance(runtime,run)
    assert lost['actuals']['tokens']==0 and lost['actuals_complete'] is False
    assert not lost['receipts'] and len(runtime[2])==1


def test_cancelled_lost_executor_retains_checkpoint_cost_and_never_dispatches(runtime,monkeypatch):
    from lib.tasks_pkg.manager import discard_task
    from lib.agent_core.execution_session import ExecutionPhase
    from lib.evolution import get_episode
    run=runtime[0].submit_run({'objective':'Account before cancelling lost work.'},user_id=OWNER)
    task=runtime[2][0];save_progress(task)
    task['_executionSession'].settle(ExecutionPhase.CANCELLED,cause='simulated_process_loss')
    discard_task(task['id'],'')
    hide(runtime,monkeypatch,task['id'])
    cancelled=runtime[0].cancel_run(run['run_id'],user_id=OWNER)
    settled=advance(runtime,cancelled)
    assert settled['status']=='cancelled' and settled['actuals']['tokens']==80
    episode=get_episode(run['episode_id'],user_id=OWNER)
    assert episode['status']=='aborted' and episode['actuals']['tokens']==80
    assert episode['actuals_complete'] is False and len(runtime[2])==1


def test_lost_ack_never_double_charges_checkpoint_consumption(runtime,monkeypatch):
    from lib.storage.errors import StorageError
    runs=runtime[0];run=reviewing(runtime);task=runtime[2][-1]
    save_progress(task,tools=0);hide(runtime,monkeypatch,task['id'])
    save=runs._save
    def lost_ack(value,version,owner):
        result=save(value,version,owner)
        if value.get('stage_losses'):raise StorageError('database_timeout','lost loss-record acknowledgement')
        return result
    monkeypatch.setattr(runs,'_save',lost_ack)
    with pytest.raises(StorageError):advance(runtime,run)
    monkeypatch.setattr(runs,'_save',save)
    stored=runs.get_run(run['run_id'],user_id=OWNER)
    assert stored['actuals']['tokens']==95
    resumed=advance(runtime,stored)
    assert resumed['actuals']['tokens']==95 and len(runtime[2])==3
    assert sum(row.get('checkpoint_lower_bound',False) for row in resumed['receipts'])==1


def model_payments(task,count=1,*,name='paid-worker'):
    from lib.agent_core.execution_budget import inherit_execution_budget,close_execution_budget
    worker={'id':name,'config':{},'usage':{}}
    inherit_execution_budget(task,worker)
    account=task['_executionBudget']
    try:
        for _ in range(count):
            lease=account.reserve_model(worker,input_tokens=7,max_output_tokens=5)
            lease.mark_dispatched();lease.settle({'input_tokens':7,'output_tokens':5})
    finally:
        close_execution_budget(worker)


def test_lost_executor_consumes_journal_only_cost_once_without_replaying(runtime,monkeypatch):
    from lib.evolution import get_episode
    run=runtime[0].submit_run({'objective':'Preserve child costs after parent loss.'},user_id=OWNER)
    task=runtime[2][0];model_payments(task,3)
    hide(runtime,monkeypatch,task['id'])
    lost=advance(runtime,run)
    assert lost['actuals']['tokens']==36 and lost['actuals_complete'] is False
    receipt=lost['receipts'][0]
    assert receipt['checkpoint_source']=='model_attempts' and receipt['model_attempts_count']==3
    assert receipt['model_attempt_tokens']==36 and 'checkpoint_version' not in receipt
    assert 'terminal_verdict' not in receipt and len(runtime[2])==1
    episode=get_episode(run['episode_id'],user_id=OWNER)
    assert episode['status']=='aborted' and episode['actuals']['tokens']==36
    assert episode['actuals_complete'] is False
    again=advance(runtime,lost)
    assert again['receipts']==lost['receipts'] and again['actuals']==lost['actuals']


@pytest.mark.parametrize('payments,expected',[(1,92),(10,200)])
def test_journal_and_task_checkpoint_combine_by_maximum_not_duplicate_addition(runtime,monkeypatch,payments,expected):
    run=runtime[0].submit_run({'objective':'Keep independent cost witnesses coherent.'},user_id=OWNER)
    task=runtime[2][0];save_progress(task);model_payments(task,payments)
    hide(runtime,monkeypatch,task['id'])
    lost=advance(runtime,run)
    assert lost['actuals']['tokens']==expected and lost['actuals']['tool_calls']==3
    assert lost['receipts'][0]['model_attempt_tokens']==12*payments
    assert lost['actuals_complete'] is False


def test_model_cost_history_is_task_indexed_and_continues_across_pages(runtime,monkeypatch):
    from lib.evolution import records,stage_inputs
    run=runtime[0].submit_run({'objective':'Read complete task-scoped cost history.'},user_id=OWNER)
    task=runtime[2][0];model_payments(task,201)
    foreign=runtime[0].submit_run({'objective':'Keep a second task cost separate.'},user_id=OWNER)
    other=runtime[2][-1];model_payments(other,1,name='other-worker')
    query=runtime[1].query;pages=[]
    def track(operation,payload):
        if operation=='record.list' and payload['namespace']==stage_inputs.MODEL_ATTEMPTS_NAMESPACE:
            pages.append(dict(payload))
        return query(operation,payload)
    monkeypatch.setattr(runtime[1],'query',track)
    total=stage_inputs.read_model_attempt_token_lower_bound(run,user_id=OWNER)
    assert total=={'count':201,'tokens':2412,'stage':'executing','account_tokens':2412}
    assert len(pages)>=3 and all(page['limit']<=records.MAX_LIST_LIMIT for page in pages)
    assert all(page['prefix'].startswith(str(OWNER)+':') and len(page['prefix'])>65 for page in pages)
    assert stage_inputs.read_model_attempt_token_lower_bound(foreign,user_id=OWNER)['tokens']==12


@pytest.mark.parametrize('change',['owner','task','intent','plan','birth','key'])
def test_corrupt_model_history_never_supplies_cost_or_recovery_authority(runtime,monkeypatch,change):
    from lib.evolution import stage_inputs
    run=runtime[0].submit_run({'objective':'Refuse foreign model consumption witnesses.'},user_id=OWNER)
    task=runtime[2][0];model_payments(task)
    key=next(key for key in runtime[1].rows if key[0]==stage_inputs.MODEL_ATTEMPTS_NAMESPACE)
    event=runtime[1].rows[key]
    if change=='key':
        runtime[1].rows[(key[0],key[1]+'wrong')]=runtime[1].rows.pop(key)
    else:
        field={'owner':'user_id','task':'task_id','intent':'intent_id','plan':'plan_id','birth':'birth_version'}[change]
        event['binding'][field]=OWNER+1 if change=='owner' else 900 if change=='birth' else 'foreign'
    hide(runtime,monkeypatch,task['id'])
    lost=advance(runtime,run)
    assert lost['actuals']['tokens']==0 and lost['actuals_complete'] is False
    assert not lost['receipts'] and len(runtime[2])==1


def test_entity_index_cannot_escape_its_owner_or_cursor_scope(runtime):
    from lib.evolution import records,stage_inputs
    for prefix in ['other:owner','../escape',None]:
        with pytest.raises(ValueError):
            records.list_record_page(stage_inputs.MODEL_ATTEMPTS_NAMESPACE,OWNER,kind='model attempt',limit=1,entity_prefix=prefix)
    with pytest.raises(ValueError):
        records.list_record_page(stage_inputs.MODEL_ATTEMPTS_NAMESPACE,OWNER,kind='model attempt',limit=1,
                                 entity_prefix='scope-',after_key=str(OWNER)+':another-task')


def test_old_model_witnesses_without_account_snapshots_remain_lower_bounds(runtime,monkeypatch):
    from lib.evolution import stage_inputs
    run=runtime[0].submit_run({'objective':'Read earlier immutable consumption witnesses.'},user_id=OWNER)
    task=runtime[2][0];model_payments(task,2)
    for (namespace,key),value in runtime[1].rows.items():
        if namespace==stage_inputs.MODEL_ATTEMPTS_NAMESPACE:value.pop('account_observed_tokens',None)
    hide(runtime,monkeypatch,task['id'])
    lost=advance(runtime,run)
    assert lost['actuals']['tokens']==24 and lost['actuals_complete'] is False
    assert lost['receipts'][0]['model_attempts_count']==2
    assert 'model_account_tokens' not in lost['receipts'][0]


def test_unsettled_intent_retains_prior_account_consumption_without_counting_reservation(runtime,monkeypatch):
    from lib.agent_core.execution_budget import inherit_execution_budget,close_execution_budget
    run=runtime[0].submit_run({'objective':'Preserve observed prior consumption before uncertain requests.'},user_id=OWNER)
    task=runtime[2][0];task['usage']={'input_tokens':80}
    worker={'id':'unfinished-worker','config':{},'usage':{}}
    inherit_execution_budget(task,worker)
    lease=task['_executionBudget'].reserve_model(worker,input_tokens=70,max_output_tokens=30)
    try:
        lease.mark_dispatched()
        hide(runtime,monkeypatch,task['id'])
        lost=advance(runtime,run)
        assert lost['actuals']['tokens']==80 and lost['actuals_complete'] is False
        receipt=lost['receipts'][0]
        assert receipt['model_attempt_tokens']==0 and receipt['model_account_tokens']==80
        assert receipt['model_attempts_count']==1 and len(runtime[2])==1
    finally:
        lease.settle();close_execution_budget(worker)


def execute_observed_tool(task,call_id):
    from types import SimpleNamespace
    from lib.tasks_pkg.executor import _execute_tool_one
    task['_tool_env']=SimpleNamespace(resolve=lambda name:lambda *args:(call_id,'original reply',False))
    return _execute_tool_one(task,{'id':call_id},'fixture-tool',call_id,{},1,{},task['config'],None,False)


def test_lost_executor_recovers_charged_tools_without_parent_checkpoint(runtime,monkeypatch):
    run=runtime[0].submit_run({'objective':'Retain charged tools across worker loss.'},user_id=OWNER)
    task=runtime[2][0]
    for number in range(3):execute_observed_tool(task,'call-'+str(number))
    hide(runtime,monkeypatch,task['id']);lost=advance(runtime,run)
    assert lost['actuals']['tool_calls']==3 and lost['actuals_complete'] is False
    receipt=lost['receipts'][0]
    assert receipt['checkpoint_source']=='tool_attempts' and receipt['tool_attempts_count']==3
    assert receipt['tool_account_calls']==3 and 'checkpoint_version' not in receipt
    assert advance(runtime,lost)['actuals']==lost['actuals'] and len(runtime[2])==1


def test_tool_history_combines_charged_account_with_old_checkpoint_without_addition(runtime,monkeypatch):
    run=runtime[0].submit_run({'objective':'Preserve charged slots while avoiding duplicates.'},user_id=OWNER)
    task=runtime[2][0];save_progress(task,tools=3)
    for number in range(2):execute_observed_tool(task,'new-'+str(number))
    hide(runtime,monkeypatch,task['id']);lost=advance(runtime,run)
    assert lost['actuals']=={'tokens':80,'tool_calls':5,'wall_clock_seconds':lost['actuals']['wall_clock_seconds']}
    assert lost['receipts'][0]['tool_account_calls']==5 and lost['actuals_complete'] is False


def test_legacy_tool_records_never_guess_slot_count_from_attempts(runtime,monkeypatch):
    from lib.evolution import stage_inputs
    run=runtime[0].submit_run({'objective':'Keep legacy unknown tool charges explicit.'},user_id=OWNER)
    task=runtime[2][0];execute_observed_tool(task,'legacy-call')
    for (namespace,key),value in runtime[1].rows.items():
        if namespace==stage_inputs.TOOL_ATTEMPTS_NAMESPACE:value.pop('account_tool_calls',None)
    assert stage_inputs.read_tool_account_lower_bound(run,user_id=OWNER) is None
    hide(runtime,monkeypatch,task['id']);lost=advance(runtime,run)
    assert lost['actuals']['tool_calls']==0 and lost['actuals_complete'] is False


def test_tool_charge_scan_is_paged_and_owned(runtime,monkeypatch):
    from lib.evolution import stage_inputs
    run=runtime[0].submit_run({'objective':'Retain long tool histories.'},user_id=OWNER)
    task=runtime[2][0]
    for number in range(201):execute_observed_tool(task,'paged-'+str(number))
    result=stage_inputs.read_tool_account_lower_bound(run,user_id=OWNER)
    assert result=={'count':201,'calls':201,'stage':'executing'}
    assert stage_inputs.read_tool_account_lower_bound(run,user_id=OWNER+1) is None


@pytest.mark.parametrize('change',['owner','plan','birth','counter','address'])
def test_corrupt_tool_charge_history_cannot_supply_cost_or_execution_authority(runtime,monkeypatch,change):
    from lib.evolution import stage_inputs
    run=runtime[0].submit_run({'objective':'Refuse corrupt tool charge witnesses.'},user_id=OWNER)
    task=runtime[2][0];execute_observed_tool(task,'corrupt-call')
    key=next(key for key in runtime[1].rows if key[0]==stage_inputs.TOOL_ATTEMPTS_NAMESPACE)
    value=runtime[1].rows[key]
    if change=='address':runtime[1].rows[(key[0],key[1]+'wrong')]=runtime[1].rows.pop(key)
    elif change=='counter':value['account_tool_calls']=-1
    else:value['binding'][{'owner':'user_id','plan':'plan_id','birth':'birth_version'}[change]]=OWNER+1 if change=='owner' else 999 if change=='birth' else 'foreign-plan'
    hide(runtime,monkeypatch,task['id']);lost=advance(runtime,run)
    assert lost['actuals']['tool_calls']==0 and lost['actuals_complete'] is False
    assert not lost['receipts'] and len(runtime[2])==1
