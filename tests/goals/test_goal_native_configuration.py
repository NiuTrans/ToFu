"""Only trusted application policy exposes native pending-Goal recovery."""
from types import SimpleNamespace

import pytest

from lib.goal_runs.native_recovery import NativeGoalRecoveryConfiguration, recovery_options

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('value', [False, 0, -1, 1.5, '1000', 1<<64])
def test_recovery_bounds_reject_inexact_or_nonpositive_values(value):
    with pytest.raises(ValueError):
        NativeGoalRecoveryConfiguration(evidence_bytes=value)


def test_defaults_scale_with_issued_transport_and_explicit_overrides_remain():
    storage = SimpleNamespace(request_timeout_seconds=7.0001)
    for size in (4096, 1<<20):
        transport = SimpleNamespace(frame_bytes=size, configuration_bytes=size*4)
        bounds = NativeGoalRecoveryConfiguration().bounds(transport, storage=storage)
        assert bounds == dict(header_bytes=size, snapshot_bytes=size, identity_bytes=size,
                              evidence_bytes=size*4, io_timeout_ms=7001)
        explicit=NativeGoalRecoveryConfiguration(snapshot_bytes=123,io_timeout_ms=91).bounds(transport,storage=object())
        assert explicit['snapshot_bytes']==123 and explicit['io_timeout_ms']==91


def test_large_host_uses_storage_document_capacity_without_capping_other_transport():
    from tofu_db.generated_checkpoint_control import CHECKPOINT_DOCUMENT_MAX_BYTES
    size = CHECKPOINT_DOCUMENT_MAX_BYTES * 4096
    transport = SimpleNamespace(frame_bytes=size, configuration_bytes=size * 2)
    bounds = NativeGoalRecoveryConfiguration().bounds(transport,
        storage=SimpleNamespace(request_timeout_seconds=31))
    assert bounds['header_bytes'] == bounds['snapshot_bytes'] == CHECKPOINT_DOCUMENT_MAX_BYTES
    assert bounds['identity_bytes'] == size and bounds['evidence_bytes'] == CHECKPOINT_DOCUMENT_MAX_BYTES
    assert transport.frame_bytes == size and bounds['io_timeout_ms'] == 31000
    for field in ('header_bytes', 'snapshot_bytes', 'evidence_bytes'):
        with pytest.raises(ValueError, match='checkpoint document capacity'):
            NativeGoalRecoveryConfiguration(**{field: CHECKPOINT_DOCUMENT_MAX_BYTES + 1})


@pytest.mark.parametrize('seconds',[None,False,0,-1,float('inf'),float('nan'),'5',1<<64,1<<1024,((1<<64)-1)/1000])
def test_missing_or_invalid_selected_io_policy_refuses_before_claim(seconds):
    with pytest.raises(ValueError,match='storage I/O policy'):
        NativeGoalRecoveryConfiguration().bounds(SimpleNamespace(frame_bytes=1,configuration_bytes=1),
            storage=SimpleNamespace(request_timeout_seconds=seconds))


@pytest.mark.parametrize('kind',['sql','native','embedded'])
def test_storage_timeout_is_read_only_and_requires_no_transport_acquisition(kind):
    from unittest.mock import Mock
    from lib.storage.client import StorageClient
    from lib.storage.v2_client import StorageV2Client
    from lib.storage.v2_embedded_client import EmbeddedStorageV2Client
    if kind=='sql':
        client=StorageClient('127.0.0.1',1,'s'*48,timeout=2.75)
    elif kind=='native':
        client=StorageV2Client('127.0.0.1',1,b's'*48,owner_id=7,tenant_id=None,timeout=2.75)
    else:
        client=EmbeddedStorageV2Client(Mock(),b's'*48,owner_id=7,tenant_id=None,timeout=2.75)
    try:
        assert client.request_timeout_seconds==2.75
        with pytest.raises(AttributeError):client.request_timeout_seconds=99
        bounds=NativeGoalRecoveryConfiguration().bounds(SimpleNamespace(frame_bytes=4096,configuration_bytes=8192),storage=client)
        assert bounds['io_timeout_ms']==2750
    finally:
        close=getattr(client,'close',None)
        if close:close()


def test_request_flags_and_unissued_ports_cannot_enable_pending_recovery():
    assert recovery_options({'_nativePreparedInvocation': object(),
                             'config': {'goal_recovery': True}}) == {}


def test_application_requires_actor_checkpoint_configuration():
    from lib.agent_core.native_process import NativeConfigurationError
    from lib.tasks_pkg.native_application import NativeRootConfiguration
    from tests.tasks_pkg.test_native_application import profile
    from pathlib import Path
    with pytest.raises(NativeConfigurationError, match='actor checkpoint'):
        NativeRootConfiguration(configuration=profile(), working_directory=Path('/tmp'),
            goal_recovery=NativeGoalRecoveryConfiguration(), checkpoint_factory=lambda *args: None)


def test_goal_bridge_refuses_its_own_bounded_callback_worker_before_storage():
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from functools import partial
    import json
    from unittest.mock import Mock
    from lib.agent_core.native_process import NativeConfigurationError
    from lib.goal_runs.native_recovery import execute_pending_goal_flow
    from tests.tasks_pkg.test_native_invocation import binding

    async def run():
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=1) as callbacks:
            port = binding(loop, callbacks)
            storage = Mock()
            port.goal_recovery = NativeGoalRecoveryConfiguration(storage_factory=storage)
            port.checkpoint_factory = Mock()
            port._profile = json.dumps(dict(json.loads(port._profile), checkpoint_actor_account=True,
                                             checkpoint_shared_account=True))
            seed = Mock()
            task = {'_nativePreparedInvocation': port, '_goalRecoverySeed': seed}
            with pytest.raises(NativeConfigurationError, match='separate bounded worker'):
                await asyncio.wait_for(loop.run_in_executor(callbacks, partial(
                    execute_pending_goal_flow, task, {}, None, journal_factory=Mock(),
                    on_event=Mock(), execution_kwargs={})), timeout=2)
            storage.assert_not_called()
            seed.inspect_pending_inputs.assert_not_called()
    asyncio.run(run())


def test_serving_loop_cancellation_keeps_the_task_cancellation_type():
    import asyncio
    from lib.agent_core.abort_signal import AbortSignal
    from lib.goal_runs.native_recovery import _on_loop

    async def cancelled():
        raise asyncio.CancelledError()

    async def run():
        port = SimpleNamespace(loop=asyncio.get_running_loop())
        with pytest.raises(asyncio.CancelledError):
            await asyncio.to_thread(_on_loop, port, cancelled(), AbortSignal())
    asyncio.run(run())
