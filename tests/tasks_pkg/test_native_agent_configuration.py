"""Trusted SDK composition rejects request authority and blocking-loop lanes."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from lib.agent_core.native_process import NativeConfigurationError
from lib.tasks_pkg.native_host import NativeHost
from tests.tasks_pkg.test_native_host import image
from tests.tasks_pkg.test_native_application import profile
from tofu_agent import AgentRuntime,NativeAgentConfiguration,AgentConfigurationError
from tests.model_routing.test_native_route import principal

pytestmark=pytest.mark.unit


def configure(loop,executor,configuration):
    return NativeAgentConfiguration(host=NativeHost(image()),loop=loop,executor=executor,
        configuration=configuration,working_directory=Path('/tmp'),resource_policy={'frame_maximum':65536},
        pending_calls=8,pending_bytes=65536)


@pytest.mark.parametrize('field',['endpoint','headers','template','task_id','owner_user_id','epoch','checkpoint_control','checkpoint_child_routes'])
def test_request_or_durable_authority_refuses_before_image_acquisition(field):
    async def run():
        with ThreadPoolExecutor(max_workers=1) as executor:
            value=profile();value['model_control']['pricing']=False;value[field]={}
            with pytest.raises(NativeConfigurationError):configure(asyncio.get_running_loop(),executor,value)
    asyncio.run(run())


def test_same_loop_sync_operations_refuse_before_admission():
    async def run():
        with ThreadPoolExecutor(max_workers=1) as executor:
            value=profile();value['model_control']['pricing']=False
            native=configure(asyncio.get_running_loop(),executor,value)
            runtime=AgentRuntime(principal=principal(),max_inflight=1,native_execution=native)
            try:
                with pytest.raises(AgentConfigurationError,match='async SDK'):
                    runtime.run([{'role':'user','content':'hello'}])
                with pytest.raises(AgentConfigurationError,match='async SDK'):
                    next(runtime.stream([{'role':'user','content':'hello'}]))
                assert runtime.in_flight==0 and not runtime._executions
                assert native.host.image._image is None
            finally:await runtime.close_async();await native.host.close()
    asyncio.run(run())


def test_native_public_result_and_close_wait_for_worker_join(monkeypatch):
    from concurrent.futures import Future
    from lib.agent_core.admission import fire_terminal_callbacks
    from tests.tasks_pkg.test_public_agent_runtime import _access,_patch_route_slot
    from tofu_agent import AgentTimeoutError
    async def run():
        _patch_route_slot(monkeypatch)
        worker=Future()
        def spawn(task,*,executor):
            task.update(status='done',content='settled',finishReason='stop')
            fire_terminal_callbacks(task['id'])  # projected terminal precedes physical join
            return worker
        monkeypatch.setattr('lib.tasks_pkg.spawn.spawn_task',spawn)
        with ThreadPoolExecutor(max_workers=1) as executor:
            value=profile();value['model_control']['pricing']=False
            native=configure(asyncio.get_running_loop(),executor,value)
            runtime=AgentRuntime(principal=principal(),max_inflight=1,model_routing=_access(),native_execution=native)
            execution=runtime.start([{'role':'user','content':'hello'}])
            try:
                assert execution.status=='done' and not execution.done and runtime.in_flight==1
                with pytest.raises(AgentConfigurationError,match='async SDK'):
                    execution.result()
                with pytest.raises(AgentTimeoutError,match='not joined'):
                    await runtime.close_async(timeout_s=0)
                assert runtime.in_flight==1 and not execution._terminal.is_set()
                worker.set_result(None)
                result=await execution.result_async(timeout_s=1)
                assert result.content=='settled' and execution.done and runtime.in_flight==0
            finally:
                if not worker.done():worker.set_result(None)
                await runtime.close_async();await native.host.close()
    asyncio.run(run())
