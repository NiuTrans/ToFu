"""Real-owner concurrency: productive expansion, draining, lazy lifecycle."""
import asyncio
from dataclasses import replace
import threading
from types import SimpleNamespace

import pytest

from lib.agent_core.worker_executor import RecoverableAgentExecutor
from lib.resource_control import (ResourceController, ResourceLease, ResourceOwner,
    ResourcePort, ResourceSample, install_controller, uninstall_controller)

pytestmark = pytest.mark.unit


def test_worker_demand_starts_immediately_and_idle_resize_keeps_jobs():
    first_started, second_started, release = (threading.Event() for _ in range(3))
    pool = RecoverableAgentExecutor(max_workers=1, queue_capacity=4,
                                   max_abandoned_workers=1, thread_name_prefix='resource-test')
    controller = ResourceController()
    controller.bind('execution.root', ResourcePort(
        lambda: ResourceSample(**pool.resource_snapshot()), pool.resize, 1, 4))
    try:
        first = pool.submit(lambda: (first_started.set(), release.wait(3)))
        assert first_started.wait(1)
        second = pool.submit(lambda: (second_started.set(), release.wait(3)))
        assert second_started.wait(1), 'submitted work must not wait for controller feedback'
        assert controller.snapshot()['resources']['execution.root']['waiting'] == 0
        controller.apply(controller.plan({'execution.root': 1}))
        assert not first.done() and not second.done()
        assert pool.resource_snapshot()['used'] == 2
        release.set()
        first.result(2)
        second.result(2)
        assert pool.resource_snapshot()['used'] == 0
        assert pool.resource_snapshot()['completed'] == 2
    finally:
        release.set()
        controller.close()
        pool.shutdown(wait=True, cancel_futures=True)


def test_model_accounting_cannot_reintroduce_a_request_ceiling():
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    from lib.llm_dispatch.slot import Slot
    dispatcher = LLMDispatcher()
    dispatcher._initialized = True
    slot = Slot(key_name='synthetic', api_key='test', model='synthetic', capabilities={'text'})
    dispatcher.slots = [slot]
    dispatcher.resize_resource_capacity(2)
    assert dispatcher.pick_top_n(1) == [slot]
    assert dispatcher.pick_top_n(1) == [slot]
    dispatcher.resize_resource_capacity(1)
    assert dispatcher.pick_top_n(1) == [slot]
    assert dispatcher.resource_snapshot()['used'] == 3
    assert dispatcher.pick_top_n(1) == [slot]
    for _ in range(4):
        slot.release()
    assert dispatcher.resource_snapshot()['used'] == 0


def test_storage_pool_drains_only_idle_and_released_sessions():
    from lib.storage.v2_client import _SessionPool
    class Session:
        closed = False
        def close(self):
            self.closed = True
    pool = _SessionPool(lambda _timeout: Session(), size=2)
    try:
        first, second = pool.acquire(1), pool.acquire(1)
        pool.resize(1)
        assert not first.closed and not second.closed
        assert pool.resource_snapshot()['used'] == 2
        pool.release(first, broken=False)
        assert not first.closed and not second.closed
        assert pool.resource_snapshot()['idle'] == 1
        pool.release(second, broken=False)
        assert pool.resource_snapshot()['used'] == 0
        assert pool.resource_snapshot()['completed'] == 2
        assert not first.closed and second.closed
    finally:
        pool.close()


def test_response_bytes_remain_owned_while_draining():
    from lib.storage.frame_admission import FrameByteAdmission
    frames = FrameByteAdmission(capacity_bytes=100)
    frames.acquire(80, timeout_s=1)
    frames.resize(50)
    assert frames.metrics()['frame_bytes_inflight'] == 80
    frames.release(80)
    assert frames.metrics()['frame_bytes_inflight'] == 0


def test_failed_release_can_be_retried():
    calls = []
    def release():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError('injected before owner release')
    lease = ResourceLease('execution.child', ResourceOwner(7, 'task'), release)
    with pytest.raises(RuntimeError):
        lease.release()
    lease.release()
    assert lease.release() is False
    assert len(calls) == 2


