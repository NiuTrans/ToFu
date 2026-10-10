"""Cold discovery is owned, scoped, read-only and never grants continuation."""
from copy import deepcopy
import pytest
from lib.research.action_discovery import discover_unfinished_actions
from lib.research.action_observations import ActionObservations,BINDING_NAMESPACE,_digest
from lib.research.action_pending import NAMESPACE as PENDING_NAMESPACE
from lib.research.execution_owner import ActionExecutionOwners
from lib.research.request_context import PART_NAMESPACE,RequestContexts
from lib.research.model_accounting import ModelCalls
from lib.storage.errors import StorageError
from tests.research.test_research_process_recovery import VersionedRecords,setup
from tests.research.test_research_request_context import defined_material
from tests.research.test_research_model_accounting import snapshot
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark=pytest.mark.unit


@pytest.mark.parametrize('cache',['valid','missing','invalid_page','wrong_binding'])
def test_original_owned_action_is_found_without_private_chunks_or_writes(monkeypatch,cache):
    client=VersionedRecords();repo,_,_,_,_=setup(client,monkeypatch)
    if cache=='missing':client.rows.pop((PENDING_NAMESPACE,repo.prefix))
    elif cache=='wrong_binding':client.rows[PENDING_NAMESPACE,repo.prefix]['value']['binding']['revision']+=1
    original=client.query;reads=[]
    def query(operation,payload):
        reads.append((operation,deepcopy(payload)))
        if cache=='invalid_page' and operation=='record.list' and payload['namespace']==PENDING_NAMESPACE:return None
        return original(operation,payload)
    client.query=query;client.command=lambda *args:pytest.fail('discovery wrote or executed')
    monkeypatch.setattr('lib.storage.get_storage_client',lambda **kwargs:client)
    result=discover_unfinished_actions('Frozen basis',lang='en',user_id=1)
    assert result['complete'] is True and len(result['tasks'])==1
    task=result['tasks'][0]
    assert task['id']=='research_action_observed' and task['status']=='interrupted'
    assert task['meta']['action']=='experiment' and task['replay_allowed'] is False
    assert not any(payload['namespace']==PART_NAMESPACE for _,payload in reads)
    for direction,lang,owner in [('Other objective','en',1),('Frozen basis','zh',1),('Frozen basis','en',2)]:
        assert discover_unfinished_actions(direction,lang=lang,user_id=owner)['tasks']==[]
    assert all(payload['prefix'] in ('1:','2:') for operation,payload in reads if operation=='record.list')


def test_unknown_or_missing_execution_evidence_is_not_running_or_resumable(monkeypatch):
    import lib.research.execution_owner as owners
    client=VersionedRecords();repo,_,_,_,_=setup(client,monkeypatch)
    monkeypatch.setattr('lib.storage.get_storage_client',lambda **kwargs:client)
    monkeypatch.setattr(owners,'owner_state',lambda value:'unknown')
    assert discover_unfinished_actions('Frozen basis',user_id=1)['tasks'][0]['status']=='unknown'
    client.rows.pop((BINDING_NAMESPACE,repo.prefix))
    assert discover_unfinished_actions('Frozen basis',user_id=1)['tasks'][0]['status']=='unknown'


def test_two_distinct_actions_report_ambiguity_instead_of_selecting_one(monkeypatch):
    client=VersionedRecords();repo,_,_,_,_=setup(client,monkeypatch)
    basis=repo.load_basis()
    second=ActionObservations(client,user_id=1,task_id='second-original-action',direction='Frozen basis',
        lang='en',revision=7,basis_digest=_digest(basis));second.freeze_basis(basis)
    ActionExecutionOwners.open(client,user_id=1,task_id='second-original-action').claim_initial()
    context=RequestContexts.open(client,user_id=1,task_id='second-original-action')
    digest=context.save_request(defined_material())
    before=snapshot(0);before['agent_dispatches']=1
    ModelCalls.open(client,user_id=1,task_id='second-original-action').admit_model_call(ordinal=1,
        round_index=0,requested_model='controlled',request_digest=digest,max_output=8000,
        usage_before=before,request_snapshot_version=1)
    monkeypatch.setattr('lib.storage.get_storage_client',lambda **kwargs:client)
    result=discover_unfinished_actions('Frozen basis',user_id=1)
    assert len(result['tasks'])==2 and result['complete'] is False
    assert {task['id'] for task in result['tasks']}=={'research_action_observed','second-original-action'}


