"""All application HTTP uses shared route admission and connect-only failover."""
from __future__ import annotations

import asyncio
import io
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler

import pytest
import requests

from lib import proxy
from lib.network_routes import ProbeResult, Route, RouteManager
from tests.support.loopback import LoopbackThreadingHTTPServer

pytestmark = pytest.mark.unit


@pytest.fixture
def isolated_routes(monkeypatch):
    for key in ('http_proxy', 'https_proxy', 'all_proxy', 'HTTP_PROXY',
                'HTTPS_PROXY', 'ALL_PROXY', 'no_proxy', 'NO_PROXY'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(proxy, '_proxy_pool', [])
    monkeypatch.setattr(proxy, '_proxy_topology_epoch', 0)
    monkeypatch.setattr(proxy, '_pool_health', {})
    monkeypatch.setattr(proxy, '_pool_choice', {})
    monkeypatch.setattr(proxy, '_cred_cache', {})
    monkeypatch.setattr(proxy, '_bypass_domains', ())
    monkeypatch.setattr(proxy, '_proxy_config', {})
    monkeypatch.setattr(proxy, '_ENV_HTTP_PROXY', '')
    monkeypatch.setattr(proxy, '_ENV_HTTPS_PROXY', '')
    return proxy


def _entry(name, scope='global'):
    return {'id': name, 'name': name, 'scope': scope, 'enabled': True,
            'url': f'http://{name}.invalid:8080'}


def test_unknown_hosts_automatically_enter_network_routing(isolated_routes):
    isolated_routes._proxy_pool = [_entry('global'), _entry('oauth', 'subscription')]
    url = 'https://new-provider.invalid/v1/request'
    generic = isolated_routes.network_route_specs(url)
    subscription = isolated_routes.network_route_specs(url, scope='subscription')
    assert [route.pool_id for route in generic if route.pool_id] == ['global']
    assert [route.pool_id for route in subscription if route.pool_id] == ['oauth', 'global']


@pytest.mark.parametrize('url', [
    'http://127.0.0.1:1234/private', 'http://[::1]:1234/private',
    'http://localhost:1234/private', 'https://service.internal.invalid/private',
])
def test_declared_bypass_never_probes_or_uses_a_proxy(isolated_routes, monkeypatch, url):
    isolated_routes._proxy_pool = [_entry('global')]
    monkeypatch.setenv('no_proxy', '.internal.invalid')
    routes = isolated_routes.network_route_specs(url)
    assert len(routes) == 1 and routes[0].mode == 'direct'


def test_health_isolated_by_scheme_port_and_destination():
    manager = RouteManager(probe=lambda *_: ProbeResult('network_fail'), jitter=lambda value: value)
    route = Route('proxy', 'configured proxy', 'proxy', proxy_url='http://proxy.invalid:8080')
    try:
        manager.report('https://service.invalid:443/one', route, True, 10)
        assert manager.cached_candidates('https://service.invalid/two', [route]) == [route]
        assert manager.cached_candidates('http://service.invalid:80/one', [route]) == []
        assert manager.cached_candidates('https://service.invalid:8443/one', [route]) == []
        manager.report('https://other.invalid/one', route, False)
        assert manager.cached_candidates('https://service.invalid/two', [route]) == [route]
    finally:
        manager.close()


@pytest.fixture
def proxy_server():
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('content-length') or 0))
            received.append({'path': self.path, 'body': body, 'headers': dict(self.headers)})
            payload = json.dumps({'ok': True}).encode()
            self.send_response(200)
            self.send_header('content-length', str(len(payload)))
            self.send_header('content-type', 'application/json')
            self.end_headers()
            self.wfile.write(payload)

    server = LoopbackThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', received
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


@pytest.fixture
def failover_routes(monkeypatch, proxy_server, isolated_routes):
    url, received = proxy_server
    # Bound but not listening: a deterministic TCP refusal before request bytes.
    refused = socket.socket()
    refused.bind(('127.0.0.1', 0))
    routes = [
        Route('bad', 'unreachable proxy', 'proxy',
              proxy_url=f'http://127.0.0.1:{refused.getsockname()[1]}'),
        Route('good', 'working proxy', 'proxy', proxy_url=url),
    ]
    isolated_routes._proxy_pool = [
        {**_entry(route.route_id), 'url': route.proxy_url} for route in routes]
    reports = []
    monkeypatch.setattr(proxy, 'network_route_candidates', lambda *a, **k: routes, raising=False)
    monkeypatch.setattr(proxy, 'report_network_route',
                        lambda target, route, ok, *a, **k: reports.append((route.route_id, ok)),
                        raising=False)
    try:
        yield received, reports
    finally:
        refused.close()


@pytest.mark.parametrize('multipart', [False, True])
def test_sync_request_fails_over_before_delivery_with_the_same_body(failover_routes, multipart):
    from lib.http_transport.client import http_request
    received, reports = failover_routes
    content = b'one application effect'
    kwargs = {'files': {'file': ('sample.txt', io.BytesIO(content))}} if multipart else {'data': content}
    response = http_request('POST', 'http://service.invalid/effect', timeout=1, **kwargs)
    try:
        assert response.json() == {'ok': True}
        assert len(received) == 1 and content in received[0]['body']
        assert reports == [('bad', False), ('good', True)]
    finally:
        response.close()


