"""Prepared native entry reuses owner/context and joins route acquisition."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import threading
from unittest.mock import AsyncMock,patch
import pytest
import json
from lib.agent_core.native_process import TerminalRequest
from lib.agent_core.abort_signal import AbortSignal
from lib.tasks_pkg.native_prepared import run_native_prepared_task
from lib.tasks_pkg.orchestrator._root_agent_loop import RootLoopRequest
from tests.model_routing.test_native_route import Repository,principal,selection

pytestmark=pytest.mark.unit


def prepared():
    task={'id':'prepared','_userId':7,'status':'running','config':{'model':'fixture'},'abort_event':AbortSignal()}
    return RootLoopRequest(task=task,state=SimpleNamespace(model='fixture'),messages=[{'role':'user','content':'question'}],
        tool_list=[],all_search_results_text=[],cfg=task['config'],tid='prepared',thinking_depth=None,
        temperature=None,max_tokens=512,response_format=None,project_path='',project_enabled=False,search_enabled=False)


@pytest.mark.parametrize('cited',[False,True])
def test_prepared_projection_reuses_source_policy_before_terminal_commit(cited):
    async def run():
        request=prepared();request.all_search_results_text.extend([
            'URL: https://example.test/page#part','URL: https://example.test/page'])
        client=SimpleNamespace(principal=principal(),protocol_version='tofu.native-agent-stdio/v1')
        content='answer https://example.test/page' if cited else 'answer'
        async def execute(client,route,configuration,**kwargs):
            assert kwargs['gateway'].search_results is request.all_search_results_text
            candidate=TerminalRequest(principal(),'prepared','commit',1,json.dumps({
                'type':'done','status':'done','result':{'messages':[{'role':'assistant','content':content}],'usage':[]}}).encode(),
                b'{"invariants_satisfied":true}')
            kwargs['project_terminal'](request.task,candidate)
            assert request.task['content'].count('https://example.test/page')==1
            assert ('**来源 / Sources**' in request.task['content']) is not cited
            assert request.task['messages'][-1]['content']==content
            return 'committed'
        with ThreadPoolExecutor(max_workers=1) as executor,patch('lib.tasks_pkg.native_prepared.run_native_task',side_effect=execute):
            assert await run_native_prepared_task(client,Repository(),selection(),{},request=request,
                executor=executor,working_directory=Path('/tmp'),pending_calls=8,pending_bytes=65536)=='committed'
    asyncio.run(run())


@pytest.mark.parametrize('tools',[[],None])
def test_prepared_context_catalog_route_and_resource_owners_are_reused(tools):
    async def run():
        request=prepared();request.tool_list=tools
        repo=Repository();profile={'epoch':'epoch','deadline_ms':None,'rounds':None}
        client=SimpleNamespace(principal=principal(),protocol_version='tofu.native-agent-stdio/v1')
        captured=[]
        async def execute(client,route,configuration,**kwargs):
            public,_=route.consume(client.principal)
            assert public['owner_user_id']==7
            assert kwargs['task'] is request.task
            assert configuration['messages'] is request.messages
            assert configuration['owner_user_id']==7 and configuration['task_id']=='prepared'
            assert kwargs['callbacks']=={}
            assert 'total_tool_calls' in configuration and configuration['total_tool_calls'] is None
            assert configuration['retain_execution_history'] is True
            captured.append(kwargs['gateway'])
            assert captured[-1].native_catalog()=={}
            return 'completed'
        with ThreadPoolExecutor(max_workers=1) as executor,patch('lib.tasks_pkg.native_prepared.run_native_task',side_effect=execute):
            assert await run_native_prepared_task(client,repo,selection(),profile,request=request,
                executor=executor,working_directory=Path('/tmp'),pending_calls=8,pending_bytes=65536)=='completed'
        assert profile=={'epoch':'epoch','deadline_ms':None,'rounds':None}
        assert len(repo.secret_calls)==1
        assert captured[0]._closed and captured[0].round_source is None
    asyncio.run(run())


def test_foreign_or_overridden_preparation_refuses_before_repository_access():
    async def run():
        request=prepared();repo=Repository();client=SimpleNamespace(principal=principal(),protocol_version='tofu.native-agent-stdio/v1')
        with ThreadPoolExecutor(max_workers=1) as executor,patch('lib.tasks_pkg.native_prepared.run_native_task',new_callable=AsyncMock) as execute:
            async def attempt(profile):
                return await run_native_prepared_task(client,repo,selection(),profile,request=request,
                    executor=executor,working_directory=Path('/tmp'),pending_calls=8,pending_bytes=65536)
            request.task['_userId']=8
            with pytest.raises(ValueError):await attempt({})
            request.task['_userId']=7
            for key in ['messages','tools','owner_user_id','task_id','endpoint','headers','template']:
                with pytest.raises(ValueError):await attempt({key:'foreign'})
            request.cfg={'model':'different'}
            with pytest.raises(ValueError):await attempt({})
            execute.assert_not_awaited()
        assert repo.secret_calls==[]
    asyncio.run(run())


def test_invalid_automatic_resources_refuse_before_route_or_gateway_acquisition():
    from lib.agent_core.native_process import Budget
    async def run():
        repo=Repository()
        client=SimpleNamespace(principal=principal(),protocol_version='tofu.native-agent-stdio/v1',
            budget=Budget(1,1<<20,65536,131072,2))
        with ThreadPoolExecutor(max_workers=1) as executor,patch('lib.tasks_pkg.native_prepared.NativeGateway') as gateway:
            with pytest.raises(ValueError):
                await run_native_prepared_task(client,repo,selection(),{},request=prepared(),
                    executor=executor,working_directory=Path('/tmp'),pending_calls=8,pending_bytes=65536,
                    resource_policy={'frame_maximum':0})
            gateway.assert_not_called()
        assert repo.secret_calls==[]
    asyncio.run(run())


@pytest.mark.parametrize('fail',[False,True])
def test_cancellation_joins_route_compilation_and_closes_late_private_route(fail):
    async def run():
        entered=threading.Event();release=threading.Event();closed=threading.Event()
        request=prepared();client=SimpleNamespace(principal=principal(),protocol_version='tofu.native-agent-stdio/v1')
        def compile(*args,**kwargs):
            entered.set();assert release.wait(3)
            if fail:raise OSError('route read unavailable')
            return SimpleNamespace(close=closed.set)
        with ThreadPoolExecutor(max_workers=1) as executor,patch('lib.tasks_pkg.native_prepared.compile_native_application_route',side_effect=compile),patch('lib.tasks_pkg.native_prepared.run_native_task',new_callable=AsyncMock) as execute:
            work=asyncio.create_task(run_native_prepared_task(client,object(),selection(),{},request=request,
                executor=executor,working_directory=Path('/tmp'),pending_calls=8,pending_bytes=65536))
            try:
                assert await asyncio.to_thread(entered.wait,2)
                work.cancel();await asyncio.sleep(0)
                assert not work.done()
            finally:release.set()
            with pytest.raises(asyncio.CancelledError):await work
            assert closed.is_set()==(not fail)
            execute.assert_not_awaited()
        assert request.task['status']=='running'
    asyncio.run(run())


def test_route_acquisition_preserves_call_context_and_cleanup_failure_keeps_primary():
    from contextvars import ContextVar
    marker=ContextVar('native-prepared-test-marker',default=None)
    async def run():
        request=prepared();client=SimpleNamespace(principal=principal(),protocol_version='tofu.native-agent-stdio/v1')
        def compile(*args,**kwargs):
            assert marker.get()=='owned'
            return SimpleNamespace(close=lambda:(_ for _ in ()).throw(OSError('close unavailable')))
        primary=RuntimeError('execution failed')
        token=marker.set('owned')
        try:
            with ThreadPoolExecutor(max_workers=1) as executor,patch('lib.tasks_pkg.native_prepared.compile_native_application_route',side_effect=compile),patch('lib.tasks_pkg.native_prepared.run_native_task',new_callable=AsyncMock,side_effect=primary):
                with pytest.raises(RuntimeError) as caught:
                    await run_native_prepared_task(client,object(),selection(),{},request=request,
                        executor=executor,working_directory=Path('/tmp'),pending_calls=8,pending_bytes=65536)
                assert caught.value is primary
                assert primary.__notes__==['native prepared route cleanup failed: OSError']
        finally:marker.reset(token)
    asyncio.run(run())


def test_prepared_native_execution_binds_the_tasks_existing_route_group():
    from tests.model_routing.test_native_issued_route import group
    async def run():
        repo=Repository();issued=group(repo,1);request=prepared()
        request.task.update(_model_routing_group=issued,_pinned_provider_id=issued.pin_id)
        client=SimpleNamespace(principal=principal(),protocol_version='tofu.native-agent-stdio/v1')
        async def execute(client,route,configuration,**kwargs):
            metadata,_=route.consume(client.principal)
            assert metadata['provider_id']=='provider-b'
            return 'committed'
        with ThreadPoolExecutor(max_workers=1) as executor,patch(
                'lib.tasks_pkg.native_prepared.run_native_task',side_effect=execute):
            assert await run_native_prepared_task(client,repo,selection(),{},request=request,
                executor=executor,working_directory=Path('/tmp'),pending_calls=8,pending_bytes=65536)=='committed'
        assert not issued.disposed
    asyncio.run(run())


@pytest.mark.parametrize('invalid',['pin','disposed','missing'])
def test_prepared_invalid_issued_route_refuses_before_gateway_or_execution(invalid):
    from tests.model_routing.test_native_issued_route import group
    async def run():
        repo=Repository();issued=group(repo);request=prepared()
        request.task.update(_model_routing_group=issued,_pinned_provider_id=issued.pin_id)
        if invalid=='pin':request.task['_pinned_provider_id']='other'
        elif invalid=='disposed':issued.disposed=True
        else:request.task['_model_routing_group']=None
        client=SimpleNamespace(principal=principal(),protocol_version='tofu.native-agent-stdio/v1')
        with ThreadPoolExecutor(max_workers=1) as executor,patch(
                'lib.tasks_pkg.native_prepared.NativeGateway') as gateway,patch(
                'lib.tasks_pkg.native_prepared.run_native_task',new_callable=AsyncMock) as execute:
            with pytest.raises(ValueError,match='issued route differs'):
                await run_native_prepared_task(client,repo,selection(),{},request=request,
                    executor=executor,working_directory=Path('/tmp'),pending_calls=8,pending_bytes=65536)
            gateway.assert_not_called();execute.assert_not_awaited()
        assert not repo.secret_calls
    asyncio.run(run())


@pytest.mark.parametrize('profile,declared,mutation',[
    ({},False,{}), ({'checkpoint_control':{}},True,{}),
    ({'checkpoint_binding':{}},True,{}), ({},True,{'_turnId':'turn'}),
    ({},True,{'_billingUserId':'wallet'}),
])
def test_transient_preparation_refuses_durable_ports_before_route_acquisition(profile,declared,mutation):
    async def run():
        request=prepared();request.task.update(_transientRuntime=True,**mutation)
        client=SimpleNamespace(principal=principal(),protocol_version='tofu.native-agent-stdio/v1')
        with ThreadPoolExecutor(max_workers=1) as executor,patch(
                'lib.tasks_pkg.native_prepared.compile_native_application_route') as acquire:
            with pytest.raises(ValueError,match='task or context differs'):
                await run_native_prepared_task(client,Repository(),selection(),profile,request=request,
                    executor=executor,working_directory=Path('/tmp'),pending_calls=8,pending_bytes=65536,
                    transient=declared)
            acquire.assert_not_called()
    asyncio.run(run())


def test_explicit_transient_preparation_passes_selected_lifetime_to_terminal_owner():
    async def run():
        request=prepared();request.task['_transientRuntime']=True
        client=SimpleNamespace(principal=principal(),protocol_version='tofu.native-agent-stdio/v1')
        with ThreadPoolExecutor(max_workers=1) as executor,patch(
                'lib.tasks_pkg.native_prepared.run_native_task',new_callable=AsyncMock) as execute:
            execute.return_value='accepted'
            assert await run_native_prepared_task(client,Repository(),selection(),{},request=request,
                executor=executor,working_directory=Path('/tmp'),pending_calls=8,pending_bytes=65536,
                transient=True)=='accepted'
            assert execute.call_args.kwargs['transient'] is True
            assert execute.call_args.kwargs['task'] is request.task
    asyncio.run(run())
