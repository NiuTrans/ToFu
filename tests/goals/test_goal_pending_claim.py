"""Pending Goal claims preserve exact stored positions and original fee evidence."""
from copy import deepcopy
from dataclasses import replace
import threading
from types import SimpleNamespace

import pytest

from lib.goal_runs.pending_claim import claim_pending_goal_actors
from lib.goal_runs.pending_recovery import journal_positions
from lib.goal_runs.recovery import GoalRecoverySeed
from lib.goal_runs.journal_codec import encode_event, decode_events
from lib.identity import PrincipalContext
from lib.orchestration.execution_journal import JournalRecovery, RecoveryRefused, fingerprint
from tests.goals.test_goal_pending_fee_evidence import mixed
from tofu_db.generated_checkpoint_control import GOAL_CLAIM_FORMAT

pytestmark=pytest.mark.unit


@pytest.fixture
def candidate():
    evidence,pending,storage,record,_=mixed()
    evidence.metadata.update(tenant_id='',objective='Keep the full original scope')
    keys=list(pending.actors)+list(evidence.journal.records)
    positions=dict(source_cursor=90,launch_sequence=7,account_sequence=80,actors=dict(zip(keys,[20,40])))
    pending=replace(pending,positions=positions)
    seed=GoalRecoverySeed('goal_flow-root',{},evidence.metadata,evidence.journal.records,pending_actors=pending)
    task=dict(id='target',_goalRunId='goal_target',_userId=7,convId='conversation',_turnId='new-turn',_attemptId='new-attempt',
        _goalRecoverySeed=seed,abort_event=threading.Event(),config=dict(_goalRecoverySourceRunId=seed.source_run_id,
            _goalObjective=evidence.metadata['objective']))
    principal=PrincipalContext.user(subject_id='fixture',owner_user_id=7)
    attempt=dict(taskId='target',turnId='new-turn',conversationId='conversation',status='running',config={
        '_goalRecoverySourceRunId':seed.source_run_id,'_turnPrincipalTenantId':''})
    query=storage.query
    storage.query=lambda operation,payload: deepcopy(attempt) if operation=='turn.attempt.get' else query(operation,payload)
    storage.native_checkpoint_available=lambda **kwargs:True
    calls=[]
    def native(operation,payload,**options):
        calls.append((operation,deepcopy(payload),options))
        request=payload['goal_claim']['request']
        receipt=dict(type='execution_recovery_claim',successor_run_id='goal_target',native_actor_claim=dict(
            format=GOAL_CLAIM_FORMAT,request_sha256=fingerprint(request),target_attempt_id='new-attempt',
            source_cursor=90,account_version=pending.account_frontier['version'],invoice=deepcopy(request['invoice']),
            actors=[dict(task_id=row['admission']['invocation_id'],source_version=row['frontier']['version'],
                version=row['frontier']['version']+1) for row in pending.actors.values()]))
        return dict(accepted=True,state=receipt)
    storage.native_checkpoint_control=native
    return SimpleNamespace(evidence=evidence,pending=pending,seed=seed,task=task,principal=principal,storage=storage,
        attempt=attempt,calls=calls,record=record)


def claim(c,**overrides):
    return claim_pending_goal_actors(c.task,c.seed,**dict(dict(principal=c.principal,storage=c.storage,
        wallet_user_id='original-wallet',reserved_micro=100000,header_bytes=4096,snapshot_bytes=1048576,
        evidence_bytes=8*1048576,io_timeout_ms=5000),**overrides))


def test_complete_mixed_claim_preserves_paid_totals_positions_and_original_seed(candidate):
    c=candidate;before=deepcopy(c.seed)
    admitted=claim(c)
    assert c.seed==before and '_executionBudget' not in c.task
    operation,payload,options=c.calls[0];request=payload['goal_claim']['request']
    assert operation=='goal_claim' and len(c.calls)==1
    assert request['actors']==[dict(event_sequence=20,registration_version=1,checkpoint_version=3),
                              dict(event_sequence=40,registration_version=4)]
    assert (request['source_cursor'],request['launch_sequence'],request['account_sequence'])==(90,7,80)
    assert request['invoice']['componentsMicro']['totalMicro']==15186
    assert request['invoice']['roundCount']==2 and request['invoice']['task_id']=='flow-root'
    admitted.require_matches(c.task,c.seed)
    assert options['user_id']==7 and not c.task.get('_executionBudget')
    with pytest.raises(RecoveryRefused):c.seed.require_settled()


@pytest.mark.parametrize('damage',['missing_positions','duplicate_position','foreign_run','principal','tenant','limits',
    'cancelled','accepted_task','accepted_turn','accepted_status','version','no_native','bad_bound'])