def test_async_request_and_stream_share_route_failover(failover_routes):
    from lib.http_transport.client import async_http_request, async_http_stream, close_async_http_clients
    received, reports = failover_routes

    async def scenario():
        try:
            response = await async_http_request(
                'POST', 'http://service.invalid/effect', content=b'normal', timeout=1)
            assert response.json() == {'ok': True}
            async with async_http_stream(
                    'POST', 'http://service.invalid/effect', content=b'stream', timeout=1) as stream:
                assert json.loads(await stream.aread()) == {'ok': True}
        finally:
            await close_async_http_clients()

    asyncio.run(scenario())
    assert [entry['body'] for entry in received] == [b'normal', b'stream']
    assert reports == [('bad', False), ('good', True)] * 2


@pytest.mark.parametrize('error', [
    requests.exceptions.ReadTimeout('response did not arrive'),
    requests.exceptions.ConnectionError('delivery is ambiguous'),
    requests.exceptions.SSLError('TLS connection broke after sending'),
])
def test_sync_ambiguous_delivery_never_replays(monkeypatch, failover_routes, error):
    from lib.http_transport.client import http_request
    attempted = []

    def send(adapter, prepared, **kwargs):
        attempted.append(prepared)
        raise error

    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)
    with pytest.raises(type(error)):
        http_request('POST', 'http://service.invalid/effect', data=b'one effect', timeout=1)
    assert len(attempted) == 1


def test_old_connect_context_does_not_make_a_response_failure_replayable():
    from lib.network_routes import is_safe_connect_failure
    error = requests.exceptions.ReadTimeout('response stalled')
    error.__context__ = requests.exceptions.ConnectTimeout('an earlier attempt')
    assert not is_safe_connect_failure(error)


def test_generic_probes_never_post_or_forward_path_secrets(monkeypatch):
    from lib.network_routes import probe_route
    observed = []
    class Session:
        trust_env = True

        def request(self, method, url, **kwargs):
            observed.append((method, url, kwargs))
            return type('Response', (), {'status_code': 403, 'close': lambda self: None})()

        def close(self):
            pass

    monkeypatch.setattr(requests, 'Session', Session)
    result = probe_route('https://new-api.invalid/mutate?token=secret#fragment',
                         Route('direct', 'direct', 'direct'))
    assert result.ok  # Application auth/permission is not a broken network.
    assert observed[0][0:2] == ('HEAD', 'https://new-api.invalid/')
    assert 'headers' not in observed[0][2] and 'json' not in observed[0][2]


@pytest.mark.parametrize('factory_name', ['application', 'mcp'])
def test_sdk_client_uses_shared_failover(factory_name, failover_routes):
    from lib.http_transport.outbound_async import managed_async_client
    from lib.http_transport.sdk import mcp_http_client
    received, reports = failover_routes
    factory = managed_async_client if factory_name == 'application' else mcp_http_client

    async def run():
        async with factory() as client:
            response = await client.post('http://sdk.invalid/call', json={'x': 1})
            assert response.json() == {'ok': True}
    asyncio.run(run())
    assert len(received) == 1
    assert reports == [('bad', False), ('good', True)]


def test_cancelled_route_wait_is_joined_even_when_worker_fails(monkeypatch):
    started, finished = threading.Event(), threading.Event()
    def wait(_url, *, cancelled, **_):
        started.set()
        while not cancelled():
            finished.wait(0.001)
        finished.set()
        raise RuntimeError('worker failed during cancellation')
    monkeypatch.setattr(proxy, 'network_route_candidates', wait)

    async def run():
        task = asyncio.create_task(proxy.async_network_route_candidates('https://sdk.invalid'))
        await asyncio.to_thread(started.wait)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
    asyncio.run(run())


def test_route_health_retention_reclaims_only_cache():
    from lib.network_routes import HEALTH_CACHE_ORIGINS, HEALTH_CACHE_TTL_S
    now = [1.0]
    manager = RouteManager(clock=lambda: now[0])
    direct = Route('direct', 'direct', 'direct')
    try:
        for index in range(HEALTH_CACHE_ORIGINS + 3):
            manager.report(f'https://host{index}.invalid', direct, True, 1)
        assert len(manager.status()['routes']) == HEALTH_CACHE_ORIGINS
        assert manager.cached_candidates('https://host0.invalid', [direct]) == []
        now[0] += HEALTH_CACHE_TTL_S + 1
        manager.report('https://next.invalid', direct, True, 1)
        assert list(manager.status()['routes']) == ['https://next.invalid:443']
    finally:
        manager.close()


