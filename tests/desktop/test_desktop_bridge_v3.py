"""Negotiated device admission, deadline replay and owner-scoped wake behavior."""
import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from lib.desktop import bridge
from lib.desktop_agent._runtime import ExecutionBudget, RuntimeState, execution_budget, validate_poll_response
from lib.desktop_wire import contract, limit, protocol_version

pytestmark = pytest.mark.unit


@pytest.fixture
def state():
    clock = [0.0]
    runtime = RuntimeState(threading.Event(), budget=ExecutionBudget(2, 4, 8, 4096, 8, 4096),
                           clock=lambda: clock[0])
    runtime.observe_server_time(1000)
    return runtime, clock


@pytest.fixture
def queue(monkeypatch):
    monkeypatch.setattr(bridge, 'command_queue', {})
    monkeypatch.setattr(bridge, '_agents', {})
    monkeypatch.setattr(bridge, '_streams', {})
    monkeypatch.setattr(bridge, '_async_waiters', [])
    monkeypatch.setattr(bridge, '_last_poll', [0.0])
    bridge.register_agent('device-a', {'protocol_version': 3, 'execution_slots': 2,
                                      'active_command_ids': []}, user_id='owner')
    bridge.register_agent('device-b', {'protocol_version': 3, 'execution_slots': 2,
                                      'active_command_ids': []}, user_id='owner')
    return bridge


def command(cmd_id='work', **extra):
    return dict(id=cmd_id, type='desktop_system_info', params={}, target_agent_id='device-a',
                expires_at=1010, **extra)


def test_wire_authority_matches_consumers():
    wire = {'protocol_version': protocol_version(), 'server_time': 1000,
            'commands': [command()]}
    Draft202012Validator.check_schema(contract())
    Draft202012Validator(contract()).validate(wire)
    assert contract()['properties']['commands']['maxItems'] == limit('command_batch')
    assert validate_poll_response(wire, agent_id='device-a') == wire['commands']


@pytest.mark.parametrize('update', [
    {'protocol_version': 4}, {'protocol_version': True}, {'server_time': float('nan')},
    {'server_time': -1}, {'server_time': True},
])
def test_invalid_batch_never_acknowledges(update):
    wire = {'protocol_version': 3, 'server_time': 1000, 'commands': [command()]}
    wire.update(update)
    with pytest.raises(ValueError):
        validate_poll_response(wire, agent_id='device-a')


@pytest.mark.parametrize('field,value', [('expires_at', None), ('expires_at', float('inf')),
                                         ('target_agent_id', 'device-b'), ('target_agent_id', '')])
def test_command_deadline_and_address_validate_before_dispatch(field, value):
    cmd = command()
    cmd[field] = value
    with pytest.raises(ValueError):
        validate_poll_response({'protocol_version': 3, 'server_time': 1000, 'commands': [cmd]},
                               agent_id='device-a')


def test_legacy_compatible_but_negotiated_run_refuses_downgrade():
    assert validate_poll_response({'commands': []}) == []
    with pytest.raises(ValueError, match='Unsupported'):
        validate_poll_response({'commands': []}, require_v3=True)


def test_expiry_reclaims_only_acknowledged_completed_work(state):
    runtime, clock = state
    assert runtime.admit('work', expires_at=1001)
    clock[0] = 2
    assert runtime.available_slots() == limit("command_batch")
    assert 'work' in runtime.seen  # Running work survives its admission deadline.
    runtime.results.append({'id': 'work', 'result': {}, 'error': None})
    runtime.streams.append({'cmd_id': 'work', 'seq': 1})
    runtime.finish('work')
    runtime.available_slots()
    assert 'work' in runtime.seen  # Both outboxes must acknowledge first.
    with runtime.lock:
        del runtime.results[:1]
    runtime.available_slots()
    assert 'work' in runtime.seen
    with runtime.lock:
        del runtime.streams[:1]
    assert runtime.available_slots() == limit("command_batch")
    assert 'work' not in runtime.seen
    assert not runtime.admit('work', expires_at=1001)  # Old redelivery cannot execute.
    assert 'expired' in list(runtime.results)[-1]['error']
    assert runtime.admit('next', expires_at=1005)