def test_sampler_owned_by_serving_loop(monkeypatch):
    from lib.server.server_loop_runtime import ServingLoopRuntime
    from lib.resource_control import current_controller
    import lib.resource_control.runtime as control_runtime
    called = asyncio.Event()
    async def sample(runtime):
        called.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(control_runtime, 'run_controller', sample)
    async def exercise():
        runtime = ServingLoopRuntime(asyncio.get_running_loop(), threading.Event(),
            environ={'TOFU_AGENT_WORKERS': '2', 'TOFU_SYNC_WORKERS': '2'}).start()
        try:
            await called.wait()
            controller, task = current_controller(), runtime.resource_task
            assert controller is runtime.resource_controller
        finally:
            await runtime.stop()
        assert task.cancelled() and current_controller() is None
        assert controller.snapshot()['closed']
    asyncio.run(exercise())


def test_lazy_bind_initializes_owner_once_under_race():
    controller = ResourceController()
    identity, calls = object(), []
    def make_port():
        calls.append(1)
        return ResourcePort(lambda: ResourceSample(2), lambda target: None, 1, 4)
    threads = [threading.Thread(target=controller.bind_lazy,
        args=('model.requests', identity, make_port)) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert calls == [1]


def test_race_single_candidate_releases_before_regular_dispatch(monkeypatch):
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    from lib.llm_dispatch.slot import Slot
    import lib.llm_dispatch._api_multi as multi
    slot = Slot(key_name='test', api_key='test', model='synthetic', capabilities={'text'})
    dispatcher = LLMDispatcher()
    dispatcher._initialized = True
    dispatcher.slots = [slot]
    dispatcher.resize_resource_capacity(1)
    monkeypatch.setattr(multi, 'get_dispatcher', lambda: dispatcher)
    def dispatch(*args, **kwargs):
        assert slot.inflight == 0
        return 'complete', {}
    monkeypatch.setattr(multi, 'dispatch_chat', dispatch)
    assert multi.dispatch_fastest([]) == ('complete', {})
    assert slot.inflight == 0
    slot.record_request()
    # Busy resources are handed to the existing cancel-aware waiting path.
    monkeypatch.setattr(multi, 'dispatch_chat', lambda *a, **kw: ('queued', {}))
    assert multi.dispatch_fastest([]) == ('queued', {})
    assert slot.inflight == 1
    slot.release()


def test_race_interruption_releases_every_reservation(monkeypatch):
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    from lib.llm_dispatch.slot import Slot
    import lib.llm_dispatch._api_multi as multi
    import lib.llm as llm
    dispatcher = LLMDispatcher()
    dispatcher._initialized = True
    dispatcher.slots = [Slot(key_name=f'test-{i}', api_key='test', model='synthetic',
                             capabilities={'text'}) for i in range(2)]
    monkeypatch.setattr(multi, 'get_dispatcher', lambda: dispatcher)
    def fail(**kw):
        raise KeyboardInterrupt('synthetic cancellation')
    monkeypatch.setattr(llm, 'chat', fail)
    with pytest.raises(KeyboardInterrupt):
        multi.dispatch_fastest([])
    assert all(slot.inflight == 0 for slot in dispatcher.slots)


def test_metrics_expose_declared_aggregate_resources_only():
    from routes.metrics import _collect_resource_metrics
    controller = ResourceController()
    controller.bind('execution.root', ResourcePort(
        lambda: ResourceSample(3, used=2, blocked=7, errors=2, pressure=True),
        lambda _: None, 1, 8))
    install_controller(controller)
    try:
        lines = []
        _collect_resource_metrics(lines)
        assert 'tofu_resource_used{resource="execution.root",unit="count"} 2' in lines
        assert 'tofu_resource_effective{resource="execution.root",unit="count"} 3' in lines
        assert 'tofu_resource_blocked{resource="execution.root",unit="count"} 7' in lines
        assert 'tofu_resource_errors{resource="execution.root",unit="count"} 2' in lines
        assert 'tofu_resource_pressure{resource="execution.root",unit="count"} 1' in lines
    finally:
        uninstall_controller(controller)


def test_native_storage_budget_is_authoritative():
    from lib.storage.v2_client import StorageV2Client
    client = StorageV2Client(host='127.0.0.1', port=1, secret=b't' * 32, owner_id=7, tenant_id=None, session_pool_size=2)
    try:
        client.set_readiness_budget({'maximumConnections': 8})
        client.resize_resource_capacity(3)
        assert client.resource_snapshot()['capacity'] == 3
        assert client.resource_snapshot()['ceiling'] == 4
        with pytest.raises(ValueError):
            client.resize_resource_capacity(5)
        assert client.resource_snapshot()['capacity'] == 3
    finally:
        client.close()


def test_frame_growth_and_native_pool_ports_do_not_open_connections():
    from lib.storage.v2_client import StorageV2Client
    from lib.storage.frame_admission import FrameByteAdmission
    from lib.resource_control.adapters import bind_storage
    controller = ResourceController()
    controller.launch_policy = {'automatic': True, 'frame_maximum': 200}
    client = StorageV2Client('127.0.0.1', 1, b't' * 32, owner_id=7, tenant_id=None,
                            response_frame_admission=FrameByteAdmission(capacity_bytes=100))
    client.set_readiness_budget({'maximumConnections': 8})
    install_controller(controller)
    try:
        bind_storage(client)
        assert set(controller.snapshot()['resources']) == {'storage.rpc', 'storage.frames'}
        plan = controller.plan({'storage.rpc': 2, 'storage.frames': 150})
        applied = controller.apply(plan)['resources']
        assert applied['storage.frames']['effective'] == 150
        assert applied['storage.rpc']['effective'] == 2
        assert applied['storage.rpc']['used'] == 0
        with pytest.raises(ValueError):
            controller.plan({'storage.rpc': 5})
    finally:
        uninstall_controller(controller)
        client.close()


def test_memory_observation_combines_reclaimable_cgroup_and_host(monkeypatch):
    import lib.cgroup_guard as guard
    import lib.resource_control.runtime as runtime
    monkeypatch.setattr(guard, 'process_rss_admission_snapshot', lambda: {
        'headroom_bytes': 1000, 'reserve_bytes': 100, 'allowed': True})
    monkeypatch.setattr(guard, 'pressure', lambda: {
        'limit': 1000, 'usage': 950, 'reclaimable': 400})
    monkeypatch.setattr(runtime, 'observe_host_memory_available_bytes', lambda: 2000)
    assert runtime.memory_observation() == (False, 450)
    monkeypatch.setattr(runtime, 'observe_host_memory_available_bytes', lambda: 50)
    assert runtime.memory_observation() == (True, 50)


def test_controller_sampler_runs_feedback_and_survives_probe_failure(monkeypatch):
    import lib.resource_control.runtime as runtime
    class Controller:
        def snapshot(self):
            return {'revision': 1, 'resources': {}}
        def tick(self, **kw):
            assert kw == {'memory_pressure': False, 'headroom_bytes': 123, 'allow_growth': True}
            stopped.set()
            calls.append('tick')
            return self.snapshot()
    stopped, calls = threading.Event(), []
    def memory():
        calls.append('memory')
        if len(calls) == 1:
            raise OSError('synthetic kernel counter failure')
        return False, 123
    async def sleep(_):
        return
    monkeypatch.setattr(runtime, 'memory_observation', memory)
    monkeypatch.setattr(runtime.asyncio, 'sleep', sleep)
    log = SimpleNamespace(warning=lambda *args: calls.append('warning'))
    asyncio.run(runtime.run_controller(SimpleNamespace(
        shutdown_requested=stopped, resource_controller=Controller(), logger=log)))
    assert calls == ['memory', 'warning', 'memory', 'tick']


def test_disabled_root_admission_is_not_reenabled_by_pool_resize():
    from lib.resource_control.adapters import bind_root
    from lib.agent_core.admission import AdmissionController
    pool = RecoverableAgentExecutor(max_workers=2, queue_capacity=4, max_abandoned_workers=1)
    controller = ResourceController()
    controller.launch_policy = {'automatic': True, 'root_maximum': 4, 'task_bytes': 64}
    admission = AdmissionController(max_inflight=0)
    try:
        bind_root(controller, SimpleNamespace(agent_executor=pool), admission)
        controller.apply(controller.plan({'execution.root': 3}))
        assert pool.resource_snapshot()['capacity'] == 3
        assert admission.resource_snapshot()['capacity'] == 0
        controller.apply(controller.plan({'execution.root': 1}))
        assert admission.resource_snapshot()['capacity'] == 0
    finally:
        controller.close()
        pool.shutdown(wait=True)


def test_feedback_probe_cannot_block_http_event_loop(monkeypatch):
    import lib.resource_control.runtime as runtime
    started, release, stopped = threading.Event(), threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    def memory():
        assert threading.get_ident() != loop_thread
        started.set()
        assert release.wait(2), 'event loop was blocked by the probe'
        return False, 123
    class Controller:
        def snapshot(self):
            assert threading.get_ident() != loop_thread
            return {'revision': 1, 'resources': {}}
        def tick(self, **kwargs):
            stopped.set()
            return self.snapshot()
    monkeypatch.setattr(runtime, 'memory_observation', memory)
    monkeypatch.setattr(runtime, 'catalog_document', lambda: {'control': {'sample_seconds': 0}})
    async def scenario():
        worker = asyncio.create_task(runtime.run_controller(SimpleNamespace(
            shutdown_requested=stopped, resource_controller=Controller(),
            logger=SimpleNamespace(warning=lambda *args: None))))
        try:
            while not started.is_set():
                await asyncio.sleep(0.001)
            # An HTTP handler/heartbeat gets the loop even during slow I/O.
            await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(worker, 2)
        finally:
            release.set()
            stopped.set()
            if not worker.done():
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(scenario())


def test_cancelled_feedback_cannot_apply_a_late_adjustment(monkeypatch):
    import lib.resource_control.runtime as runtime
    started, release, finished, stopped = (threading.Event() for _ in range(4))
    calls = []
    def memory():
        started.set()
        assert release.wait(2)
        return False, 123
    class Controller:
        def snapshot(self):
            finished.set()
            return {'revision': 1, 'resources': {}}
        def tick(self, **kwargs):
            calls.append('late adjustment')
            return self.snapshot()
    monkeypatch.setattr(runtime, 'memory_observation', memory)
    monkeypatch.setattr(runtime, 'catalog_document', lambda: {'control': {'sample_seconds': 0}})
    async def scenario():
        worker = asyncio.create_task(runtime.run_controller(SimpleNamespace(
            shutdown_requested=stopped, resource_controller=Controller(),
            logger=SimpleNamespace(warning=lambda *args: None))))
        try:
            while not started.is_set():
                await asyncio.sleep(0.001)
            worker.cancel()
            with pytest.raises(asyncio.CancelledError):
                await worker
            release.set()
            while not finished.is_set():
                await asyncio.sleep(0.001)
        finally:
            release.set()
            stopped.set()
            await asyncio.gather(worker, return_exceptions=True)
    asyncio.run(scenario())
    assert calls == []


def test_frame_control_reports_ram_without_counting_anonymous_file_bytes():
    from lib.storage.v2_client import StorageV2Client
    from lib.storage.frame_admission import FrameByteAdmission
    from lib.resource_control.adapters import bind_storage
    controller = ResourceController()
    controller.launch_policy = {'automatic': True, 'frame_maximum': 200}
    frames = FrameByteAdmission(capacity_bytes=100)
    memory = frames.reserve_frame(80)
    disk = frames.reserve_frame(80)
    client = StorageV2Client('127.0.0.1', 1, b't' * 32, owner_id=7, tenant_id=None,
                            response_frame_admission=frames)
    install_controller(controller)
    try:
        bind_storage(client)
        sample = controller.snapshot()['resources']['storage.frames']
        assert sample['used'] == 80
        assert sample['waiting'] == 0
        assert frames.metrics()['frame_bytes_inflight'] == 160
    finally:
        memory.close(); disk.close()
        uninstall_controller(controller)
        client.close()
