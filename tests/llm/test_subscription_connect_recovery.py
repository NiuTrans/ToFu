"""Subscription connection failures recover without poisoning model health."""
import asyncio
import importlib
import json
from types import SimpleNamespace

import httpx
import pytest
import requests

from lib.desktop import egress
from lib.llm import _transport, egress_recovery
from lib.llm.llm_errors import AbortedError, RequestTransportError
from lib.subscription_routes import ProbeResult, Route, RouteManager

pytestmark = pytest.mark.unit
BASE = 'https://chatgpt.com/v1'
URL = BASE + '/chat/completions'
SSE = (b'data: {"choices":[{"delta":{"content":"recovered"}}]}\n\n'
       b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
       b'data: [DONE]\n\n')


@pytest.fixture
def network(monkeypatch):
    import lib.proxy as proxy
    import lib.subscription_routes as routes
    now = [100.0]
    route = Route('fixture-proxy', 'test proxy', 'proxy',
                  proxy_url='http://fixture.invalid:8080')
    manager = RouteManager(probe=lambda *_: ProbeResult('ok', 10),
                           clock=lambda: now[0], jitter=lambda delay: delay)
    manager.report(URL, route, True)
    monkeypatch.setattr(routes, 'manager', manager)
    monkeypatch.setattr(proxy, 'subscription_route_specs', lambda url: [route])
    monkeypatch.setattr(egress, '_online_egress_agents', lambda owner: [])
    monkeypatch.setattr(egress_recovery, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(_transport, 'retry_wait', lambda attempt: 0.25)
    state = SimpleNamespace(now=now, route=route, manager=manager, sends=0,
                            failures=1, recover_at=102.0, proxy_error=False)

    def sleep(seconds, abort_check=None):
        if abort_check and abort_check():
            raise AbortedError('cancelled')
        now[0] += seconds
        if state.recover_at is not None and now[0] >= state.recover_at:
            manager.report(URL, route, True)

    async def asleep(seconds, abort_check=None):
        sleep(seconds, abort_check)
        await asyncio.sleep(0)

    monkeypatch.setattr(_transport, 'abortable_sleep', sleep)
    monkeypatch.setattr(_transport, 'async_abortable_sleep', asleep)
    monkeypatch.setattr('lib.llm.stream.abortable_sleep', sleep)
    monkeypatch.setattr('lib.llm.astream.async_abortable_sleep', asleep)

    def before_send():
        state.sends += 1
        if state.sends <= state.failures:
            return True
        assert manager.cached_candidates(URL, [route]), 'sent through a cooling route'
        return False

    def sync_send(*args, **kwargs):
        if before_send():
            raise requests.exceptions.ConnectTimeout('fixture connect failure')
        response = requests.Response()
        response.status_code = 200
        response.headers['Content-Type'] = 'text/event-stream'
        response._content = SSE if kwargs.get('stream') else json.dumps({
            'choices': [{'message': {'content': 'recovered'}, 'finish_reason': 'stop'}],
            'usage': {}}).encode()
        response._content_consumed = True
        return response

    def async_send(request):
        if before_send():
            error = httpx.ProxyError if state.proxy_error else httpx.ConnectTimeout
            raise error('fixture connect failure')
        return httpx.Response(200, content=SSE)

    monkeypatch.setattr('lib.llm.stream.get_sync_session', lambda: SimpleNamespace(post=sync_send))
    monkeypatch.setattr('lib.http_transport.client._sync_request', sync_send)
    state.async_send = async_send
    try:
        yield state
    finally:
        manager.close()


def run_stream(monkeypatch, network, asynchronous, **kwargs):
    from lib.llm_dispatch import api
    from tests.llm_dispatch.test_dispatch_stream import _FakeDispatcher, _make_slot
    slot = _make_slot(model='gpt-test')
    slot.base_url = BASE
    dispatcher = _FakeDispatcher([slot])
    monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)
    retries = []
    options = dict(prefer_model=slot.model, strict_model=True, max_retries=1,
                   owner_user_id=7, on_retry=lambda **event: retries.append(event), **kwargs)

    async def arun():
        async with httpx.AsyncClient(transport=httpx.MockTransport(network.async_send)) as client:
            monkeypatch.setattr('lib.llm.astream.get_async_client', lambda _: client)
            return await api.async_dispatch_stream([{'role': 'user', 'content': 'hi'}], **options)

    network.slot, network.dispatcher, network.retries = slot, dispatcher, retries
    return asyncio.run(arun()) if asynchronous else api.dispatch_stream(
        [{'role': 'user', 'content': 'hi'}], **options)