def test_invalid_claim_never_enters_native_mutation(candidate,damage):
    c=candidate;options={}
    if damage=='missing_positions':c.pending.positions.clear()
    elif damage=='duplicate_position':c.pending.positions['actors']={key:20 for key in c.pending.positions['actors']}
    elif damage=='foreign_run':c.task['_goalRunId']='goal_foreign'
    elif damage=='principal':options['principal']=PrincipalContext.user(subject_id='other',owner_user_id=8)
    elif damage=='tenant':c.task['_tenant_id']='other'
    elif damage=='limits':c.task['config']['executionLimits']={'max_tokens':1,'max_tool_calls':None,'deadline':None}
    elif damage=='cancelled':c.task['abort_event'].set()
    elif damage=='accepted_task':c.attempt['taskId']='other'
    elif damage=='accepted_turn':c.attempt['turnId']='other'
    elif damage=='accepted_status':c.attempt['status']='interrupted'
    elif damage=='version':next(iter(c.pending.actors.values()))['frontier']['version']=True
    elif damage=='no_native':c.storage.native_checkpoint_available=lambda **kwargs:False
    else:options['header_bytes']=True
    with pytest.raises(RecoveryRefused):claim(c,**options)
    assert not c.calls and '_executionBudget' not in c.task


@pytest.mark.parametrize('damage',['unknown','refused','partial','wrong_actor','invoice','digest','version'])
def test_unknown_or_incomplete_ack_grants_no_journal_preparation(candidate,damage):
    c=candidate;native=c.storage.native_checkpoint_control
    def fail(operation,payload,**kwargs):
        value=native(operation,payload,**kwargs)
        if damage=='unknown':raise OSError('native outcome unknown')
        elif damage=='refused':value['accepted']=False
        elif damage=='partial':value['state']['native_actor_claim']['actors'].clear()
        elif damage=='wrong_actor':value['state']['native_actor_claim']['actors'][0]['task_id']='other'
        elif damage=='invoice':value['state']['native_actor_claim']['invoice']['roundCount']+=1
        elif damage=='digest':value['state']['native_actor_claim']['request_sha256']='0'*64
        else:value['state']['native_actor_claim']['actors'][0]['version']=True
        return value
    c.storage.native_checkpoint_control=fail
    with pytest.raises((RecoveryRefused,OSError)):claim(c)
    assert len(c.calls)==1 and '_executionBudget' not in c.task


@pytest.mark.parametrize('damage',['seed','request','receipt','task','cancelled'])
def test_claim_cannot_prepare_a_different_generation_or_mutated_seed(candidate,damage):
    c=candidate;admitted=claim(c)
    if damage=='seed':c.seed.metadata['launch']['model']='other'
    elif damage=='request':admitted.request['source_cursor']+=1
    elif damage=='receipt':admitted.receipt['successor_run_id']='goal_other'
    elif damage=='task':c.task['_attemptId']='other'
    else:c.task['abort_event'].set()
    with pytest.raises(RecoveryRefused):admitted.require_matches(c.task,c.seed)
    assert '_executionBudget' not in c.task


def test_chunked_claim_positions_come_from_reference_packets_not_payload_or_list_indexes(monkeypatch):
    monkeypatch.setattr('lib.goal_runs.journal_codec.CHUNK_BYTES',128)
    launch=dict(type='execution_journal_start',seq=99999,padding='x'*800)
    pending=dict(type='execution_effect_started',key='pending',kind='agent')
    completed=dict(type='execution_effect_completed',key='finished',kind='agent',padding='y'*800)
    account=dict(type='execution_account_frontier',padding='z'*800)
    packets=[];expected={}
    for event in (launch,pending,completed,account):
        for packet in encode_event(event):packets.append(dict(packet,seq=len(packets)+7))
        expected[event['type']]=packets[-1]['seq']
    positioned=list(decode_events(packets,include_positions=True))
    assert positioned[0][1]['seq']==99999 and positioned[0][0]==expected['execution_journal_start']
    journal=JournalRecovery({'finished':dict(kind='agent')},{'pending':pending})
    result=journal_positions(positioned,journal=journal,source_cursor=packets[-1]['seq']+4)
    assert result==dict(source_cursor=packets[-1]['seq']+4,launch_sequence=expected['execution_journal_start'],
        account_sequence=expected['execution_account_frontier'],actors=dict(pending=expected['execution_effect_started'],finished=expected['execution_effect_completed']))
    assert list(decode_events(packets))==[launch,dict(pending,seq=expected['execution_effect_started']),completed,account]


@pytest.mark.parametrize('damage',['missing','boolean','reordered','inventory','cursor'])
def test_positions_refuse_missing_or_fabricated_physical_sequence(damage):
    rows=[(2,dict(type='execution_journal_start')),(4,dict(type='execution_effect_started',kind='agent',key='a')),
          (7,dict(type='execution_account_frontier'))]
    cursor=9;journal=JournalRecovery({},dict(a={}))
    if damage=='missing':rows[0]=(None,rows[0][1])
    elif damage=='boolean':rows[0]=(True,rows[0][1])
    elif damage=='reordered':rows[1]=(2,rows[1][1])
    elif damage=='inventory':journal.pending_agents['other']={}
    else:cursor=7
    with pytest.raises(RecoveryRefused):journal_positions(rows,journal=journal,source_cursor=cursor)
