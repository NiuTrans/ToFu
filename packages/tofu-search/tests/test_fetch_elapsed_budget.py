"""Wall-clock corrections cannot extend or consume a fetch's elapsed allowance."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from importlib import import_module
from threading import Barrier
from types import SimpleNamespace

import pytest
import requests

from tofu_search import fetch_page_content
from tofu_search.config import SearchConfig, bind_call_config

core = import_module('tofu_search.fetch.core')
http = import_module('tofu_search.fetch.http')
budgets = import_module('tofu_search.fetch.request_budget')
pytestmark = pytest.mark.unit
URL = 'https://budget.example/evidence.json'


class Response:
    ok = True
    status_code = 200
    encoding = 'utf-8'

    def __init__(self, read):
        self.headers = {'Content-Type': 'application/json'}
        self.read = read
        self.closed = False

    def iter_content(self, size):
        return self.read()

    def close(self):
        self.closed = True


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(elapsed=0, correction=0)
    scoped_time = SimpleNamespace(
        time=lambda: 1_000_000 + state.elapsed + state.correction,
        monotonic=lambda: 100 + state.elapsed,
    )
    for module in (core, http, budgets):
        monkeypatch.setattr(module, 'time', scoped_time)
    return state


@pytest.fixture(autouse=True)
def request_config(monkeypatch):
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        monkeypatch.delenv(name, raising=False)
    with bind_call_config(SearchConfig(proxy_url='', proxy_dual_attempt=False,
                                      block_private_addresses=False, fetch_max_bytes=1024)):
        yield


@pytest.fixture
def page_sources(monkeypatch):
    monkeypatch.setattr(core, '_get_reader', lambda url: None)
    monkeypatch.setattr(core, 'get_auth_source_provider', lambda: None)
    monkeypatch.setattr(core, '_should_fetch', lambda url: True)
    monkeypatch.setattr(core, '_is_known_spa', lambda url: False)
    monkeypatch.setattr(core, '_fetch_cache', SimpleNamespace(get=lambda *args: None,
                                                            put=lambda *args: None))
    monkeypatch.setattr(core, '_circuit', SimpleNamespace(
        is_open=lambda url: False, record_failure=lambda url: None,
        record_success=lambda url: None))
    called = []

    def browser(url, max_chars, reason='unknown'):
        called.append(url)
        return 'recovered evidence'

    monkeypatch.setattr(core, '_try_browser_fetch', browser)
    return called


@pytest.mark.parametrize('correction', [-3600, 3600])
@pytest.mark.parametrize('elapsed', [1, 6])
@pytest.mark.parametrize('decode_retry', [False, True])
def test_body_budget_tracks_elapsed_time_on_both_decoding_paths(
        monkeypatch, clock, correction, elapsed, decode_retry):
    def read():
        clock.elapsed = elapsed
        clock.correction = correction
        yield b'{"answer": "kept"}'

    final = Response(read)
    responses = [final]
    if decode_retry:
        def corrupt():
            raise requests.exceptions.ContentDecodingError('broken br stream')
        responses.insert(0, Response(corrupt))
    pending = iter(responses)
    monkeypatch.setattr(http, '_session', SimpleNamespace(get=lambda *args, **kwargs: next(pending)))
    if elapsed >= 5:
        with pytest.raises(requests.Timeout):
            http.do_request(URL, 10, deadline_ts=1_000_005)
    else:
        response, raw = http.do_request(URL, 10, deadline_ts=1_000_005)
        assert response is final and raw == b'{"answer": "kept"}'
    assert all(response.closed for response in responses)


@pytest.mark.parametrize('correction', [-3600, 3600])
@pytest.mark.parametrize('elapsed', [1, 6])
def test_url_fallback_admission_tracks_elapsed_time(
        monkeypatch, clock, page_sources, correction, elapsed):
    def failed(*args, **kwargs):
        clock.elapsed = elapsed
        clock.correction = correction
        raise requests.Timeout('primary failed')

    monkeypatch.setattr(core, '_do_request', failed)
    result = fetch_page_content(URL, timeout=10, deadline_secs=5)
    assert result == ('recovered evidence' if elapsed < 5 else None)
    assert page_sources == ([URL] if elapsed < 5 else [])


@pytest.mark.parametrize('correction', [-3600, 3600])
@pytest.mark.parametrize('elapsed', [1, 6])
def test_url_budget_reaches_http_without_reinterpreting_wall_time(
        monkeypatch, clock, page_sources, correction, elapsed):
    def preflight(url):
        clock.elapsed = elapsed
        clock.correction = correction
        return None

    monkeypatch.setattr(core, '_get_reader', preflight)
    response = Response(lambda: iter([b'{"answer": "kept"}']))
    opened = []

    def get(*args, **kwargs):
        opened.append(URL)
        return response

    monkeypatch.setattr(http, '_session', SimpleNamespace(get=get))
    result = fetch_page_content(URL, timeout=10, deadline_secs=5)
    if elapsed < 5:
        assert result == '{"answer": "kept"}'
        assert opened == [URL] and response.closed
    else:
        assert result is None and not opened
    assert not page_sources


def test_concurrent_url_budgets_do_not_change_each_others_fallback(
        monkeypatch, clock, page_sources):
    started = Barrier(2)
    advanced = Barrier(2)

    def failed(*args, **kwargs):
        started.wait(timeout=5)
        clock.elapsed = 10
        advanced.wait(timeout=5)
        raise requests.Timeout('primary failed')

    monkeypatch.setattr(core, '_do_request', failed)
    with ThreadPoolExecutor(max_workers=2) as workers:
        short = workers.submit(copy_context().run, fetch_page_content, URL+'?short', deadline_secs=2)
        long = workers.submit(copy_context().run, fetch_page_content, URL+'?long', deadline_secs=20)
        assert short.result(timeout=5) is None
        assert long.result(timeout=5) == 'recovered evidence'
    assert page_sources == [URL+'?long']


def test_interrupted_page_scope_does_not_expire_a_later_independent_request(
        monkeypatch, clock, page_sources):
    class StopPage(BaseException):
        pass

    def preflight(url):
        clock.elapsed = 6
        raise StopPage('interrupted reader')

    monkeypatch.setattr(core, '_get_reader', preflight)
    with pytest.raises(StopPage):
        fetch_page_content(URL, deadline_secs=5)
    response = Response(lambda: iter([b'next request']))
    monkeypatch.setattr(http, '_session', SimpleNamespace(get=lambda *args, **kwargs: response))
    assert http.do_request(URL, 10)[1] == b'next request'
    assert response.closed and not page_sources


def test_disabled_page_cap_still_allows_http_after_long_preflight(monkeypatch, clock, page_sources):
    def preflight(url):
        clock.elapsed = 60
        clock.correction = 3600
        return None

    monkeypatch.setattr(core, '_get_reader', preflight)
    response = Response(lambda: iter([b'{"answer": "kept"}']))
    monkeypatch.setattr(http, '_session', SimpleNamespace(get=lambda *args, **kwargs: response))
    assert fetch_page_content(URL, timeout=10, deadline_secs=0) == '{"answer": "kept"}'
    assert response.closed and not page_sources


@pytest.mark.parametrize('exhausting_hop', ['reader', 'browser', 'replay'])
def test_expired_provider_hop_cannot_start_another_identity_or_http_path(
        monkeypatch, clock, page_sources, exhausting_hop):
    calls = []

    def hop(name):
        def run(*args, **kwargs):
            calls.append(name)
            if name == exhausting_hop:
                clock.elapsed = 6
                return None
            return 'late evidence must not be requested'
        return run

    source = {'domain': 'budget.example', 'cookies': [{'name': 'session', 'value': 'test'}],
              'access_strategy': 'cookies_replay' if exhausting_hop == 'replay' else 'browser_first'}
    monkeypatch.setattr(core, 'get_auth_source_provider', lambda: SimpleNamespace(
        match_source=lambda url: source))
    if exhausting_hop == 'reader':
        monkeypatch.setattr(core, '_get_reader', lambda url: SimpleNamespace(read=hop('reader')))
    monkeypatch.setattr(core, '_try_browser_fetch', hop('browser'))
    monkeypatch.setattr(core, '_try_authenticated_fetch', hop('replay'))
    monkeypatch.setattr(core, '_do_request', hop('http'))
    diagnosis = {}
    assert fetch_page_content(URL, deadline_secs=5, diag=diagnosis) is None
    assert calls == [exhausting_hop]
    assert diagnosis['reason'] == 'deadline'
