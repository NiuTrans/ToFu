"""Application Flow admission is lazy, serialized and joined before code release."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from unittest.mock import Mock,patch
import pytest
from lib.agent_core.native_process import NativeConfigurationError
from lib.tasks_pkg.native_host import NativeHost
from lib.tasks_pkg.native_application import NativeRootConfiguration
from tests.tasks_pkg.test_native_host import image
from tests.tasks_pkg.test_native_application import profile

pytestmark=pytest.mark.unit


@pytest.mark.parametrize('options',[{'flow_archive':1},{'flow_archive':'yes'},
    {'flow_archive':True,'flow_scheduler':object()}])
def test_archive_selection_requires_boolean_and_one_owner(options):
    with pytest.raises(NativeConfigurationError):
        NativeRootConfiguration(configuration=profile(),working_directory=Path('/tmp'),**options)


def test_host_admission_is_lazy_and_shared_across_preparation_workers():
    async def run():
        runtime=Mock()
        with patch('lib.orchestration.native_flow.NativeFlowLibrary.from_shared_archive',return_value=runtime) as load:
            host=NativeHost(image());load.assert_not_called()
            with ThreadPoolExecutor(max_workers=4) as workers:
                assert list(workers.map(lambda _:host.flow_scheduler(),range(12)))==[runtime]*12
            load.assert_called_once_with(image=host.image)
            await host.close();runtime.close.assert_called_once()
            with pytest.raises(NativeConfigurationError):host.flow_scheduler()
    asyncio.run(run())


def test_shutdown_joins_admission_in_progress_and_refuses_late_work():
    async def run():
        entered,release=threading.Event(),threading.Event();runtime=Mock()
        def load(**kw):entered.set();assert release.wait(5);return runtime
        with patch('lib.orchestration.native_flow.NativeFlowLibrary.from_shared_archive',side_effect=load):
            host=NativeHost(image())
            pending=asyncio.create_task(asyncio.to_thread(host.flow_scheduler))
            assert await asyncio.to_thread(entered.wait,5)
            closed=asyncio.create_task(host.close())
            try:
                for _ in range(10):await asyncio.sleep(0)
                assert host._closed and not closed.done()
                runtime.close.assert_not_called()
            finally:release.set()
            assert await pending is runtime
            await closed;runtime.close.assert_called_once()
            with pytest.raises(NativeConfigurationError):host.flow_scheduler()
    asyncio.run(run())


def test_failed_admission_keeps_no_scheduler_or_fallback():
    async def run():
        with patch('lib.orchestration.native_flow.NativeFlowLibrary.from_shared_archive',side_effect=ValueError('invalid archive')) as load:
            host=NativeHost(image())
            with pytest.raises(ValueError,match='invalid archive'):host.flow_scheduler()
            assert host._flow_scheduler is None
            await host.close();load.assert_called_once()
    asyncio.run(run())


def test_failed_graph_retirement_remains_visible_to_application_shutdown():
    async def run():
        runtime=Mock();runtime.close.side_effect=RuntimeError('graph still borrowed')
        with patch('lib.orchestration.native_flow.NativeFlowLibrary.from_shared_archive',return_value=runtime):
            host=NativeHost(image());host.flow_scheduler()
            with pytest.raises(BaseExceptionGroup,match='resources did not close') as caught:
                await host.close()
            assert str(caught.value.exceptions[0])=='graph still borrowed'
            with pytest.raises(NativeConfigurationError):host.flow_scheduler()
    asyncio.run(run())


@pytest.mark.parametrize('flow',[False,True])
def test_archive_policy_loads_only_for_flow_and_keeps_failed_binding_unpublished(flow):
    from types import SimpleNamespace
    from lib.app.app_factory import create_base_app
    from lib.tasks_pkg.native_host import register_native_host
    from lib.tasks_pkg.native_application import register_native_application
    from lib.tasks_pkg.native_pricing import NativeRoutePricing
    from tests.tasks_pkg.test_native_application import task,principal,Repository
    async def run():
        app=create_base_app(__name__);host=register_native_host(app,image=image())
        options=NativeRootConfiguration(configuration=profile(),working_directory=Path('/tmp'),
            repository_factory=lambda _:Repository(),pricing_factory=lambda *a:NativeRoutePricing(principal=principal(),margin=0),flow_archive=True)
        binding=register_native_application(app,configuration=options)
        with ThreadPoolExecutor(max_workers=1) as callbacks:
            async with app.test_app():
                app.extensions['tofu_serving_loop_lifecycle']={'runtime':SimpleNamespace(loop=asyncio.get_running_loop(),sync_executor=callbacks,
                    shutdown_requested=threading.Event(),resource_controller=SimpleNamespace(launch_policy={'frame_maximum':4096}))}
                value=task()
                with patch('lib.orchestration.native_flow.NativeFlowLibrary.from_shared_archive',side_effect=ValueError('invalid archive')) as load:
                    if flow:
                        with pytest.raises(ValueError,match='invalid archive'):binding.bind_task(value,flow=True)
                        assert '_nativePreparedInvocation' not in value
                        load.assert_called_once()
                    else:
                        binding.bind_task(value)
                        assert value['_nativePreparedInvocation'].flow_scheduler is None
                        load.assert_not_called()
        assert host._closed
    asyncio.run(run())
