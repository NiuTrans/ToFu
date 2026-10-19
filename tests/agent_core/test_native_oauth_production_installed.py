"""Production auth failures stop actual native execution before proxy ingress."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler,HTTPServer
import os
from pathlib import Path
import tarfile
import threading

import pytest
from lib.agent_core.execution_budget import ExecutionBudget
from lib.agent_core.native_process import Budget,NativeProcessClient,NativeProcessError
from lib.llm.llm_errors import SubscriptionCredentialUnavailableError
from lib.model_routing.domain import ModelRoutingError
from lib.model_routing.native_route import run_native_route
from lib.tasks_pkg.native_execution import compile_native_application_route
from lib.tasks_pkg.native_model_account import NativeModelAccountPolicy
from tests.model_routing.test_native_route import Repository,principal,selection
from tests.support.native_process_probe import config

pytestmark=pytest.mark.api


@pytest.mark.parametrize('mode',['source_missing','foreign_owner','revoked'])
def test_installed_production_oauth_refusal_never_opens_network_or_spends(tmp_path,monkeypatch,mode):
    archive=os.environ.get('TOFU_NATIVE_OAUTH_PRODUCTION_TEST_ARCHIVE')
    if not archive:pytest.skip('explicit verified native archive required')
    from scripts.build_native_runtime import verify
    manifest=verify(Path(archive),run=True);installed=tmp_path/'native';installed.mkdir()
    with tarfile.open(archive) as source:source.extractall(installed,filter='data')
    binary=installed/'bin/tofu-native';binary.chmod(0o755)
    repo=Repository();repo.document['connections'][0].update(protocol='openai_responses',responses_profile='codex',base_url='https://chatgpt.com/backend-api/codex')
    repo.document['credentials'][0]['kind']='oauth';repo.resolve_secret=lambda *_:'oauth:codex'
    route=compile_native_application_route(repo,principal(),selection())
    source=None if mode=='source_missing' else {'owner_user_id':8,'tenant_id':'','account_id':'account-a'}
    monkeypatch.setattr('lib.oauth.token_store.load_stored_token',lambda provider:source)
    monkeypatch.setattr('lib.oauth.outbound.resolve_oauth_request',lambda *args,**kwargs: (_ for _ in ()).throw(AssertionError('refresh was not authorized')))
    if mode=='revoked':repo.document['credentials'][0]['enabled']=False
    connections=[]
    class Proxy(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_CONNECT(self):
            connections.append(self.path)
            self.send_error(403)
    proxy=HTTPServer(('127.0.0.1',0),Proxy)
    settings=config('unused','sdk-task')
    for field in ('endpoint','headers','template'):settings.pop(field)
    settings['proxy']=f'http://127.0.0.1:{proxy.server_port}'
    settings['tools']={}
    settings['model_control']={'ack_ms':2000}
    settings['model_counter']=dict(encoding='cl100k_base',output_field='max_tokens',input_bytes=65536,text_bytes=65536,tool_json_bytes=65536,depth=128,worker_stack_bytes=1048576)
    task=dict(id='sdk-task',_userId=7,status='running',abort_event=threading.Event(),config={'executionLimits':dict(max_tokens=1000,max_tool_calls=None,deadline=None)})
    account=ExecutionBudget(task);task['_executionBudget']=account
    async def run(executor):
        model=NativeModelAccountPolicy(principal=principal(),task=task,executor=executor)
        client=NativeProcessClient(binary=binary,sha256=manifest['files']['bin/tofu-native']['sha256'],protocol_version=manifest['build']['stdio_contract'],principal=principal(),budget=Budget(1,16*1024*1024,65536,65536,5))
        try:
            with pytest.raises(NativeProcessError) as error:
                await run_native_route(client,route,settings,prepare_template=lambda metadata:dict(model=metadata['wire_model'],max_tokens=100),callbacks={},working_directory=tmp_path,model_policy=model)
            assert isinstance(error.value.__cause__,ModelRoutingError if mode=='revoked' else SubscriptionCredentialUnavailableError)
            assert client.active_runs==0 and model.prepare_headers is None
        finally:await client.close();route.close()
    thread=threading.Thread(target=proxy.serve_forever,kwargs={'poll_interval':.01});thread.start()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:asyncio.run(run(executor))
    finally:proxy.shutdown();thread.join();proxy.server_close()
    assert connections==[]
    assert account.consumption_snapshot()==(0,0,True)
    assert account.continuity_snapshot()['pending_model_requests']==0
