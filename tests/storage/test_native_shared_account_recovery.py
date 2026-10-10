"""Installed Rust and selected real embedded journal preserve spent tree credit."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import subprocess
import tarfile
import threading
import time

import pytest
from lib.agent_core.execution_budget import ExecutionBudget, ModelBudgetExceeded
from lib.agent_core.native_process import NativeProcessClient, Budget, NativeProcessError
from lib.identity import PrincipalContext
from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
from lib.tasks_pkg.native_model_account import NativeModelAccountPolicy
from tofu_db.embedded import EmbeddedRuntime

pytestmark=pytest.mark.api

@pytest.mark.parametrize('exhausted',[False,True])
def test_installed_shared_budget_checkpoint_resumes_without_tool_replay_or_credit_reset(tmp_path,exhausted):
    archive=os.environ.get('TOFU_NATIVE_SHARED_CHECKPOINT_TEST_ARCHIVE');library=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_LIBRARY');binary=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_BINARY');expected=os.environ.get('TOFU_CHECKPOINT_CABI_TEST_SOURCE_SHA256')
    if not all((archive,library,binary,expected)):pytest.skip('explicit verified native archive and checkpoint library/binary/source required')
    from scripts.build_native_runtime import verify
    manifest=verify(Path(archive),run=True);installed=tmp_path/'native';installed.mkdir()
    with tarfile.open(archive) as source:source.extractall(installed,filter='data')
    executable=installed/'bin/tofu-native';executable.chmod(0o755)
    directory=tmp_path/'authority';directory.mkdir();subprocess.run([binary,'init','--data-dir',str(directory)],check=True,capture_output=True)
    owner=PrincipalContext.user(subject_id='shared-checkpoint-e2e',owner_user_id=7)
    seen=[];tools=[];snapshots=[];cost=999 if exhausted else 5
    original_deadline=time.time()+300.125
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));seen.append(body)
            delta=({'tool_calls':[{'index':0,'id':'call','type':'function','function':{'name':'lookup','arguments':'{}'}}]} if len(seen)==1 else {'content':'done'})
            finish='tool_calls' if len(seen)==1 else 'stop'
            wire=('data: '+json.dumps({'choices':[{'delta':delta,'finish_reason':finish}], 'usage':{'prompt_tokens':cost-2,'completion_tokens':2}})+'\n\ndata: [DONE]\n\n').encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(wire)));self.end_headers();self.wfile.write(wire)
    server=HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
    settings=dict(version='tofu.native-agent-stdio/v1',type='configure',owner_user_id=7,task_id='root',epoch='epoch',endpoint=f'http://127.0.0.1:{server.server_port}/chat',headers={},template={'model':'fixture','max_tokens':100},messages=[{'role':'user','content':'question'}],
        tools={'lookup':{'name':'lookup','permission':'read','contractVersion':'tofu.tool-contract/v2','arguments_schema':{'type':'object'}}},event_bytes=65536,output_bytes=65536,tool_calls=8,request_bytes=65536,rounds=3,retained_bytes=1048576,feedback_bytes=4096,tool_frame_bytes=65536,deadline_ms=5000,
        policy=dict(connections=1,idle_connections=1,keepalive_ms=1000,connect_ms=1000,request_headers_ms=3000,pool_wait_ms=1000,stream_idle_ms=1000,drain_timeout_ms=100,drain_bytes=65536,drain_chunks=16),
        checkpoint_shared_account=True,model_control={'ack_ms':3000},model_counter=dict(encoding='cl100k_base',output_field='max_tokens',input_bytes=1048576,text_bytes=1048576,tool_json_bytes=1048576,depth=128,worker_stack_bytes=1048576),
        terminal_persistence=dict(operation_id='commit',attempts=2,ack_ms=3000,frame_bytes=65536))
    class RoundPolicy:
        async def reserve(self,request):return True
        async def settle(self,result):pass
    class Terminal:
        async def persist(self,request):return 'accepted'
    async def restore(request,state):snapshots.append(state['shared_model_account'])
    async def lookup(request):
        assert account.reserve_tool(task) is None
        tools.append(request.name)
        return 'stored tool result'
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            for run in [0,1]:
                task=dict(id='root',_userId=7,status='running',abort_event=threading.Event(),config={'executionLimits':dict(max_tokens=1000,max_tool_calls=3,deadline=original_deadline)})
                account=ExecutionBudget(task);task['_executionBudget']=account
                with EmbeddedRuntime(Path(library),directory,owner_id=7,tenant_id=3,secret=b'x'*32) as native:
                    assert native.build_identity()['source_sha256']==expected
                    storage=EmbeddedStorageV2Client(native,b'x'*32,owner_id=7,tenant_id=3)
                    checkpoint=NativeCheckpointPolicy(principal=owner,task=task,storage=storage,executor=executor,epoch='epoch',binding='fixed-route-tools-account',token=f'worker-{run}',header_bytes=4096,snapshot_bytes=1048576,identity_bytes=1024,io_timeout_ms=30000,restore=restore,suspend=run==0)
                    model=NativeModelAccountPolicy(principal=owner,task=task,executor=executor)
                    async def execute():
                        client=NativeProcessClient(binary=executable,sha256=manifest['files']['bin/tofu-native']['sha256'],protocol_version=manifest['build']['stdio_contract'],principal=owner,budget=Budget(1,16*1024*1024,65536,65536,5))
                        accepted=False
                        try:
                            if exhausted and run==1:
                                with pytest.raises(NativeProcessError) as caught:await client.run(settings,callbacks={'lookup':lookup},working_directory=tmp_path,round_policy=RoundPolicy(),model_policy=model,checkpoint_policy=checkpoint,terminal_policy=Terminal())
                                assert isinstance(caught.value.__cause__,ModelBudgetExceeded)
                            else:
                                event=await client.run(settings,callbacks={'lookup':lookup},working_directory=tmp_path,round_policy=RoundPolicy(),model_policy=model,checkpoint_policy=checkpoint,terminal_policy=Terminal())
                                assert event['status']==('aborted' if run==0 else 'done')
                            accepted=True
                        finally:
                            await client.close();await checkpoint.close(terminal_saved=accepted)
                        assert client.active_runs==0 and account.continuity_snapshot()['pending_model_requests']==0
                    asyncio.run(execute());storage.close()
                assert account.consumption_snapshot()==(cost if run==0 else cost*(1 if exhausted else 2),1,True)
    finally:server.shutdown();thread.join();server.server_close()
    assert tools==['lookup'] and len(seen)==(1 if exhausted else 2)
    assert len(snapshots)==1 and snapshots[0]['tokens']==cost and snapshots[0]['model_ordinal']==1
    assert snapshots[0]['limits']['deadline']==original_deadline
    assert snapshots[0]['tool_calls']==1 and snapshots[0]['control_tool_calls']==1
