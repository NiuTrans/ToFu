"""A fresh executor continues an acknowledged frontier after real process exit.

Controlled provider frames and prices prove lifecycle/accounting, not science.
Tool commands, journals, workspace CAS, terminal persistence and process death
are real on each selected store. The child uses a new authenticated RPC client.
"""
from pathlib import Path
import json
import os
import shlex
import subprocess
import sys
import uuid

import pytest
from tests.research.test_research_workspace_evidence_guard import storage as storage
from tests.support.research_observations import observation_payload

pytestmark = pytest.mark.unit


def configure_worker(monkeypatch, client, model):
    import lib
    import lib.research.action as action
    from lib.paper.agent_usage import PaperAgentUsageMeter
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kwargs: client)
    monkeypatch.setattr('lib.research.workspace._storage', lambda **kwargs: client)
    monkeypatch.setattr(action, '_research_context', lambda *args: {})
    monkeypatch.setattr(lib, 'LLM_MODEL', model)
    monkeypatch.setattr(PaperAgentUsageMeter, '_compute_cost',
        lambda *args, **kwargs: {'costUsd':0.01,'costCny':0.07})


def provider_frame(message, finish):
    from lib.llm.stream_result import ProviderStreamResult, ProviderStreamState
    return ProviderStreamResult(message=message, compatibility_finish_reason=finish,
        provider_finish_reason=finish, state=ProviderStreamState.PROVIDER_FINISHED,
        saw_done=True, saw_finish_reason=True,
        usage={'prompt_tokens':3,'completion_tokens':4,'total_tokens':7})


def first_worker(config_path):
    """Fresh-interpreter fixture worker exits immediately after checkpoint ACK."""
    from lib.storage.client import StorageClient
    from lib.storage.v2_client import StorageV2Client
    from lib.research.action_runtime import create_action_task
    from lib.research.loop_checkpoint import LoopCheckpoints
    from lib.research import action
    config=json.loads(Path(config_path).read_text())
    if config['backend']=='tofudb':
        client=StorageV2Client(config['host'],config['port'],config['token'].encode(),
            owner_id=config['owner_id'],tenant_id=config['tenant_id'])
    else:
        client=StorageClient(config['host'],config['port'],config['token'])
    with pytest.MonkeyPatch.context() as monkeypatch:
        configure_worker(monkeypatch,client,'original-default')
        def provider(messages,**kwargs):
            assert kwargs['prefer_model']=='original-default'
            assert kwargs['strict_model'] is False
            return provider_frame({'role':'assistant','content':'','tool_calls':[
                {'id':'before-exit','type':'function','function':{
                    'name':'run_command','arguments':json.dumps({'command':config['command']})}}]},'tool_calls')
        monkeypatch.setattr('lib.llm_dispatch.api.dispatch_stream',provider)
        original=LoopCheckpoints.save_checkpoint
        def save(self,intent,material):
            digest=original(self,intent,material)
            # No terminal record or workspace CAS is manufactured by cleanup.
            os._exit(73)
        monkeypatch.setattr(LoopCheckpoints,'save_checkpoint',save)
        task=create_action_task(config['task_id'],user_id=1,fields=config['fields'])
        action.run_research_action(task)
        raise AssertionError('child did not reach acknowledged checkpoint')


