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
    {'method': 'swarm_abort', 'taskId': ''}, {'method': 'swarm_agent_stop'},
    {'method': 'swarm_agent_stop', 'params': {'agent_id': ''}},
    {'method': 'background_activity'},  # An owner method names no task.
    {'method': 'background_activity', 'taskId': '', 'params': {'agent_id': 'a'}},
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


@pytest.mark.parametrize('method,task_id,result', [
    ('interrupt', 'task', {}), ('interrupt', 'task', {'interrupted': 'yes'}),
    ('interrupt', 'task', {'backgrounded': True}),
    ('swarm_abort', 'swarm', {'aborted': 'yes'}), ('swarm_abort', 'swarm', {'stopped': True}),
    ('swarm_agent_stop', 'run', {'stopped': True, 'status': 3}),
    ('swarm_agent_stop', 'run', {'stopped': False, 'pid': 9}),
    # An owner view is never "no such task", and each list holds rows.
    ('background_activity', '', None), ('background_activity', '', {'swarms': []}),
    ('background_activity', '', {'swarms': [1], 'backgroundCommands': []}),
    # Process metrics are never "no such task"; every series is complete and typed.
    ('storage_rpc_metrics', '', None), ('storage_rpc_metrics', '', {'swarms': []}),
    ('storage_rpc_metrics', '', {'histograms': [{'labels': {}, 'buckets': [1.0], 'counts': [1]}]}),
    ('storage_rpc_metrics', '', {'histograms': [{'labels': {'kind': 3}, 'buckets': [1.0],
                                                 'counts': [1], 'sum': 0.5, 'count': 1}]}),
    ('storage_rpc_metrics', '', {'histograms': [{'labels': {}, 'buckets': [1.0, 2.0],
                                                 'counts': [1], 'sum': 0.5, 'count': 1}]}),
    ('storage_rpc_metrics', '', {'histograms': [{'labels': {}, 'buckets': [1.0],
                                                 'counts': [-1], 'sum': 0.5, 'count': 1}]}),
])
def test_remote_response_requires_the_requested_operation_result(method, task_id, result):
    from lib.tasks_pkg.command_control_transport import CommandControlServer, call, CommandControlUnavailable
    params = {'agent_id': 'a'} if method == 'swarm_agent_stop' else {}
    async def run():
        server = CommandControlServer('incarnation', lambda *args, **kwargs: result)
        endpoint = await server.start()
        try:
            with pytest.raises(CommandControlUnavailable):
                await asyncio.to_thread(call, endpoint, method, task_id, user_id=41, params=params)
        finally:
            await server.close()
    asyncio.run(run())


def test_swarm_controls_and_owner_view_round_trip_exact_arguments():
    from lib.tasks_pkg.command_control_transport import CommandControlServer, call
    calls = []
    results = {'swarm_abort': {'aborted': True},
               'swarm_agent_stop': {'stopped': False, 'reason': 'not_running', 'status': 'pending'},
               'background_activity': {'swarms': [{'swarm_id': 'run'}], 'backgroundCommands': []}}
    def apply(method, task_id, *, user_id, deadline_unix_ms, **params):
        calls.append((method, task_id, user_id, params))
        return results[method]
    async def run():
        server = CommandControlServer('incarnation', apply)
        endpoint = await server.start()
        try:
            for method, task_id, params in (('swarm_abort', 'swarm', {}),
                                            ('swarm_agent_stop', 'run', {'agent_id': 'a'}),
                                            ('background_activity', '', {})):
                assert await asyncio.to_thread(call, endpoint, method, task_id, user_id=41,
                                               params=params) == results[method]
        finally:
            await server.close()
    asyncio.run(run())
    assert calls == [('swarm_abort', 'swarm', 41, {}),
                     ('swarm_agent_stop', 'run', 41, {'agent_id': 'a'}),
                     ('background_activity', '', 41, {})]


def test_execution_storage_histogram_round_trips_from_the_owning_process():
    """The real control owner answers with this process's storage RPC series."""
    from lib import observability
    from lib.tasks_pkg.command_control_transport import CommandControlServer, call
    from lib.tasks_pkg.command_controls import apply_command_control
    store = observability._STORE
    with store.lock:  # shared with neighbors on this xdist worker
        saved = dict(store.histograms)
        store.histograms.clear()
    observability.record_storage_rpc('query', 'turn.get', 0.003)
    observability.record_storage_rpc('query', 'turn.get', 0.2)
    observability.record_storage_rpc('command', 'turn.event.record', 0.04, outcome='error')

    async def run():
        server = CommandControlServer('incarnation', apply_command_control)
        endpoint = await server.start()
        try:
            return await asyncio.to_thread(call, endpoint, 'storage_rpc_metrics', '',
                                           user_id=41, params={})
        finally:
            await server.close()
    try:
        result = asyncio.run(run())
    finally:
        with store.lock:
            store.histograms.clear()
            store.histograms.update(saved)
    rows = {(row['labels']['kind'], row['labels']['operation'], row['labels']['outcome']): row
            for row in result['histograms']}
    assert set(rows) == {('query', 'turn.get', 'success'), ('command', 'turn.event.record', 'error')}
    read = rows[('query', 'turn.get', 'success')]
    assert read['count'] == 2 and read['sum'] == pytest.approx(0.203)
    assert dict(zip(read['buckets'], read['counts']))[0.005] == 1
    assert read['counts'][-1] == 2 and read['counts'] == sorted(read['counts'])


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
