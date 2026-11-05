"""SDK transport ownership and shared stream/error behavior at real I/O seams."""
import asyncio
import json
from pathlib import Path

import httpx
import pytest
import requests

from tofu_sdk import AsyncTofu, Tofu, TofuError
from tofu_sdk._streaming import _parse_sse
from tofu_sdk._streaming import _parse_async_sse

pytestmark = pytest.mark.unit
CASES = json.loads((Path(__file__).resolve().parents[1] / 'fixtures/sdk/stream_frames.json').read_text())


class SyncResponse:
    def __init__(self, wire):
        self.wire = wire
        self.closed = False

    def iter_content(self, chunk_size):
        for byte in self.wire:
            yield bytes([byte])

    def close(self):
        self.closed = True


class AsyncBytes(httpx.AsyncByteStream):
    def __init__(self, wire):
        self.wire = wire
        self.closed = False

    async def __aiter__(self):
        for byte in self.wire:
            yield bytes([byte])

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize('case', CASES, ids=lambda case: case['name'])
def test_sync_and_async_streams_obey_the_same_frame_contract(case):
    wire = case['wire'].encode()
    sync = SyncResponse(wire)
    actual = list(_parse_sse(sync))
    assert sync.closed
    assert actual == case['expected']

    async def check_async():
        body = AsyncBytes(wire)
        response = httpx.Response(200, stream=body)
        events = [event async for event in _parse_async_sse(response)]
        assert body.closed
        assert events == case['expected']
    asyncio.run(check_async())


@pytest.mark.parametrize('stream', [False, True], ids=['json', 'stream'])
def test_borrowed_http_client_preserves_sdk_auth_without_mutating_its_defaults(stream):
    async def check():
        observed = []
        async def respond(request):
            observed.append(request)
            if stream:
                return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                                      content=b'data: {"type":"done"}\n\n')
            return httpx.Response(200, json={'data': {'ready': True}, 'meta': {}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond),
                                     headers={'X-Shared': 'keep', 'Authorization': 'Bearer other-client'},
                                     auth=httpx.BasicAuth('shared', 'synthetic-secret')) as http:
            original = dict(http.headers)
            first = AsyncTofu(base_url='https://first.test', api_key='first-token', timeout=123, client=http)
            second = AsyncTofu(base_url='https://second.test', api_key='second-token', timeout=123, client=http)
            for client in [first, second]:
                if stream:
                    assert [e async for e in client.tasks.stream('task', reconnect=False)] == [{'type': 'done'}]
                else:
                    assert await client.capabilities() == {'ok': True, 'ready': True}
                await client.aclose()
            assert not http.is_closed
            assert dict(http.headers) == original
            assert [r.headers['authorization'] for r in observed] == ['Bearer first-token', 'Bearer second-token']
            assert all(r.headers['x-shared'] == 'keep' for r in observed)
            assert all(r.extensions['timeout']['read'] == 123 for r in observed)
            assert all(r.headers['user-agent'].startswith('tofu-sdk-python/') for r in observed)
    asyncio.run(check())


@pytest.mark.parametrize('status,body,kind', [
    (400, {'code': 'invalid_request', 'status': 400, 'detail': 'bad'}, 'invalid_request'),
    (429, {'error': {'kind': 'rate_limited'}, 'retry_after': 2}, 'rate_limited'),
    (503, {'error_kind': 'upstream_unavailable'}, 'upstream_unavailable'),
])
def test_typed_error_keeps_the_wire_body_and_canonical_kind(status, body, kind):
    error = TofuError(status, body)
    assert error.body is body
    assert error.kind == kind
    assert error.retryable is (status == 429 or status >= 500)


def test_oversized_sequence_is_ignored_without_losing_the_event():
    response = SyncResponse(('id: ' + '9' * 5000 + '\ndata: {"text":"keep"}\n\n').encode())
    assert list(_parse_sse(response)) == [{'text': 'keep'}]
    assert response.closed


@pytest.mark.parametrize('model', [
    {'creator_id': None, 'model_id': 'test'},
    {'provider_id': 'test', 'offering_id': None},
])
def test_null_model_identity_is_rejected_before_transport(model, monkeypatch):
    with Tofu(base_url='https://sdk.test') as client:
        def unexpected_request(*args, **kwargs):
            pytest.fail('invalid model identity reached transport')
        monkeypatch.setattr(client._session, 'request', unexpected_request)
        with pytest.raises(ValueError, match='non-empty'):
            client.chat(messages=[], model=model)


def test_sync_task_reconnect_uses_only_valid_sequences(monkeypatch):
    observed = []
    wires = iter([
        b'data: {"seq":12,"text":"first"}\n\ndata: {"text":"unsequenced"}\n\n'
        b'data: {"seq":"bad","text":"invalid"}\n\n',
        b'event: done\ndata: {"seq":13}\n\n',
    ])
    def request(method, url, **kwargs):
        if url.endswith('/stream'):
            observed.append(kwargs['params']['cursor'])
            response = SyncResponse(next(wires))
            response.status_code = 200
            return response
        response = requests.Response()
        response.status_code = 200
        response.headers['content-type'] = 'application/json'
        response._content = b'{"status":"running"}'
        response._content_consumed = True
        return response
    with Tofu(base_url='https://sdk.test', backoff_base=0.05) as client:
        monkeypatch.setattr(client._session, 'request', request)
        events = list(client.tasks.stream('task', cursor=9, max_reconnects=1))
    assert events[-1] == {'seq': 13, 'event': 'done'}
    assert observed == [9, 13]