@pytest.mark.parametrize('entry',['worker','resume_command','startup'])
def test_fresh_worker_continues_original_action_without_replay(storage,monkeypatch,tmp_path,record_property,entry):
    from lib.research import action
    from lib.research.action_runtime import create_action_task, _research_action_runtime
    from lib.research.action_continuation import prepare_action_continuation
    from lib.research.action_terminal import load_action_terminal
    from lib.research.execution_owner import ActionExecutionOwners, NAMESPACE
    from lib.research.model_accounting import ModelCalls
    from lib.research.workspace import empty_workspace,save_workspace,load_workspace
    from lib.process_owner import owner_state
    supervisor,backend=storage; client=supervisor.client
    configure_worker(monkeypatch,client,'original-default')
    direction='Process continuation '+uuid.uuid4().hex
    workspace=empty_workspace(direction)
    workspace['hypothesis']='Synthetic negative observation stays authoritative.'
    workspace['protocol'].update(dataset='synthetic fixture',primary_metric='accuracy',baseline='reference',
        random_seeds=[1,2,3],decision_rule={'direction':'higher','minimum_effect':0,'uncertainty_multiplier':2})
    saved=save_workspace(direction,'en',workspace,expected_revision=0,user_id=1)
    observed=observation_payload(saved['protocol'],'original-process-observation','refuted')
    effects=tmp_path/'effects.txt'
    command=shlex.quote(sys.executable)+' -c '+shlex.quote(
        'from pathlib import Path; Path('+repr(str(effects))+').open("a").write("before\\n"); print('+repr(json.dumps(observed))+')')
    task_id='research_action_'+uuid.uuid4().hex[:16]
    fields={'direction':direction,'lang':'en','action':'experiment','expected_revision':saved['revision'],
        'confirm_external_writes':True,'model':'','logical_budget':{'token_budget':320000,'dispatch_budget':10},'tool_rounds':[]}
    host,port=client.endpoint
    config={'backend':backend,'host':host,'port':port,'token':supervisor._token,
        'owner_id':getattr(client,'_owner_id',1),'tenant_id':getattr(client,'_tenant_id',None),
        'task_id':task_id,'fields':fields,'command':command}
    config_path=tmp_path/'worker.json'; config_path.write_text(json.dumps(config));config_path.chmod(0o600)
    # Use the current verified interpreter with its existing loader/library path.
    command_prefix=[sys.executable]
    script='from tests.research.test_research_action_process_continuation import first_worker; first_worker('+repr(str(config_path))+')'
    child=subprocess.run(command_prefix+['-c',script],cwd=Path(__file__).resolve().parents[2],
        capture_output=True,text=True,timeout=240)
    assert child.returncode==73,child.stderr[-4000:]
    assert effects.read_text().splitlines()==['before']
    owners=ActionExecutionOwners.open(client,user_id=1,task_id=task_id)
    original=owners._get(NAMESPACE,owners.prefix,'executionOwner')
    assert owner_state(original['process'])=='dead'
    assert load_workspace(direction,'en',user_id=1)['revision']==saved['revision']
    assert load_action_terminal(task_id,user_id=1)['found'] is False
    # Change the live selector: continuation must retain the original preference.
    monkeypatch.setattr('lib.LLM_MODEL','changed-default')
    import weakref
    from lib.research.loop_checkpoint import LoopCheckpoints
    checkpoints_read=[]
    class TrackedCheckpoint(dict): pass
    original_restore=LoopCheckpoints.restore_checkpoint
    def tracked_restore(self,*args,**kwargs):
        material,meter=original_restore(self,*args,**kwargs)
        material=TrackedCheckpoint(material)
        checkpoints_read.append(weakref.ref(material))
        return material,meter
    monkeypatch.setattr(LoopCheckpoints,'restore_checkpoint',tracked_restore)
    continuation=None
    if entry=='worker':
        continuation=prepare_action_continuation(task_id,user_id=1,checkpoint_ordinal=1)
        assert continuation.meter.snapshot()['agent_tokens']==7
        assert continuation.meter.fallback_model=='original-default'
    after_command=shlex.quote(sys.executable)+' -c '+shlex.quote(
        'from pathlib import Path; Path('+repr(str(effects))+').open("a").write("after\\n"); print("new step")')
    invocations=[]
    def provider(messages,**kwargs):
        assert kwargs['prefer_model']=='original-default' and kwargs['strict_model'] is False
        assert all(reference() is None for reference in checkpoints_read)
        invocations.append(messages)
        if len(invocations)==1:
            assert any(row.get('role')=='tool' and row.get('tool_call_id')=='before-exit' for row in messages)
            return provider_frame({'role':'assistant','content':'','tool_calls':[{'id':'after-exit','type':'function',
                'function':{'name':'run_command','arguments':json.dumps({'command':after_command})}}]},'tool_calls')
        assert len(invocations)==2
        return provider_frame({'role':'assistant','content':json.dumps({'summary':'Synthetic completion.',
            'run':{'id':'continued-run','status':'passed','scientific_outcome':'supported'}})},'stop')
    monkeypatch.setattr('lib.llm_dispatch.api.dispatch_stream',provider)
    if entry=='worker':
        task=create_action_task(task_id,user_id=1,fields=continuation.task_fields())
    else:
        from lib.research.action_runtime import _production
        from lib.research.action_continuation import resume_action
        workers=[]; original_submit=_production.submit_detached
        def submit(*args,**kwargs):
            worker=original_submit(*args,**kwargs);workers.append(worker);return worker
        monkeypatch.setattr(_production,'submit_detached',submit)
        if entry=='startup':
            from lib.research.action_startup import resume_interrupted_actions
            assert resume_interrupted_actions()==1
        else:
            result=resume_action(task_id,user_id=1)
            assert result=={'task_id':task_id,'resumed':True,'deduped':False}
        task=_research_action_runtime.get_owned(task_id,user_id=1)
    try:
        if entry=='worker':
            action.run_research_action(task,continuation=continuation)
        else:
            assert resume_action(task_id,user_id=1)['deduped'] is True
            if entry=='startup':assert resume_interrupted_actions()==0
            assert len(workers)==1
            workers[0].join(timeout=30)
            assert not workers[0].is_alive()
        assert task['status']=='done',task.get('error')
        assert effects.read_text().splitlines()==['before','after']
        assert len(invocations)==2
        record_property('retained_checkpoint_containers_at_dispatch',0)
        assert task['tool_rounds'][0]['toolCallId']=='before-exit'
        assert [row['llmRound'] for row in task['tool_rounds']]==[0,1]
        calls=ModelCalls.open(client,user_id=1,task_id=task_id)
        assert calls.load_model_intent(2)['usage_before']['agent_tokens']==7
        assert calls.load_model_usage(3)['usage_snapshot']['agent_tokens']==21
        assert calls.load_model_usage(3)['usage_snapshot']['agent_dispatch_budget']==10
        updated=load_workspace(direction,'en',user_id=1)
        assert updated['revision']==saved['revision']+1
        assert updated['runs'][-1]['scientific_outcome']=='refuted'
        terminal=load_action_terminal(task_id,user_id=1)['terminal']
        assert terminal['status']=='done' and terminal['usage_snapshot']['agent_dispatches']==3
        assert owners._get(NAMESPACE,owners.prefix,'executionOwner')==original
    finally:
        _research_action_runtime.remove_owned(task_id,user_id=1)


