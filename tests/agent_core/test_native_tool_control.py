"""Concurrent native callbacks retain exact results and joined ownership."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lib.agent_core.native_process import NativeProcessClient, NativeProcessError, _joined
from lib.agent_core.native_tool_control import NativeToolControl
from tests.agent_core.test_native_process import budget, principal

pytestmark = pytest.mark.unit


def control(handler, *, broken_writer=False):
    client = NativeProcessClient(binary=Path('/does-not-exist'), sha256='0' * 64,
        protocol_version='fixture/v1', principal=principal(), budget=budget())
    frames = []
    class Writer:
        def write(self, data):
            if broken_writer:
                raise BrokenPipeError('injected transport fault')
            frames.append(json.loads(data))
        async def drain(self):
            await asyncio.sleep(0)
    process = SimpleNamespace(stdin=Writer(), stdout=asyncio.StreamReader())
    group = NativeToolControl(client, process, dict(task_id='task', tool_frame_bytes=4096), {'read': handler}, None)
    return group, frames


def submit(group, i):
    group.submit(dict(request_id=str(i), name='read', arguments={}, model_call_id=f'original-{i}'))


def test_eight_callbacks_return_in_reverse_order_with_exact_identity():
    async def run():
        entered = asyncio.Event()
        releases = [asyncio.Event() for _ in range(8)]
        entered_ids = []
        async def read(request):
            entered_ids.append(request.model_call_id)
            if len(entered_ids) == 8:
                entered.set()
            await releases[int(request.request_id)].wait()
            return request.model_call_id
        group, frames = control(read)
        for i in range(8): submit(group, i)
        try:
            await asyncio.wait_for(entered.wait(), 2)
            for i in reversed(range(8)):
                releases[i].set()
                await group.pending[str(i)].work
            await group.settle()
        finally:
            for release in releases: release.set()
            await group.close()
            await group.client.close()
        assert [f['request_id'] for f in frames] == list(map(str, reversed(range(8))))
        assert all(f['content'] == 'original-' + f['request_id'] for f in frames)
        assert not group.pending and not group.client._tool_callbacks
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['exception', 'self_cancel', 'invalid_result', 'self_close'])
def test_callback_failure_preserves_successful_sibling_and_releases_registry(failure):
    async def run():
        entered = asyncio.Event()
        async def read(request):
            if request.request_id == '1':
                entered.set()
                return 'successful sibling'
            await entered.wait()
            if failure == 'exception': raise ValueError('injected callback fault')
            if failure == 'self_cancel': raise asyncio.CancelledError()
            if failure == 'self_close': await group.client.close()
            return object()
        group, frames = control(read)
        submit(group, 0); submit(group, 1)
        await asyncio.wait_for(group.settle(), 2)
        await group.close()
        assert group.failure is not None
        assert {f['request_id']: f.get('failed', False) for f in frames} == {'0': True, '1': False}
        assert next(f for f in frames if f['request_id'] == '1')['content'] == 'successful sibling'
        assert not group.pending and not group.client._tool_callbacks
        await group.client.close()
    asyncio.run(run())


def test_stop_signals_every_callback_and_repeated_cancellation_still_joins():
    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()
        seen = []
        stopped = []
        async def read(request):
            seen.append(request.request_id)
            if len(seen) == 8: entered.set()
            await request.cancelled.wait()
            stopped.append(request.request_id)
            await release.wait()
            return 'joined-' + request.request_id
        group, frames = control(read)
        for i in range(8): submit(group, i)
        await asyncio.wait_for(entered.wait(), 2)
        close = asyncio.create_task(group.close())
        join = asyncio.create_task(_joined(close))
        try:
            for _ in range(10):
                if len(stopped) == 8: break
                await asyncio.sleep(0)
            assert len(stopped) == 8 and not close.done()
            join.cancel(); await asyncio.sleep(0); join.cancel()
        finally:
            release.set()
            await join
        assert not group.pending and not group.client._tool_callbacks
        assert sum(f['type'] == 'tool_result' for f in frames) == 8
        await group.client.close()
    asyncio.run(run())


def test_reply_transport_failure_interrupts_silent_reader_and_still_joins():
    async def run():
        async def read(_request): return 'finished'
        group, _ = control(read, broken_writer=True)
        submit(group, 0)
        with pytest.raises(BrokenPipeError):
            await asyncio.wait_for(group.readline(), 2)
        await group.close()
        assert not group.pending and not group.client._tool_callbacks
        await group.client.close()
    asyncio.run(run())


def test_duplicate_active_request_is_rejected_without_detaching_original():
    async def run():
        async def read(request):
            await request.cancelled.wait()
            return 'joined'
        group, frames = control(read)
        submit(group, 0)
        with pytest.raises(NativeProcessError): submit(group, 0)
        await group.close()
        assert sum(f['type'] == 'tool_result' for f in frames) == 1
        assert not group.client._tool_callbacks
        await group.client.close()
    asyncio.run(run())


@pytest.mark.parametrize('names', [['foreign'], ['read', 'read'], [False], 'read'])
def test_invalid_parallel_qualification_refuses_before_binary_acquisition(names):
    from lib.agent_core.native_process import NativeConfigurationError
    async def run():
        async def read(_request): pytest.fail('invalid qualification reached a tool')
        group, _ = control(read)
        settings = dict(version='fixture/v1', type='configure', owner_user_id=7, task_id='task',
            tools={'read': {'permission': 'read'}}, parallel_tool_names=names)
        with pytest.raises(NativeConfigurationError, match='parallel tools'):
            await group.client.run(settings, callbacks={'read': read}, working_directory=Path('/tmp'))
        assert group.client._image is None and group.client.active_runs == 0
        await group.client.close()
    asyncio.run(run())
