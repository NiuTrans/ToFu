"""Verified installed native process uses the actual shared Python tree account."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import tarfile
import threading

import pytest
from lib.agent_core.execution_budget import ExecutionBudget, ModelBudgetExceeded
from lib.agent_core.native_process import NativeProcessClient, Budget, NativeProcessError
from lib.tasks_pkg.native_model_account import NativeModelAccountPolicy
from tests.agent_core.test_native_task_terminal import principal

pytestmark=pytest.mark.api

@pytest.mark.parametrize('cap',[1000,0])
def test_installed_model_control_respects_parent_budget_and_charges_once(tmp_path,cap):
    archive=os.environ.get('TOFU_NATIVE_MODEL_CONTROL_TEST_ARCHIVE')
    if not archive:pytest.skip('explicit verified model-control native archive required')
    from scripts.build_native_runtime import verify
    manifest=verify(Path(archive),run=True)
    installed=tmp_path/'native';installed.mkdir()
    with tarfile.open(archive) as source:source.extractall(installed,filter='data')
    binary=installed/'bin/tofu-native';binary.chmod(0o755)
    root={'id':'parent','_userId':7,'config':{'executionLimits':dict(max_tokens=cap,max_tool_calls=None,deadline=None)}}
    account=ExecutionBudget(root)
    task={'id':'sdk-task','_userId':7,'config':{},'abort_event':threading.Event(),'_executionBudget':account}
    account.attach(task)
    seen=[];grants=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])));seen.append(body)
            assert body['max_tokens']==grants[0] and body['max_tokens']<1000
            wire=('data: '+json.dumps({'choices':[{'delta':{'content':'done'},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':3,'completion_tokens':2}})+'\n\ndata: [DONE]\n\n').encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(wire)));self.end_headers();self.wfile.write(wire)
    server=HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
    config=dict(version='tofu.native-agent-stdio/v1',type='configure',owner_user_id=7,task_id=task['id'],epoch='epoch',endpoint=f'http://127.0.0.1:{server.server_port}/chat',headers={},template={'model':'fixture','max_tokens':1000},
        messages=[{'role':'user','content':'hello'}],tools={},event_bytes=65536,output_bytes=65536,tool_calls=8,request_bytes=65536,rounds=3,retained_bytes=65536,feedback_bytes=4096,tool_frame_bytes=65536,deadline_ms=3000,
        policy=dict(connections=1,idle_connections=1,keepalive_ms=1000,connect_ms=1000,request_headers_ms=2000,pool_wait_ms=1000,stream_idle_ms=1000,drain_timeout_ms=100,drain_bytes=65536,drain_chunks=16),
        model_control={'ack_ms':2000},model_counter=dict(encoding='cl100k_base',output_field='max_tokens',input_bytes=65536,text_bytes=65536,tool_json_bytes=65536,depth=128,worker_stack_bytes=1048576))
    async def run(executor):
        policy=NativeModelAccountPolicy(principal=principal(),task=task,executor=executor)
        original=policy.reserve
        async def reserve(request):
            value=await original(request);grants.append(value);return value
        policy.reserve=reserve
        client=NativeProcessClient(binary=binary,sha256=manifest['files']['bin/tofu-native']['sha256'],protocol_version=manifest['build']['stdio_contract'],principal=principal(),budget=Budget(1,16*1024*1024,65536,65536,5))
        try:
            if cap:
                event=await client.run(config,callbacks={},working_directory=tmp_path,model_policy=policy)
                assert event['status']=='done'
            else:
                with pytest.raises(NativeProcessError) as caught:await client.run(config,callbacks={},working_directory=tmp_path,model_policy=policy)
                assert isinstance(caught.value.__cause__,ModelBudgetExceeded)
            assert client.active_runs==0 and account.continuity_snapshot()['pending_model_requests']==0
        finally:await client.close()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:asyncio.run(run(executor))
    finally:
        server.shutdown();thread.join();server.server_close()
    account.close(task)
    assert len(seen)==int(bool(cap))
    assert account.consumption_snapshot()==(5 if cap else 0,0,True)
