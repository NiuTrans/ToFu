"""Real native rounds refresh attempt headers without another account or vault."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import tarfile
import threading

import pytest
from lib.agent_core.execution_budget import ExecutionBudget
from lib.agent_core.native_process import Budget, NativeProcessClient, NativeProcessError
from lib.llm_errors import SubscriptionLoginRequiredError
from lib.tasks_pkg.native_model_account import NativeModelAccountPolicy
from tests.agent_core.test_native_task_terminal import principal
from tests.support.native_process_probe import config

pytestmark=pytest.mark.api


@pytest.mark.parametrize('mode',['ok','login_required','invalid_headers','empty'])
def test_installed_attempt_credentials_rotate_and_unentered_refresh_never_spends(tmp_path,mode):
    archive=os.environ.get('TOFU_NATIVE_DISPATCH_CREDENTIAL_TEST_ARCHIVE')
    if not archive:pytest.skip('explicit verified native archive required')
    from scripts.build_native_runtime import verify
    manifest=verify(Path(archive),run=True)
    installed=tmp_path/'native';installed.mkdir()
    with tarfile.open(archive) as source:source.extractall(installed,filter='data')
    binary=installed/'bin/tofu-native';binary.chmod(0o755)
    seen=[];prepared=[];tools=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            seen.append((self.headers.get('Authorization'),self.headers.get('X-Removed'),body))
            delta=({'tool_calls':[{'index':0,'id':'call','type':'function','function':{'name':'lookup','arguments':'{}'}}]} if len(seen)==1 else {'content':'done'})
            finish='tool_calls' if len(seen)==1 else 'stop'
            wire=('data: '+json.dumps({'choices':[{'delta':delta,'finish_reason':finish}],'usage':{'prompt_tokens':3,'completion_tokens':2}})+'\n\ndata: [DONE]\n\n').encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(wire)));self.end_headers();self.wfile.write(wire)
    server=HTTPServer(('127.0.0.1',0),Handler)
    task=dict(id='sdk-task',_userId=7,status='running',abort_event=threading.Event(),config={'executionLimits':dict(max_tokens=1000,max_tool_calls=3,deadline=None)})
    account=ExecutionBudget(task);task['_executionBudget']=account
    def refresh(request):
        assert request.principal.require_owner()==7 and request.task_id==task['id']
        prepared.append(request.attempt_ordinal)
        if request.attempt_ordinal==2:
            if mode=='login_required':raise SubscriptionLoginRequiredError('fixture sign-in required')
            if mode=='invalid_headers':return {'Authorization':'token\nsecret'}
        if mode=='empty':return {}
        return {'Authorization':f'Bearer fixture-{request.attempt_ordinal}'}
    configuration=config(f'http://127.0.0.1:{server.server_port}/chat','sdk-task')
    configuration['headers']={'Authorization':'Bearer stale','X-Removed':'old'}
    configuration['template']['max_tokens']=100
    configuration['model_control']={'ack_ms':2000}
    configuration['model_counter']=dict(encoding='cl100k_base',output_field='max_tokens',input_bytes=65536,text_bytes=65536,tool_json_bytes=65536,depth=128,worker_stack_bytes=1048576)
    async def lookup(request):
        assert account.reserve_tool(task) is None
        tools.append(request.name)
        return 'stored result'
    async def run(executor):
        policy=NativeModelAccountPolicy(principal=principal(),task=task,executor=executor,prepare_headers=refresh)
        client=NativeProcessClient(binary=binary,sha256=manifest['files']['bin/tofu-native']['sha256'],protocol_version=manifest['build']['stdio_contract'],principal=principal(),budget=Budget(1,16*1024*1024,65536,65536,5))
        try:
            if mode in ('login_required','invalid_headers'):
                with pytest.raises(NativeProcessError) as error:
                    await client.run(configuration,callbacks={'lookup':lookup},working_directory=tmp_path,model_policy=policy)
                assert isinstance(error.value.__cause__,SubscriptionLoginRequiredError if mode=='login_required' else ValueError)
            else:
                result=await client.run(configuration,callbacks={'lookup':lookup},working_directory=tmp_path,model_policy=policy)
                assert result['status']=='done'
                assert 'Authorization' not in json.dumps(result)
            assert client.active_runs==0
        finally:await client.close()
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:asyncio.run(run(executor))
    finally:server.shutdown();thread.join();server.server_close()
    assert prepared==[1,2] and tools==['lookup']
    count=1 if mode in ('login_required','invalid_headers') else 2
    assert len(seen)==count and all(removed is None for _,removed,_ in seen)
    assert [authorization for authorization,_,_ in seen]==([None]*count if mode=='empty' else [f'Bearer fixture-{n+1}' for n in range(count)])
    assert account.consumption_snapshot()==(5*count,1,True)
    assert account.continuity_snapshot()['pending_model_requests']==0
    assert 'fixture-' not in json.dumps(account.capture_shared_account(owner=7,task=task,counter_policy=dict(encoding='cl100k_base',output_field='max_tokens',wire_model='fixture')))