@pytest.mark.parametrize('asynchronous', [False, True])
def test_connect_recovery_uses_shared_route_without_model_cooldown(monkeypatch, network, asynchronous):
    result = run_stream(monkeypatch, network, asynchronous)
    assert result[0]['content'] == 'recovered'
    assert network.sends == 2
    assert network.dispatcher.picks == 1
    assert network.slot.inflight == network.slot.total_errors == 0
    assert network.slot.cooldown_until == 0
    assert network.now[0] - 100 < 3
    assert 'Waiting for network recovery' in [e['reason'] for e in network.retries]
    assert network.retries[-1]['reason'] == 'Network recovered'


@pytest.mark.parametrize('asynchronous', [False, True])
def test_persistent_connect_failure_is_bounded_and_does_not_damage_model(monkeypatch, network, asynchronous):
    network.failures = 100
    with pytest.raises(RequestTransportError):
        run_stream(monkeypatch, network, asynchronous)
    assert network.sends == 1 + _transport.MAX_STREAM_RETRIES
    assert network.dispatcher.picks == 1
    assert network.slot.inflight == network.slot.total_errors == 0
    assert network.slot.cooldown_until == 0


@pytest.mark.parametrize('asynchronous', [False, True])
def test_stop_during_connect_recovery_never_sends_again(monkeypatch, network, asynchronous):
    with pytest.raises(AbortedError):
        run_stream(monkeypatch, network, asynchronous,
                   abort_check=lambda: network.now[0] >= 101)
    assert network.sends == 1
    assert network.slot.inflight == network.slot.total_errors == 0


def test_nonstream_connect_failure_rechecks_route_before_retry(monkeypatch, network):
    chat = importlib.import_module('lib.llm.chat')
    result, _ = chat.chat([{'role': 'user', 'content': 'hi'}], model='gpt-test',
                          base_url=BASE, api_key='fixture', owner_user_id=7)
    assert result == 'recovered'
    assert network.sends == 2
    assert network.now[0] - 100 < 3


def test_async_proxy_negotiation_failure_uses_same_recovery(monkeypatch, network):
    network.proxy_error = True
    result = run_stream(monkeypatch, network, True)
    assert result[0]['content'] == 'recovered'
    assert network.sends == 2
    assert network.slot.total_errors == 0


@pytest.mark.parametrize('asynchronous', [False, True])
def test_recovery_deadline_stops_without_model_cooldown(monkeypatch, network, asynchronous):
    network.recover_at = None
    monkeypatch.setattr(_transport, 'stream_idle_timeout_seconds', lambda: 1.5)
    with pytest.raises(RequestTransportError) as caught:
        run_stream(monkeypatch, network, asynchronous)
    assert caught.value.failure_stage == 'connect'
    assert caught.value.transport_reason == 'timeout'
    assert network.sends == 1
    assert network.slot.inflight == network.slot.total_errors == 0
    assert network.slot.cooldown_until == 0


