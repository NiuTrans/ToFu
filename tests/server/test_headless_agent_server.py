"""HTTP/auth/idempotency contract for the database-free sidecar."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import threading
from types import SimpleNamespace

import pytest


class _Result:
    def __init__(self, execution):
        self.execution = execution

    def to_dict(self):
        return self.execution.snapshot()


class _Execution:
    def __init__(self, task_id: str, request):
        self.request = request
        self.task_id = task_id
        self.request_id = request.request_id or f'run-{task_id}'
        selected = request.model
        self.model = (
            str(selected.get('model_id') or selected.get('offering_id') or '')
            if isinstance(selected, Mapping)
            else 'managed-model'
        ) or 'managed-model'
        self.status = 'done'

    @property
    def done(self):
        return self.status in {'done', 'error', 'aborted'}

    @property
    def timeout_s(self):
        return self.request.timeout_s

    async def result_async(self, _timeout_s):
        return _Result(self)

    def snapshot(self):
        return {
            'id': self.request_id,
            'object': 'agent.run',
            'task_id': self.task_id,
            'status': self.status,
            'model': self.model,
            'finish_reason': 'stop',
            'content': 'sidecar-ok',
            'thinking': '',
            'usage': {},
            'n_tool_rounds': 0,
        }

    def event_page(self, *, cursor=0):
        return {'events': [], 'cursor': cursor, 'next_cursor': cursor}

    async def events_async(self, *, cursor=0):
        yield {'seq': cursor, 'type': 'done', 'finishReason': 'stop'}

    def abort(self):
        self.status = 'aborted'
        return True


class _Runtime:
    def __init__(self):
        self.principal = SimpleNamespace(subject_id='test:owner')
        self.default_model = {
            'creator_id': 'test-creator', 'model_id': 'managed-model'}
        self.model_routing = SimpleNamespace(public_dict=lambda: {
            'model_routing': {'contract_version': 'tofu.model-routing/v2'},
            'model': dict(self.default_model),
            'routing': {},
            'credential_secret_hints': {},
        })
        self.model_routing_source = 'runtime'
        self.closed = False
        self.capacity = 4
        self.in_flight = 0
        self.started = []
        self.executions = {}

    def start(self, request):
        self.started.append(request)
        execution = _Execution(f'task-{len(self.started)}', request)
        self.executions[execution.task_id] = execution
        return execution

    def get(self, task_id):
        return self.executions.get(task_id)


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.unit
def test_empty_bind_host_is_not_treated_as_loopback():
    from tofu_agent.models import AgentConfigurationError
    from tofu_agent.server import (
        HeadlessServerConfig, _authority_hostname, _is_loopback,
    )

    assert _is_loopback('127.0.0.1') is True
    assert _is_loopback('::1') is True
    assert _is_loopback('') is False
    assert _authority_hostname('localhost:15001') == 'localhost'
    assert _authority_hostname('[::1]:15001') == '::1'
    assert _authority_hostname('bad[') == ''
    with pytest.raises(AgentConfigurationError, match='must not be empty'):
        HeadlessServerConfig(bind_host='')


@pytest.mark.unit
def test_tokenless_loopback_rejects_dns_rebinding_host_authority():
    from tofu_agent.server import HeadlessServerConfig, create_app

    runtime = _Runtime()
    app = create_app(
        runtime=runtime,
        config=HeadlessServerConfig(
            bind_host='127.0.0.1', auth_mode='auto'),
    )
    app.config['TESTING'] = True

    async def scenario():
        client = app.test_client()
        scope = {'client': ('127.0.0.1', 43210)}
        allowed = await client.get(
            '/api/v1/capabilities',
            headers={'Host': '127.0.0.1:15001'},
            scope_base=scope,
        )
        rebound = await client.get(
            '/api/v1/capabilities',
            headers={'Host': 'attacker.example:15001'},
            scope_base=scope,
        )
        assert allowed.status_code == 200
        assert rebound.status_code == 401

    _run(scenario())


@pytest.mark.unit
def test_remote_sidecar_is_default_deny_and_health_hides_provider():
    from tofu_agent.server import HeadlessServerConfig, create_app

    runtime = _Runtime()
    app = create_app(
        runtime=runtime,
        config=HeadlessServerConfig(
            bind_host='0.0.0.0', token='runtime-token'))
    app.config['TESTING'] = True

    async def scenario():
        client = app.test_client()
        denied = await client.post('/api/v1/agent/run', json={
            'messages': [{'role': 'user', 'content': 'hello'}],
        })
        assert denied.status_code == 401

        ready = await client.get('/health/ready')
        assert ready.status_code == 200
        ready_body = await ready.get_json()
        assert 'model' not in ready_body
        assert 'provider_configured' not in ready_body

        capabilities = await client.get(
            '/api/v1/capabilities',
            headers={'Authorization': 'Bearer runtime-token'},
        )
        assert capabilities.status_code == 200
        capability_body = await capabilities.get_json()
        assert capability_body['features']['frontend'] is False
        assert capability_body['features']['model_routing_setup_ui'] is True
        assert capability_body['model_routing']['contract_version'] == \
            'tofu.model-routing/v2'
        assert capability_body['model_routing']['required_fields'] == [
            'model_routing', 'model', 'credential_secrets']

    _run(scenario())


@pytest.mark.unit
def test_managed_model_idempotency_and_async_handle():
    from tofu_agent.server import HeadlessServerConfig, create_app

    runtime = _Runtime()
    app = create_app(
        runtime=runtime,
        config=HeadlessServerConfig(
            bind_host='0.0.0.0', token='runtime-token'))
    app.config['TESTING'] = True
    headers = {
        'Authorization': 'Bearer runtime-token',
        'Idempotency-Key': 'stable-key',
    }
    body = {'messages': [{'role': 'user', 'content': 'hello'}]}

    async def scenario():
        client = app.test_client()
        first = await client.post(
            '/api/v1/agent/run', headers=headers, json=body)
        replay = await client.post(
            '/api/v1/agent/run', headers=headers, json=body)
        assert first.status_code == replay.status_code == 200
        assert (await first.get_json())['task_id'] == 'task-1'
        assert (await replay.get_json())['task_id'] == 'task-1'
        assert len(runtime.started) == 1
        assert runtime.started[0].model is None
        assert runtime.started[0].model_routing is None

        conflict = await client.post(
            '/api/v1/agent/run', headers=headers,
            json={'messages': [{'role': 'user', 'content': 'different'}]})
        assert conflict.status_code == 409

        async_headers = {
            'Authorization': 'Bearer runtime-token',
            'Idempotency-Key': 'async-key',
            'Prefer': 'respond-async',
        }
        accepted = await client.post(
            '/api/v1/agent/run', headers=async_headers, json=body)
        assert accepted.status_code == 202
        accepted_body = await accepted.get_json()
        assert accepted_body['task_id'] == 'task-2'
        assert accepted.headers['Location'] == '/api/v1/tasks/task-2'

    _run(scenario())


@pytest.mark.unit
@pytest.mark.parametrize('pressure', ['ttl', 'count'])
def test_live_idempotent_run_survives_response_retention(monkeypatch, pressure):
    from tofu_agent import server

    runtime = _Runtime()
    app = server.create_app(
        runtime=runtime,
        config=server.HeadlessServerConfig(
            bind_host='0.0.0.0', token='runtime-token',
            idempotency_ttl_s=60, idempotency_max_entries=16),
    )
    app.config['TESTING'] = True
    now = [100.0]
    monkeypatch.setattr(server, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    headers = {'Authorization': 'Bearer runtime-token',
               'Idempotency-Key': 'still-running', 'Prefer': 'respond-async'}
    body = {'messages': [{'role': 'user', 'content': 'finish the long task'}]}

    async def scenario():
        client = app.test_client()
        first = await client.post('/api/v1/agent/run', headers=headers, json=body)
        assert first.status_code == 202
        original = (await first.get_json())['task_id']
        runtime.executions[original].status = 'running'
        if pressure == 'ttl':
            now[0] += 61
        else:
            for index in range(20):
                now[0] += 1
                response = await client.post(
                    '/api/v1/agent/run', json=body,
                    headers={**headers, 'Idempotency-Key': f'other-{index}'})
                assert response.status_code == 202
        before_retry = len(runtime.started)
        replay = await client.post('/api/v1/agent/run', headers=headers, json=body)
        assert replay.status_code == 202
        assert (await replay.get_json())['task_id'] == original
        assert len(runtime.started) == before_retry
        conflict = await client.post(
            '/api/v1/agent/run', headers=headers,
            json={'messages': [{'role': 'user', 'content': 'different task'}]})
        assert conflict.status_code == 409

    _run(scenario())


@pytest.mark.unit
@pytest.mark.parametrize('pressure', ['ttl', 'count'])
def test_idempotency_reservation_survives_retention_before_publication(
        monkeypatch, pressure):
    from tofu_agent import server

    now = [100.0]
    monkeypatch.setattr(server, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    store = server._IdempotencyStore(ttl_s=60, max_entries=2)
    reserved, creator, conflict = store.claim('owner:pending', 'body')
    assert creator and not conflict
    if pressure == 'ttl':
        now[0] += 61
    else:
        for index in range(4):
            entry, _, _ = store.claim(f'owner:other-{index}', 'body')
            store.publish(f'owner:other-{index}', entry, SimpleNamespace(done=True))
    replay, creator, conflict = store.claim('owner:pending', 'body')
    assert replay is reserved
    assert not creator and not conflict
    execution = SimpleNamespace(done=False)
    store.publish('owner:pending', reserved, execution)
    assert replay.ready.is_set() and replay.execution is execution


@pytest.mark.unit
@pytest.mark.parametrize('pressure', ['ttl', 'count'])
def test_idempotency_releases_only_finished_executions(monkeypatch, pressure):
    from tofu_agent import server

    now = [100.0]
    monkeypatch.setattr(server, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    store = server._IdempotencyStore(ttl_s=60, max_entries=2)
    entry, _, _ = store.claim('owner:old', 'body')
    execution = SimpleNamespace(done=False)
    store.publish('owner:old', entry, execution)
    if pressure == 'ttl':
        now[0] += 61
    else:
        for index in range(3):
            now[0] += 1
            other, _, _ = store.claim(f'owner:other-{index}', 'body')
            store.publish(f'owner:other-{index}', other, SimpleNamespace(done=True))
    assert store.claim('owner:old', 'body')[0] is entry
    execution.done = True
    replacement, creator, conflict = store.claim('owner:old', 'body')
    assert replacement is not entry and creator and not conflict


@pytest.mark.unit
@pytest.mark.parametrize('fail_admission', [False, True])
def test_cancelled_admission_joins_and_resolves_idempotency(fail_admission):
    from tofu_agent.models import AgentConfigurationError
    from tofu_agent.server import HeadlessServerConfig, create_app

    entered = threading.Event()
    release = threading.Event()

    class BlockingRuntime(_Runtime):
        def start(self, request):
            entered.set()
            assert release.wait(10), 'test did not release admission'
            if fail_admission and not self.started:
                self.started.append(request)
                raise AgentConfigurationError('controlled admission failure')
            return super().start(request)

    runtime = BlockingRuntime()
    app = create_app(runtime=runtime, config=HeadlessServerConfig(
        bind_host='0.0.0.0', token='runtime-token'))
    app.config['TESTING'] = True
    headers = {'Authorization': 'Bearer runtime-token',
               'Idempotency-Key': 'cancelled-observer', 'Prefer': 'respond-async'}
    body = {'messages': [{'role': 'user', 'content': 'hello'}]}

    async def scenario():
        async with app.test_request_context(
                '/api/v1/agent/run', method='POST', headers=headers, json=body):
            endpoint, _ = app.url_map.bind('localhost').match(
                '/api/v1/agent/run', method='POST')
            pending = asyncio.create_task(app.view_functions[endpoint]())
            try:
                assert await asyncio.to_thread(entered.wait, 10)
                pending.cancel()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                assert not pending.done(), 'admission ownership was abandoned'
            finally:
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await pending
        replay = await app.test_client().post(
            '/api/v1/agent/run', headers=headers, json=body)
        assert replay.status_code == 202
        assert len(runtime.started) == (2 if fail_admission else 1)

    _run(scenario())


@pytest.mark.unit
def test_inline_provider_block_is_explicitly_removed():
    from tofu_agent.server import HeadlessServerConfig, create_app

    runtime = _Runtime()
    app = create_app(
        runtime=runtime,
        config=HeadlessServerConfig(
            bind_host='127.0.0.1', auth_mode='open'))
    app.config['TESTING'] = True

    async def scenario():
        response = await app.test_client().post('/api/v1/agent/run', json={
            'messages': [{'role': 'user', 'content': 'hello'}],
            'provider': {
                'endpoint': 'https://models.example/v1',
                'api_key': 'secret',
                'model': 'provider-model',
            },
        })
        assert response.status_code == 400
        body = await response.get_json()
        assert body['error']['kind'] == 'invalid_request'
        assert 'model-routing/v2' in body['error']['message']
        assert runtime.started == []

    _run(scenario())


@pytest.mark.unit
def test_agent_run_forwards_exclusive_custom_tool_mode():
    from tofu_agent.server import HeadlessServerConfig, create_app

    runtime = _Runtime()
    app = create_app(
        runtime=runtime,
        config=HeadlessServerConfig(
            bind_host='127.0.0.1', auth_mode='open'))
    app.config['TESTING'] = True
    tool = {
        'type': 'function',
        'function': {
            'name': 'custom__run_command',
            'parameters': {'type': 'object'},
        },
    }

    async def scenario():
        response = await app.test_client().post('/api/v1/agent/run', json={
            'messages': [{'role': 'user', 'content': 'hello'}],
            'tools': [tool],
            'custom_tools_mode': 'exclusive',
        })
        assert response.status_code == 200
        assert runtime.started[0].custom_tools_mode == 'exclusive'

    _run(scenario())


@pytest.mark.unit
def test_invalid_boolean_and_provider_shapes_fail_before_admission():
    from tofu_agent.server import HeadlessServerConfig, create_app

    runtime = _Runtime()
    app = create_app(
        runtime=runtime,
        config=HeadlessServerConfig(
            bind_host='127.0.0.1', auth_mode='open'))
    app.config['TESTING'] = True

    async def scenario():
        client = app.test_client()
        bad_boolean = await client.post('/api/v1/agent/run', json={
            'messages': [{'role': 'user', 'content': 'hello'}],
            'stream': 'false',
        })
        assert bad_boolean.status_code == 400
        assert (await bad_boolean.get_json())['error']['kind'] == 'invalid_request'
        assert runtime.started == []

        bad_provider = await client.post('/api/v1/agent/run', json={
            'messages': [{'role': 'user', 'content': 'hello'}],
            'provider': 'not-an-object',
        })
        assert bad_provider.status_code == 400
        assert (await bad_provider.get_json())['error']['kind'] == 'invalid_request'
        assert runtime.started == []

    _run(scenario())


@pytest.mark.unit
@pytest.mark.parametrize('projection',['native','agent'])
@pytest.mark.parametrize('frames_consumed',[1,2])
def test_sse_projection_close_joins_nested_event_iterator(projection,frames_consumed):
    import asyncio
    from tofu_agent.server import _native_task_stream,_agent_run_stream
    class Execution:
        request_id='request';task_id='task';model='model';closed=False
        async def events_async(self,*,cursor=0):
            try:
                yield {'seq':cursor,'type':'delta','content':'中文'}
                yield {'seq':cursor+1,'type':'done'}
            finally:
                await asyncio.sleep(0)
                self.closed=True
    async def scenario():
        execution=Execution()
        factory=_native_task_stream if projection=='native' else _agent_run_stream
        stream=factory(execution,cursor=0)
        for _ in range(frames_consumed):await anext(stream)
        await stream.aclose()
        assert execution.closed,'SSE close left event iterator to deferred finalization'
    asyncio.run(scenario())
