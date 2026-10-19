"""Pinned native process computes fees in existing settlement/round exchanges."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler,HTTPServer
import json
import os
from pathlib import Path
import threading

import pytest
from lib.agent_core.execution_budget import ExecutionBudget
from lib.agent_core.native_model_control import ModelPricing
from lib.agent_core.native_process import NativeProcessClient,Budget,NativeProcessError,NativeRoundError
from lib.tasks_pkg.native_model_account import NativeModelAccountPolicy
from tests.agent_core.test_native_task_terminal import principal
from tests.support.native_process_probe import config
pytestmark=pytest.mark.api

@pytest.mark.parametrize('mode',['priced','unpriced','wrong_prompt','consumer_fault','unknown','zero','route_priced'])
def test_installed_native_fee_is_after_paid_budget_and_delivered_once(tmp_path,mode,monkeypatch):
    source=os.environ.get('TOFU_NATIVE_PRICED_SETTLEMENT_TEST_ARCHIVE')
    if not source:pytest.skip('explicit verified native archive required')
    from scripts.build_native_runtime import verify
    archive=Path(source);manifest=verify(archive,run=True);digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    requests=[];received=[];preparations=[];actions=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            if mode=='route_priced':assert requests[-1]['model']=='alpha-wire-a'
            usage=({'prompt_tokens':0,'completion_tokens':0} if mode=='zero' else {}) if mode in {'unknown','zero'} else {'prompt_tokens':10000,'completion_tokens':200,'cache_read_tokens':9000}
            wire=('data: '+json.dumps({'usage':usage,'choices':[{'delta':{'content':'done'},'finish_reason':'stop'}]})+'\n\ndata: [DONE]\n\n').encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(wire)));self.end_headers();self.wfile.write(wire)
    server=HTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
    task={'id':'priced-task','_userId':7,'config':{'executionLimits':dict(max_tokens=100000,max_tool_calls=None,deadline=None)},'abort_event':threading.Event()}
    account=ExecutionBudget(task);task['_executionBudget']=account
    settings=config(f'http://127.0.0.1:{server.server_port}/chat',task['id']);settings['tools']={};settings['tool_frame_bytes']=65536
    settings['model_control']={'ack_ms':2000,'pricing':mode!='unpriced'}
    settings['model_counter']=dict(encoding='cl100k_base',output_field='max_tokens',input_bytes=65536,text_bytes=65536,tool_json_bytes=65536,depth=128,worker_stack_bytes=1048576)
    settings['template']['max_tokens']=1000
    def prepare(result):
        assert account.continuity_snapshot()['pending_model_requests']==0
        preparations.append(result.request.attempt_ordinal)
        if mode=='unknown':return None
        return ModelPricing.resolved(dict(prompt_tokens=9999 if mode=='wrong_prompt' else 0 if mode=='zero' else 10000,input=2.5,output=10.,cache_write_multiplier=1.25,cache_read_multiplier=.1,usd_to_cny=7.24,currency='USD',margin=.125))
    class Rounds:
        accepts_native_cost=True
        async def reserve(self,request):return True
        async def settle(self,result):
            assert account.continuity_snapshot()['pending_model_requests']==0
            if result.evidence['kind']=='returned':
                received.append(dict(result.evidence))
                if mode=='consumer_fault':raise ValueError('delivery unavailable')
    async def run(executor):
        if mode=='route_priced':
            from lib.tasks_pkg.native_pricing import NativeRoutePricing
            from lib.pricing._provider import PROVIDER_PRICING
            monkeypatch.setitem(PROVIDER_PRICING,'provider-a',{'alpha':dict(input=999.,output=999.),'alpha-wire-a':dict(input=1000.,output=1000.)})
            model=NativeModelAccountPolicy(principal=principal(),task=task,executor=executor,
                pricing_resolver=NativeRoutePricing(principal=principal(),margin=.125,rates=lambda _:7.24))
        else:
            model=NativeModelAccountPolicy(principal=principal(),task=task,executor=executor,prepare_pricing=None if mode=='unpriced' else prepare)
        for action in ('reserve','dispatch','settle'):
            original=getattr(model,action)
            async def observe(value,original=original,action=action):
                actions.append(action)
                return await original(value)
            setattr(model,action,observe)
        client=NativeProcessClient.from_archive(archive=archive,sha256=digest,application_version=manifest['build']['application_version'],principal=principal(),budget=Budget(1,16*1024*1024,65536,65536,5))
        try:
            if mode=='route_priced':
                from tests.model_routing.test_native_route import Repository,selection
                from lib.model_routing.native_route import compile_native_route,run_native_route
                repo=Repository();repo.document['connections'][0]['base_url']=f'http://127.0.0.1:{server.server_port}/v1'
                repo.document['offerings'][0]['actual_pricing'].update(input=2.5,output=10.,cache_read=.25,cache_write=3.125)
                route=compile_native_route(repo,repo.boundary,selection())
                clean={key:value for key,value in settings.items() if key not in {'endpoint','headers','template'}}
                result=await run_native_route(client,route,clean,prepare_template=lambda meta:dict(model=meta['wire_model'],max_tokens=1000),callbacks={},working_directory=tmp_path,model_policy=model,round_policy=Rounds())
                assert result.event['status']=='done'
                assert model.prepare_pricing is None and model.pricing_result is None
            elif mode=='consumer_fault':
                with pytest.raises(NativeProcessError):await client.run(settings,callbacks={},working_directory=tmp_path,model_policy=model,round_policy=Rounds())
            else:
                result=await client.run(settings,callbacks={},working_directory=tmp_path,model_policy=model,round_policy=Rounds())
                assert result['status']==('error' if mode=='wrong_prompt' else 'done'),result
                if mode=='wrong_prompt': assert 'model_pricing' in json.dumps(result),result
            assert client.active_runs==0
            assert account.continuity_snapshot()['pending_model_requests']==0
        finally:await client.close()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:asyncio.run(run(executor))
    finally:server.shutdown();thread.join();server.server_close()
    assert len(requests)==1
    assert actions==['reserve','dispatch','settle']
    assert preparations==([] if mode in {'unpriced','unknown','route_priced'} else [1])
    if mode in {'priced','consumer_fault','route_priced'}:
        assert len(received)==1
        cost=received[0]['cost']
        assert cost['amounts']['inputTokens']==1000 and cost['amounts']['totalInputTokens']==10000
        assert cost['credits']==dict(input=2500,output=2000,cache_read=2250,cache_write=0,base=6750,margin=843,total=7593)
    elif mode=='zero':
        assert len(received)==1 and received[0]['cost']['amounts'] is None
        assert all(value==0 for value in received[0]['cost']['credits'].values())
    elif mode!='wrong_prompt':
        assert len(received)==1 and 'cost' not in received[0]
    assert account.consumption_snapshot()==(10200 if mode not in {'unknown','zero'} else 0,0,mode!='unknown')
