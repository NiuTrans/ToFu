"""Actual Python account/policy adoption over the attested Goal C ABI consumer.

The opt-in fixture is a closed private authority prepared by the Rust Goal test,
not a copy of application data. Each test owns its copy and selected runtime.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from functools import partial
import json
import os
from pathlib import Path
import shutil
import threading

import pytest

from lib.agent_core.native_process import CheckpointRequest
from lib.identity import PrincipalContext
from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
from lib.tasks_pkg.native_cost_projection import NativeTaskCostPolicy
from lib.tasks_pkg.native_goal_handoff import NativeGoalHandoff
from lib.tasks_pkg.native_model_account import NativeModelAccountPolicy
from tests.billing.test_recovered_actor_fee_transfer import root, child
from tofu_db.embedded import EmbeddedRuntime

pytestmark=pytest.mark.api

class UnresolvedTools:
    async def capture(self,request):raise AssertionError('no new tool dispatch')
    async def committed(self,request):raise AssertionError('no new tool dispatch')
    async def reconcile(self,request):raise ValueError('domain tool reconciliation remains required')

@pytest.mark.parametrize('journal_bound,fault,asynchronous', [
    (False,None,False),(True,None,False),(True,'invalid_cut',False),(True,'lost_ack',False),
    (True,None,True),(True,'invalid_cut',True),(True,'lost_ack',True)])
def test_actual_native_group_load_restores_account_and_original_fees_without_reexecution(tmp_path, journal_bound, fault, asynchronous, monkeypatch):
    fixture=os.environ.get('TOFU_NATIVE_GOAL_HANDOFF_FIXTURE')
    library=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_LIBRARY')
    expected=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_SOURCE_SHA256')
    if not fixture or not library or not expected:
        pytest.skip('explicit private Goal fixture and attested native library required')
    fixture=Path(fixture);directory=tmp_path/'authority';shutil.copytree(fixture/'authority',directory)
    control=json.loads((fixture/'request.json').read_text())['goal_handoff']
    original=json.loads((Path(__file__).resolve().parents[2]/'packages/tofu-db/tests/fixtures/native_goal_actor_claim.json').read_text())
    principal=PrincipalContext.user(subject_id='goal-handoff-contract',owner_user_id=7,tenant_id='3')
    task,budget=root('target-root');budget.restore_pending_frontier(task,original['cut'],owner=7)
    from lib.agent_core.abort_signal import AbortSignal
    task['abort_event']=AbortSignal()
    with EmbeddedRuntime(Path(library),directory,owner_id=7,tenant_id=3,secret=b'x'*32) as runtime, ThreadPoolExecutor(max_workers=1 if asynchronous else 2) as executor:
        assert runtime.build_identity()['source_sha256']==expected
        storage=EmbeddedStorageV2Client(runtime,b'x'*32,owner_id=7,tenant_id=3)
        journal=publication=None
        if journal_bound:
            from lib.goal_runs.journal_publication import GoalJournalPublisher
            from lib.orchestration.execution_journal import ExecutionJournal
            from lib.orchestration.run_service import OrchestrationRunService
            from lib.orchestration.sidecar_run_store import SidecarOrchestrationRunStore
            runs=OrchestrationRunService(SidecarOrchestrationRunStore(7,tenant_id='3',client=lambda **kwargs:storage))
            task.update(_goalRunId='goal_target-root',_tenant_id='3')
            page=runs.replay(task['_goalRunId'],control['authorization']['target_launch_sequence'])
            metadata=page.events[0]['launch_metadata']
            publication=GoalJournalPublisher(task,runs,metadata,cursor=page.events[0]['seq'])
            # The private fixture already owns these exact packets. Reconcile
            # their ordinary sequence/payload CAS before exercising new writes.
            for event in page.events:publication({key:value for key,value in event.items() if key!='seq'})
            journal=ExecutionJournal(publication,account_handoff_commit=publication.commit_account_handoff)
        handoff=NativeGoalHandoff(principal=principal,task=task,storage=storage,evidence_bytes=control['evidence_bytes'],io_timeout_ms=30000,journal=journal)
        cuts=[]
        def observe(cut):
            cuts.append(deepcopy(cut))
            # Initial local bindings reproduce the fixture's existing prefix;
            # later ordinary cuts must use the same actual publication owner.
            if publication is not None and cut['version']>publication.account_version:
                journal.publish_account_frontier(cut,launch_id=metadata['launch_id'])
        budget.bind_account_frontier_observer(observe,
            actor_rebind_observer=handoff.commit_frontiers,actor_rebind_bytes=control['evidence_bytes'])
        bindings=[];restored=[]
        for actor in control['actors']:
            target=child(task,actor['task_id']);target.update(_transientRuntime=True,_invocationStorageFree=False,
                convId='conversation',_archiveScope='invocation:7:'+target['id'],abort_event=AbortSignal())
            model=NativeModelAccountPolicy(principal=principal,task=target,executor=executor)
            fees=NativeTaskCostPolicy(principal=principal,task=target,policy=model)
            fees.bind_pricing_route(dict(owner_user_id=7,tenant_id='3',logical_model='alpha',provider_id='provider-a'))
            async def restore(request,state,model=model,fees=fees):
                commit=fees.prepare_fee_checkpoint_restore(request,state)
                await model.restore_checkpoint(request,state['shared_model_account'],state['shared_model_account']['counter_policy'])
                commit();restored.append(deepcopy(state))
            policy=NativeCheckpointPolicy(principal=principal,task=target,storage=storage,executor=executor,
                epoch=actor['epoch'],binding=actor['binding'],token=actor['token'],header_bytes=control['header_bytes'],
                snapshot_bytes=control['snapshot_bytes'],identity_bytes=control['identity_bytes'],io_timeout_ms=30000,
                restore=restore,registered_child=True,tool_recovery=UnresolvedTools())
            bindings.append((policy,actor['source_task_id']))
        before=budget.consumption();ordinary=len(cuts)
        authorization={key:value for key,value in control['authorization'].items()
            if not journal_bound or key not in {'target_launch_sequence','target_account_sequence','target_cursor'}}
        async def transfer():
            if not asynchronous:
                await asyncio.get_running_loop().run_in_executor(executor,partial(handoff.consume,bindings,authorization=authorization))
                return
            from tests.tasks_pkg.test_native_prepared import prepared
            async with handoff.prepare_recipients([(policy.task, source) for policy,source in bindings],
                    authorization=authorization,executor=executor) as prepare:
                async def recipient(policy):
                    request=prepared();request.task=policy.task
                    async with prepare(request) as ready:
                        await ready(policy)
                await asyncio.gather(*(recipient(policy) for policy,_ in bindings))
            assert task['abort_event']._observers is None
            assert all(policy.task['abort_event']._observers is None for policy,_ in bindings)
        if journal_bound:
            assert cuts[-1]==page.events[-1]['frontier']
            assert publication.cursor==control['authorization']['target_cursor']
        if fault:
            from lib.storage.errors import StorageError
            from tofu_db.embedded import EmbeddedTransportError
            native=storage.native_checkpoint_control;entered=[]
            def damaged(operation,payload,**kwargs):
                if operation=='goal_handoff':
                    entered.append(operation)
                    if fault=='invalid_cut':
                        payload=deepcopy(payload)
                        # Full account validation remains at native admission;
                        # the Python publication owner checks scope/sequence.
                        payload['goal_handoff']['authorization']['account_frontiers'][0]['snapshot']['tokens']+=1
                    response=native(operation,payload,**kwargs)
                    if fault=='lost_ack':raise OSError('lost acknowledged native handoff response')
                    return response
                return native(operation,payload,**kwargs)
            monkeypatch.setattr(storage,'native_checkpoint_control',damaged)
            async def refused():
                with pytest.raises((ValueError,StorageError,EmbeddedTransportError,OSError)):
                    await transfer()
                for policy,_ in bindings:await policy.close()
            asyncio.run(refused())
            cursor=control['authorization']['target_cursor']
            assert entered==['goal_handoff'] and publication.cursor==cursor
            assert journal.abort_requested() and budget.reason()=='execution_account_frontier_unavailable'
            assert all(policy.capability is None for policy,_ in bindings)
            page=runs.replay(task['_goalRunId'],cursor)
            assert page.next_cursor==cursor+(len(bindings)+1 if fault=='lost_ack' else 0)
            assert budget.consumption()==before
            handoff.close();storage.close()
            return
        async def run():
            await transfer()
            assert len(cuts)==ordinary and budget.consumption()==before
            for policy,source in bindings:
                request=CheckpointRequest(principal,policy.task_id,'load',0,policy.epoch,policy.binding,threading.Event())
                state=await policy.load(request)
                assert state['tools_pending'] is True and state['host_fee_projection']['rounds']==original['states'][source]['host_fee_projection']['rounds']
                assert state['shared_model_account']['actor_task_id']==policy.task_id
                assert policy.task['apiRounds'][0]['billableTaskId']=='source-root'
                with pytest.raises(ValueError,match='ineligible'):
                    await policy.begin(replace(request, action="begin", round=1))
                await policy.close()
        asyncio.run(run())
        assert len(restored)==2 and budget.consumption()==before
        if journal_bound:
            cursor=control['authorization']['target_cursor']
            assert publication.cursor==cursor+len(bindings)+1
            budget.close(bindings[0][0].task)
            assert journal.call('tool','read-only-result',0,{},lambda:'observed')=='observed'
            following=runs.replay(task['_goalRunId'],cursor)
            assert following.next_cursor==publication.cursor==cursor+len(bindings)+4
            assert [row['seq'] for row in following.events]==list(range(cursor,publication.cursor))
            assert [row['type'] for row in following.events]==[
                'execution_account_frontier','execution_account_frontier','execution_recovery_consumed',
                'execution_account_frontier','execution_effect_started','execution_effect_completed']
            assert following.events[-3]['frontier']['reason']=='detached'
        handoff.close();storage.close()
