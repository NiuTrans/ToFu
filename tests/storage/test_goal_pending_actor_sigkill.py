"""Killed Goal and native actor retain one paid pending seed, never a result."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from tests.storage.test_native_checkpoint_frontier import actor_snapshot

pytestmark = pytest.mark.api

WRITER = r'''
import json,signal,sys,time
from pathlib import Path
from types import SimpleNamespace
from tofu_db.embedded import EmbeddedRuntime
from tofu_db.generated_checkpoint_control import FORMAT, RESPONSE_HEADROOM_BYTES
from lib.storage import service
from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
from lib.turn_lifecycle import create_turn_pair,claim_attempt_start,bind_task,mark_task_started
from lib.goal_runs.contract import goal_run_policy
from lib.goal_runs.repository import SidecarGoalRunRepository
from lib.goal_runs.execution_journal import create_goal_execution_journal
from lib.orchestration.chat_launch import OrchestrationChatFlowLaunchSpec
from lib.orchestration.run_service import OrchestrationRunService
from lib.orchestration.sidecar_run_store import SidecarOrchestrationRunStore
from lib.orchestration.invocation_recovery import RegisteredInvocationCompletion
from lib.agent_core.execution_budget import inherit_execution_budget
library,directory,ready,source,phase,fixture=sys.argv[1:]
runtime=EmbeddedRuntime(Path(library),Path(directory),owner_id=7,tenant_id=3,secret=b'x'*32)
assert runtime.build_identity()['source_sha256']==source
client=EmbeddedStorageV2Client(runtime,b'x'*32,owner_id=7,tenant_id=3)
provider=lambda **kwargs:client
service.storage_runtime=lambda:SimpleNamespace(client=provider)
pair=create_turn_pair('conversation',command_id='goal-pending-birth',user_id=7,
 input_projection={'content':'Preserve pending paid work'},config={},
 conversation_defaults={'allowCreate':True,'title':'Pending Goal','settings':{}})
attempt=pair['attempt']['attemptId']
assert claim_attempt_start(attempt,user_id=7)
assert bind_task(attempt,'flow-root',user_id=7)['taskId']=='flow-root'
assert mark_task_started(attempt,'flow-root',user_id=7)['status']=='running'
task=dict(id='flow-root',_goalRunId='goal_flow-root',convId='conversation',_userId=7,_tenant_id=3,
 _turnId=pair['turn']['turnId'],_attemptId=attempt,status='running',
 messages=[{'role':'user','content':'Preserve pending paid work'}],config={'_goalObjective':'Preserve pending paid work'})
if phase=='claim':
 from lib.billing import users,deposit,reserve
 for index in range(2,8):wallet=users.create_user(f'goal-claim-{index}@example.test')
 assert wallet.owner_user_id==7
 deposit(wallet.id,200000,ref_id='goal-claim-seed');reserve(wallet.id,100000,ref_id=task['id'])
 task.update(_tenant_id='',_billingUserId=wallet.id,_billing_reservation_micro=100000,_nativePricingRequired=True)
 task['messages'].append({'role':'system','content':'owned-prefix '*60000})
definition={'schema':'tofu.orchestration/v1','name':'Pending Goal','nodes':[
 {'id':'start','type':'control','kind':'start'}, {'id':'worker','type':'role','role':'general'},
 {'id':'stop','type':'control','kind':'stop'}], 'edges':[{'from':'start','to':'worker'},{'from':'worker','to':'stop'}]}
goals=SidecarGoalRunRepository(7,client=provider)
goals.start(task['_goalRunId'],conversation_id='conversation',objective='Preserve pending paid work',definition=definition,policy=goal_run_policy())
runs=OrchestrationRunService(SidecarOrchestrationRunStore(7,client=provider))
launch=OrchestrationChatFlowLaunchSpec(projection='autopilot',initial_phase='worker',initial_context='Preserve pending paid work',
 system_prompt_base='fixture',tools=(),model='fixture',project_path='',thinking_enabled=False,max_iterations=2)
journal=create_goal_execution_journal(task,definition,launch,runs=runs)
birth=client.command('invocation.register_child',dict(user_id=7,conversation_id='conversation',
 parent_task_id='flow-root',child_task_id='task'),'goal-pending-child')
child=dict(id='task',_userId=7,convId='conversation',_parentTaskId='flow-root',_archiveScope=birth['archive_scope'],status='running',config={})
inherit_execution_budget(task,child)
state=json.loads(Path(fixture).read_text())
def call(op,payload):
 return runtime.checkpoint_control(op,dict(format=FORMAT,**payload),deadline_unix_ms=time.time_ns()//1000000+10000,response_bytes=1048576+4096+RESPONSE_HEADROOM_BYTES)
def operation(admit):
 global state
 admit(invocation_id='task',completion=RegisteredInvocationCompletion(child))
 account=task['_executionBudget'];lease=account.reserve_model(child,input_tokens=10000,max_output_tokens=200)
 if phase=='claim':
  from lib.billing.native_admission import protect_before_dispatch
  protect_before_dispatch(child,budget=account,owner=7)
 lease.mark_dispatched();lease.settle(state['usage'][0])
 parameters=dict(owner_user_id=7,task_id='task',epoch='actor-epoch',binding='actor-policy',token='writer',header_bytes=4096,snapshot_bytes=1048576,identity_bytes=1024)
 reply=call('create',{'parameters':parameters});assert reply['accepted'];cap=reply['capability_id']
 assert call('load',{'capability_id':cap})['accepted']
 assert call('begin',{'capability_id':cap,'round':0})['accepted']
 if phase.startswith('file_'):
  from tests.support.goal_actor_file_round import GoalActorFileRound
  file_round=GoalActorFileRound(child,state,directory,ready,task['_goalRunId'],phase)
  state=file_round.state
 if phase=='completed':
  state.pop('tools_pending');state['messages'].append({'role':'tool','tool_call_id':'write-1','content':'Published once'})
 assert call('complete' if phase=='completed' else 'pending',{'capability_id':cap,'state':state})['accepted']
 if phase.startswith('file_'):file_round.execute_until_interruption()
 if phase=='unsettled':
  request=account.reserve_model(task,input_tokens=0,max_output_tokens=1);request.mark_dispatched()
 Path(ready).write_text(json.dumps({'run_id':task['_goalRunId'],**({'wallet_id':wallet.id} if phase=='claim' else {})}))
 while True:signal.pause()
if phase=='claim':
 from lib.orchestration.engine import FlowExecutor
 from lib.orchestration.agent_runner import OrchestrationInvocationRunner
 # Substitute provider/checkpoint work, retaining the actual graph-to-journal
 # input construction used by the application and recovery inspection.
 OrchestrationInvocationRunner.__call__=lambda self,node,context,iteration,before_dispatch=None:operation(before_dispatch)
 FlowExecutor(definition,execution_journal=journal,worker_context_policy='conversation',
  parent_task=task,model='fixture').run(initial_context=launch.initial_context)
else:
 journal.call('agent','worker',0,[definition['nodes'][1],launch.initial_context],operation,prepare_before_admission=True)
'''


@pytest.mark.parametrize('phase', ['pending', 'completed', 'unsettled', 'file_published', 'file_not_published', 'file_unknown'])
def test_sigkill_loads_pending_actor_evidence_with_original_fees_and_no_execution_authority(tmp_path, phase, monkeypatch):
    from tofu_db.embedded import EmbeddedRuntime
    from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
    from lib.orchestration.run_service import OrchestrationRunService
    from lib.orchestration.sidecar_run_store import SidecarOrchestrationRunStore
    from lib.orchestration.startup_recovery import retire_interrupted_orchestration_runs_detailed
    from lib.goal_runs.recovery import load_goal_recovery_seed
    from lib.orchestration.execution_journal import RecoveryRefused
    binary=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_BINARY')
    library=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_LIBRARY')
    source=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_SOURCE_SHA256')
    if not all((binary,library,source)) or sys.platform!='linux':
        pytest.skip('explicit private native engine and Linux SIGKILL required')
    directory=tmp_path/'authority';directory.mkdir()
    subprocess.run([binary,'init','--data-dir',str(directory)],check=True,capture_output=True)
    state = actor_snapshot(); state['host_fee_projection']['tenant_id'] = '3'
    fixture=tmp_path/'snapshot.json';fixture.write_text(json.dumps(state))
    ready=tmp_path/'ready.json';errors=tmp_path/'writer.stderr'
    with errors.open('wb') as stderr:
        writer=subprocess.Popen([sys.executable,'-c',WRITER,library,str(directory),str(ready),source,phase,str(fixture)],
            stdout=subprocess.DEVNULL,stderr=stderr)
        try:
            expires=time.monotonic()+30
            while not ready.exists():
                assert writer.poll() is None,errors.read_text()
                assert time.monotonic()<expires,errors.read_text()
                time.sleep(.02)
            writer.kill();assert writer.wait(10)==-signal.SIGKILL
        finally:
            if writer.poll() is None:writer.kill();writer.wait(10)
    with EmbeddedRuntime(Path(library),directory,owner_id=7,tenant_id=3,secret=b'x'*32) as runtime:
        assert runtime.build_identity()['source_sha256']==source
        client=EmbeddedStorageV2Client(runtime,b'x'*32,owner_id=7,tenant_id=3)
        try:
            provider=lambda **kwargs:client
            retired=retire_interrupted_orchestration_runs_detailed(error={
                'kind':'worker_lost','source':'orchestration.startup_recovery'},client=provider)
            assert retired.retired==1
            runs=OrchestrationRunService(SidecarOrchestrationRunStore(7,client=provider))
            monkeypatch.setattr('lib.project_mod.modifications.SESSIONS_DIR', str(tmp_path/'file-journal'))
            bounds=dict(header_bytes=4096,snapshot_bytes=1048576,identity_bytes=1024,io_timeout_ms=10000)
            def inspect():
                return load_goal_recovery_seed(runs,'goal_flow-root',owner_user_id=7,tenant_id=3,
                    storage=client,pending_actor_bounds=bounds)
            if phase=='unsettled':
                with pytest.raises(RecoveryRefused,match='settled direct actor set'):inspect()
                return
            if phase=='file_unknown':
                with pytest.raises(RecoveryRefused,match='remains unresolved'):inspect()
                assert not (tmp_path/'project'/'result.txt').exists()
                return
            with pytest.raises(RecoveryRefused,match='external effects are unknown'):
                load_goal_recovery_seed(runs,'goal_flow-root',owner_user_id=7,tenant_id=3,storage=client)
            seed=inspect();again=inspect()
            assert seed==again and seed.records=={} and seed.account_snapshot is None
            pending=seed.pending_actors;assert pending is not None
            assert pending.account_frontier['snapshot']['tokens']==10200
            assert len(pending.actors)==1
            actor=next(iter(pending.actors.values()))
            from lib.goal_runs.execution_journal import GoalExecutionEvidence
            from lib.goal_runs.pending_fee_evidence import preflight_pending_fees
            from lib.orchestration.execution_journal import JournalRecovery
            financial = preflight_pending_fees(GoalExecutionEvidence(seed.metadata,
                JournalRecovery(seed.records, {key: row['admission'] for key, row in pending.actors.items()}),
                pending.account_frontier), pending, storage=client,
                wallet_user_id='original-wallet', reserved_micro=100000)
            assert financial.invoice['componentsMicro']['totalMicro'] == 7593
            assert financial.invoice['roundCount'] == 1
            assert financial.actors[next(iter(pending.actors))]['checkpoint_version'] == actor['frontier']['version']
            if phase.startswith('file_'):
                proof = actor['pending_file_tools']
                assert proof['charged_tool_calls'] == 1
                assert proof['observations'][0]['result']['outcome'] == phase
                assert proof['observations'][0]['result']['operationId'] == json.loads(ready.read_text())['operation_id']
                assert actor['frontier']['snapshot']['shared_model_account']['tool_calls'] == 0
                assert pending.account_frontier['snapshot']['tool_calls'] == 1
                output = tmp_path / 'project' / 'result.txt'
                assert (output.read_text() if output.exists() else None) == ('once\n' if phase == 'file_published' else None)
                from lib.agent_core.execution_budget import _ensure_execution_budget, inherit_execution_budget
                successor = dict(id='new-root', _userId=7, status='running', config={})
                budget = _ensure_execution_budget(successor)
                budget.restore_pending_frontier(successor, pending.account_frontier, owner=7)
                target = dict(id='new-actor', _userId=7, _parentTaskId='new-root', status='running', config={})
                inherit_execution_budget(successor, target)
                budget.rebind_actor_prefix(target, source_task_id='task', owner=7)
                assert budget.consumption_snapshot()[:2] == (10200, 1)
                assert budget.tool_calls_for_control(target) == 1
                # Domain/account integration after a proven prefix binding. The
                # ordinary Goal gate below still refuses automatic takeover;
                # this read-only probe creates no native lease or worker turn.
                import asyncio
                from concurrent.futures import ThreadPoolExecutor
                from copy import deepcopy
                from dataclasses import replace
                from types import SimpleNamespace
                import threading
                from lib.identity import PrincipalContext
                from lib.agent_core.native_model_control import ModelControl
                from lib.agent_core.native_process import CheckpointRequest
                from lib.tasks_pkg.native_model_account import NativeModelAccountPolicy
                from lib.tasks_pkg.native_file_recovery import NativeFileToolRecovery
                principal=PrincipalContext.user(subject_id='goal-file-inspection',owner_user_id=7,tenant_id='3')
                target.update(convId=seed.metadata['conversation_id'],_transientRuntime=True,
                    _invocationStorageFree=False,_archiveScope='invocation:7:new-actor',abort_event=threading.Event())
                restored=deepcopy(actor['frontier']['snapshot'])
                restored['task_id']=target['id']
                restored['shared_model_account'].update(root_task_id=successor['id'],actor_task_id=target['id'])
                before=budget.consumption_snapshot()
                with ThreadPoolExecutor(max_workers=1) as executor:
                    policy=NativeModelAccountPolicy(principal=principal,task=target,executor=executor)
                    model=ModelControl(None,policy,target['id'])
                    model.counter_policy=restored['shared_model_account']['counter_policy']
                    recovery=NativeFileToolRecovery(principal=principal,task=target,executor=executor,
                        registered_child=True,storage=client)
                    recovery.bind_gateway(SimpleNamespace(task=target,executor=executor,owner=7,
                        permissions={'write_file':'write'},_closed=False,_progress=None))
                    request=CheckpointRequest(principal,target['id'],'load',0,restored['epoch'],restored['binding'],threading.Event())
                    async def reconcile():
                        await model.restore_checkpoint(request,restored)
                        return await recovery.reconcile(replace(request,action='reconcile',state=restored))
                    observations=asyncio.run(reconcile())
                    assert json.loads(observations[0]['content'])==proof['observations'][0]['result']
                    assert observations[0]['call_id']==restored['messages'][-1]['tool_calls'][0]['id']
                assert budget.consumption_snapshot()==before and budget.reason() is None
                assert budget.capture_actor_account(owner=7,task=target,
                    counter_policy=restored['shared_model_account']['counter_policy'])['complete'] is True
                assert not budget.actuals_complete()  # A live child cannot certify terminal tree settlement.
                assert (output.read_text() if output.exists() else None)==('once\n' if phase=='file_published' else None)
            assert actor['frontier']['phase']==('completed' if phase=='completed' else 'tools_pending')
            assert actor['registration_version']>=1
            assert actor['frontier']['snapshot']['host_fee_projection']['rounds'][0]['cost']['credits']['total']==7593
            assert actor['frontier']['snapshot']['host_fee_projection']['rounds'][0]['billable_task_id']=='flow-root'
            assert actor['admission']['invocation_completion']['identity']['parent_task_id']=='flow-root'
            assert 'result' not in actor['admission']
            with pytest.raises(RecoveryRefused,match='atomic takeover'):seed.require_settled()
            run=runs.get_execution_snapshot('goal_flow-root')
            assert run['status']=='error'
            assert client.query('record.get',dict(namespace='task_results',key='task',user_id=7))['version']==actor['registration_version']
        finally:client.close()
