"""Ordinary v4 requests select native ROOT through application-owned composition."""
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import threading
import time
import uuid

import pytest
from tests.storage.test_storage_application_contract import application_storage as application_storage

pytestmark=pytest.mark.api


@pytest.fixture
def application_storage_owner_id(resume_operation):
    # The first opaque billing user owns numerical id 2; local owner 1 remains
    # distinct. Bind the disposable authority to the actor actually exercised.
    return 2 if (resume_operation or '').startswith('billing_') else 1


@pytest.mark.parametrize('scenario,prebind_failure,resume_operation',[
    ('text',False,None),('mixed_direct_first',False,None),('text',True,None),
    ('mixed_direct_first',False,'continue'),('mixed_direct_first',False,'checkpoint_resume'),
    ('mixed_direct_first',False,'unknown_continue'),
    ('mixed_direct_first',False,'unknown_checkpoint_resume'),
    ('mixed_direct_first',False,'billing_continue'),
    ('mixed_direct_first',False,'billing_checkpoint_resume'),
    ('mixed_direct_first',False,'billing_legacy_continue'),
    ('mixed_direct_first',False,'billing_legacy_checkpoint_resume'),
    ('mixed_direct_first',False,'billing_journal_continue'),
    ('mixed_direct_first',False,'billing_journal_checkpoint_resume')])