def test_cancel_during_route_probe_never_sends_late_request(monkeypatch):
    from lib.http_transport.outbound import (
        managed_session, network_cancellation, NetworkRequestCancelled)
    stopped = threading.Event()
    def candidates(*args, **kwargs):
        stopped.set()
        return [Route('direct', 'direct', 'direct')]
    monkeypatch.setattr(proxy, 'network_route_candidates', candidates)
    def send(*args, **kwargs):
        pytest.fail('cancelled request reached the wire')
    monkeypatch.setattr(requests.adapters.HTTPAdapter, 'send', send)
    with managed_session() as session, network_cancellation(stopped.is_set):
        with pytest.raises(NetworkRequestCancelled):
            session.post('https://cancel.invalid/', json={'operation': 1})


def test_config_rotation_preserves_open_stream_and_retires_its_pool(monkeypatch, isolated_routes):
    import httpx
    from lib.http_transport.outbound_async import managed_async_client
    events = []
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'complete body'
        async def aclose(self):
            events.append('body closed')
    class Wire:
        def __init__(self, **kwargs):
            self.number = len([e for e in events if e == 'pool created'])
            events.append('pool created')
        async def handle_async_request(self, request):
            return httpx.Response(200, stream=Stream())
        async def aclose(self):
            events.append(('pool closed', self.number))
    monkeypatch.setattr(httpx, 'AsyncHTTPTransport', Wire)
    async def run():
        async with managed_async_client() as client:
            async with client.stream('GET', 'http://localhost/a') as first:
                isolated_routes._proxy_topology_epoch += 1
                assert (await client.get('http://localhost/b')).content == b'complete body'
                assert ('pool closed', 0) not in events
                assert await first.aread() == b'complete body'
            assert ('pool closed', 0) in events
        assert ('pool closed', 1) in events
    asyncio.run(run())
    assert events.count(('pool closed', 0)) == 1
    assert events.count(('pool closed', 1)) == 1


def test_client_close_joins_physical_cleanup_after_cancellation(monkeypatch, isolated_routes):
    import httpx
    from lib.http_transport.outbound_async import managed_async_client
    async def run():
        closing, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class Wire:
            def __init__(self, **kwargs):
                pass
            async def handle_async_request(self, request):
                return httpx.Response(200, content=b'ok')
            async def aclose(self):
                closing.set()
                await release.wait()
                closed.set()
        monkeypatch.setattr(httpx, 'AsyncHTTPTransport', Wire)
        client = managed_async_client()
        await client.get('http://localhost/')
        task = asyncio.create_task(client.aclose())
        await closing.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not closed.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert closed.is_set()
    asyncio.run(run())


def test_future_oauth_host_gets_subscription_scope_without_host_list(monkeypatch):
    from lib.oauth import http
    from lib.http_transport import client
    observed = []
    monkeypatch.setattr(client, 'http_post', lambda *args, **kwargs: observed.append(kwargs))
    http.http_post('https://future-provider.invalid/token', data={'grant_type': 'test'})
    assert observed[0]['network_scope'] == 'subscription'


@pytest.mark.parametrize('transport_kind', ['sync', 'async', 'mcp'])
def test_httpx_connect_response_timeout_can_change_route(monkeypatch, isolated_routes, transport_kind):
    import importlib
    from lib.http_transport.outbound_async import managed_async_client
    from lib.http_transport.outbound_httpx import managed_httpx_client
    http = importlib.import_module('httpx2' if transport_kind == 'mcp' else 'httpx')
    release, connected = threading.Event(), threading.Event()
    delivered = []
    class ProxyHandler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_CONNECT(self):
            connected.set()
            release.wait()
    server = LoopbackThreadingHTTPServer(('127.0.0.1', 0), ProxyHandler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    routes = [Route('hung', 'hung CONNECT', 'proxy', proxy_url=f'http://127.0.0.1:{server.server_port}'),
              Route('good', 'working route', 'proxy', proxy_url='http://good.invalid:8080')]
    monkeypatch.setattr(proxy, 'network_route_candidates', lambda *a, **kw: routes)
    monkeypatch.setattr(proxy, 'report_network_route', lambda *a, **kw: None)
    base = http.HTTPTransport if transport_kind == 'sync' else http.AsyncHTTPTransport
    class Wire(base):
        def __init__(self, **options):
            self.good = options['proxy'] == 'http://good.invalid:8080'
            super().__init__(**options)
        def handle_request(self, request):
            if not self.good:
                return super().handle_request(request)
            delivered.append(request.read())
            return http.Response(200, content=b'ok')
        async def handle_async_request(self, request):
            if not self.good:
                return await super().handle_async_request(request)
            delivered.append(await request.aread())
            return http.Response(200, content=b'ok')
    monkeypatch.setattr(http, 'HTTPTransport' if transport_kind == 'sync' else 'AsyncHTTPTransport', Wire)
    try:
        if transport_kind == 'sync':
            with managed_httpx_client(timeout=0.1) as client:
                assert client.post('https://target.invalid/operation', content=b'once').text == 'ok'
        else:
            async def run():
                async with managed_async_client(http_module=http, timeout=0.1) as client:
                    response = await client.post('https://target.invalid/operation', content=b'once')
                    assert response.text == 'ok'
            asyncio.run(run())
        assert connected.is_set()
        assert delivered == [b'once']
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join()
