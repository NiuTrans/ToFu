"""Fetch layer honours the configured proxy (offline).

Incident 2026-08-21: the fetch layer had NO proxy support at all — engines
searched fine through the proxy while every page fetch went direct, so on a
host without direct egress all full_content fetches failed with DNS errors.
``do_request`` now shares the search side's adaptive proxy plan (per-host
sticky learning, proxy chain → direct fallback); with no proxy configured
the request is byte-identical to before (one direct attempt, no
``proxies=`` kwarg).
"""

import pytest
import requests

import tofu_search.config as _config
from tofu_search.fetch import http as fetch_http
from tofu_search.search.proxy_mode import (
    DIRECT,
    PROXY,
    _reset_proxy_mode_manager,
    proxy_mode_manager,
)

_URL = 'https://example.com/page'
_HOST_KEY = 'fetch:example.com'
_PROXY = {'http': 'http://proxy:8080', 'https': 'http://proxy:8080'}
_DIRECT_MARK = {'no_proxy': '*'}


class FakeResp:
    def __init__(self, status_code=200, body=b'hello'):
        self.status_code = status_code
        self._body = body
        self.headers = {'Content-Type': 'text/html', 'Content-Length': str(len(body))}
        self.closed = False

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def iter_content(self, _chunk):
        yield self._body

    def close(self):
        self.closed = True


class FakeSession:
    """Records per-call kwargs; the ``behaviour`` fn maps proxies→resp/raise."""
    def __init__(self, behaviour):
        self.calls = []
        self._behaviour = behaviour

    def get(self, url, **kw):
        self.calls.append(kw)
        return self._behaviour(kw.get('proxies'))


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    for k in ('https_proxy', 'HTTPS_PROXY', 'http_proxy', 'HTTP_PROXY',
              'all_proxy', 'ALL_PROXY'):
        monkeypatch.delenv(k, raising=False)
    _reset_proxy_mode_manager()
    yield
    _reset_proxy_mode_manager()


def _use(monkeypatch, behaviour, **cfg_overrides):
    sess = FakeSession(behaviour)
    monkeypatch.setattr(fetch_http, '_session', sess)
    monkeypatch.setattr(_config, '_global_config',
                        _config.SearchConfig(**cfg_overrides))
    return sess


def test_no_proxy_single_direct_attempt_no_kwarg(monkeypatch):
    sess = _use(monkeypatch, lambda _p: FakeResp())
    resp, raw = fetch_http.do_request(_URL, 5)
    assert raw == b'hello'
    assert len(sess.calls) == 1
    assert 'proxies' not in sess.calls[0]      # byte-identical to before


def test_proxy_failure_falls_back_to_direct(monkeypatch):
    def behaviour(proxies):
        if proxies == _PROXY:
            raise requests.exceptions.ProxyError('proxy dead')
        return FakeResp()

    sess = _use(monkeypatch, behaviour, proxy_url='http://proxy:8080')
    resp, raw = fetch_http.do_request(_URL, 5)
    assert raw == b'hello'
    assert [c.get('proxies') for c in sess.calls] == [_PROXY, _DIRECT_MARK]
    assert proxy_mode_manager._preferred(_HOST_KEY) == DIRECT


def test_direct_failure_falls_back_to_proxy(monkeypatch):
    # Pin DIRECT-first ordering (as if previously learned), direct is dead.
    proxy_mode_manager.record_success(_HOST_KEY, DIRECT)

    def behaviour(proxies):
        if proxies == _DIRECT_MARK:
            raise requests.exceptions.ConnectionError('no direct egress')
        return FakeResp()

    sess = _use(monkeypatch, behaviour, proxy_url='http://proxy:8080')
    resp, raw = fetch_http.do_request(_URL, 5)
    assert raw == b'hello'
    assert [c.get('proxies') for c in sess.calls] == [_DIRECT_MARK, _PROXY]
    assert proxy_mode_manager._preferred(_HOST_KEY) == PROXY


def test_dual_disabled_single_proxy_attempt(monkeypatch):
    def behaviour(proxies):
        raise requests.exceptions.ProxyError('proxy dead')

    sess = _use(monkeypatch, behaviour, proxy_url='http://proxy:8080',
                proxy_dual_attempt=False)
    with pytest.raises(requests.exceptions.ProxyError):
        fetch_http.do_request(_URL, 5)
    assert len(sess.calls) == 1


def test_http_status_does_not_switch_path(monkeypatch):
    sess = _use(monkeypatch, lambda _p: FakeResp(status_code=403),
                proxy_url='http://proxy:8080')
    with pytest.raises(fetch_http.HttpError) as exc:
        fetch_http.do_request(_URL, 5)
    assert exc.value.status_code == 403
    assert len(sess.calls) == 1                # statuses never path-switch