def test_invalid_terminal_evidence_blocks_discovery(monkeypatch):
    from lib.research.action_terminal import NAMESPACE as TERMINAL_NAMESPACE
    client=VersionedRecords();repo,_,_,_,_=setup(client,monkeypatch)
    client.rows[TERMINAL_NAMESPACE,repo.prefix]={'version':1,'value':{}}
    monkeypatch.setattr('lib.storage.get_storage_client',lambda **kwargs:client)
    with pytest.raises(StorageError):discover_unfinished_actions('Frozen basis',user_id=1)


def test_cold_discovery_survives_selected_store_restart(storage,monkeypatch):
    supervisor,_=storage;repo,_,_,_,_=setup(supervisor.client,monkeypatch)
    supervisor.stop();supervisor.start()
    monkeypatch.setattr('lib.storage.get_storage_client',lambda **kwargs:supervisor.client)
    result=discover_unfinished_actions('Frozen basis',user_id=1)
    assert result['tasks'][0]['id']==repo.binding['task_id']
    assert result['tasks'][0]['status']=='interrupted'


def test_original_settled_history_avoids_redundant_binding_reads(monkeypatch, record_property):
    from lib.agent_core.task_runtime import TaskRuntime
    from lib.research.action_terminal import NAMESPACE as TERMINAL, persist_action_terminal
    from lib.research.action_observations import _task_prefix
    from tests.research.test_research_action_terminal import create
    client=VersionedRecords();repo,_,_,_,_=setup(client,monkeypatch)
    monkeypatch.setattr('lib.storage.get_storage_client',lambda **kwargs:client)
    runtime=TaskRuntime('cold-history',terminal_persist=persist_action_terminal)
    task=create(runtime,'settled-history-template')
    runtime.update_fields(task['id'],fields={'direction':'Frozen basis','expected_revision':7})
    runtime.finish(task['id'],result={'observed':'retained-history'})
    terminal=client.rows[TERMINAL,_task_prefix(1,task['id'])[1]]['value']
    history_keys=set()
    for index in range(2048):
        task_id=f'settled-history-{index}'
        key=_task_prefix(1,task_id)[1];history_keys.add(key)
        binding={**repo.binding,'task_id':task_id}
        client.rows[BINDING_NAMESPACE,key]={'version':1,'value':{'schema_version':1,'binding':binding}}
        client.rows[TERMINAL,key]={'version':1,'value':{**deepcopy(terminal),'task_id':task_id}}
    reads=[];query=client.query
    def measured(operation,payload):
        reads.append((operation,deepcopy(payload)));return query(operation,payload)
    client.query=measured
    client.command=lambda *args:pytest.fail('discovery wrote or executed')
    result=discover_unfinished_actions('Frozen basis',user_id=1)
    assert result['complete'] is True
    assert [task['id'] for task in result['tasks']]==[repo.binding['task_id']]
    redundant=[payload for operation,payload in reads if operation=='record.get'
        and payload['namespace']==BINDING_NAMESPACE and payload['key'] in history_keys]
    assert redundant==[]
    assert sum(operation=='record.get' and payload['namespace']==TERMINAL
        and payload['key'] in history_keys for operation,payload in reads)==2048
    assert not any(payload['namespace']==PART_NAMESPACE for _,payload in reads)
    record_property('settled_history_bindings',2048)
    record_property('redundant_settled_binding_rpcs',len(redundant))


@pytest.mark.parametrize('corruption',['result','scope'])
def test_original_terminal_fast_path_still_rejects_corrupt_authority(monkeypatch,corruption):
    from lib.agent_core.task_runtime import TaskRuntime
    from lib.research.action_terminal import NAMESPACE as TERMINAL,persist_action_terminal
    from tests.research.test_research_action_terminal import create
    client=VersionedRecords();repo,_,_,_,_=setup(client,monkeypatch)
    monkeypatch.setattr('lib.storage.get_storage_client',lambda **kwargs:client)
    runtime=TaskRuntime('cold-terminal-integrity',terminal_persist=persist_action_terminal)
    task=create(runtime,repo.binding['task_id'])
    runtime.update_fields(task['id'],fields={'direction':'Frozen basis','expected_revision':7})
    runtime.finish(task['id'],result={'observed':True})
    value=client.rows[TERMINAL,repo.prefix]['value']
    if corruption=='result':value['result']={'observed':False}
    else:
        from lib.research.persistence import research_direction_hash
        value['direction_hash']=research_direction_hash('Other objective')
    client.rows.pop((PENDING_NAMESPACE,repo.prefix),None)
    with pytest.raises(StorageError):discover_unfinished_actions('Frozen basis',user_id=1)