def test_server_clock_never_moves_replay_watermark_backwards(state):
    runtime, clock = state
    clock[0] = 10
    runtime.observe_server_time(990)
    assert not runtime.admit('stale', expires_at=1005)


def test_long_running_agent_exceeds_lifetime_ledger_without_restart(state):
    runtime, clock = state
    for index in range(70000):
        clock[0] = index
        assert runtime.admit(str(index), expires_at=1000 + index + .5)
        runtime.finish(str(index))
    assert len(runtime.seen) <= 1
    assert len(runtime._expiry_heap) <= 1
    assert not list(runtime.results)


def test_legacy_ids_remain_pinned(state):
    runtime, clock = state
    assert runtime.admit('legacy')
    runtime.finish('legacy')
    clock[0] = 100000
    runtime.available_slots()
    assert not runtime.admit('legacy')
    assert 'legacy' in runtime.seen


def test_capacity_scales_up_down_and_override_errors(monkeypatch):
    import runtime_guards
    monkeypatch.delenv('TOFU_DESKTOP_EXECUTION_WORKERS', raising=False)
    monkeypatch.setattr(runtime_guards, 'probe_system_resources', lambda: SimpleNamespace(
        effective_cpu_count=64, effective_memory_capacity_mb=65536,
        effective_memory_available_mb=32768))
    large = execution_budget()
    assert large.workers == 32
    assert large.result_items == 512 and large.stream_items == 256
    assert large.result_bytes > 16 * 1024 * 1024
    monkeypatch.setattr(runtime_guards, 'probe_system_resources', lambda: SimpleNamespace(
        effective_cpu_count=64, effective_memory_capacity_mb=65536,
        effective_memory_available_mb=512))
    assert execution_budget().workers == 1
    monkeypatch.setenv('TOFU_DESKTOP_EXECUTION_WORKERS', '0')
    assert execution_budget().workers == 1  # Retired worker overrides cannot reject work.


def enqueue(queue, cid, kind='desktop_system_info', target='device-a'):
    result, error = queue.enqueue_desktop_command(kind, {}, user_id='owner',
                                                 target_agent_id=target, cmd_id=cid)
    assert error is None and result == cid


def test_server_dispatches_demand_with_stable_replay_horizon_and_explicit_address(queue):
    for index in range(4):
        enqueue(queue, str(index))
    first = queue.take_pending_commands(agent_id='device-a', user_id='owner')
    assert [cmd['id'] for cmd in first] == ['0', '1', '2', '3']
    assert all(cmd['target_agent_id'] == 'device-a' for cmd in first)
    replay = queue.take_pending_commands(agent_id='device-a', user_id='owner')
    assert replay == first  # Lost response retries do not extend expiry.
    queue.register_agent('device-a', {'protocol_version': 3, 'execution_slots': 1,
                                     'active_command_ids': ['0']}, user_id='owner')
    assert [c['id'] for c in queue.take_pending_commands(agent_id='device-a', user_id='owner')] == ['1', '2', '3']
    queue.register_agent('device-a', {'protocol_version': 3, 'execution_slots': 2,
                                     'active_command_ids': ['0', '1']}, user_id='owner')
    assert [c['id'] for c in queue.take_pending_commands(agent_id='device-a', user_id='owner')] == ['2', '3']
    assert queue.take_pending_commands(agent_id='device-b', user_id='owner') == []
    assert queue.take_pending_commands(agent_id='device-a', user_id='other') == []
    assert json.dumps(queue.list_agents('owner'))  # Internal active sets never leak.


