"""A remote observer controls the existing owner; delivery is not settlement."""
import asyncio
import json
import struct
import threading
import time

import pytest

pytestmark = pytest.mark.unit


def test_control_round_trip_checks_incarnation_owner_and_exact_call():
    from lib.tasks_pkg.command_control_transport import CommandControlServer, call, CommandControlUnavailable
    calls = []
    def apply(method, task_id, *, user_id, deadline_unix_ms, **params):
        assert deadline_unix_ms > 0
        calls.append((method, task_id, user_id, params))
        return {'backgrounded': False, 'reason': 'handoff_pending'}
    async def run():
        server = CommandControlServer('incarnation', apply)
        endpoint = await server.start()
        try:
            result = await asyncio.to_thread(call, endpoint, 'background', 'task', user_id=41,
                params={'round_num': 2, 'tool_call_id': 'exact'})
            assert result == {'backgrounded': False, 'reason': 'handoff_pending'}
            for changed in ({'token': 'wrong'}, {'serviceId': 'old'}):
                with pytest.raises(CommandControlUnavailable):
                    await asyncio.to_thread(call, {**endpoint, **changed}, 'interrupt', 'task', user_id=41)
            assert calls == [('background', 'task', 41, {'round_num': 2, 'tool_call_id': 'exact'})]
        finally:
            await server.close()
        assert not server.active_count
    asyncio.run(run())


@pytest.mark.parametrize('changed', [
    {'ownerId': True}, {'ownerId': 0}, {'method': 'kill'}, {'taskId': ''},
    {'deadlineUnixMs': 0}, {'params': {'unexpected': True}}, {'extra': True},
])
def test_invalid_or_expired_wire_request_never_reaches_the_owner(changed):
    from lib.tasks_pkg.command_control_transport import CommandControlServer
    async def run():
        server = CommandControlServer('incarnation', lambda *args, **kwargs: pytest.fail('invalid control entered'))
        endpoint = await server.start()
        reader, writer = await asyncio.open_connection(endpoint['host'], endpoint['port'])
        try:
            request = dict(format=endpoint['format'], serviceId=endpoint['serviceId'],
                           token=endpoint['token'], requestId='exact', ownerId=41,
                           method='interrupt', taskId='task', params={},
                           deadlineUnixMs=int(time.time() * 1000) + 2000)
            request.update(changed)
            encoded = json.dumps(request).encode()
            writer.write(struct.pack('!I', len(encoded)) + encoded)
            await writer.drain()
            length = struct.unpack('!I', await reader.readexactly(4))[0]
            response = json.loads(await reader.readexactly(length))
            assert response['result'] is None
            assert response['error'] in {'control_refused', 'control_unavailable'}
        finally:
            writer.close()
            await writer.wait_closed()
            await server.close()
    asyncio.run(run())


def test_listener_shutdown_joins_entered_callback_and_closes_admission():
    from lib.tasks_pkg.command_control_transport import CommandControlServer, call, CommandControlUnavailable
    entered, release = threading.Event(), threading.Event()
    def apply(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return {'interrupted': True, 'pid': 123}
    async def run():
        server = CommandControlServer('incarnation', apply)
        endpoint = await server.start()
        request = asyncio.create_task(asyncio.to_thread(call, endpoint, 'interrupt', 'task', user_id=41))
        assert await asyncio.to_thread(entered.wait, 5)
        close = asyncio.create_task(server.close())
        await asyncio.sleep(0)
        assert not close.done()
        try:
            with pytest.raises(CommandControlUnavailable):
                await asyncio.to_thread(call, endpoint, 'interrupt', 'task', user_id=41)
        finally:
            release.set()
        assert await request == {'interrupted': True, 'pid': 123}
        await close
        assert server.active_count == 0
    asyncio.run(run())


@pytest.mark.parametrize('result', [{}, {'interrupted': 'yes'}, {'backgrounded': True}])
def test_remote_response_requires_the_requested_operation_result(result):
    from lib.tasks_pkg.command_control_transport import CommandControlServer, call, CommandControlUnavailable
    async def run():
        server = CommandControlServer('incarnation', lambda *args, **kwargs: result)
        endpoint = await server.start()
        try:
            with pytest.raises(CommandControlUnavailable):
                await asyncio.to_thread(call, endpoint, 'interrupt', 'task', user_id=41)
        finally:
            await server.close()
    asyncio.run(run())


def test_repeated_close_cancellation_still_joins_the_callback():
    from lib.tasks_pkg.command_control_transport import CommandControlServer, call
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    def apply(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        exited.set()
        return {'interrupted': True, 'pid': 123}
    async def run():
        server = CommandControlServer('incarnation', apply)
        endpoint = await server.start()
        request = asyncio.create_task(asyncio.to_thread(call, endpoint, 'interrupt', 'task', user_id=41))
        assert await asyncio.to_thread(entered.wait, 5)
        close = asyncio.create_task(server.close())
        try:
            for _ in range(3):
                await asyncio.sleep(0)
                close.cancel()
            await asyncio.sleep(0)
            assert not close.done()
            assert not exited.is_set()
        finally:
            release.set()
            await request
            with pytest.raises(asyncio.CancelledError):
                await close
        assert exited.is_set()
        assert server.active_count == 0
    asyncio.run(run())