def test_async_task_reconnect_uses_only_valid_sequences():
    async def check():
        observed = []
        wires = iter([
            b'data: {"seq":12,"text":"first"}\n\ndata: {"text":"unsequenced"}\n\n'
            b'data: {"seq":"bad","text":"invalid"}\n\n',
            b'event: done\ndata: {"seq":13}\n\n',
        ])
        async def respond(request):
            if request.url.path.endswith('/stream'):
                observed.append(int(request.url.params['cursor']))
                return httpx.Response(200, content=next(wires))
            return httpx.Response(200, json={'status': 'running'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            async with AsyncTofu(base_url='https://sdk.test', client=http, backoff_base=0.05) as client:
                events = [e async for e in client.tasks.stream('task', cursor=9, max_reconnects=1)]
        assert events[-1] == {'seq': 13, 'event': 'done'}
        assert observed == [9, 13]
    asyncio.run(check())


@pytest.mark.parametrize('exceptional_exit', [False, True])
def test_sync_context_releases_its_owned_session(exceptional_exit, monkeypatch):
    client = Tofu(base_url='https://sdk.test')
    closed = []
    original_close = client._session.close
    def close():
        closed.append(True)
        original_close()
    monkeypatch.setattr(client._session, 'close', close)
    try:
        with client as entered:
            assert entered is client
            if exceptional_exit:
                raise LookupError('application error')
    except LookupError:
        assert exceptional_exit
    assert closed == [True]


@pytest.mark.parametrize('status,wire', [
    (200, b'{"data":{"ready":true},"meta":{}}'),
    (403, b'{"code":"access_denied"}'),
    (200, b'{invalid json'),
])
def test_sync_json_releases_responses_on_success_error_and_decode_failure(status, wire, monkeypatch):
    response = requests.Response()
    response.status_code = status
    response.headers['content-type'] = 'application/json'
    response._content, response._content_consumed = wire, True
    closed = []
    original_close = response.close
    def close():
        closed.append(True)
        original_close()
    monkeypatch.setattr(response, 'close', close)
    with Tofu(base_url='https://sdk.test') as client:
        monkeypatch.setattr(client._session, 'request', lambda *a, **kw: response)
        if status == 403:
            with pytest.raises(TofuError, match='access_denied'):
                client.capabilities()
        elif wire.startswith(b'{invalid'):
            with pytest.raises(ValueError):
                client.capabilities()
        else:
            assert client.capabilities() == {'ok': True, 'ready': True}
    assert closed == [True]


@pytest.mark.parametrize('exceptional_exit', [False, True])
def test_async_context_releases_its_owned_client(exceptional_exit):
    async def check():
        client = AsyncTofu(base_url='https://sdk.test')
        try:
            async with client as entered:
                assert entered is client
                if exceptional_exit:
                    raise LookupError('application error')
        except LookupError:
            assert exceptional_exit
        assert client._client.is_closed
    asyncio.run(check())


def test_async_task_stream_preserves_structured_http_errors_and_closes_body():
    async def check():
        body = AsyncBytes(b'{"status":403,"code":"access_denied","detail":"scope missing"}')
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(403, stream=body))) as http:
            async with AsyncTofu(base_url='https://sdk.test', client=http) as client:
                with pytest.raises(TofuError) as failure:
                    await anext(client.tasks.stream('task'))
                assert failure.value.status == 403
                assert failure.value.kind == 'access_denied'
                assert failure.value.body['detail'] == 'scope missing'
                assert not failure.value.retryable
        assert body.closed
    asyncio.run(check())


@pytest.mark.parametrize('retry_after', ['NaN', 'Infinity', 'not a number'])
def test_sync_retry_uses_finite_backoff_for_malformed_retry_after(retry_after, monkeypatch):
    responses = []
    for status, payload in [(503, {'error': 'temporary'}), (200, {'status': 'done'})]:
        response = requests.Response()
        response.status_code = status
        response.headers.update({'content-type': 'application/json', 'Retry-After': retry_after})
        response._content = json.dumps(payload).encode()
        response._content_consumed = True
        responses.append(response)
    attempts, sleeps = [], []
    def request(*args, **kwargs):
        attempts.append(True)
        return responses[len(attempts) - 1]
    monkeypatch.setattr('time.sleep', sleeps.append)
    with Tofu(base_url='https://sdk.test', backoff_base=0.5, max_retries=1) as client:
        monkeypatch.setattr(client._session, 'request', request)
        assert client.tasks.get('task') == {'status': 'done'}
    assert len(attempts) == 2
    assert sleeps == [0.5]
