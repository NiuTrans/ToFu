"""Issued OAuth routing binds owner/reference/destination to joined native refresh."""
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
from lib.llm.llm_errors import SubscriptionLoginRequiredError
from lib.tasks_pkg.native_model_account import NativeModelAccountPolicy
from tests.agent_core.test_native_task_terminal import principal
from tests.support.native_process_probe import config
from tests.model_routing.test_native_route import Repository, selection
from lib.model_routing.native_route import compile_native_route, run_native_route

pytestmark=pytest.mark.api


@pytest.mark.parametrize('mode',['ok','login_required','invalid_headers','missing_credentials'])
def test_installed_oauth_route_binds_selected_identity_destination_and_cache(tmp_path,mode):
    archive=os.environ.get('TOFU_NATIVE_OAUTH_ROUTE_TEST_ARCHIVE')
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
            assert body['instructions']=='' and body['store'] is False
            assert body['prompt_cache_key']==self.headers['session-id']
            assert self.path=='/v1/responses'

            output=([{'type':'reasoning','id':'reason','encrypted_content':'opaque-fixture'}, {'type':'function_call','id':'item','call_id':'call','name':'lookup','arguments':'{}'}] if len(seen)==1 else [{'type':'message','role':'assistant','content':[{'type':'output_text','text':'done'}]}])
            if len(seen)==2:
                assert any(item.get('encrypted_content')=='opaque-fixture' for item in body['input'])
            events=([{'type':'response.output_item.added','output_index':1,'item':output[1]}] if len(seen)==1 else [])
            events.append({'type':'response.completed','response':{'output':output,'usage':{'input_tokens':3,'output_tokens':2}}})
            wire=''.join('data: '+json.dumps(event)+'\n\n' for event in events).encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(wire)));self.end_headers();self.wfile.write(wire)
    server=HTTPServer(('127.0.0.1',0),Handler)
    task=dict(id='sdk-task',_userId=7,status='running',abort_event=threading.Event(),config={'executionLimits':dict(max_tokens=1000,max_tool_calls=3,deadline=None)})
    account=ExecutionBudget(task);task['_executionBudget']=account
    requests=[]
    def refresh(request):
        requests.append(request)
        assert request.subscription=='codex' and request.secret_reference=='secret-a'
        assert request.credential_id=='credential-a'
        assert request.endpoint==f'http://127.0.0.1:{server.server_port}/v1/responses'
        assert request.wire_model=='alpha-wire-a'
        assert request.extra_headers['X-Region']=='fixture'
        assert request.principal.require_owner()==7 and request.task_id==task['id']
        prepared.append(request.attempt_ordinal)
        if request.attempt_ordinal==2:
            if mode=='login_required':raise SubscriptionLoginRequiredError('fixture sign-in required')
            if mode=='invalid_headers':return {'Authorization':'Bearer token\nsecret'}
        if mode=='missing_credentials':return {}
        return {'Authorization':f'Bearer fixture-{request.attempt_ordinal}', 'session-id':request.cache_affinity_key}
    configuration=config(f'http://127.0.0.1:{server.server_port}/chat','sdk-task')
    for field in ('endpoint','headers','template'):configuration.pop(field)
    repo=Repository()
    repo.document['connections'][0].update(protocol='openai_responses',responses_profile='codex',base_url=f'http://127.0.0.1:{server.server_port}/v1',inference_profile={'features':{'responses_prompt_cache':'supported'}})
    repo.document['credentials'][0]['kind']='oauth'
    def secret(boundary,reference):
        repo.secret_calls.append((boundary,reference))
        return 'oauth:codex'
    repo.resolve_secret=secret
    route=compile_native_route(repo,repo.boundary,selection(),credential_resolver=refresh)
    configuration['model_control']={'ack_ms':2000}
    configuration['model_counter']=dict(encoding='cl100k_base',output_field='max_tokens',input_bytes=65536,text_bytes=65536,tool_json_bytes=65536,depth=128,worker_stack_bytes=1048576)
    async def lookup(request):
        assert account.reserve_tool(task) is None
        tools.append(request.name)
        return 'stored result'
    async def run(executor):
        policy=NativeModelAccountPolicy(principal=principal(),task=task,executor=executor)
        client=NativeProcessClient(binary=binary,sha256=manifest['files']['bin/tofu-native']['sha256'],protocol_version=manifest['build']['stdio_contract'],principal=principal(),budget=Budget(1,16*1024*1024,65536,65536,5))
        try:
            if mode in ('login_required','invalid_headers','missing_credentials'):
                with pytest.raises(NativeProcessError) as error:
                    await run_native_route(client,route,configuration,prepare_template=lambda metadata: dict(model=metadata['wire_model'],max_tokens=100),callbacks={'lookup':lookup},working_directory=tmp_path,model_policy=policy)
                assert isinstance(error.value.__cause__,SubscriptionLoginRequiredError if mode=='login_required' else ValueError)
            else:
                result=await run_native_route(client,route,configuration,prepare_template=lambda metadata: dict(model=metadata['wire_model'],max_tokens=100),callbacks={'lookup':lookup},working_directory=tmp_path,model_policy=policy)
                assert result.event['status']=='done', result.event
                assert 'Authorization' not in json.dumps(result.event)
            assert client.active_runs==0
        finally:
            await client.close()
            route.close()
            assert policy.prepare_headers is None
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:asyncio.run(run(executor))
    finally:server.shutdown();thread.join();server.server_close()
    assert prepared==([1] if mode=='missing_credentials' else [1,2])
    assert tools==([] if mode=='missing_credentials' else ['lookup'])
    count=0 if mode=='missing_credentials' else 1 if mode in ('login_required','invalid_headers') else 2
    assert len(seen)==count and all(removed is None for _,removed,_ in seen)
    assert [authorization for authorization,_,_ in seen]==[f'Bearer fixture-{n+1}' for n in range(count)]
    assert account.consumption_snapshot()==(5*count,0 if mode=='missing_credentials' else 1,True)
    assert account.continuity_snapshot()['pending_model_requests']==0
    assert 'fixture-' not in json.dumps(account.capture_shared_account(owner=7,task=task,counter_policy=dict(encoding='cl100k_base',output_field='max_tokens',wire_model='fixture')))

    assert len(repo.secret_calls)==1 and len(requests)==len(prepared)
    assert len({request.cache_affinity_key for request in requests})==1
    assert 'Bearer' not in repr(requests[0])
