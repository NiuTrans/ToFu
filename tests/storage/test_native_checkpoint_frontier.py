"""SIGKILL leaves a readable paid actor frontier, never a read-created lease."""
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.api


def actor_snapshot():
    from lib.agent_core.execution_budget import _ensure_execution_budget, inherit_execution_budget
    from tests.tasks_pkg.test_native_cost_projection import setup
    task, _, policy, result = setup()
    task['config'] = {}
    root = dict(id='flow-root', _userId=7, config={}, status='running')
    account = _ensure_execution_budget(root); inherit_execution_budget(root, task)
    lease = account.reserve_model(task, input_tokens=10000, max_output_tokens=200)
    lease.mark_dispatched(); lease.settle(result.evidence['usage'])
    asyncio.run(policy.settle(result))
    state = dict(format='tofu.native-agent-checkpoint/v1', owner_user_id=7,
        task_id='task', epoch='actor-epoch', binding='actor-policy', next_round=1, tool_calls=1,
        messages=[{'role':'user','content':'Write result'}, {'role':'assistant','tool_calls':[
            {'id':'write-1','type':'function','function':{'name':'write_file','arguments':'{}'}}]}],
        usage=[result.evidence['usage']], round_limit=3, tool_limit=3,
        no_progress_limit=0, failure_limit=0, tools_pending=True,
        progress=dict(codec='sha256-token-sequence/v1', last_calls=None, last_world='',
            last_evidence=None, last_failure=None, no_progress_streak=0, failure_streak=0),
        shared_model_account=account.capture_actor_account(owner=7,task=task,
            counter_policy=dict(encoding='cl100k_base',output_field='max_tokens',wire_model='alpha-wire')))
    state['host_fee_projection'] = policy.capture_fee_checkpoint(result.request,state)
    return state


WRITER = r'''
import json,signal,sys,time
from pathlib import Path
from types import SimpleNamespace
from tofu_db.embedded import EmbeddedRuntime
from tofu_db.generated_checkpoint_control import FORMAT,RESPONSE_HEADROOM_BYTES
from lib.storage import service
from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
from lib.turn_lifecycle import create_turn_pair,claim_attempt_start,bind_task,mark_task_started
library,directory,ready,source,phase,fixture=sys.argv[1:]
runtime=EmbeddedRuntime(Path(library),Path(directory),owner_id=7,tenant_id=3,secret=b'x'*32)
assert runtime.build_identity()['source_sha256']==source
client=EmbeddedStorageV2Client(runtime,b'x'*32,owner_id=7,tenant_id=3)
service.storage_runtime=lambda:SimpleNamespace(client=lambda **kwargs:client)
pair=create_turn_pair('conversation',command_id='frontier-birth',user_id=7,
 input_projection={'content':'Inspect an interrupted child'},config={},
 conversation_defaults={'allowCreate':True,'title':'Frontier test','settings':{}})
attempt=pair['attempt']['attemptId']
assert claim_attempt_start(attempt,user_id=7)
assert bind_task(attempt,'flow-root',user_id=7)['taskId']=='flow-root'
assert mark_task_started(attempt,'flow-root',user_id=7)['status']=='running'
client.command('invocation.register_child',dict(user_id=7,conversation_id='conversation',
 parent_task_id='flow-root',child_task_id='task'),'frontier-register-child')
def call(op,payload):
 return runtime.checkpoint_control(op,dict(format=FORMAT,**payload),deadline_unix_ms=time.time_ns()//1000000+10000,response_bytes=1048576+4096+RESPONSE_HEADROOM_BYTES)
p=dict(owner_user_id=7,task_id='task',epoch='actor-epoch',binding='actor-policy',token='writer',header_bytes=4096,snapshot_bytes=1048576,identity_bytes=1024)
reply=call('create',{'parameters':p});assert reply['accepted'];cap=reply['capability_id']
assert call('load',{'capability_id':cap})['accepted']
assert call('begin',{'capability_id':cap,'round':0})['accepted']
state=json.loads(Path(fixture).read_text())
if phase!='pending':
 state.pop('tools_pending');state['messages'].append({'role':'tool','tool_call_id':'write-1','content':'Published once'})
assert call('pending' if phase=='pending' else 'complete',{'capability_id':cap,'state':state})['accepted']
if phase=='inflight':assert call('begin',{'capability_id':cap,'round':1})['accepted']
query={k:p[k] for k in ('owner_user_id','task_id','header_bytes','snapshot_bytes','identity_bytes')}
assert call('frontier',{'frontier':query})['accepted'] is False
Path(ready).write_text(json.dumps(state))
while True:signal.pause()
'''


