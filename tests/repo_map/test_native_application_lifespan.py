"""Application composition owns repository images through joined shutdown."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading

import pytest
from test_native_extraction_stream import stream as stream, assert_joined
from test_native_extraction_owned import blocked_sources, observe_first_ast
from test_native_application_scanner import observe_installed_images
from tests.tasks_pkg.test_native_application import profile, task
from tests.tasks_pkg.test_native_host import image
from tests.model_routing.test_native_route import Repository

pytestmark = pytest.mark.unit


def composition(root, configuration):
    from lib.app.app_factory import create_base_app
    from lib.tasks_pkg.native_application import NativeRootConfiguration, register_native_application
    from lib.tasks_pkg.native_host import register_native_host
    from lib.tasks_pkg.native_pricing import NativeRoutePricing
    app = create_base_app(__name__)
    register_native_host(app, image=image())
    options = NativeRootConfiguration(configuration=profile(),working_directory=root,
        repository_factory=lambda principal:Repository(),
        pricing_factory=lambda principal,task:NativeRoutePricing(principal=principal,margin=0),
        native_repository=configuration)
    return app, register_native_application(app,configuration=options)


def serving(app, executor):
    app.extensions['tofu_serving_loop_lifecycle'] = {'runtime':SimpleNamespace(
        loop=asyncio.get_running_loop(),sync_executor=executor,shutdown_requested=threading.Event(),
        resource_controller=SimpleNamespace(launch_policy={'frame_maximum':4096}))}


def test_application_instances_own_distinct_lazy_images_and_refuse_foreign_selection(stream):
    from tofu_agent import NativeRepositoryConfiguration
    from lib.agent_core.native_process import NativeConfigurationError
    scanner, root = stream
    configuration = NativeRepositoryConfiguration(str(scanner.grammar_directory),scanner.limits.arguments())
    async def exercise():
        app, binding = composition(root,configuration)
        other_app, other = composition(root,configuration)
        assert binding.repository_runtime is not other.repository_runtime
        with ThreadPoolExecutor(max_workers=1) as executor:
            async with other_app.test_app():
                async with app.test_app():
                    serving(app,executor)
                    value = task(); value['_repository_runtime'] = other.repository_runtime
                    with pytest.raises(NativeConfigurationError,match='repository owner differs'):
                        binding.bind_task(value)
                    assert '_nativePreparedInvocation' not in value
                    assert binding.repository_runtime.retained_image_bytes() == 0
                assert binding.repository_runtime._closed and not other.repository_runtime._closed
        assert other.repository_runtime._closed
    asyncio.run(exercise())


def test_cancelled_application_shutdown_joins_blocked_scan_and_keeps_one_close_task(stream,monkeypatch):
    from tofu_agent import NativeRepositoryConfiguration
    from lib.agent_core.execution_session import ExecutionSession,ExecutionPhase
    from lib.repo_map.native_runtime import scanner_for_task
    from tofu_repo_kernel._process_discovery import DiscoveryCancelled
    scanner, root = stream
    blocked_sources(root); entered = observe_first_ast(monkeypatch)
    images = observe_installed_images(monkeypatch)
    children = []
    from tofu_repo_kernel._stream import StreamExecutable
    original_start = StreamExecutable.start
    def start(self,*args,**kwargs):
        process=original_start(self,*args,**kwargs);children.append(process);return process
    monkeypatch.setattr(StreamExecutable,'start',start)
    configuration = NativeRepositoryConfiguration(str(scanner.grammar_directory),scanner.limits.arguments())
    async def exercise():
        app,binding = composition(root,configuration)
        close_entered,release_close = threading.Event(),threading.Event()
        close_calls = []
        original_close = binding.repository_runtime.close
        def close():
            close_calls.append(True);close_entered.set()
            assert release_close.wait(5)
            original_close()
        monkeypatch.setattr(binding.repository_runtime,'close',close)
        with ThreadPoolExecutor(max_workers=1) as executor:
            async with app.test_app():
                serving(app,executor)
                value=task();value['abort_event']=threading.Event()
                session=ExecutionSession(execution_id=value['id'],kind='repository-lifespan',owner_user_id=value['_userId'])
                value['_executionSession']=session
                binding.bind_task(value)
                port=scanner_for_task(value,root)
                running=asyncio.create_task(asyncio.to_thread(port,root))
                closing=None
                try:
                    assert await asyncio.to_thread(entered.wait,5)
                    closing=asyncio.create_task(binding.close())
                    assert await asyncio.to_thread(close_entered.wait,5)
                    for _ in range(3):
                        closing.cancel();await asyncio.sleep(0);await asyncio.sleep(0)
                        assert not closing.done()
                    release_close.set()
                    await asyncio.wait_for(closing,5)
                    with pytest.raises(DiscoveryCancelled):await running
                    assert close_calls==[True]
                    assert len(images)==1 and images[0]._image.closed
                    assert binding.repository_runtime.retained_image_bytes()==0
                finally:
                    release_close.set()
                    if closing is not None:await asyncio.gather(closing,return_exceptions=True)
                    await binding.close()
                    await asyncio.gather(running,return_exceptions=True)
                    session.settle(ExecutionPhase.CANCELLED)
                assert value['_repository_scanner_closed'] and '_repository_scanner_binding' not in value
    asyncio.run(exercise())
    assert_joined(children)


@pytest.mark.parametrize('fault', ['none','parent_id','parent_port','parent_owner','foreign_child','runtime_type'])
def test_native_child_borrows_only_issued_parent_runtime_before_scope_acquisition(tmp_path,fault):
    import json
    from lib.repo_map.native_runtime import NativeRepositoryRuntime
    from lib.agent_core.native_process import NativeConfigurationError
    from tests.tasks_pkg.test_native_invocation import binding
    from tests.model_routing.test_native_route import selection
    from tests.model_routing.test_native_issued_route import group
    runtime=NativeRepositoryRuntime(tmp_path,(8<<20,8<<20,32<<20,16<<20))
    async def exercise():
        with ThreadPoolExecutor(max_workers=1) as executor:
            port=binding(asyncio.get_running_loop(),executor);port._transient=True
            port._profile=json.dumps(dict(json.loads(port._profile),terminal_persistence={'attempts':2,'ack_ms':5000}))
            issued=group(Repository())
            child={'id':'child','_parentTaskId':'epoch','status':'pending','_userId':7,'_transientRuntime':True,
                   '_model_routing_group':issued,'_model_routing_selection':selection(),
                   '_pinned_provider_id':issued.pin_id,'config':{}}
            parent={'id':'epoch','_userId':7,'_nativePreparedInvocation':port,'_repository_runtime':runtime,
                    '_repository_scanner_binding':object()}
            if fault=='parent_id':parent['id']='foreign'
            elif fault=='parent_port':parent['_nativePreparedInvocation']=object()
            elif fault=='parent_owner':parent['_userId']=8
            elif fault=='foreign_child':child['_repository_runtime']=object()
            elif fault=='runtime_type':parent['_repository_runtime']=object()
            try:
                if fault=='none':
                    port.bind_transient_child(child,parent_task_id='epoch',parent_task=parent)
                    assert child['_repository_runtime'] is runtime
                    assert '_repository_scanner_binding' not in child
                else:
                    with pytest.raises(NativeConfigurationError,match='repository requires its issued parent'):
                        port.bind_transient_child(child,parent_task_id='epoch',parent_task=parent)
                    assert '_nativePreparedInvocation' not in child
                assert runtime.retained_image_bytes()==0 and port.host.image._image is None
            finally:
                await port.host.close()
    try:asyncio.run(exercise())
    finally:runtime.close()