def test_cancel_bypasses_saturated_execution_slots(queue):
    enqueue(queue, 'work')
    enqueue(queue, 'cancel', kind='egress_cancel')
    queue.register_agent('device-a', {'protocol_version': 3, 'execution_slots': 0,
                                     'active_command_ids': []}, user_id='owner')
    assert [c['id'] for c in queue.take_pending_commands(agent_id='device-a', user_id='owner')] == ['cancel', 'work']
    assert queue.command_queue['work'].get('claimed_agent_id') == 'device-a'


def test_legacy_response_batch_is_bounded(queue):
    queue.register_agent('device-a', {}, user_id='owner')
    for index in range(300):
        enqueue(queue, str(index))
    assert len(queue.take_pending_commands(agent_id='device-a', user_id='owner')) == limit('command_batch')


def test_targeted_wake_avoids_other_devices_and_owners(queue):
    calls = []
    loop = SimpleNamespace(call_soon_threadsafe=lambda callback: calls.append(callback))
    for device, owner in [('device-a', 'owner'), ('device-b', 'owner'), ('device-a', 'other')]:
        queue._async_waiters.append({'loop': loop, 'event': threading.Event(),
                                     'agent_id': device, 'user_id': owner})
    enqueue(queue, 'one')
    assert calls == [queue._async_waiters[0]['event'].set]


@pytest.mark.parametrize('frame,code', [
    ({'protocol_version': 99}, 426),
    ({'protocol_version': True}, 426),
    ({'protocol_version': 3, 'execution_slots': -1, 'active_command_ids': []}, 400),
    ({'protocol_version': 3, 'execution_slots': 1, 'active_command_ids': [{}]}, 400),
])
def test_route_rejects_bad_admission_before_settlement(monkeypatch, frame, code):
    from quart import Quart
    from routes import desktop
    monkeypatch.setattr(desktop, '_resolve_bridge_caller', lambda kind: (True, 'owner', 'key'))
    monkeypatch.setattr(desktop, 'register_agent', lambda *a, **kw: pytest.fail('registered bad frame'))
    app = Quart(__name__)
    app.register_blueprint(desktop.desktop_bp)
    async def request():
        response = await app.test_client().post('/api/desktop/poll',
            json={'agent': dict(agent_id='device-a', **frame), 'results': [], 'streams': []})
        assert response.status_code == code
    asyncio.run(request())


def test_poll_negotiation_emits_valid_wire_and_updates_owner_registration(queue, monkeypatch):
    from quart import Quart
    from routes import desktop
    monkeypatch.setattr(desktop, '_resolve_bridge_caller', lambda kind: (True, 'owner', 'key'))
    monkeypatch.setattr(desktop, 'register_agent', queue.register_agent)
    monkeypatch.setattr(desktop, 'resolve_results', queue.resolve_results)
    monkeypatch.setattr(desktop, 'resolve_streams', queue.resolve_streams)
    enqueue(queue, 'one')
    async def take(**kwargs):
        kwargs.pop("timeout", None)
        return queue.take_pending_commands(**kwargs)
    monkeypatch.setattr(desktop, 'take_pending_commands_async', take)
    app = Quart(__name__)
    app.register_blueprint(desktop.desktop_bp)
    async def request():
        response = await app.test_client().post('/api/desktop/poll', json={
            'agent': {'agent_id': 'device-a', 'protocol_version': 3,
                      'execution_slots': 1, 'active_command_ids': []},
            'results': [], 'streams': []})
        assert response.status_code == 200
        wire = await response.get_json()
        Draft202012Validator(contract()).validate(wire)
        assert len(wire['commands']) == 1
        assert queue._agents['device-a']['user_id'] == 'owner'
        assert queue._agents['device-a']['key_id'] == 'key'
    asyncio.run(request())