@pytest.mark.parametrize('failure',['foreign_owner','revision','basis','missing_basis','epoch','policy'])
def test_preparation_refuses_changed_authority_before_task_registration(monkeypatch,failure):
    from copy import deepcopy
    from types import SimpleNamespace
    from lib.storage.errors import StorageError
    from lib.research.action_continuation import prepare_action_continuation
    from lib.research.action_observations import BASIS_NAMESPACE
    import tests.research.test_research_process_recovery as fixture
    client=fixture.VersionedRecords()
    defined=fixture.defined_material
    def analyze_material():
        material=defined();material['action_definition']['action']='analyze';return material
    monkeypatch.setattr(fixture,'defined_material',analyze_material)
    repo,_,material,_,_=fixture.setup(client,monkeypatch)
    workspace={'direction':'Frozen basis','lang':'en','revision':7,**repo.load_basis()}
    if failure=='revision':workspace['revision']=8
    elif failure=='basis':workspace['hypothesis']='different scientific basis'
    elif failure=='missing_basis':client.rows.pop((BASIS_NAMESPACE,repo.prefix))
    monkeypatch.setattr('lib.storage.get_storage_client',lambda **kwargs:client)
    monkeypatch.setattr('lib.research.workspace.load_workspace',lambda *args,**kwargs:workspace)
    monkeypatch.setattr('lib.research.action.build_action_tool_epoch',lambda **kwargs:(
        SimpleNamespace(epoch_hash='changed-epoch' if failure=='epoch' else 'declared-epoch'),[],[]))
    before=deepcopy(client.rows)
    if failure=='policy':
        continuation=prepare_action_continuation('research_action_observed',user_id=1,checkpoint_ordinal=1)
        task={'task_id':continuation.task_id,'user_id':1,**continuation.task_fields()}
        task['logical_budget']['dispatch_budget']=20
        after_claim=deepcopy(client.rows)
        with pytest.raises(StorageError,match='original task policy'):continuation.validate_task(task)
        assert client.rows==after_claim
    else:
        with pytest.raises(StorageError):
            prepare_action_continuation('research_action_observed',user_id=2 if failure=='foreign_owner' else 1,
                checkpoint_ordinal=1)
        assert client.rows==before
