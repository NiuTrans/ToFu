"""Native actor transfers and ordinary Goal events retain one publication cursor."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace
import threading
from unittest.mock import AsyncMock

import pytest

from lib.goal_runs.account_journal import install_account_journal, read_account_frontier
from lib.goal_runs.journal_publication import GoalJournalPublisher
from lib.orchestration.execution_journal import ExecutionJournal, RecoveryRefused
from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
from lib.tasks_pkg.native_goal_handoff import NativeGoalHandoff
from tests.agent_core.test_native_goal_handoff import Storage, principal
from tests.agent_core.test_recovered_actor_prefix import root, child, source
from tests.agent_core.test_shared_account_checkpoint import PROFILE

pytestmark = pytest.mark.unit


class Events:
    def __init__(self): self.rows=[]
    def project_event(self, run, cursor, event):
        assert run == 'goal_target'
        if cursor != len(self.rows): return False
        self.rows.append(deepcopy(event))
        return True


@pytest.fixture
def composed():
    task,budget=root('target');task['_goalRunId']='goal_target'
    budget.restore_pending_frontier(task,source(),owner=7)
    metadata=dict(run_id='goal_target',task_id='target',owner_user_id=7,launch_id='launch',
        account_frontier_format='tofu.execution-account-frontier/v1')
    events=Events();publication=GoalJournalPublisher(task,events,metadata,cursor=0)
    journal=ExecutionJournal(publication,account_handoff_commit=publication.commit_account_handoff)
    journal.begin({},'',launch_metadata=metadata)
    storage=Storage()
    handoff=NativeGoalHandoff(principal=principal(),task=task,storage=storage,
        evidence_bytes=1048576,io_timeout_ms=5000,journal=journal)
    install_account_journal(task,journal,metadata,native_handoff=handoff)
    native=storage.native_checkpoint_control
    def commit(operation,payload,**kwargs):
        response=native(operation,payload,**kwargs)
        if operation=='goal_handoff':
            control=payload['goal_handoff'];auth=control['authorization'];cursor=auth['target_cursor']
            for cut in auth['account_frontiers']:
                assert events.project_event('goal_target',cursor,dict(type='execution_account_frontier',launch_id='launch',frontier=cut))
                cursor+=1
            assert events.project_event('goal_target',cursor,dict(type='execution_recovery_consumed'))
        return response
    storage.native_checkpoint_control=commit
    with ThreadPoolExecutor(max_workers=2) as executor:
        policies=[]
        for suffix in ('a','b'):
            actor=child(task,'target-'+suffix)
            actor.update(_transientRuntime=True,_invocationStorageFree=False,convId='conversation',_archiveScope='invocation:7:'+actor['id'])
            birth=dict(user_id=7,conversation_id='conversation',parent_task_id='target',child_task_id=actor['id'],archive_scope=actor['_archiveScope'])
            storage.records[actor['id']]={'value':dict(child_invocation=birth,task_id=actor['id'],user_id=7,conv_id='conversation')}
            policy=NativeCheckpointPolicy(principal=principal(),task=actor,storage=storage,executor=executor,
                epoch='epoch',binding='binding',token='token-'+suffix,header_bytes=4096,snapshot_bytes=65536,
                identity_bytes=1024,io_timeout_ms=5000,restore=AsyncMock(),registered_child=True)
            policies.append((policy,'source-'+suffix))
        case=SimpleNamespace(task=task,budget=budget,metadata=metadata,events=events,publication=publication,
            journal=journal,handoff=handoff,storage=storage,policies=policies)
        try: yield case
        finally:
            import asyncio
            for policy,_ in policies:asyncio.run(policy.close())
            handoff.close()


def consume(case,**positions):
    case.handoff.consume(case.policies,authorization=dict(claim_request={'invoice':{'task_id':'source'}},**positions))


def test_atomic_native_ack_advances_existing_cursor_before_new_tools_and_effects(composed):
    c=composed;cursor=c.publication.cursor;version=c.publication.account_version
    consume(c)
    assert c.publication.cursor==len(c.events.rows)==cursor+3
    assert c.publication.account_sequence==cursor+1 and c.publication.account_version==version+2
    assert [p.capability for p,_ in c.policies]==[11,12]
    actor=c.policies[0][0].task
    saved=c.budget.capture_actor_account(owner=7,task=actor,counter_policy=PROFILE)
    c.budget.reconcile_recovered_actor_tools(saved,owner=7,task=actor,source_task_id='source-a',count=1)
    assert c.journal.call('tool','following',0,{},lambda:'done')=='done'
    assert c.publication.cursor==len(c.events.rows)==cursor+6
    assert [r['type'] for r in c.events.rows[cursor:]]==[
        'execution_account_frontier','execution_account_frontier','execution_recovery_consumed',
        'execution_account_frontier','execution_effect_started','execution_effect_completed']
    assert read_account_frontier(c.events.rows,c.metadata)['snapshot']['tool_calls']==6
    assert c.events.rows[cursor+3]['frontier']['reason']=='tools_reconciled'


@pytest.mark.parametrize('field',['target_launch_sequence','target_account_sequence','target_cursor'])
def test_stale_supplied_journal_position_fences_before_native_commit(composed,field):
    c=composed;cursor=c.publication.cursor
    with pytest.raises(ValueError,match='position changed'):consume(c,**{field:999})
    assert not c.storage.calls and c.publication.cursor==cursor
    assert c.journal.abort_requested() and c.budget.reason()=='execution_account_frontier_unavailable'


@pytest.mark.parametrize('committed',[False,True])
def test_unknown_native_ack_never_advances_or_retries_the_ordinary_cursor(composed,committed):
    c=composed;native=c.storage.native_checkpoint_control;cursor=c.publication.cursor;calls=[]
    def fail(operation,payload,**kwargs):
        if operation=='goal_handoff':
            calls.append(operation)
            if committed:native(operation,payload,**kwargs)
            raise OSError('native acknowledgement unavailable')
        return native(operation,payload,**kwargs)
    c.storage.native_checkpoint_control=fail
    with pytest.raises(OSError):consume(c)
    assert calls==['goal_handoff'] and c.publication.cursor==cursor
    assert len(c.events.rows)==cursor+(3 if committed else 0)
    assert all(p.capability is None for p,_ in c.policies)
    assert c.budget.consumption()==(36,5) and c.journal.abort_requested()
    with pytest.raises(RecoveryRefused):c.journal.call('tool','forbidden',0,{},lambda:pytest.fail('effect executed'))


def test_sibling_publication_waits_for_the_atomic_native_ack(composed):
    c=composed;native=c.storage.native_checkpoint_control;entered=threading.Event();release=threading.Event();attempted=threading.Event()
    def block(operation,payload,**kwargs):
        if operation=='goal_handoff':
            entered.set();assert release.wait(5)
        return native(operation,payload,**kwargs)
    c.storage.native_checkpoint_control=block
    def sibling():
        attempted.set();return c.journal.call('tool','sibling',0,{},lambda:'done')
    before=c.publication.cursor
    with ThreadPoolExecutor(max_workers=2) as executor:
        transfer=executor.submit(consume,c)
        try:
            assert entered.wait(5)
            effect=executor.submit(sibling);assert attempted.wait(5)
            assert not effect.done() and c.publication.cursor==before
        finally:release.set()
        transfer.result(timeout=5);assert effect.result(timeout=5)=='done'
    assert c.publication.cursor==len(c.events.rows)==before+5


def test_swallowed_reentrant_publication_still_revokes_capability_adoption(composed):
    c=composed;native=c.storage.native_checkpoint_control;cursor=c.publication.cursor
    def reenter(operation,payload,**kwargs):
        if operation=='goal_handoff':
            with pytest.raises(RecoveryRefused):c.journal.call('tool','reentrant',0,{},lambda:pytest.fail('effect executed'))
        return native(operation,payload,**kwargs)
    c.storage.native_checkpoint_control=reenter
    with pytest.raises(RecoveryRefused,match='revoked during'):consume(c)
    assert c.publication.cursor==len(c.events.rows)==cursor+3
    assert all(p.capability is None for p,_ in c.policies)
    assert c.journal.abort_requested() and c.budget.reason()=='execution_account_frontier_unavailable'
    assert [op for op,_ in c.storage.calls]==['goal_handoff','forget','forget']


@pytest.mark.parametrize('damage',['owner','version','reason','task','run','tenant'])
def test_mismatched_cuts_never_enter_native_publication(composed,damage):
    c=composed;cut=deepcopy(c.events.rows[-1]['frontier']);cut['version']+=1;cut['reason']='actor_rebound'
    if damage=='owner':cut['snapshot']['owner_user_id']=8
    elif damage=='version':cut['version']+=1
    elif damage=='reason':cut['reason']='tools_reconciled'
    elif damage=='task':c.task['id']='other'
    elif damage=='run':c.task['_goalRunId']='goal_other'
    else:c.task['_tenant_id']='foreign'
    with pytest.raises((RecoveryRefused,ValueError)):
        c.journal.commit_account_handoff([cut],lambda positions:pytest.fail('native commit entered'))
    assert c.journal.abort_requested()


def test_native_principal_tenant_must_match_the_journal(composed):
    c=composed;c.handoff.tenant_id='foreign'
    with pytest.raises(RecoveryRefused,match='binding'):consume(c)
    assert not c.storage.calls and c.journal.abort_requested()
    assert c.budget.reason()=='execution_account_frontier_unavailable'


def test_generic_journal_requires_an_explicit_account_transaction_port():
    journal=ExecutionJournal(lambda event:None)
    with pytest.raises(RecoveryRefused):journal.commit_account_handoff([],lambda positions:pytest.fail('native commit entered'))
    assert journal.abort_requested()


@pytest.mark.parametrize('failure',[KeyboardInterrupt, SystemExit])
def test_interrupted_native_callback_revokes_account_and_journal(composed,failure):
    c=composed;native=c.storage.native_checkpoint_control;cursor=c.publication.cursor
    def interrupt(operation,payload,**kwargs):
        if operation=='goal_handoff':raise failure('interrupted native wait')
        return native(operation,payload,**kwargs)
    c.storage.native_checkpoint_control=interrupt
    with pytest.raises(failure):consume(c)
    assert c.publication.cursor==cursor and c.journal.abort_requested()
    assert c.budget.reason()=='execution_account_frontier_unavailable'


def test_chunked_prefix_tracks_logical_reference_positions_before_native_cuts(composed,monkeypatch):
    from lib.goal_runs.journal_codec import CHUNK_EVENT
    c=composed;monkeypatch.setattr('lib.goal_runs.journal_codec.CHUNK_BYTES',128)
    metadata=dict(c.metadata,padding='long launch '+('x'*900))
    events=Events();publisher=GoalJournalPublisher(c.task,events,metadata,cursor=0)
    journal=ExecutionJournal(publisher,account_handoff_commit=publisher.commit_account_handoff)
    journal.begin({},'',launch_metadata=metadata)
    original=deepcopy(c.events.rows[-1]['frontier'])
    journal.publish_account_frontier(original,launch_id=metadata['launch_id'])
    launch_sequence=next(i for i,row in enumerate(events.rows) if row['type']=='execution_journal_start')
    account_sequence=len(events.rows)-1;cursor=len(events.rows)
    assert any(row['type']==CHUNK_EVENT for row in events.rows)
    consume(c)
    cuts=c.storage.calls[0][1]['goal_handoff']['authorization']['account_frontiers']
    observed=[]
    journal.commit_account_handoff(cuts,observed.append)
    assert observed==[dict(target_launch_sequence=launch_sequence,target_account_sequence=account_sequence,target_cursor=cursor)]
    assert publisher.cursor==cursor+len(cuts)+1