def test_ambiguous_preheader_disconnect_does_not_replay_or_cool_model(monkeypatch, network):
    def post(*args, **kwargs):
        network.sends += 1
        raise requests.exceptions.ConnectionError('connection reset after body upload')
    monkeypatch.setattr('lib.llm.stream.get_sync_session', lambda: SimpleNamespace(post=post))
    with pytest.raises(RequestTransportError) as caught:
        run_stream(monkeypatch, network, False)
    assert caught.value.transport_reason == 'delivery_ambiguous'
    assert network.sends == 1
    assert network.slot.inflight == network.slot.total_errors == 0
    assert network.slot.cooldown_until == 0


def test_exhaustion_keeps_safe_classification_without_proxy_credentials(monkeypatch):
    from lib.http_transport import client
    from lib.llm.llm_errors import SubscriptionConnectionError, _RETRYABLE

    route = Route('proxy', 'test proxy', 'proxy',
                  proxy_url='http://user:secret@fixture.invalid:8080')
    monkeypatch.setattr(client, 'subscription_route_candidates', lambda url: [route])
    monkeypatch.setattr(client, 'report_subscription_route', lambda *args: None)
    def post(*args, **kwargs):
        raise requests.exceptions.ProxyError(route.proxy_url)
    monkeypatch.setattr(client, '_sync_request', post)
    with pytest.raises(requests.exceptions.ConnectionError) as caught:
        client.http_post(URL, json={})
    assert 'secret' not in str(caught.value)
    assert caught.value.__suppress_context__
    error = _transport.classify_request_transport_error(caught.value, base_url=URL)
    assert isinstance(error, SubscriptionConnectionError)
    assert isinstance(error, RequestTransportError)
    assert isinstance(error, _RETRYABLE)


def test_real_http_receives_body_once_after_connect_recovery(monkeypatch):
    """Fault only connection setup; retain actual HTTP, SSE parsing and cleanup."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib3.exceptions import NewConnectionError
    import lib.llm.stream as stream
    from lib.llm._sync_http import model_http_adapter

    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Content-Length', str(len(SSE)))
            self.end_headers()
            self.wfile.write(SSE)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    base = 'http://127.0.0.1:%s' % server.server_port
    now, connects = [100.0], []
    route = Route('fixture-direct', 'fixture direct', 'direct')
    manager = RouteManager(probe=lambda *_: ProbeResult('ok', 1),
                           clock=lambda: now[0], jitter=lambda delay: delay)
    manager.report(base, route, True)
    session = requests.Session()
    session.trust_env = False
    adapter = model_http_adapter(write_timeout=1, max_retries=0)
    session.mount('http://', adapter)
    connection_class = adapter.poolmanager.pool_classes_by_scheme['http'].ConnectionCls
    connect = connection_class.connect

    def flaky_connect(connection):
        connects.append(1)
        if len(connects) == 1:
            raise NewConnectionError(connection, 'fixture connect refused')
        return connect(connection)

    def sleep(seconds, abort_check=None):
        now[0] += seconds
        if now[0] >= 102:
            manager.report(base, route, True)

    monkeypatch.setattr(connection_class, 'connect', flaky_connect)
    monkeypatch.setattr(stream, 'get_sync_session', lambda: session)
    monkeypatch.setattr('lib.subscription_routes.manager', manager)
    monkeypatch.setattr('lib.proxy.subscription_route_specs', lambda url: [route])
    monkeypatch.setattr(egress, 'host_allowed', lambda url: True)
    monkeypatch.setattr(egress, '_online_egress_agents', lambda owner: [])
    monkeypatch.setattr(egress_recovery, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(_transport, 'abortable_sleep', sleep)
    monkeypatch.setattr(stream, 'abortable_sleep', sleep)
    try:
        result = stream.stream_chat(
            {'model': 'gpt-test', 'messages': [{'role': 'user', 'content': 'hi'}]},
            base_url=base, api_key='fixture', owner_user_id=7)
        assert result.message['content'] == 'recovered'
        assert len(connects) == 2
        assert len(received) == 1
        assert received[0]['messages'] == [{'role': 'user', 'content': 'hi'}]
        assert now[0] == 102
    finally:
        session.close()
        manager.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        assert not worker.is_alive()
