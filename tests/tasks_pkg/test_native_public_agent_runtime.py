"""Real storage-free public SDK executes its issued preparation in Rust."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading

import pytest
from tofu_agent import AgentRuntime,NativeAgentConfiguration,ModelRoutingConfig,PrincipalContext,AgentConfigurationError
from lib.agent_core.native_image import NativeArchiveImage
from lib.agent_core.native_process import Budget
from lib.tasks_pkg.native_host import NativeHost
from tests.support.standalone_model_routing import standalone_model_routing_envelope
from tests.tasks_pkg.test_native_application import profile

pytestmark=pytest.mark.api


@pytest.mark.parametrize('mode',['async','sync','stream','tool','abort','close','prompt_refusal','provider_error','child','child_model','child_unknown_stop'])
def test_public_sdk_native_success_has_no_storage_or_python_model_loop(tmp_path,monkeypatch,mode):
    archive=os.environ.get('TOFU_NATIVE_TERMINAL_TEST_ARCHIVE')
    if not archive:pytest.skip('explicit verified native SDK archive required')
    from scripts.build_native_runtime import verify
    manifest=verify(Path(archive));budget=Budget(1,16<<20,1<<20,1<<20,5)
    image=NativeArchiveImage(archive=Path(archive),sha256=hashlib.sha256(Path(archive).read_bytes()).hexdigest(),
        application_version=manifest['build']['application_version'],budget=budget)
    host=NativeHost(image);seen=[]
    entered,release=threading.Event(),threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_args):pass
        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            if mode in {'abort','close'} or (mode=='child_unknown_stop' and len(seen)==2):
                self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
                self.wfile.flush();entered.set();release.wait(5);return
            if mode=='provider_error':
                self.send_response(400);self.send_header('Content-Length','0');self.end_headers();return
            if mode in {'tool','child','child_model','child_unknown_stop'} and len(seen)==1:
                delta={'tool_calls':[{'index':0,'id':'call-sdk','type':'function',
                    'function':{'name':'custom__echo','arguments':'{"value":"hello"}'}}]}
                reason='tool_calls'
            else:delta={'content':'sdk-native-answer'};reason='stop'
            wire=('data: '+json.dumps({'choices':[{'delta':delta,'finish_reason':reason}],
                'usage':{'prompt_tokens':3,'completion_tokens':2}})+'\n\ndata: [DONE]\n\n').encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(wire)));self.end_headers();self.wfile.write(wire)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
    storage_calls=[]
    def deny_storage(*_a,**_k):
        import traceback
        storage_calls.append(''.join(traceback.format_stack(limit=7)))
        print('FORBIDDEN STORAGE CALL\n'+storage_calls[-1],flush=True)
        raise RuntimeError('SDK accessed storage')
    monkeypatch.setattr('lib.storage.get_storage_client',deny_storage)
    monkeypatch.setattr('lib.tasks_pkg.orchestrator._run.run_root_agent_loop',lambda *_a,**_k:pytest.fail('SDK entered Python model loop'))
    envelope=standalone_model_routing_envelope(base_url=f'http://127.0.0.1:{server.server_port}/v1')
    envelope['model_routing']['models'][0]['context_window']=131072
    if mode!='prompt_refusal':envelope['model_routing']['deployments'][0]['max_output_tokens']=1024
    if mode=='child_model':
        import copy
        document=envelope['model_routing']
        other=standalone_model_routing_envelope(
            base_url=f'http://127.0.0.1:{server.server_port}/v1',model_id='model-b')
        model=copy.deepcopy(other['model_routing']['models'][0])
        document['models'].append(model)
        reference={'creator_id':'test-creator','model_id':'model-b'}
        document['credentials'][0]['authorization']['models'].append(reference)
        offering=copy.deepcopy(document['offerings'][0])
        offering.update(offering_id='child-offering',model=reference)
        document['offerings'].append(offering)
        deployment=copy.deepcopy(document['deployments'][0])
        deployment.update(deployment_id='child-deployment',offering_id='child-offering',wire_model_id='wire/model-b')
        document['deployments'].append(deployment)
    access=ModelRoutingConfig.from_mapping(envelope)
    async def run():
        with ThreadPoolExecutor(max_workers=2) as executor:
            configuration=profile();configuration['model_control']['pricing']=False
            configuration['model_counter'].update(input_bytes=1<<20,text_bytes=1<<20,tool_json_bytes=1<<20)
            native=NativeAgentConfiguration(host=host,loop=asyncio.get_running_loop(),executor=executor,
                configuration=configuration,working_directory=tmp_path,resource_policy={'frame_maximum':1<<20},
                pending_calls=8,pending_bytes=1<<20)
            runtime=AgentRuntime(principal=PrincipalContext.user(subject_id='native-sdk',owner_user_id=73,tenant_id='sdk-tenant'),
                model_routing=access,max_inflight=1,native_execution=native)
            try:
                with pytest.raises(AgentConfigurationError,match='async SDK'):
                    runtime.run([{'role':'user','content':'hello'}])
                messages=[{'role':'user','content':'hello'}]
                if mode=='sync':result=await asyncio.to_thread(runtime.run,messages,timeout_s=10)
                elif mode=='stream':
                    events=[event async for event in runtime.stream_async(messages,timeout_s=10)]
                    assert any(event.get('type')=='done' for event in events)
                    result=next(iter(runtime._executions.values())).result()
                elif mode in {'tool','child','child_model','child_unknown_stop'}:
                    schema={'type':'function','function':{'name':'custom__echo','description':'Echo input',
                        'parameters':{'type':'object','properties':{'value':{'type':'string'}},'required':['value']}},
                        'execution':{'mode':'client'}}
                    limits={'executionLimits':{'max_tokens':30000,'max_tool_calls':None,'deadline':None}} if mode=='child_unknown_stop' else {}
                    execution=runtime.start(messages,custom_tools=[schema],custom_tools_mode='exclusive',timeout_s=10,config=limits)
                    resolved=[]
                    async for event in execution.events_async(timeout_s=10):
                        if event.get('type')=='custom_tool_call':
                            assert event['arguments']=={'value':'hello'}
                            content='echo-result'
                            if mode in {'child','child_model','child_unknown_stop'}:
                                from lib.tasks_pkg.child_invocation import create_child_invocation,run_child_invocation
                                child_events=[]
                                child=create_child_invocation(execution._task,model='model-b' if mode=='child_model' else 'model-a',tools=[],
                                    objective='child objective',event_sink=child_events.append,
                                    abort_check=lambda:execution._task['abort_event'].is_set())
                                root_port=execution._task['_nativePreparedInvocation']
                                assert child['_nativePreparedInvocation'] is not root_port
                                assert child['_nativePreparedInvocation'].host is root_port.host
                                assert child['_model_routing_group'] is not execution._task['_model_routing_group']
                                account=execution._task['_executionBudget']
                                running_child=asyncio.create_task(asyncio.to_thread(run_child_invocation,child))
                                if mode=='child_unknown_stop':
                                    assert await asyncio.to_thread(entered.wait,5),'native child provider request did not enter'
                                    assert len(root_port.host._clients)==2
                                    child['abort_event'].set()
                                    try:await running_child
                                    except BaseException as error:
                                        assert isinstance(error,asyncio.CancelledError),type(error)
                                    completed=child
                                    assert completed['status']=='aborted'
                                    assert not execution._task['abort_event'].is_set()
                                    assert account.actuals_complete() is False
                                    assert execution.resolve_custom_tool_call(event['callId'],content='child-cancelled')
                                    resolved.append(event['callId'])
                                    continue
                                completed=await running_child
                                assert completed['status']=='done' and completed['usage']['prompt_tokens']==3
                                assert completed['content']=='sdk-native-answer'
                                assert child_events and completed['_invocationPublicationClosed']
                                assert not completed.get('_invocationEventSink')
                                assert completed['_executionBudget'] is account
                                content='echo-result:'+completed['content']
                            assert execution.resolve_custom_tool_call(event['callId'],content=content)
                            resolved.append(event['callId'])
                    result=await execution.result_async()
                    assert len(resolved)==1
                    if mode!='child_unknown_stop':assert any(message.get('role')=='tool' and 'echo-result' in str(message.get('content')) for message in seen[-1]['messages'])
                elif mode in {'abort','close'}:
                    execution=runtime.start(messages,timeout_s=10)
                    assert await asyncio.to_thread(entered.wait,5),'native provider request not entered'
                    if mode=='abort':assert execution.abort()
                    else:await runtime.close_async(timeout_s=5)
                    result=await execution.result_async()
                else:result=await runtime.run_async(messages,timeout_s=10)
                expected='aborted' if mode in {'abort','close'} else 'error' if mode in {'prompt_refusal','provider_error','child_unknown_stop'} else 'done'
                assert result.status==expected,(result.error,storage_calls)
                if expected=='done':
                    assert result.content=='sdk-native-answer'
                    assert result.usage.get('prompt_tokens')==(6 if mode in {'tool','child','child_model'} else 3),result.usage
                    assert result.usage.get('completion_tokens')==(4 if mode in {'tool','child','child_model'} else 2),result.usage
                assert not storage_calls,storage_calls
                assert len(seen)==(0 if mode=='prompt_refusal' else 3 if mode in {'child','child_model'} else 2 if mode in {'tool','child_unknown_stop'} else 1)
                assert runtime.in_flight==0 and not host._clients
                if mode in {'child','child_model'}:
                    assert account.consumption()[0]==15
                    assert account.actuals_complete() is True
                    if mode=='child_model':assert [body['model'] for body in seen]==['wire/model-a','wire/model-b','wire/model-a']
                from tofu_agent.capabilities import runtime_capabilities
                assert runtime_capabilities(runtime)['execution']['backend']=='rust'
            finally:
                await runtime.close_async();await host.close()
            assert image._closed and image._image.result().closed
    try:asyncio.run(run())
    finally:release.set();server.shutdown();server.server_close();thread.join(2);assert not thread.is_alive()