def test_same_url_credential_repair_rebinds_in_place(monkeypatch):
    from lib.desktop_agent import _run
    stop = threading.Event()
    requests = []
    monkeypatch.setattr(_run, '_ensure_agent_id', lambda: 'device-a')
    monkeypatch.setattr(_run, 'load_config', lambda: {})
    monkeypatch.setattr(_run, 'retry_delay', lambda *args: .001)
    class Response:
        status_code = 502
        def json(self):
            return {'commands': []}
    def post(self, url, **kwargs):
        requests.append(kwargs['headers'].get('X-Bridge-Secret'))
        response = Response()
        if len(requests) == 7:
            response.status_code = 200
            stop.set()
        elif len(requests) > 7:
            pytest.fail('Repair did not settle')
        return response
    monkeypatch.setattr(_run.requests.Session, 'post', post)
    _run.run_agent('https://configured.test', {}, .001, bridge_secret='old-test-only',
                   stop_event=stop, route_repair=lambda: ('https://configured.test', 'new-test-only'))
    assert requests == ['old-test-only'] * 6 + ['new-test-only']


def test_real_poll_connection_is_reused_and_closed(monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from lib.desktop_agent import _run
    stop = threading.Event()
    accepted = []
    count = []
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args):
            return  # Synthetic loopback exchange needs no HTTP log.
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            count.append(1)
            payload = b'{"commands":[]}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
            if len(count) == 2:
                stop.set()
    class Server(ThreadingHTTPServer):
        def get_request(self):
            sock, address = super().get_request()
            accepted.append(address)
            return sock, address
    server = Server(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    deadline = threading.Timer(5, stop.set)
    deadline.start()
    monkeypatch.setenv('NO_PROXY', '127.0.0.1')
    monkeypatch.setattr(_run, '_ensure_agent_id', lambda: 'device-a')
    monkeypatch.setattr(_run, 'load_config', lambda: {})
    try:
        _run.run_agent('http://127.0.0.1:%s' % server.server_port, {}, .001, stop_event=stop)
    finally:
        stop.set()
        deadline.cancel()
        deadline.join(1)
        server.shutdown()
        worker.join(2)
        server.server_close()
    assert len(count) == 2 and len(accepted) == 1
    assert not worker.is_alive()


def test_live_execution_outlives_pickup_deadline_on_other_device_poll(queue):
    import time
    enqueue(queue, 'long-work')
    queue.command_queue['long-work']['ttl'] = 90
    queue.take_pending_commands(agent_id='device-a', user_id='owner')
    queue.command_queue['long-work']['created_at'] = time.time() - 1000
    queue.register_agent('device-a', {'protocol_version': 3, 'execution_slots': 1,
                                     'active_command_ids': ['long-work']}, user_id='owner')
    assert queue.take_pending_commands(agent_id='device-b', user_id='owner') == []
    assert not queue.command_queue['long-work']['event'].is_set()
    queue._agents['device-a']['last_seen'] = time.time() - 1000
    queue.take_pending_commands(agent_id='device-b', user_id='owner')
    assert queue.command_queue['long-work']['event'].is_set()
    assert 'expired' in queue.command_queue['long-work']['error']


def test_cancel_has_priority_even_when_work_fills_batch(queue):
    queue.register_agent('device-a', {'protocol_version': 3, 'execution_slots': 1000,
                                     'active_command_ids': []}, user_id='owner')
    for index in range(300):
        enqueue(queue, str(index))
    enqueue(queue, 'cancel', kind='paper_build_cancel')
    wire = queue.take_pending_commands(agent_id='device-a', user_id='owner')
    assert wire[0]['id'] == 'cancel'
    assert len(wire) == limit('command_batch')


def test_wire_prefix_is_bounded_independently_of_resident_capacity():
    from lib.desktop_agent._runtime import Outbox
    lock = threading.Condition(threading.RLock())
    outbox = Outbox(lock, threading.Event(), 100, 1000, memory_bytes=10000)
    for index in range(10):
        outbox.append({'id': str(index), 'text': '\U0001f600' * 10})
    batch = outbox.prefix(2, 1000)
    assert [item['id'] for item in batch] == ['0', '1']
    assert len(outbox.pending_ids()) == 10
    assert len(outbox.prefix(10, len(json.dumps(batch[0], ensure_ascii=True).encode('utf-8')))) == 1
    with lock:
        del outbox[:len(batch)]
    assert outbox.prefix(1, 1000)[0]['id'] == '2'
    with pytest.raises(ValueError, match='budget'):
        outbox.append({'text': 'x' * 1001})


def test_delayed_first_response_cannot_execute_expired_command(state):
    runtime, _ = state
    runtime.observe_server_time(1000, request_elapsed=20)
    assert not runtime.admit('delayed', expires_at=1010)
    assert runtime.admit('fresh', expires_at=1030)


def test_zero_slots_and_old_queue_age_cannot_refuse_accepted_work(queue):
    import time
    for i in range(40):
        enqueue(queue,str(i));queue.command_queue[str(i)]['created_at']=time.time()-1000
    queue.register_agent('device-a',{'protocol_version':3,'execution_slots':0,'active_command_ids':[]},user_id='owner')
    batch=queue.take_pending_commands(agent_id='device-a',user_id='owner')
    assert len(batch)==40 and all(row['deadline_enforced'] is False for row in batch)
    assert all(not queue.command_queue[str(i)]['event'].is_set() for i in range(40))
    assert queue.take_pending_commands(agent_id='device-a',user_id='owner')==batch


def test_old_default_replay_horizon_does_not_refuse_execution(state):
    runtime,_=state
    assert runtime.admit('accepted',expires_at=900,deadline_enforced=False)
    assert not runtime.admit('accepted',expires_at=900,deadline_enforced=False)
    assert not runtime.admit('explicit-expired',expires_at=900,deadline_enforced=True)


def test_rotating_active_pages_keep_delivery_acknowledged_and_allow_new_work(queue,state,monkeypatch):
    from lib.desktop_agent import _runtime
    runtime,_=state;original=_runtime.limit
    monkeypatch.setattr(_runtime,'limit',lambda name:2 if name=='legacy_execution_ids' else original(name))
    for i in range(5):enqueue(queue,str(i))
    queue.take_pending_commands(agent_id='device-a',user_id='owner')
    for i in range(4):assert runtime.admit(str(i))
    seen=set()
    for _ in range(2):
        ids=runtime.active_command_ids();assert len(ids)==2;seen.update(ids)
        queue.register_agent('device-a',{'protocol_version':3,'execution_slots':0,'active_command_ids':ids},user_id='owner')
    assert seen=={'0','1','2','3'}
    assert [row['id'] for row in queue.take_pending_commands(agent_id='device-a',user_id='owner')]==['4']
    queue.register_agent('device-a',{'protocol_version':3,'execution_slots':0,'active_command_ids':[]},user_id='owner')
    assert [row['id'] for row in queue.take_pending_commands(agent_id='device-a',user_id='owner')]==['4']


def test_explicit_pickup_ttl_still_refuses_before_dispatch(queue):
    import time
    enqueue(queue,'explicit');row=queue.command_queue['explicit'];row.update(ttl=1,created_at=time.time()-2)
    assert queue.take_pending_commands(agent_id='device-a',user_id='owner')==[]
    assert row['event'].is_set() and 'expired' in row['error']


def test_active_metadata_pages_obey_bytes_without_losing_ids(state,monkeypatch):
    from lib.desktop_agent import _runtime
    runtime,_=state;original=_runtime.limit
    monkeypatch.setattr(_runtime,'limit',lambda name:200 if name=='stream_bytes_floor' else original(name))
    expected={('材料'*10)+str(i) for i in range(9)}
    runtime.running.update(expected);observed=set()
    for _ in range(9):
        page=runtime.active_command_ids();observed.update(page)
        assert len(json.dumps(page,ensure_ascii=True).encode('ascii'))<=200
    assert observed==expected
