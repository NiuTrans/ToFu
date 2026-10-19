"""Actual native Suspend/resume preserves prices without replaying tool/model work."""
import asyncio
import copy
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
from lib.agent_core.native_process import NativeProcessClient,Budget,NativeProcessError
from lib.tasks_pkg.native_model_account import NativeModelAccountPolicy
from lib.tasks_pkg.native_cost_projection import NativeTaskCostPolicy
from tests.agent_core.test_native_task_terminal import principal
pytestmark=pytest.mark.api


@pytest.mark.parametrize('mode',['resume','legacy_round','missing','credit','tokens','oversize','old_artifact'])
def test_installed_priced_checkpoint_preserves_fees_before_next_http(tmp_path,mode,checkpoint_factory=None,task_ids=None,project_lookup=False,principal_context=None,with_priced_child=False,tree_usage=False,child_routes=False,pending_tools=False,actor_account=False):
    archive=os.environ.get('TOFU_NATIVE_MODEL_CONTROL_TEST_ARCHIVE')
    if mode=='old_artifact':archive=os.environ.get('TOFU_NATIVE_PRICED_SETTLEMENT_TEST_ARCHIVE')
    if not archive:pytest.skip('explicit verified native archive required')
    from scripts.build_native_runtime import verify
    application_version = verify(Path(archive))['build']['application_version']
    actor=principal() if principal_context is None else principal_context
    owner=actor.require_owner(context='priced checkpoint fixture')
    seen=[];tools=[];saved=[];commits=[];pending_sequence=[]
    publication=tmp_path/'published.txt'
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            delta=({'tool_calls':[{'index':0,'id':'call','type':'function','function':{'name':'lookup','arguments':'{}'}}]}
                if len(seen)==1 else {'content':'answer'})
            wire=('data: '+json.dumps({'choices':[{'delta':delta,'finish_reason':'tool_calls' if len(seen)==1 else 'stop'}],
                'usage':{'prompt_tokens':3,'completion_tokens':2}})+'\n\ndata: [DONE]\n\n').encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream')
            self.send_header('Content-Length',str(len(wire)));self.end_headers();self.wfile.write(wire)
    server=HTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01});thread.start()
    config=dict(version='tofu.native-agent-stdio/v1',type='configure',owner_user_id=owner,task_id='fee-task',epoch='epoch',
        endpoint=f'http://127.0.0.1:{server.server_port}/chat',headers={},template={'model':'fixture','max_tokens':16},
        messages=[{'role':'user','content':'hello'}],tools={'lookup':{'name':'lookup','contractVersion':'tofu.tool-contract/v2','permission':'read','arguments_schema':{'type':'object','properties':{},'additionalProperties':False}}},
        event_bytes=65536,output_bytes=65536,tool_calls=8,request_bytes=65536,rounds=3,retained_bytes=65536,feedback_bytes=4096,tool_frame_bytes=65536,deadline_ms=10000,
        round_control=True,checkpoint_control=True,checkpoint_binding='fees-binding',checkpoint_frame_bytes=65536,checkpoint_shared_account=True,
        policy=dict(connections=1,idle_connections=1,keepalive_ms=1000,connect_ms=1000,request_headers_ms=2000,pool_wait_ms=1000,stream_idle_ms=1000,drain_timeout_ms=100,drain_bytes=65536,drain_chunks=16),
        model_control={'ack_ms':5000,'pricing':True},model_counter=dict(encoding='cl100k_base',output_field='max_tokens',input_bytes=65536,text_bytes=65536,tool_json_bytes=65536,depth=128,worker_stack_bytes=1048576))
    if pending_tools:
        # Recovery must carry admitted history beyond PIPE_BUF through the
        # existing exclusive checkpoint frame, including joined Stop handling.
        config['messages'][0]['content']='paid context '*800
    if actor_account:
        config['checkpoint_actor_account'] = True
    class Delivery:
        async def reserve(self,request):return True
        async def settle(self,result):commits.append(result.request.round)
    class Checkpoints:
        binding='fees-binding'
        supports_pending_tools=pending_tools
        async def load(self,request):
            if not saved:return None
            state=copy.deepcopy(saved[0])
            if mode=='legacy_round':state['host_fee_projection']['rounds'][0].pop('task_id')
            if mode=='missing':state.pop('host_fee_projection')
            elif mode=='credit':state['host_fee_projection']['rounds'][0]['cost']['credits']['total']+=1
            elif mode=='tokens':state['shared_model_account']['tokens']+=1
            return state
        async def begin(self,request):return None
        async def pending(self,request):
            assert request.state['tools_pending'] is True
            assert not publication.exists()
            assert request.state['host_fee_projection']['rounds'][0]['cost']['credits']['total']==31
            saved.append(copy.deepcopy(request.state));pending_sequence.append('pending')
        async def reconcile(self,request):
            assert request.state==saved[0] and request.round==0
            assert publication.read_bytes()==b'published\n'
            assert len(seen)==1 and tools==['lookup']
            pending_sequence.append('reconcile')
            return [dict(call_id='call',content='feedback')]
        async def complete(self,request):
            if pending_tools:
                assert 'tools_pending' not in request.state
                assert request.state['usage']==saved[0]['usage']
                assert request.state['host_fee_projection']==saved[0]['host_fee_projection']
                assert request.state['shared_model_account']==saved[0]['shared_model_account']
                pending_sequence.append('complete')
                return 'continue'
            saved.append(copy.deepcopy(request.state));return 'suspend'
    async def invoke(executor):
        tasks=[]
        for iteration in range(2):
            task_id='fee-task' if task_ids is None else task_ids[iteration]
            task=dict(id=task_id,_userId=owner,status='running',abort_event=threading.Event(),config={'executionLimits':dict(max_tokens=100000,max_tool_calls=None,deadline=None)})
            task['_executionBudget']=ExecutionBudget(task);tasks.append(task)
            model=NativeModelAccountPolicy(principal=actor,task=task,executor=executor,
                prepare_pricing=lambda result:ModelPricing.resolved(dict(prompt_tokens=3,input=2.5 if iteration==0 else 5.,output=10. if iteration==0 else 20.,cache_write_multiplier=1.25,cache_read_multiplier=.1,usd_to_cny=7.24,currency='USD',margin=.125)))
            costs=NativeTaskCostPolicy(principal=actor,task=task,policy=Delivery())
            costs.bind_pricing_route(dict(owner_user_id=owner,logical_model='alpha',provider_id='provider-a'))
            if mode=='oversize':
                capture=costs.capture_fee_checkpoint
                def oversized_capture(request,state):
                    projection=copy.deepcopy(capture(request,state))
                    projection['rounds'][0]['cost']['price']['padding']='x'*65000
                    return projection
                costs.capture_fee_checkpoint=oversized_capture
            async def lookup(request):
                tools.append(request.name)
                if pending_tools:
                    with publication.open('ab') as output:
                        output.write(b'published\n');output.flush();os.fsync(output.fileno())
                if with_priced_child:
                    from lib.billing.cost import transfer_native_child_fees
                    child=dict(id='paid-native-child',_userId=owner,status='running',config=task['config'],
                        _parentTaskId=task['id'],_executionBudget=task['_executionBudget'],
                        abort_event=threading.Event(),_executionActualsComplete=True)
                    task['_executionBudget'].attach(child,parent_task_id=task['id'])
                    child_model=NativeModelAccountPolicy(principal=actor,task=child,executor=executor,
                        prepare_pricing=lambda _result:ModelPricing.resolved(dict(prompt_tokens=3,input=2.5,
                            output=10.,cache_write_multiplier=1.25,cache_read_multiplier=.1,
                            usd_to_cny=7.24,currency='USD',margin=.125)))
                    child_costs=NativeTaskCostPolicy(principal=actor,task=child,policy=Delivery())
                    child_costs.bind_pricing_route(dict(owner_user_id=owner,logical_model='alpha',provider_id='provider-a'))
                    child_config=copy.deepcopy(config)
                    for field in ('checkpoint_control','checkpoint_binding','checkpoint_frame_bytes',
                                  'checkpoint_shared_account','checkpoint_tree_usage'):
                        child_config.pop(field,None)
                    child_config.update(task_id=child['id'],epoch=child['id'],tools={})
                    child_client=NativeProcessClient.from_shared_archive(image=shared_image,principal=actor,
                        budget=Budget(1,256*1024*1024,65536,65536,5))
                    try:
                        child_result=await child_client.run(child_config,callbacks={},working_directory=tmp_path,
                            round_policy=child_costs,model_policy=child_model)
                        assert child_result['status']=='done'
                        child['status']='done'
                        transfer_native_child_fees(task,child)
                    finally:
                        await child_client.close()
                        task['_executionBudget'].close(child)
                if project_lookup:
                    from lib.tasks_pkg.tool_display._dispatch import _build_tool_round_entry
                    from lib.tasks_pkg.executor._finalize import _finalize_tool_round
                    # This fixture's provider emits the exact ID "call"; the
                    # standalone SDK callback need not carry application IDs.
                    tool_call_id=request.call_id or 'call'
                    number,entry,_=_build_tool_round_entry(request.name,dict(request.arguments),tool_call_id,
                        json.dumps(dict(request.arguments)),len(task['toolRounds']),False,task=task)
                    task['toolRounds'].append(entry)
                    entry['toolContent']='feedback'
                    _finalize_tool_round(task,number,entry,[{'role':'tool','tool_call_id':tool_call_id,'content':'feedback'}])
                return 'feedback'
            checkpoint,terminal=(checkpoint_factory(task,executor,iteration) if checkpoint_factory is not None else (Checkpoints(),None))
            progress=None
            if pending_tools:
                from lib.agent_core.native_process import ProgressLimits
                class Progress:
                    limits=ProgressLimits(3,3)
                    async def observe(self,request):
                        assert request.round==0 and request.tool_calls==1
                        if iteration==0:
                            pending_sequence.append('lost-progress')
                            raise OSError('injected failure after durable publication')
                        assert pending_sequence[-1]=='reconcile'
                        pending_sequence.append('progress')
                        return dict(worldVersion='published',evidenceIds=['publication-proof'],
                            resultEvidenceComplete=True,verification='',nonretryableFailureSignatures=[])
                progress=Progress()
            run_config=dict(config,task_id=task_id)
            if with_priced_child or tree_usage:run_config['checkpoint_tree_usage']=True
            if child_routes:run_config['checkpoint_child_routes']=True
            if terminal is not None:run_config=dict(run_config,terminal_persistence={
                'operation_id':f'commit-{iteration}','ack_ms':5000,'attempts':2,'frame_bytes':65536})
            client=NativeProcessClient.from_archive(archive=Path(archive),sha256=hashlib.sha256(Path(archive).read_bytes()).hexdigest(),
                application_version=application_version,principal=actor,budget=Budget(1,256*1024*1024,65536,65536,5))
            if with_priced_child:
                client=NativeProcessClient.from_shared_archive(image=shared_image,principal=actor,
                    budget=Budget(1,256*1024*1024,65536,65536,5))
            try:
                if mode in {'oversize','old_artifact'} or (iteration==1 and mode not in ('resume','legacy_round')) or (pending_tools and iteration==0):
                    with pytest.raises(NativeProcessError):
                        await client.run(run_config,callbacks={'lookup':lookup},working_directory=tmp_path,
                            round_policy=costs,model_policy=model,checkpoint_policy=checkpoint,terminal_policy=terminal,progress_policy=progress)
                else:
                    result=await client.run(run_config,callbacks={'lookup':lookup},working_directory=tmp_path,
                        round_policy=costs,model_policy=model,checkpoint_policy=checkpoint,terminal_policy=terminal,progress_policy=progress)
                    assert result['status']==('aborted' if iteration==0 else 'done')
            finally:
                await client.close()
                if checkpoint_factory is not None:
                    await checkpoint.close(terminal_saved=terminal.accepted)
            if mode=='old_artifact':
                assert saved==[] and commits==[] and seen==[] and tools==[]
                assert task['_executionBudget'].consumption_snapshot()==(0,0,True)
                return
            if mode=='oversize':
                assert saved==[] and commits==[0] and len(seen)==1 and tools==['lookup']
                assert task['_executionBudget'].consumption_snapshot()==(5,0,True)
                assert task['apiRounds'][0]['nativeCredits']['total']==31
                return
            if iteration==0:
                if checkpoint_factory is None:
                    assert len(saved)==1 and saved[0]['shared_model_account']['tokens']==(10 if with_priced_child else 5)
                    assert saved[0]['host_fee_projection']['rounds'][0]['cost']['credits']['total']==31
                    assert saved[0]['host_fee_projection']['rounds'][0]['task_id']=='fee-task'
        if mode in ('resume','legacy_round'):
            assert [row['nativeCredits']['total'] for row in tasks[1]['apiRounds']]==([31,31,61] if with_priced_child else [31,61])
            assert [row['taskId'] for row in tasks[1]['apiRounds']]==(['fee-task','paid-native-child','fee-task'] if with_priced_child else ['fee-task','fee-task'] if task_ids is None else list(task_ids))
            assert tasks[1]['_executionBudget'].consumption_snapshot()==(15 if with_priced_child else 10,0,True)
            assert commits==([0,0,1] if with_priced_child else [0,1]) and len(seen)==(3 if with_priced_child else 2)
        else:
            assert tasks[1]['_executionBudget'].consumption_snapshot()==(0,0,True)
            assert 'apiRounds' not in tasks[1] and commits==[0] and len(seen)==1
        assert tools==['lookup']
        if pending_tools:
            expected=['pending','lost-progress']
            if mode=='resume':expected+=['reconcile','progress','complete']
            assert pending_sequence==expected
            assert publication.read_bytes()==b'published\n'
    shared_image=None
    if with_priced_child:
        from lib.agent_core.native_image import NativeArchiveImage
        shared_image=NativeArchiveImage(archive=Path(archive),sha256=hashlib.sha256(Path(archive).read_bytes()).hexdigest(),
            application_version=application_version,budget=Budget(1,256*1024*1024,65536,65536,5))
    async def invoke_owned(executor):
        try:
            await invoke(executor)
        finally:
            if shared_image is not None:await shared_image.close()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:asyncio.run(invoke_owned(executor))
    finally:
        server.shutdown();thread.join();server.server_close()


@pytest.mark.parametrize('mode', ['resume', 'missing', 'credit', 'tokens'])
def test_installed_paid_pending_reconciles_before_progress_and_next_http(tmp_path, mode):
    test_installed_priced_checkpoint_preserves_fees_before_next_http(
        tmp_path, mode, pending_tools=True)