@pytest.mark.parametrize('phase', ['pending','completed','inflight'])
def test_dead_writer_frontier_keeps_original_quotes_and_lease_generation(tmp_path, phase):
    from tofu_db.embedded import EmbeddedRuntime, EmbeddedTransportError
    from tofu_db.generated_checkpoint_control import FORMAT, RESPONSE_HEADROOM_BYTES
    binary=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_BINARY')
    library=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_LIBRARY')
    source=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_SOURCE_SHA256')
    if not all((binary,library,source)) or sys.platform!='linux':
        pytest.skip('explicit private native engine and Linux death-proof required')
    directory=tmp_path/'authority';directory.mkdir()
    subprocess.run([binary,'init','--data-dir',str(directory)],check=True,capture_output=True)
    fixture=tmp_path/'snapshot.json';fixture.write_text(json.dumps(actor_snapshot()))
    ready=tmp_path/'ready.json';errors=tmp_path/'writer.stderr'
    with errors.open('wb') as stderr:
        child=subprocess.Popen([sys.executable,'-c',WRITER,library,str(directory),str(ready),source,phase,str(fixture)],stderr=stderr,stdout=subprocess.DEVNULL)
        try:
            expires=time.monotonic()+20
            while not ready.exists():
                assert child.poll() is None,errors.read_text()
                assert time.monotonic()<expires,errors.read_text()
                time.sleep(.02)
            child.kill();assert child.wait(10)==-signal.SIGKILL
        finally:
            if child.poll() is None:child.kill();child.wait(10)
    with EmbeddedRuntime(Path(library),directory,owner_id=7,tenant_id=3,secret=b'x'*32) as runtime:
        assert runtime.build_identity()['source_sha256']==source
        capacity=1048576+4096+RESPONSE_HEADROOM_BYTES
        query=dict(owner_user_id=7,task_id='task',header_bytes=4096,snapshot_bytes=1048576,identity_bytes=1024)
        def call(op,fields,limit=capacity):
            return runtime.checkpoint_control(op,dict(format=FORMAT,**fields),deadline_unix_ms=time.time_ns()//1000000+10000,response_bytes=limit)
        with pytest.raises(EmbeddedTransportError):call('frontier',{'frontier':query},capacity-1)
        observed=call('frontier',{'frontier':query})
        assert observed['capability_id'] is None
        if phase=='inflight':
            assert observed['accepted'] is False and observed['error']=='ineligible'
            return
        assert observed['accepted'] is True,observed
        assert observed['state']['snapshot']==json.loads(ready.read_text())
        assert observed['state']['snapshot']['host_fee_projection']['rounds'][0]['cost']['credits']['total']==7593
        assert observed['state']['snapshot']['host_fee_projection']['rounds'][0]['billable_task_id']=='flow-root'
        assert call('frontier',{'frontier':query})==observed
        assert call('frontier',{'frontier':dict(query,owner_user_id=8)})['accepted'] is False
        from lib.orchestration.invocation_frontier import read_registered_actor_frontier
        from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
        client=EmbeddedStorageV2Client(runtime,b'x'*32,owner_id=7,tenant_id=3)
        try:
            birth=client.query('invocation.resolve_child',dict(user_id=7,archive_scope='invocation:7:task'))
            admission=dict(kind='agent',invocation_id='task',input_hash='a'*64,
                invocation_completion=dict(format='tofu.flow.invocation-completion/v1',
                    nonce='a'*32,identity=birth,account_root_task_id='flow-root'))
            registered=read_registered_actor_frontier(client,admission,owner_user_id=7,
                conversation_id='conversation',parent_task_id='flow-root',header_bytes=4096,
                snapshot_bytes=1048576,identity_bytes=1024,io_timeout_ms=10000)
            assert registered['frontier']==observed['state']
        finally:
            client.close()
        parameters=dict(query,epoch='actor-epoch',binding='actor-policy',token='new-writer')
        recovered=call('recover',{'parameters':parameters})
        assert recovered['accepted'] and recovered['capability_id']==1,'read allocated an execution capability'
        assert call('load',{'capability_id':recovered['capability_id']})['state']==observed['state']['snapshot']
        assert call('frontier',{'frontier':query})['accepted'] is False,'new live writer was not fenced'
