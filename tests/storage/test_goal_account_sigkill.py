"""A killed Goal writer leaves exact spent authority or unresolved request debt."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.api

WRITER = r'''
import json,signal,sys
from pathlib import Path
from tofu_db.embedded import EmbeddedRuntime
from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
from lib.goal_runs.contract import goal_run_policy
from lib.goal_runs.repository import SidecarGoalRunRepository
from lib.goal_runs.execution_journal import create_goal_execution_journal
from lib.orchestration.chat_launch import OrchestrationChatFlowLaunchSpec
from lib.orchestration.run_service import OrchestrationRunService
from lib.orchestration.sidecar_run_store import SidecarOrchestrationRunStore
library,directory,ready,source,phase=sys.argv[1:]
runtime=EmbeddedRuntime(Path(library),Path(directory),owner_id=7,tenant_id=3,secret=b'x'*32)
assert runtime.build_identity()['source_sha256']==source
client=EmbeddedStorageV2Client(runtime,b'x'*32,owner_id=7,tenant_id=3)
provider=lambda **kwargs:client
task=dict(id='executor',_goalRunId='goal_executor',convId='conversation',_userId=7,
 _turnId='output-turn',_attemptId='attempt',status='running',messages=[{'role':'user','content':'original objective'}],
 config={'_goalObjective':'original objective','executionLimits':{'max_tokens':20,'max_tool_calls':1,'deadline':None}})
if phase=='unbound_actor':
 from tests.agent_core.test_recovered_actor_prefix import source
 from lib.agent_core.execution_budget import _ensure_execution_budget,inherit_execution_budget,close_execution_budget
 frontier=source();task['config']['executionLimits']=frontier['snapshot']['limits']
 _ensure_execution_budget(task).restore_pending_frontier(task,frontier,owner=7)
definition={'schema':'tofu.orchestration/v1','name':'kill accounting',
 'nodes':[{'id':'start','type':'control','kind':'start'},{'id':'stop','type':'control','kind':'stop'}],
 'edges':[{'from':'start','to':'stop'}]}
goals=SidecarGoalRunRepository(7,client=provider)
goals.start('goal_executor',conversation_id='conversation',objective='original objective',definition=definition,policy=goal_run_policy())
runs=OrchestrationRunService(SidecarOrchestrationRunStore(7,client=provider))
launch=OrchestrationChatFlowLaunchSpec(projection='autopilot',initial_phase='worker',initial_context='original objective',
 system_prompt_base='fixture',tools=(),model='fixture',project_path='',thinking_enabled=False,max_iterations=2)
create_goal_execution_journal(task,definition,launch,runs=runs)
account=task['_executionBudget']
if phase=='unbound_actor':
 actor=dict(id='recipient-a',_userId=7,_parentTaskId=task['id'],status='running',config={})
 inherit_execution_budget(task,actor)
 account.rebind_actor_prefix(actor,source_task_id='source-a',owner=7)
 close_execution_budget(actor)
 assert account.consumption()==(36,5) and not account.actuals_complete()
else:
 lease=account.reserve_model(task,input_tokens=2,max_output_tokens=13)
 lease.mark_dispatched()
if phase=='settled':
 lease.settle({'prompt_tokens':2,'completion_tokens':13})
 assert account.reserve_tool(task) is None
Path(ready).write_text(json.dumps({'tokens':15,'tools':1}))
while True:signal.pause()
'''


@pytest.mark.parametrize('phase', ['dispatched', 'settled', 'unbound_actor'])
def test_sigkill_preserves_whole_goal_account_without_any_completed_child_receipt(tmp_path, phase):
    from tofu_db.embedded import EmbeddedRuntime
    from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
    from lib.orchestration.run_service import OrchestrationRunService
    from lib.orchestration.sidecar_run_store import SidecarOrchestrationRunStore
    from lib.goal_runs.execution_journal import read_goal_execution
    from lib.orchestration.execution_journal import RecoveryRefused
    binary=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_BINARY')
    library=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_LIBRARY')
    source=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_SOURCE_SHA256')
    if not all((binary,library,source)) or sys.platform!='linux':
        pytest.skip('explicit private native engine and Linux SIGKILL required')
    directory=tmp_path/'authority';directory.mkdir()
    subprocess.run([binary,'init','--data-dir',str(directory)],check=True,capture_output=True)
    ready=tmp_path/'ready.json';errors=tmp_path/'stderr'
    with errors.open('wb') as stderr:
        child=subprocess.Popen([sys.executable,'-c',WRITER,library,str(directory),str(ready),source,phase],
            stdout=subprocess.DEVNULL,stderr=stderr)
        try:
            expires=time.monotonic()+30
            while not ready.exists():
                assert child.poll() is None,errors.read_text()
                assert time.monotonic()<expires,errors.read_text()
                time.sleep(.02)
            child.kill();assert child.wait(10)==-signal.SIGKILL
        finally:
            if child.poll() is None:child.kill();child.wait(10)
    with EmbeddedRuntime(Path(library),directory,owner_id=7,tenant_id=3,secret=b'x'*32) as runtime:
        assert runtime.build_identity()['source_sha256']==source
        client=EmbeddedStorageV2Client(runtime,b'x'*32,owner_id=7,tenant_id=3)
        try:
            runs=OrchestrationRunService(SidecarOrchestrationRunStore(7,client=lambda **kwargs:client))
            run=runs.get_execution_snapshot('goal_executor');events=[];cursor=0
            while True:
                page=runs.replay('goal_executor',cursor);events.extend(page.events)
                if page.caught_up:break
                assert page.next_cursor>cursor;cursor=page.next_cursor
            if phase=='unbound_actor':
                from lib.goal_runs.execution_journal import inspect_goal_execution
                evidence=inspect_goal_execution(events,run=run,owner_user_id=7)
                cut=evidence.account_frontier
                assert cut['snapshot']['tokens']==36 and cut['snapshot']['tool_calls']==5
                assert set(cut['actors'])=={'executor'} and not cut['snapshot']['complete']
                assert set(cut['recovery']['pending_actors'])=={'source-b'}
                assert cut['closed_tokens']==23
                with pytest.raises(RecoveryRefused,match='complete settled frontier'):
                    read_goal_execution(events,run=run,owner_user_id=7,include_account=True)
            elif phase=='dispatched':
                with pytest.raises(RecoveryRefused,match='complete settled frontier'):
                    read_goal_execution(events,run=run,owner_user_id=7,include_account=True)
            else:
                _,records,account=read_goal_execution(events,run=run,owner_user_id=7,include_account=True)
                assert records=={} and (account['tokens'],account['tool_calls'])==(15,1)
                assert account['limits']==dict(max_tokens=20,max_tool_calls=1,deadline=None)
        finally:
            client.close()
