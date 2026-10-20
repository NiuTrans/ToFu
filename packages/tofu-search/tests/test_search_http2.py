"""DDG HTTP/2 recovery keeps the common search policy and bounded transport."""

import ssl
from types import SimpleNamespace

import httpx
import pytest
import requests

from tofu_search import SearchConfig, http_client, perform_web_search
from tofu_search.config import bind_call_config, get_config
from tofu_search.search import _common, orchestrator
from tofu_search.search.engines import ddg
from tofu_search.search.proxy_mode import ProxyModeManager

pytestmark = pytest.mark.unit
QUERY = 'Python asyncio TaskGroup cancellation'
HTML = (
    '<div class="result"><a class="result__a" '
    'href="https://docs.python.org/3/library/asyncio-task.html">'
    'Python asyncio TaskGroup cancellation</a><a class="result__snippet">'
    'TaskGroup propagates cancellation between Python asyncio tasks.</a></div>'
).encode()


class Stream(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False
        self.reads = 0

    def __iter__(self):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk() if callable(chunk) else chunk

    def close(self):
        self.closed = True


@pytest.fixture
def transport(monkeypatch):
    """Keep the real HTTPX response lifecycle, with no sockets or TLS I/O."""
    seen = {'clients': [], 'options': [], 'requests': [], 'responses': [], 'streams': []}
    original = httpx.Client

    def handler(request):
        seen['requests'].append(request)
        outcome = seen['responses'].pop(0) if seen['responses'] else (200, [HTML], {})
        if isinstance(outcome, Exception):
            raise outcome
        status, chunks, headers = outcome
        stream = Stream(chunks)
        seen['streams'].append(stream)
        return httpx.Response(status, stream=stream, headers=headers, request=request,
                              extensions={'http_version': b'HTTP/2'})

    def client(**options):
        seen['options'].append(options)
        instance = original(transport=httpx.MockTransport(handler), trust_env=False,
                            headers=options.get('headers'))
        seen['clients'].append(instance)
        if seen.get('on_client_ready'):
            seen['on_client_ready']()
        return instance

    monkeypatch.setattr(httpx, 'Client', client)
    monkeypatch.setattr(_common, 'engine_circuit', _common._EngineCircuit())
    monkeypatch.setattr(_common, 'proxy_mode_manager', ProxyModeManager())
    monkeypatch.setattr(_common, 'host_throttle', _common._HostThrottle())
    # The old HTTP/1-only route is reachable, but cannot complete this search.
    def challenged(*args, **kwargs):
        response = requests.Response()
        response.status_code = 202
        response._content = b'<form id="challenge-form"></form>'
        response._content_consumed = True
        return response
    monkeypatch.setattr(_common.search_session, 'get', challenged)
    return seen


def direct_get(**kwargs):
    return http_client._search_http2_get(
        'https://html.duckduckgo.com/html/', params={'q': QUERY},
        headers={'User-Agent': 'owned-test-header'}, timeout=1,
        deadline=http_client.time.monotonic() + 2, **kwargs)


def test_ddg_can_retrieve_through_http2_with_query_and_freshness_intact(transport):
    with bind_call_config(SearchConfig(proxy_dual_attempt=False, min_request_interval_ms=0)):
        rows = ddg.search_ddg_html(QUERY, freshness='month')
    assert rows[0]['url'] == 'https://docs.python.org/3/library/asyncio-task.html'
    request, = transport['requests']
    assert request.url.params['q'] == QUERY and request.url.params['df'] == 'm'
    assert transport['options'][0]['http2'] is True
    assert all(client.is_closed for client in transport['clients'])


def test_http2_evidence_survives_the_public_search_pipeline(transport):
    rows = perform_web_search(
        QUERY, engines=['DDG-HTML'], fetch_pages=False,
        config=SearchConfig(proxy_dual_attempt=False, min_request_interval_ms=0))
    assert rows and rows[0]['url'] == 'https://docs.python.org/3/library/asyncio-task.html'
    assert rows._search_diag['engine_ok'] == ['DDG-HTML']
    assert not rows._search_diag['engine_errors']


@pytest.mark.parametrize('proxies, expected', [
    (None, 'http://environment.invalid:8123'),
    ({'http': '', 'https': '', 'all': ''}, None),
    ({'https': 'http://chosen.invalid:8123'}, 'http://chosen.invalid:8123'),
])
def test_proxy_selection_is_explicit_and_direct_never_inherits_environment(
        transport, monkeypatch, proxies, expected):
    monkeypatch.setenv('https_proxy', 'http://environment.invalid:8123')
    monkeypatch.delenv('no_proxy', raising=False)
    monkeypatch.delenv('NO_PROXY', raising=False)
    direct_get(proxies=proxies)
    assert transport['options'][0]['proxy'] == expected
    assert transport['options'][0]['trust_env'] is False
    assert transport['options'][0]['verify'] is True
    assert transport['requests'][0].headers['user-agent'] == 'owned-test-header'


def test_http2_failure_uses_the_existing_proxy_failover_owner(transport):
    transport['responses'].append(httpx.ProxyError('private proxy password'))
    with bind_call_config(SearchConfig(proxy_url='http://chosen.invalid:8123',
                                       proxy_dual_attempt=True, min_request_interval_ms=0)):
        assert ddg.search_ddg_html(QUERY)
    assert [row['proxy'] for row in transport['options']] == ['http://chosen.invalid:8123', None]
    assert all(client.is_closed for client in transport['clients'])


@pytest.mark.parametrize('error, expected', [
    (httpx.ConnectTimeout('private credentials'), requests.ConnectTimeout),
    (httpx.ReadTimeout('private credentials'), requests.ReadTimeout),
    (httpx.ConnectError('private credentials'), requests.ConnectionError),
    (httpx.ProxyError('private credentials'), requests.exceptions.ProxyError),
    (httpx.RemoteProtocolError('private credentials'), requests.RequestException),
])
def test_transport_errors_keep_retry_semantics_without_disclosing_secrets(transport, error, expected):
    transport['responses'].append(error)
    with pytest.raises(expected) as caught:
        direct_get()
    assert 'private credentials' not in str(caught.value)
    assert all(client.is_closed for client in transport['clients'])


def test_configured_ca_bundle_keeps_certificate_and_hostname_verification(transport, monkeypatch):
    monkeypatch.setenv('REQUESTS_CA_BUNDLE', requests.certs.where())
    direct_get()
    verify = transport['options'][0]['verify']
    assert isinstance(verify, ssl.SSLContext)
    assert verify.check_hostname and verify.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize('variable', ['REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE'])
def test_configured_ca_bundle_also_verifies_https_proxy_tls(transport, monkeypatch, variable):
    for name in ('REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(variable, requests.certs.where())
    direct_get(proxies={'https': 'https://chosen.invalid:8443'})
    options, = transport['options']
    proxy = options['proxy']
    assert isinstance(proxy, httpx.Proxy)
    assert proxy.url == httpx.URL('https://chosen.invalid:8443')
    assert proxy.ssl_context is options['verify']
    assert proxy.ssl_context.check_hostname
    assert proxy.ssl_context.verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize('headers', [{}, {'content-length': '99'}, {'content-length': 'invalid'}])
def test_decoded_byte_budget_or_invalid_length_fails_and_closes(transport, monkeypatch, headers):
    monkeypatch.setattr(http_client, '_SEARCH_RESPONSE_BYTES', 3)
    transport['responses'].append((200, [b'abcd'], headers))
    with pytest.raises(requests.RequestException, match='response'):
        direct_get()
    assert all(stream.closed for stream in transport['streams'])
    assert all(client.is_closed for client in transport['clients'])


@pytest.mark.parametrize('status', [202, 204, 206, 302])
def test_noncompleted_search_status_is_not_a_no_match(transport, status):
    transport['responses'] = [(status, [b'never download this body'], {})] * 2
    with bind_call_config(SearchConfig(proxy_dual_attempt=False, min_request_interval_ms=0)):
        with pytest.raises(requests.HTTPError):
            ddg.search_ddg_html(QUERY)
    assert all(stream.closed and stream.reads == 0 for stream in transport['streams'])


def test_deadline_during_stream_preserves_timeout_type_and_closes(transport, monkeypatch):
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(http_client, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    def late_chunk():
        clock.now = 3
        return HTML
    transport['responses'].append((200, [late_chunk], {}))
    with pytest.raises(requests.Timeout):
        direct_get()
    assert all(stream.closed for stream in transport['streams'])
    assert all(client.is_closed for client in transport['clients'])


def test_shared_deadline_prevents_rate_limit_retry_after_budget(transport, monkeypatch):
    clock = SimpleNamespace(now=0)
    def sleep(seconds):
        clock.now += seconds
    fake_time = SimpleNamespace(monotonic=lambda: clock.now, time=lambda: clock.now, sleep=sleep)
    monkeypatch.setattr(_common, 'time', fake_time)
    monkeypatch.setattr(http_client, 'time', fake_time)
    transport['responses'].append((202, [], {}))
    with bind_call_config(SearchConfig(proxy_dual_attempt=False, min_request_interval_ms=0,
                                       search_deadline_secs=0.1)):
        with pytest.raises(requests.Timeout):
            ddg.search_ddg_html(QUERY)
    assert len(transport['requests']) == 1
    assert clock.now <= 0.1


def test_client_setup_cannot_start_a_request_after_the_deadline(transport, monkeypatch):
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(http_client, 'time', SimpleNamespace(monotonic=lambda: clock.now))
    transport['on_client_ready'] = lambda: setattr(clock, 'now', 3)
    with pytest.raises(requests.Timeout):
        direct_get()
    assert not transport['requests']
    assert all(client.is_closed for client in transport['clients'])


def test_completed_empty_page_remains_a_genuine_empty(transport):
    transport['responses'].append((200, [b'<main>No results.</main>'], {}))
    with bind_call_config(SearchConfig(proxy_dual_attempt=False, min_request_interval_ms=0)):
        assert ddg.search_ddg_html(QUERY) == []
    assert not _common.engine_circuit._state
    assert all(stream.closed for stream in transport['streams'])


def test_pipeline_recovery_uses_remaining_budget_instead_of_restarting_it(monkeypatch):
    clock = SimpleNamespace(now=0)
    def sleep(seconds):
        clock.now += seconds
    monkeypatch.setattr(orchestrator, 'time', SimpleNamespace(time=lambda: clock.now, sleep=sleep))
    for name in ('search_brave', 'search_bing', 'search_ddg_api', 'search_searxng', 'search_marginalia'):
        monkeypatch.setattr(orchestrator, name, lambda *args: [])
    budgets = []
    def search(*args):
        budgets.append(get_config().search_deadline_secs)
        if len(budgets) == 1:
            clock.now += 8
            return []
        return [{'title': QUERY, 'snippet': QUERY,
                 'url': 'https://docs.python.org/3/library/asyncio-task.html', 'source': 'DuckDuckGo'}]
    monkeypatch.setattr(orchestrator, 'search_ddg_html', search)
    cfg = SearchConfig(search_deadline_secs=20)
    assert perform_web_search(QUERY, fetch_pages=False, filter_pages=False, config=cfg)
    assert len(budgets) == 2 and budgets[0] == 20
    assert 0 < budgets[1] <= 11.2
    assert cfg.search_deadline_secs == 20
