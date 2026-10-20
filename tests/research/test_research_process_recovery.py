"""Dead-process ownership transfer preserves exact completed work and budgets."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from pathlib import Path
import subprocess

import pytest

from lib.agent_loop.agent_loop import AbortSignal, run_agent_loop
from lib.paper.agent_usage import PaperAgentUsageMeter
from lib.research.execution_owner import ActionExecutionOwners, NAMESPACE, HEAD_NAMESPACE, HISTORY_NAMESPACE
from lib.research.loop_checkpoint import LoopCheckpoints
from lib.research.request_context import RequestContexts
from lib.research.model_response import ModelResponses
from lib.storage.errors import StorageError
from tests.research.test_research_action_observations import MemoryRecords
from tests.research.test_research_model_accounting import repository
from tests.research.test_research_request_context import defined_material
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


class VersionedRecords(MemoryRecords):
    def __init__(self):
        super().__init__(); self.lock = Lock(); self.lose_recovery_ack = False

    def command(self, operation, payload, idempotency_key):
        if payload['namespace'] != HEAD_NAMESPACE:
            return super().command(operation, payload, idempotency_key)
        with self.lock:
            key = payload['namespace'], payload['key']
            current = self.rows.get(key)
            version = current['version'] if current else 0
            if version != payload['expected_version']:
                raise StorageError('database_conflict', 'version changed')
            self.rows[key] = {'version': version + 1, 'value': deepcopy(payload['value'])}
            if self.lose_recovery_ack:
                raise StorageError('database_unavailable', 'lost recovery acknowledgment')
            return {'version': version + 1}


def setup(client, monkeypatch, *, unmetered=False, halted=False):
    import lib.research.execution_owner as owners
    process = owners.process_owner()
    if process is None:
        pytest.skip('process identity unavailable')
    child = subprocess.Popen(['sleep', '60'])
    try:
        tail = Path(f'/proc/{child.pid}/stat').read_text().rsplit(') ', 1)[1].split()
        old_process = {**process, 'pid': child.pid, 'started': tail[19]}
    finally:
        child.terminate(); child.wait(timeout=5)
    assert owners.owner_state(old_process) == 'dead'
    monkeypatch.setattr(owners, 'process_owner', lambda: old_process)
    calls = repository(client)
    repo = ActionExecutionOwners.open(client, user_id=1, task_id='research_action_observed')
    old_claim = repo.claim_initial()
    monkeypatch.setattr(owners, 'process_owner', lambda: process)
    context = RequestContexts.open(client, user_id=1, task_id='research_action_observed')
    request = defined_material(); digest = context.save_request(request)
    meter = PaperAgentUsageMeter('research-action', token_budget=320000, dispatch_budget=10)
    meter._compute_cost = lambda *args, **kwargs: {'costUsd': 0.01, 'costCny': 0.07}
    meter.begin_dispatch()
    intent = calls.admit_model_call(ordinal=1, round_index=0, requested_model='controlled',
        request_digest=digest, max_output=8000, usage_before=meter.snapshot(),
        request_snapshot_version=1, response_snapshot_version=1, loop_checkpoint_version=1)
    message = {'tool_calls':[{'id':'completed-command','function':{'name':'run_command','arguments':'{}'}}]}
    response = ModelResponses.open(client, user_id=1, task_id='research_action_observed')
    response_digest = response.save_response(intent, message, 'tool_calls')
    usage = None if unmetered else {'prompt_tokens':3, 'completion_tokens':4, 'total_tokens':7}
    meter.observe_agent_round(usage, message)
    calls.record_model_usage(intent, meter.snapshot(), usage_mapping_received=isinstance(usage, dict), response_digest=response_digest)
    captured=[]
    class Stop(Exception): pass
    def checkpoint(value): captured.append(value); raise Stop()
    with pytest.raises(Stop):
        run_agent_loop(abort=AbortSignal.never(), round_tools=[], dispatch=lambda *args:(message,'tool_calls',usage),
            execute_tool=lambda *args:'finished output', max_consecutive_no_progress_rounds=3,
            on_checkpoint=checkpoint)
    material={'messages':[{'role':'tool','tool_call_id':'completed-command','content':'finished output'}],
        'tool_rounds':[{'roundNum':1,'llmRound':0,'status':'done','toolCallId':'completed-command'}],
        'usage_snapshot':meter.snapshot(),'chassis':captured[0]}
    if halted:
        material['chassis']['progress']['no_progress_streak'] = 3
    checkpoints=LoopCheckpoints.open(client,user_id=1,task_id='research_action_observed')
    checkpoints.save_checkpoint(intent,material)
    return repo,old_claim,material,request['action_definition'],calls


def recover(repo, material, **overrides):
    return repo.recover_checkpoint(1, tool_epoch_hash='declared-epoch',
        expected_limits=material['chassis']['limits'], **overrides)


def test_transfer_preserves_original_records_counters_tools_and_policy(monkeypatch):
    client=VersionedRecords(); repo,old,material,definition,_=setup(client,monkeypatch)
    before=deepcopy(client.rows)
    client.lose_recovery_ack=True
    client.lose_ack=True
    claim,restored,meter,policy=recover(repo,material)
    claim.assert_current()
    assert restored==material and meter.snapshot()==material['usage_snapshot'] and policy==definition
    assert meter.fallback_model == 'controlled'
    assert meter.snapshot()['agent_tokens']==7 and meter.snapshot()['agent_dispatches']==1
    assert all(client.rows[key]==value for key,value in before.items())
    assert set(client.rows)-set(before)=={(HEAD_NAMESPACE,repo.prefix),(HISTORY_NAMESPACE,repo.prefix+old.value['nonce'])}
    with pytest.raises(StorageError,match='no longer owns'): old.assert_current()
    with pytest.raises(StorageError,match='proven old process death'): recover(repo,material)
    with pytest.raises(StorageError): repo.claim_initial()


@pytest.mark.parametrize('failure',['alive','unknown','unknown_current','terminal','successor','unmetered','halted','epoch','limits','missing_response','missing_history'])
def test_uncertain_or_incompatible_frontier_never_grants_takeover(monkeypatch,failure):
    import lib.research.execution_owner as owners
    from lib.research.action_terminal import NAMESPACE as TERMINAL_NAMESPACE
    client=VersionedRecords(); repo,old,material,_,calls=setup(client,monkeypatch,unmetered=failure=='unmetered',halted=failure=='halted')
    epoch='declared-epoch'; limits=deepcopy(material['chassis']['limits'])
    if failure in ('alive','unknown'): monkeypatch.setattr(owners,'owner_state',lambda value:failure)
    elif failure=='unknown_current': monkeypatch.setattr(owners,'process_owner',lambda:None)
    elif failure=='terminal': client.rows[TERMINAL_NAMESPACE,repo.prefix]={'version':1,'value':{}}
    elif failure=='successor':
        before=deepcopy(material['usage_snapshot']); before['agent_dispatches']=2
        calls.admit_model_call(ordinal=2,round_index=1,requested_model='controlled',request_digest='d'*64,
            max_output=8000,usage_before=before)
    elif failure=='epoch': epoch='changed-epoch'
    elif failure=='limits': limits['max_retry_bonus']+=1
    elif failure=='missing_response':
        from lib.research.model_response import NAMESPACE as RESPONSE_NAMESPACE
        client.rows.pop((RESPONSE_NAMESPACE, repo.prefix+'1'))
    elif failure=='missing_history':
        recover(repo,material)
        client.rows.pop((HISTORY_NAMESPACE,repo.prefix+old.value['nonce']))
    before=deepcopy(client.rows)
    with pytest.raises((StorageError,ValueError)):
        repo.recover_checkpoint(1,tool_epoch_hash=epoch,expected_limits=limits)
    assert client.rows==before


def test_concurrent_recovery_has_one_winner(storage,monkeypatch):
    supervisor,_=storage
    repo,old,material,_,_=setup(supervisor.client,monkeypatch)
    def attempt(_):
        try: return recover(repo,material)[0]
        except StorageError: return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        winners=[claim for claim in executor.map(attempt,range(2)) if claim is not None]
    assert len(winners)==1
    winners[0].assert_current()
    with pytest.raises(StorageError): old.assert_current()


def test_repeated_process_loss_advances_one_head_and_keeps_immutable_history(monkeypatch):
    import lib.research.execution_owner as owners
    client=VersionedRecords(); repo,initial,material,_,_=setup(client,monkeypatch)
    first,*_=recover(repo,material)
    next_process={**first.value['process'],'pid':first.value['process']['pid']+1}
    monkeypatch.setattr(owners,'process_owner',lambda:next_process)
    monkeypatch.setattr(owners,'owner_state',lambda value:'alive' if value==next_process else 'dead')
    second,restored,meter,_=recover(repo,material)
    assert second.value['recovery']['generation']==2
    assert second.value['recovery']['previous_nonce']==first.value['nonce']
    assert client.rows[HEAD_NAMESPACE,repo.prefix]['version']==2
    assert client.rows[NAMESPACE,repo.prefix]['value']==initial.value
    assert restored==material and meter.snapshot()==material['usage_snapshot']
    with pytest.raises(StorageError): first.assert_current()
    with pytest.raises(StorageError): initial.assert_current()
    second.assert_current()


def test_restored_chassis_continues_at_next_round_without_reexecuting_completed_command(monkeypatch):
    client=VersionedRecords(); repo,_,material,_,calls=setup(client,monkeypatch)
    claim,restored,meter,_=recover(repo,material)
    effects=[]; dispatches=[]
    def dispatch(round_index,tools):
        claim.assert_current()
        dispatches.append(round_index)
        assert round_index==1
        before=deepcopy(meter.snapshot()); before['agent_dispatches']=2
        calls.admit_model_call(ordinal=2,round_index=round_index,requested_model='controlled',
            request_digest='d'*64,max_output=8000,usage_before=before)
        return {'content':'continue from completed evidence'},'stop',{}
    run_agent_loop(abort=AbortSignal.never(),round_tools=[],dispatch=dispatch,
        execute_tool=lambda *args:effects.append(args), max_consecutive_no_progress_rounds=3,
        resume_checkpoint=restored['chassis'])
    assert dispatches==[1] and effects==[]
    assert calls.load_model_intent(2)['usage_before']['agent_tokens']==7
    assert calls.load_model_intent(2)['usage_before']['agent_dispatch_budget']==10


def test_claim_validation_read_count_does_not_scale_with_owner_history(monkeypatch):
    import lib.research.execution_owner as owners
    client=VersionedRecords(); repo,old,material,_,_=setup(client,monkeypatch)
    original_query=client.query; reads=[]
    def query(operation,payload):
        reads.append(operation)
        return original_query(operation,payload)
    client.query=query
    # Inspect the initial claim under its own original process incarnation.
    current=owners.process_owner()
    monkeypatch.setattr(owners,'process_owner',lambda:old.value['process'])
    old.assert_current()
    assert reads==['record.get','record.get']
    monkeypatch.setattr(owners,'process_owner',lambda:current)
    claim,*_=recover(repo,material)
    reads.clear(); claim.assert_current()
    assert reads==['record.get','record.get','record.get']


def test_cold_task_queries_follow_recovered_owner_without_scanning_or_writing(monkeypatch):
    import lib.research.execution_owner as owners
    from lib.research.action_terminal import load_public_action_task
    client=VersionedRecords(); repo,_,material,_,_=setup(client,monkeypatch)
    claim,*_=recover(repo,material)
    monkeypatch.setattr('lib.storage.get_storage_client',lambda **kwargs:client)
    original_query=client.query; reads=[]
    def query(operation,payload):
        reads.append(operation)
        return original_query(operation,payload)
    client.query=query
    monkeypatch.setattr(client,'command',lambda *args:pytest.fail('cold lookup wrote or replayed'))
    assert owners.owner_state(claim.value['process'])=='alive'
    assert load_public_action_task('research_action_observed',user_id=1) is None
    assert reads==['record.get']*7
    monkeypatch.setattr(owners,'owner_state',lambda value:'dead')
    reads.clear()
    public=load_public_action_task('research_action_observed',user_id=1)
    assert public['status']=='interrupted' and public['result'] is None
    assert reads==['record.get']*7


def test_terminal_publication_during_handoff_returns_no_execution_claim(monkeypatch):
    from lib.research.action_terminal import NAMESPACE as TERMINAL_NAMESPACE
    client=VersionedRecords(); repo,_,material,_,_=setup(client,monkeypatch)
    original_command=client.command
    def command(operation,payload,key):
        ack=original_command(operation,payload,key)
        if payload['namespace']==HEAD_NAMESPACE:
            client.rows[TERMINAL_NAMESPACE,repo.prefix]={'version':1,'value':{}}
        return ack
    client.command=command
    with pytest.raises(StorageError,match='settled during recovery'):
        recover(repo,material)