def test_http_application_issues_native_root_without_task_sentinel(
        application_storage,tmp_path,monkeypatch,caplog,scenario,prebind_failure,resume_operation,priced_child=False,priced_child_depth=1,native_flow=False,flow_parallel=False,flow_child_model="alpha",native_flow_kernel=False,flow_verifier_loop=False,flow_first_verdict="[VERDICT: CONTINUE_WORKER]",image_preparation=False,flow_branch=False,flow_archive=False,installed_executable=False,installed_token_library=False,actor_checkpoint=False,actor_observer=None,actor_tool_round=False,flow_definition=None,caller_tool_schemas=None,application_configuration_factory=None,after_completion=None):
    archive=os.environ.get('TOFU_NATIVE_TERMINAL_TEST_ARCHIVE')
    if not archive:pytest.skip('explicit verified native release required')
    journal_resume=bool(resume_operation and 'journal_' in resume_operation)
    if actor_checkpoint:
        assert native_flow and not resume_operation
        if not callable(getattr(application_storage,'native_checkpoint_available',None)) or not application_storage.native_checkpoint_available(user_id=2):
            pytest.skip('selected authority has no native checkpoint port')
    if journal_resume and (not callable(getattr(application_storage,'native_checkpoint_available',None))
            or not application_storage.native_checkpoint_available(user_id=2)):
        pytest.skip('this selected authority has no native checkpoint control port')
    from benchmarks.native_agent_root import RootWorkload
    from lib.agent_core.native_image import NativeArchiveImage
    from lib.agent_core.native_process import Budget
    from lib.app.app_assembly import create_application
    from lib.model_routing.repository import OwnerBoundary
    from lib.resource_control.runtime import launch_policy
    from lib.server.server_loop_runtime import ServingLoopRuntime
    from lib.tasks_pkg.native_application import NativeRootConfiguration
    from lib.tasks_pkg.native_pricing import NativeRoutePricing
    from lib.turn_lifecycle import get_turn,get_attempt
    from scripts.build_native_runtime import verify
    from tests.model_routing.test_native_route import Repository
    from tests.tasks_pkg.test_native_application import profile
    from concurrent.futures import ThreadPoolExecutor

    conv_id=uuid.uuid4().hex
    source=Path(archive);manifest=verify(source)
    budget=Budget(1,16<<20,1<<20,1<<20,5)
    image=NativeArchiveImage(archive=source,sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        application_version=manifest['build']['application_version'],budget=budget)
    if installed_executable:
        from lib.agent_core.native_image import NativeExecutableImage
        from scripts.build_native_runtime import install_executable
        receipt = install_executable(source, tmp_path/'installed-tofu-native',
            archive_sha256=image.sha256, application_version=image.application_version)
        image = NativeExecutableImage(binary=Path(receipt['executable']),
            sha256=receipt['executable_sha256'], application_version=receipt['application_version'], budget=budget)
    if installed_token_library:
        assert installed_executable
        from scripts.build_native_runtime import install_token_library
        receipt = install_token_library(source, tmp_path/'installed-token-library.so',
            archive_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
            application_version=manifest['build']['application_version'])
        image = NativeExecutableImage(binary=image.binary, sha256=image.sha256,
            application_version=image.application_version, budget=budget,
            token_library=Path(receipt['token_library']),token_library_sha256=receipt['token_library_sha256'])
    flow_requests=4 if flow_verifier_loop else 2
    if actor_tool_round:
        flow_requests *= 2
    flow_library=None
    if native_flow_kernel:
        source_library=os.environ.get('TOFU_NATIVE_FLOW_TEST_LIBRARY')
        source_flow_archive=os.environ.get('TOFU_NATIVE_FLOW_TEST_ARCHIVE')
        from lib.orchestration.native_flow import NativeFlowLibrary
        if flow_archive:
            assert native_flow_kernel and not flow_verifier_loop
        elif source_flow_archive:
            flow_path=Path(source_flow_archive);flow_manifest=verify(flow_path,run=False)
            flow_library=NativeFlowLibrary.from_archive(archive=flow_path,
                sha256=hashlib.sha256(flow_path.read_bytes()).hexdigest(),
                application_version=flow_manifest['build']['application_version'],maximum_bytes=64<<20)
        else:
            if not source_library:pytest.skip('explicit verified Flow kernel required')
            flow_library=NativeFlowLibrary(path=Path(source_library),
                sha256=hashlib.sha256(Path(source_library).read_bytes()).hexdigest(),maximum_bytes=64<<20)
        monkeypatch.setattr('lib.orchestration.engine.FlowExecutor._walk_python',
            lambda *a,**k:pytest.fail('native Flow entered the Python graph walker'))
        for name in ('single_next','find_start','loop_parts','find_loop_planner','find_common_barrier','reachable','can_reach','distance'):
            monkeypatch.setattr('lib.orchestration.graph.GraphNavigator.'+name,
                lambda *a,**k:pytest.fail('native Flow entered Python topology traversal'))
    if flow_verifier_loop:
        if getattr(flow_library._library,'tofu_flow_loop_open',None) is not None:
            monkeypatch.setattr('lib.orchestration.loop_runtime.OrchestrationLoopRuntime._run_python',
                lambda *a,**k:pytest.fail('native loop entered Python loop policy'))
            monkeypatch.setattr('lib.orchestration.engine._parse_progress',
                lambda *a,**k:pytest.fail('native loop entered Python progress parser'))
        monkeypatch.setattr('lib.orchestration.engine._classify_verdict_core',
            lambda *a,**k:pytest.fail('native loop invoked Python verdict policy'))
    if native_flow:
        monkeypatch.setattr('lib.tasks_pkg.orchestrator._run.run_root_agent_loop',
            lambda *a,**k:pytest.fail('native Flow must not execute the Python role loop'))
    legacy_invoice=bool(resume_operation and resume_operation.startswith('billing_legacy_'))
    billed_resume=bool(priced_child or native_flow or (resume_operation and resume_operation.startswith('billing_')))
    if billed_resume:
        from lib.billing import users,deposit,reserve,get_balance
        from lib.billing.request_flow import settle_task
        wallet=users.create_user(uuid.uuid4().hex+'@native-resume.example')
        owner_id=wallet.owner_user_id
        from lib.identity import PrincipalContext
        principal=PrincipalContext.user(subject_id=wallet.id,owner_user_id=owner_id)
        monkeypatch.setattr('routes.conversation_sync_v3._request_user_id',lambda:owner_id)
        monkeypatch.setattr('routes.conversation_sync_v3._request_principal',lambda:principal)
        deposit(wallet.id,10000,ref_id='seed-'+conv_id)
        monkeypatch.setattr('lib.relay_config.billing_enabled',lambda:True)
        monkeypatch.setattr('lib.billing.compute_request_cost',lambda *a,**k:pytest.fail('native resume must not reprice'))
    if not billed_resume:owner_id=1
    application_storage.command('conversation.create',dict(conv_id=conv_id,user_id=owner_id,
        title='Native application',created_at=1,updated_at=1,settings={}),uuid.uuid4().hex)
    unknown_resume=bool(resume_operation and resume_operation.startswith('unknown_'))
    entered,release=threading.Event(),threading.Event()
    if resume_operation and not unknown_resume and not journal_resume:
        from lib.tasks_pkg.orchestrator import _root_agent_loop
        original=_root_agent_loop.prepare_round_context
        paused=[]
        def prepare(task,messages,**values):
            result=original(task,messages,**values)
            if not paused and any(message.get('role')=='tool' for message in messages):
                paused.append(task['id']);entered.set()
                assert release.wait(10),'test did not release prepared round'
            return result
        monkeypatch.setattr(_root_agent_loop,'prepare_round_context',prepare)
    async def run():
        with ThreadPoolExecutor(max_workers=1) as fixture_executor:
            provider=RootWorkload(storage=application_storage,host=None,executor=fixture_executor,
                directory=tmp_path,budget=budget,resource_policy=launch_policy({}),scenario=scenario,
                expected_wire_models=('alpha-wire-a',flow_child_model+'-wire-a') if native_flow else ('alpha-wire-a',))
            ordinary_provider_post = provider.server.RequestHandlerClass.do_POST
            if flow_branch:
                provider.response_text=lambda ordinal:'Security Review' if ordinal==0 else 'durable fixture answer'
            if flow_verifier_loop:
                provider.response_text=lambda ordinal:('durable fixture answer\n'+flow_first_verdict if ordinal==1
                    else 'durable fixture answer\n[VERDICT: STOP]' if ordinal==3 else 'durable fixture answer')
            if actor_tool_round:
                def file_round(request):
                    body=json.loads(request.rfile.read(int(request.headers['Content-Length'])))
                    assert body['model'] in provider.expected_wire_models
                    provider.bodies.append(body)
                    if any(row.get('role')=='tool' for row in body['messages']):
                        delta,finish={'content':'durable fixture answer'},'stop'
                    else:
                        identity=uuid.uuid4().hex
                        delta={'tool_calls':[{'index':0,'id':'file-'+identity,'type':'function',
                            'function':{'name':'write_file','arguments':json.dumps({
                                'description':'Record one actor write',
                                'path':'actor-'+identity+'.txt','content':'once\n','append':True})}}]}
                        finish='tool_calls'
                    wire=('data: '+json.dumps({'choices':[{'delta':delta,'finish_reason':finish}],
                        'usage':{'prompt_tokens':3,'completion_tokens':2,'total_tokens':5}})+'\n\ndata: [DONE]\n\n').encode()
                    request.send_response(200);request.send_header('Content-Type','text/event-stream')
                    request.send_header('Content-Length',str(len(wire)));request.end_headers();request.wfile.write(wire)
                provider.server.RequestHandlerClass.do_POST=file_round
            if flow_parallel:
                handler=provider.server.RequestHandlerClass
                original_post=handler.do_POST
                both_roles=threading.Barrier(2)
                def concurrent_roles(request):
                    both_roles.wait(timeout=10)
                    return original_post(request)
                handler.do_POST=concurrent_roles
            if unknown_resume:
                handler=provider.server.RequestHandlerClass
                original_post=handler.do_POST
                held=[]
                def receive_without_usage(request):
                    if not held and len(provider.bodies)==1:
                        held.append(True)
                        body=json.loads(request.rfile.read(int(request.headers['Content-Length'])))
                        provider.bodies.append(body)
                        entered.set();release.wait(10)
                        return
                    return original_post(request)
                handler.do_POST=receive_without_usage
            repo=Repository();repo.boundary=OwnerBoundary.create(owner_id)
            repo.document['connections'][1]['enabled']=False
            repo.document['connections'][0]['base_url']=f'http://127.0.0.1:{provider.server.server_port}/v1'
            monkeypatch.setattr('lib.model_routing.ModelRoutingRepository',lambda:repo)
            config=profile()
            for field in ('input_bytes','text_bytes','tool_json_bytes'):
                config['model_counter'][field]=1<<20
            if journal_resume:
                config.update(checkpoint_shared_account=True,checkpoint_frame_bytes=1<<20)
                if priced_child:config['checkpoint_tree_usage']=True
            if actor_checkpoint:
                config.update(checkpoint_actor_account=True,checkpoint_shared_account=True,checkpoint_frame_bytes=1<<20)
            created=[]
            checkpoints=[];restored_frontiers=[];checkpoint_sizes=[];restored_tree_sizes=[]
            def checkpoint_factory(request,principal,executor,profile):
                from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
                task=request.task;resuming=bool(checkpoints)
                if actor_checkpoint:
                    async def no_restore(callback,state):
                        pytest.fail('new actor checkpoint unexpectedly restored an old frontier')
                    class ActorCheckpoint(NativeCheckpointPolicy):
                        async def _call(self,operation,**fields):
                            if operation in {'pending','complete'}:
                                from pathlib import Path
                                import jsonschema
                                schema=json.loads(Path('contracts/native_agent_checkpoint_v1.schema.json').read_text())
                                jsonschema.validate(fields['state'],schema)
                            result=await super()._call(operation,**fields)
                            if operation in {'pending','complete'} and actor_observer is not None:
                                actor_observer(task,operation,json.loads(json.dumps(fields['state'])))
                            return result
                    from lib.tasks_pkg.native_file_recovery import NativeFileToolRecovery
                    tool_recovery=(NativeFileToolRecovery(principal=principal,task=task,
                        executor=executor,registered_child=True) if actor_tool_round else None)
                    checkpoint=ActorCheckpoint(principal=principal,task=task,storage=application_storage,
                        executor=executor,epoch=task['id'],binding='registered-flow-actor',token=uuid.uuid4().hex,
                        header_bytes=4096,snapshot_bytes=1<<20,identity_bytes=4096,io_timeout_ms=10000,
                        restore=no_restore,registered_child=True,tool_recovery=tool_recovery)
                    checkpoints.append(checkpoint)
                    return checkpoint
                async def restore(callback,state):
                    restored_frontiers.append(state['next_round'])
                    assert state['task_id']==state['shared_model_account']['root_task_id']==task['id']
                    assert state['host_fee_projection']['rounds'][0]['task_id']==created[0][0]['id']
                    restored_tree_sizes.append((state['shared_model_account'].get('root_tokens',state['shared_model_account']['tokens']),len(state['host_fee_projection'].get('child_rounds',[]))))
                class MeasuredCheckpoint(NativeCheckpointPolicy):
                    async def complete(self,callback):
                        result=await super().complete(callback)
                        checkpoint_sizes.append(len(json.dumps(callback.state,separators=(',',':'),
                            ensure_ascii=False,allow_nan=False).encode()))
                        return result
                checkpoint=MeasuredCheckpoint(principal=principal,task=task,storage=application_storage,
                    executor=executor,epoch='turn:'+task['config']['_turnId'],binding='native-http-checkpoint',
                    token=uuid.uuid4().hex,header_bytes=4096,snapshot_bytes=1<<20,identity_bytes=4096,
                    io_timeout_ms=10000,restore=restore,suspend=not resuming,handoff_from_accepted=resuming)
                checkpoints.append(checkpoint);return checkpoint
            if legacy_invoice:
                from lib.tasks_pkg.native_cost_projection import NativeTaskCostPolicy
                original_row=NativeTaskCostPolicy._row
                def legacy_row(policy,*args,**kwargs):
                    row=original_row(policy,*args,**kwargs)
                    if created and policy.task_id==created[0][0]['id']:
                        row.pop('taskId',None)
                    return row
                monkeypatch.setattr(NativeTaskCostPolicy,'_row',legacy_row)
            def pricing(principal,task):
                created.append((task,task['_model_routing_group']))
                if prebind_failure:raise RuntimeError('fixture pricing authority unavailable')
                if billed_resume:
                    reserve(wallet.id,100,ref_id=task['id'],note='fixture explicit owned hold')
                    task['_billingUserId']=wallet.id;task['_billing_reservation_micro']=100
                rates={'input':2.5,'output':10} if len(created)==1 else {'input':5.,'output':20.}
                return NativeRoutePricing(principal=principal,margin=0,
                    lookup=lambda owner,model,provider,**k:({'input':1,'output':2} if native_flow and model=='beta' else rates),rates=lambda p:7.24)
            child_results=[]
            if priced_child:
                from lib.tasks_pkg.native_gateway import NativeGateway
                original_execute=NativeGateway._execute
                def delegate(gateway,request,approved):
                    if request.name=='todo_write' and not gateway.task.get('_parentTaskId') and not child_results:
                        from lib.tasks_pkg.child_invocation import create_child_invocation,run_child_invocation
                        child=create_child_invocation(gateway.task,model=gateway.task['config']['model'],tools=[],objective='priced child',
                            event_sink=lambda _event:None,abort_check=lambda:gateway.task['abort_event'].is_set())
                        child_results.append(child)
                        # Swarm/Goal agents own a worker distinct from the bounded callback pool.
                        def execute_child():
                            if priced_child_depth == 2:
                                grandchild = create_child_invocation(child,
                                    model=child['config']['model'], tools=[], objective='priced grandchild',
                                    event_sink=lambda _event:None,
                                    abort_check=lambda:gateway.task['abort_event'].is_set())
                                child_results.append(grandchild)
                                run_child_invocation(grandchild)
                                assert grandchild['status'] == 'done'
                            run_child_invocation(child)
                        with ThreadPoolExecutor(max_workers=1) as child_worker:
                            child_worker.submit(execute_child).result()
                        assert child['status']=='done'
                    return original_execute(gateway,request,approved)
                monkeypatch.setattr(NativeGateway,'_execute',delegate)
            configuration_factory=application_configuration_factory or NativeRootConfiguration
            options=configuration_factory(configuration=config,working_directory=tmp_path,
                repository_factory=lambda principal:repo,pricing_factory=pricing,image_preparation=image_preparation,
                checkpoint_factory=checkpoint_factory if journal_resume or actor_checkpoint else None,flow_scheduler=flow_library,flow_archive=flow_archive)
            runtime=None
            async def start_runtime():
                nonlocal runtime
                runtime=ServingLoopRuntime(asyncio.get_running_loop(),threading.Event(),environ={
                    'TOFU_TASK_CLEANUP_INTERVAL':'0','TOFU_EXECUTOR_IDLE_SECONDS':'0',
                    'TOFU_SYNC_WORKERS':'2','TOFU_AGENT_WORKERS':'2'}).start()
                app.extensions['tofu_serving_loop_lifecycle']={'runtime':runtime}
            async def stop_runtime():
                if runtime is not None:await runtime.stop()
            app=create_application(__name__,static_dir=str(tmp_path),logger=logging.getLogger(__name__),
                secret_key='fixture',config={'TESTING':True},native_image=image,native_root=options,
                native_token_count=installed_token_library,
                startup_handlers=[('fixture.serving',start_runtime)],shutdown_handlers=[('fixture.serving',stop_runtime)])
            if installed_token_library:
                from lib.token_counter import tiktoken_counter
                tiktoken_counter._reset_text_count_cache_for_tests()
                monkeypatch.setattr(tiktoken_counter,'_encoders',{})
                monkeypatch.setattr('tiktoken.get_encoding',lambda *a,**kw:pytest.fail('selected native context loaded Python tokenizer'))
            try:
                async with app.test_app():
                    body=dict(commandId=uuid.uuid4().hex,inputTurn={'content':'question'},config={
                        'modelRef':{'creator_id':'creator','model_id':'alpha'},'maxTokens':64,
                        'thinkingEnabled':False,'memoryEnabled':False,'projectEnabled':False,
                        'searchMode':'off','tools':{'programmaticCalling':'off'}})
                    if caller_tool_schemas is not None:
                        body['config']['tools'] = caller_tool_schemas
                        body['config']['tools.programmaticCalling'] = 'off'
                    if native_flow:
                        from lib.orchestration._definition_contract import SCHEMA_ID
                        body['config']['flowDefinition']={
                            'schema':SCHEMA_ID,'name':'Native role fixture',
                            'nodes':[{'id':'start','type':'control','kind':'start'},
                                {'id':'first','type':'role','role':'worker',
                                 'params':{'objective':'First native role','model_override':'alpha','tools':[]}},
                                {'id':'second','type':'role','role':'worker',
                                 'params':{'objective':'Second native role','model_override':flow_child_model,'tools':[]}},
                                {'id':'stop','type':'control','kind':'stop'}],
                            'edges':[{'from':'start','to':'first'},{'from':'first','to':'second'},
                                     {'from':'second','to':'stop'}]}
                    if actor_tool_round:
                        body['config'].update(projectEnabled=True,projectPath=str(tmp_path))
                        for node in body['config']['flowDefinition']['nodes']:
                            if node.get('type')=='role':node['params']['tools']=['write_file']
                    if flow_verifier_loop:
                        body['config']['preferencesEnabled']=False
                        definition=body['config']['flowDefinition']
                        definition['nodes'][2]['role']='critic'
                        definition['nodes'].append({'id':'loop','type':'control','kind':'loop','params':{'max_iterations':2}})
                        definition['edges']=[{'from':'start','to':'loop'},{'from':'loop','to':'first'},
                            {'from':'first','to':'second'},{'from':'second','to':'loop'},{'from':'loop','to':'stop'}]
                    if flow_parallel:
                        definition=body['config']['flowDefinition']
                        definition['nodes'].extend([
                            {'id':'fork','type':'control','kind':'parallel','params':{'max_concurrent':2}},
                            {'id':'join','type':'control','kind':'barrier'}])
                        definition['edges']=[{'from':'start','to':'fork'},
                            {'from':'fork','to':'first'},{'from':'fork','to':'second'},
                            {'from':'first','to':'join'},{'from':'second','to':'join'},
                            {'from':'join','to':'stop'}]
                    if flow_branch:
                        definition=body['config']['flowDefinition']
                        definition['nodes'][1]['name']='Writer'
                        definition['nodes'][2]['name']='Security Review'
                        definition['nodes'].append({'id':'branch','type':'control','kind':'branch',
                            'params':{'classifier':'worker','tier':'light'}})
                        definition['edges']=[{'from':'start','to':'branch'},
                            {'from':'branch','to':'first'},{'from':'branch','to':'second'},
                            {'from':'first','to':'stop'},{'from':'second','to':'stop'}]
                    if flow_definition is not None:
                        assert native_flow
                        body['config']['flowDefinition']=json.loads(json.dumps(flow_definition))
                        body['config']['preferencesEnabled']=False
                    client=app.test_client()
                    operation_started_ns=time.perf_counter_ns()
                    response=await client.post(f'/api/v4/conversations/{conv_id}/turns',json=body)
                    payload=await response.get_json()
                    if prebind_failure:
                        assert response.status_code==500 and payload['ok'] is False,payload
                        failed,issued=created[0]
                        attempt=await asyncio.to_thread(get_attempt,failed['_attemptId'],user_id=owner_id)
                        assert attempt['status']=='failed'
                        assert issued.disposed and '_model_routing_selection' not in failed
                        assert '_nativePreparedInvocation' not in failed
                        assert not provider.bodies and not app.extensions['tofu_native_host']._clients
                    else:
                        assert response.status_code==200 and payload['ok'] is True,payload
                        attempt_id=payload['attempt']['attemptId'];turn_id=payload['turn']['turnId']
                        expires=time.monotonic()+15
                        if resume_operation and not journal_resume:
                            try:
                                assert await asyncio.to_thread(entered.wait,5)
                                aborted=await client.post(f'/api/v4/attempts/{attempt_id}/abort',json={})
                                assert aborted.status_code==200,await aborted.get_json()
                            finally:release.set()
                        while True:
                            attempt=await asyncio.to_thread(get_attempt,attempt_id,user_id=owner_id)
                            if attempt['status'] in {'completed','failed','interrupted'}:break
                            assert time.monotonic()<expires,attempt
                            await asyncio.sleep(.02)
                        turn=await asyncio.to_thread(get_turn,conv_id,turn_id,user_id=owner_id)
                        if resume_operation:
                            assert attempt['status']==turn['status']=='interrupted',turn
                            while app.extensions['tofu_native_host']._clients:
                                assert time.monotonic()<expires,'aborted native scope did not join'
                                await asyncio.sleep(.02)
                            assert len(provider.bodies)==(2 if unknown_resume else 1+priced_child_depth if priced_child else 1)
                            assert turn['projection']['todoState']['update_count']==1
                            assert turn['projection']['usage']['total_tokens']==5
                            first_task,first_route=created[0]
                            first_replay=await asyncio.to_thread(application_storage.query,'task_results.replay_get',
                                {'key':first_task['id'],'user_id':owner_id})
                            assert first_replay['status']=='aborted',first_replay
                            assert not first_task.get('_terminalPersistencePending')
                            assert not any('rejected terminal rewrite aborted -> error' in record.getMessage()
                                for record in caplog.records)
                            assert first_task['_executionBudget'].consumption_snapshot()==(5*(1+priced_child_depth) if priced_child else 5,2,not unknown_resume)
                            if unknown_resume:
                                assert first_task['_executionActualsComplete'] is False
                                assert turn['projection'].get('usageComplete') is False,turn['projection'].keys()
                            assert first_route.disposed
                            original_price=json.loads(json.dumps(first_task['apiRounds'][0]['nativePrice']))
                            if billed_resume:
                                first_charge=sum(row['nativeCredits']['total'] for row in first_task['apiRounds'])
                                settled=settle_task(first_task,user_id=wallet.id,model='alpha',raise_on_error=True)
                                assert settled['cost_micro']==first_charge
                                assert get_balance(wallet.id)==10000-first_charge
                            resumed=await client.post(f'/api/v4/conversations/{conv_id}/turns/{turn_id}/attempts',json={
                                'commandId':uuid.uuid4().hex,'operation':resume_operation.removeprefix('unknown_').removeprefix('billing_').removeprefix('legacy_').removeprefix('journal_'),
                                'expectedProjectionRevision':turn['projectionRevision'],'config':{**body['config'],
                                    **({'checkpointUsageComplete':True} if unknown_resume else {})}})
                            reply=await resumed.get_json()
                            assert resumed.status_code==200 and reply['ok'] is True,reply
                            attempt_id=reply['attempt']['attemptId'];expires=time.monotonic()+15
                            while True:
                                attempt=await asyncio.to_thread(get_attempt,attempt_id,user_id=owner_id)
                                if attempt['status'] in {'completed','failed','interrupted'}:break
                                assert time.monotonic()<expires,attempt
                                await asyncio.sleep(.02)
                            turn=await asyncio.to_thread(get_turn,conv_id,turn_id,user_id=owner_id)
                            if unknown_resume:
                                assert attempt['status']=='failed',turn
                                assert len(provider.bodies)==2
                                assert turn['projection']['usage']['total_tokens']==5
                                assert turn['projection']['usageComplete'] is False
                                final_task=created[-1][0]
                                assert final_task['_executionBudget'].consumption_snapshot()==(5,0,False)
                            else:
                                assert turn['projection']['usage']['total_tokens']==10
                                assert len(turn['projection']['apiRounds'])==(2+priced_child_depth if priced_child else 2)
                                assert [row['round'] for row in created[-1][0]['apiRounds']]==list(range(1,(3+priced_child_depth) if priced_child else 3))
                                final_task=created[-1][0]
                                assert final_task['_executionBudget'].consumption_snapshot()[0]==(5*(2+priced_child_depth) if priced_child else 10)
                                assert final_task['apiRounds'][0]['nativePrice']==original_price
                                assert final_task['apiRounds'][-1]['nativePrice']['input']==5.
                                assert final_task['apiRounds'][-1]['nativePrice']['output']==20.
                        if not unknown_resume:
                            if journal_resume:
                                assert restored_frontiers==[1]
                            assert attempt['status']==turn['status']=='completed',turn
                            assert turn['projection']['content']==('Security Review' if flow_branch else 'durable fixture answer')
                            assert len(provider.bodies)==(flow_requests if native_flow else (2+priced_child_depth) if priced_child else 1 if scenario=='text' else 2)
                            assert turn['projection']['apiRounds']
                        # Durable completion precedes joined post-commit callbacks
                        # and native process close. Observe both boundaries.
                        while app.extensions['tofu_native_host']._clients:
                            assert time.monotonic()<expires,'native scope failed to join'
                            await asyncio.sleep(.02)
                        while True:
                            replay=await asyncio.to_thread(application_storage.query,'task_results.replay_get',
                                {'key':attempt['taskId'],'user_id':owner_id,'include_metadata':unknown_resume})
                            if not native_flow or replay['status']!='pending':break
                            # The graph parent owns no native client: child close
                            # does not join its later durable result publication.
                            assert time.monotonic()<expires,'Flow result publication failed to settle'
                            await asyncio.sleep(.02)
                        assert replay['status']==('error' if unknown_resume else 'done')
                        if scenario!='text':assert turn['projection']['todoState']['update_count']==1
                        task,issued=created[-1]
                        if caller_tool_schemas is not None:
                            names = {tool['function']['name'] for tool in caller_tool_schemas}
                            wire_names = {tool['function']['name'] for tool in provider.bodies[0]['tools']}
                            assert names <= wire_names <= names | {'search_tools', 'execute_tools'}
                        if not unknown_resume:
                            assert task.get('_nativeFrozenCostProjection') is True
                        else:
                            assert task['apiRounds'][0]['nativePrice']==original_price
                            assert replay['metadata']['usageComplete'] is False
                        while not issued.disposed:
                            assert time.monotonic()<expires,'task route failed to retire'
                            await asyncio.sleep(.02)
                        if journal_resume and not unknown_resume:
                            assert len(checkpoints)==2 and all(value.closed for value in checkpoints)
                        assert '_model_routing_selection' not in task
                        while any(value.get('_nativePreparedInvocation') is not None for value,_ in created):
                            assert time.monotonic()<expires,'owned native invocation failed to retire'
                            await asyncio.sleep(.002)
                        assert all(value.get('_nativePreparedInvocation') is None for value,_ in created)
                        if native_flow:
                            assert task['flow_mode'] and task['_executionBudget'].consumption()[0]==5*flow_requests
                            assert len(task['apiRounds'])==flow_requests
                            assert all(row['taskId']!=task['id'] and row['billableTaskId']==task['id'] for row in task['apiRounds'])
                            charge=sum(row['nativeCredits']['total'] for row in task['apiRounds'])
                            if flow_child_model=='beta':
                                # Each worker/verifier iteration retains its original paid rows.
                                for model, fee in (('alpha', 28), ('beta', 7)):
                                    rows = [row for row in task['apiRounds'] if row['model'] == model]
                                    assert len(rows) == flow_requests // 2
                                    assert all(row['nativeCredits']['total'] == fee for row in rows)
                                assert charge == 35 * (flow_requests // 2)
                            assert task['_nativeBillingSettlement']['componentsMicro']['totalMicro']==charge
                            assert settle_task(task,user_id=wallet.id,model='alpha',raise_on_error=True)['cost_micro']==charge
                            assert get_balance(wallet.id)==10000-charge
                            operation_elapsed_ns=time.perf_counter_ns()-operation_started_ns
                            assert len(provider.bodies)==flow_requests
                            if flow_branch:
                                from lib.turn_lifecycle import list_turns
                                snapshot=await asyncio.to_thread(list_turns,conv_id,user_id=owner_id)
                                visible=[row for row in snapshot['turns'] if row.get('actor')!='human']
                                assert [row['projection']['content'] for row in visible]==['Security Review','durable fixture answer']
                                assert all(row['status']=='completed' for row in visible)
                                assert len({row['turnId'] for row in visible})==2
                                assert task['content']=='durable fixture answer'
                                assert 'Routing decision' in json.dumps(provider.bodies[0])
                                assert 'Second native role' in json.dumps(provider.bodies[1])
                                assert 'First native role' not in json.dumps(provider.bodies[1])
                            assert {row['model'] for row in task['apiRounds']}=={'alpha',flow_child_model}
                            assert {body['model'] for body in provider.bodies}=={'alpha-wire-a',flow_child_model+'-wire-a'}
                        if priced_child:
                            assert len(child_results)==priced_child_depth and len(task['apiRounds'])==2+priced_child_depth
                            child=child_results[0]
                            assert {row['taskId'] for row in task['apiRounds'] if row.get('billableTaskId')}=={value['id'] for value in child_results}
                            fresh_rows=task['apiRounds'][len(task.get('_checkpointApiRounds') or []):]
                            charge=sum(row['nativeCredits']['total'] for row in fresh_rows)
                            assert task['_nativeBillingSettlement']['componentsMicro']['totalMicro']==charge
                            assert task['_nativeBillingSettlement']['roundCount']==len(fresh_rows)
                            assert task['_executionBudget'].consumption()[0]==5*(2+priced_child_depth)
                            from unittest.mock import patch
                            from lib.billing import settle as actual_child_settle
                            wallet_candidates=[]
                            def recorded_child_settle(*args,**kwargs):
                                candidate=(args,kwargs)
                                if wallet_candidates:
                                    assert candidate==wallet_candidates[0],(wallet_candidates[0],candidate)
                                wallet_candidates.append(candidate)
                                return actual_child_settle(*args,**kwargs)
                            monkeypatch.setattr('lib.billing.settle',recorded_child_settle)
                            def lost_child_ack(*args,**kwargs):
                                recorded_child_settle(*args,**kwargs)
                                raise OSError('child wallet acknowledgement lost')
                            with patch('lib.billing.settle',side_effect=lost_child_ack):
                                with pytest.raises(OSError,match='acknowledgement lost'):
                                    settle_task(task,user_id=wallet.id,model='alpha',raise_on_error=True)
                            settled=settle_task(task,user_id=wallet.id,model='alpha',raise_on_error=True)
                            assert settled['cost_micro']==charge
                            assert get_balance(wallet.id)==10000-(first_charge if resume_operation else 0)-charge
                            from lib.billing.native_settlement import recover
                            assert recover(wallet_user_id=wallet.id,task_id=task['id'],
                                held_micro=task['_billing_reservation_micro'])
                            assert get_balance(wallet.id)==10000-(first_charge if resume_operation else 0)-charge
                            assert len(provider.bodies)==2+priced_child_depth
                        if billed_resume and not priced_child and not native_flow:
                            second_charge=task['apiRounds'][1]['nativeCredits']['total']
                            assert task['apiRounds'][0]['taskId']==first_task['id']
                            assert task['apiRounds'][1]['taskId']==task['id']!=first_task['id']
                            assert task['_nativeBillingSettlement']['roundCount']==1
                            assert task['_nativeBillingSettlement']['componentsMicro']['totalMicro']==second_charge
                            from unittest.mock import patch
                            from lib.billing import settle as actual_settle
                            def lost_ack(*args,**kwargs):
                                actual_settle(*args,**kwargs)
                                raise OSError('wallet committed acknowledgement lost')
                            with patch('lib.billing.settle',side_effect=lost_ack):
                                with pytest.raises(OSError,match='acknowledgement lost'):
                                    settle_task(task,user_id=wallet.id,model='alpha',raise_on_error=True)
                            settled=settle_task(task,user_id=wallet.id,model='alpha',raise_on_error=True)
                            assert settled['cost_micro']==second_charge
                            assert get_balance(wallet.id)==10000-first_charge-second_charge
                    additional_completion = (await after_completion(app,provider,created,owner_id,checkpoints,ordinary_provider_post)
                                             if after_completion is not None else None)
                assert app.extensions['tofu_native_application'].closed and image._closed
                if installed_token_library:
                    counter=app.extensions['tofu_native_host']._token_counter
                    assert counter is not None and counter._state=='closed'
                    assert counter._image.closed and counter._library is None
                if flow_archive:
                    owned_library=app.extensions['tofu_native_host']._flow_scheduler
                    assert owned_library is not None and owned_library._closed
                    assert owned_library._fd is None and owned_library._library is None
                destination=os.environ.get('TOFU_NATIVE_APPLICATION_RECEIPTS')
                if destination:
                    path=Path(destination);path.mkdir(parents=True,exist_ok=True)
                    backend=os.environ['TOFU_PERSONAL_STORAGE_BACKEND']
                    transport=os.environ.get('TOFUDB_TRANSPORT','daemon')
                    receipt=dict(scope='ordinary_v4_application_native_root',image_preparation=image_preparation,backend=backend,
                        transport=transport,scenario=scenario,prebind_failure=prebind_failure,resume_operation=resume_operation,
                        provider_requests=len(provider.bodies),attempt_status=attempt['status'],
                        request_counter_owner='native model control' if not prebind_failure else 'no dispatch',
                        selected_route_retired=issued.disposed,application_closed=True,
                        archive_sha256=image.sha256,performance_admitted=False,default_selection_admitted=False)
                    if additional_completion is not None:
                        receipt['additional_application_completion'] = additional_completion
                    if native_flow:
                        receipt.update(host_flow_archive=flow_archive,branch_scenario=flow_branch,native_branch_policy=flow_branch and (flow_archive or (flow_library is not None and getattr(flow_library._library,"tofu_flow_branch_choose",None) is not None)),native_flow=True,native_graph_scheduler=native_flow_kernel,operation_elapsed_ns=operation_elapsed_ns,parallel=flow_parallel,child_model=flow_child_model,role_requests=flow_requests,tree_tokens=5*flow_requests,native_verdict_policy=flow_verifier_loop,native_loop_policy=flow_verifier_loop and getattr(flow_library._library,'tofu_flow_loop_open',None) is not None,first_verdict=flow_first_verdict if flow_verifier_loop else None,
                            quotes_by_model=sorted((row['model'],row['nativeCredits']['total']) for row in task['apiRounds']),
                            wallet_charge=charge,wallet_balance=get_balance(wallet.id),
                            original_child_quotes_preserved=True)
                    if priced_child:
                        receipt.update(child_task_ids=[value['id'] for value in child_results],paid_rounds=2+priced_child_depth,
                            cumulative_tree_tokens=5*(2+priced_child_depth),total_micro=charge,
                            final_balance=get_balance(wallet.id),lost_wallet_ack_idempotent=True,
                            wallet_debits=([first_charge,charge] if resume_operation else [charge]),
                            durable_capsule_recovery_idempotent=True,children_joined=True)
                    if billed_resume and not priced_child and not native_flow:
                        receipt.update(wallet_debits=[first_charge,second_charge],
                            final_balance=get_balance(wallet.id),
                            producer_metadata_bytes_per_round=len(json.dumps({'taskId':task['id']},separators=(',',':')).encode())-1,
                            retained_fee_not_recharged=True,lost_wallet_ack_idempotent=True,
                            legacy_invoice_origin_recovered=legacy_invoice)
                    if journal_resume:
                        receipt.update(journal_handoff=True,restored_frontiers=restored_frontiers,
                            checkpoint_policies_closed=all(value.closed for value in checkpoints),
                            stable_epoch=checkpoints[0].epoch==checkpoints[1].epoch,
                            completed_snapshot_json_bytes=checkpoint_sizes,restored_tree_sizes=restored_tree_sizes,
                            source_and_target_task_ids_distinct=first_task['id']!=task['id'])
                    if unknown_resume:
                        receipt.update(attempts=2,usage_complete=False,paid_token_lower_bound=5,
                            unknown_usage_refused_before_dispatch=True,
                            forged_complete_config_did_not_override_authority=True)
                    elif resume_operation:
                        receipt.update(attempts=2,first_task_replay=first_replay['status'],
                            paid_tokens=[first_task['_executionBudget'].consumption_snapshot()[0],
                                final_task['_executionBudget'].consumption_snapshot()[0]],
                            paid_rounds=[row['round'] for row in final_task['apiRounds']],
                            old_price_preserved=final_task['apiRounds'][0]['nativePrice']==original_price,
                            new_input_price=final_task['apiRounds'][-1]['nativePrice']['input'],
                            checklist_updates=turn['projection']['todoState']['update_count'])
                    (path/(backend+'-'+transport+'-'+scenario+(('-flow-parallel' if flow_parallel else '-flow-sequential')+'-'+flow_child_model+('-native-graph' if native_flow_kernel else '') if native_flow else '')+('-verifier-loop'+('-unresolved-stop' if 'still failing' in flow_first_verdict else '') if flow_verifier_loop else '')+('-image-prepared' if image_preparation else '')+('-refused' if prebind_failure else '')+('-'+resume_operation if resume_operation else '')+'.json')).write_text(
                        json.dumps(receipt,indent=2)+'\n')
            finally:
                release.set()
                provider.close()
    try:
        asyncio.run(run())
    finally:
        if flow_library is not None:
            assert not flow_library._graphs
            flow_library.close()
