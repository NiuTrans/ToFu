"""tests/self_update/test_self_update_version.py — GitHub tags API failure handling.

Pins the two production fixes for the proxy-403 spam on api.github.com:

  1. A proxy that refuses the CONNECT tunnel (ProxyError / "Tunnel connection
     failed: 403") is failed over to the remaining healthy pool entries, and
     — only when NO pool is configured — retried DIRECTLY once, so the tags
     check still succeeds when only the chosen proxy is broken for GitHub. A
     bare direct retry is NEVER attempted while a pool exists (a pooled
     deployment typically has no direct egress, and the guaranteed DNS error
     would otherwise be cached as the failure, hiding the real proxy cause).
  2. A failure is cached for ``TOFU_UPDATE_CHECK_MIN_INTERVAL_S`` (default
     60s); repeated checks within that window return the cached error without
     touching the network (the first failure stays a WARNING, the rest are
     DEBUG).
"""

from __future__ import annotations

import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from lib.self_update import _version as ver  # noqa: E402

pytestmark = pytest.mark.unit


def _reset_state(monkeypatch):
    monkeypatch.setenv('TOFU_UPDATE_CHECK_MIN_INTERVAL_S', '60')
    ver._failure_state.update({'ts': 0.0, 'error': None})
    # Default: no proxy pool configured. Pool-present scenarios override this.
    monkeypatch.setattr(ver, '_pool_failover_proxies', lambda: [])


class _FakeResponse:
    status_code = 200

    def json(self):
        return [{'name': 'v1.2.3'}, {'name': 'v0.9.0'}]


def test_proxy_tunnel_refusal_retries_direct(monkeypatch):
    _reset_state(monkeypatch)
    calls = []

    def fake_http_get(url, **kw):
        calls.append(kw.get('use_proxy', True))
        if kw.get('use_proxy', True):
            raise requests.exceptions.ProxyError(
                'Unable to connect to proxy', OSError(
                    'Tunnel connection failed: 403 Forbidden'))
        return _FakeResponse()

    monkeypatch.setattr(ver, 'http_get', fake_http_get)

    payload, err = ver._fetch_latest_release_detailed()

    assert err is None
    assert payload == {'tag': 'v1.2.3', 'version': '1.2.3'}
    assert calls == [True, False], (
        'proxy refusal must be retried exactly once without the proxy')


def test_plain_tunnel_403_also_retries_direct(monkeypatch):
    """A non-ProxyError ConnectionError carrying the 403 tunnel signature is
    treated the same — some requests versions surface it without the typed
    exception."""
    _reset_state(monkeypatch)
    calls = []

    def fake_http_get(url, **kw):
        calls.append(kw.get('use_proxy', True))
        if kw.get('use_proxy', True):
            raise requests.exceptions.ConnectionError(
                "HTTPSConnectionPool(host='api.github.com', port=443): "
                'Max retries exceeded (Caused by ProxyError(... '
                'Tunnel connection failed: 403 Forbidden))')
        return _FakeResponse()

    monkeypatch.setattr(ver, 'http_get', fake_http_get)

    payload, err = ver._fetch_latest_release_detailed()

    assert err is None
    assert payload == {'tag': 'v1.2.3', 'version': '1.2.3'}
    assert calls == [True, False]


def test_failure_backoff_skips_repeated_network(monkeypatch):
    _reset_state(monkeypatch)
    calls = []

    def fake_http_get(url, **kw):
        calls.append(1)
        raise OSError('dns error: name or service not known')

    monkeypatch.setattr(ver, 'http_get', fake_http_get)

    _p1, e1 = ver._fetch_latest_release_detailed()
    _p2, e2 = ver._fetch_latest_release_detailed()

    assert e1 is not None and e2 is not None
    assert e2 == e1
    assert len(calls) == 1, (
        'the second check within the backoff window must not touch the network')


def test_success_clears_failure_backoff(monkeypatch):
    _reset_state(monkeypatch)
    calls = []

    def fake_http_get(url, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise OSError('boom')
        return _FakeResponse()

    monkeypatch.setattr(ver, 'http_get', fake_http_get)

    _p1, e1 = ver._fetch_latest_release_detailed()
    assert e1 is not None

    # A later (post-window, but success-cleared) attempt goes through again.
    ver._failure_state['ts'] = 0.0
    payload, err = ver._fetch_latest_release_detailed()
    assert err is None
    assert payload == {'tag': 'v1.2.3', 'version': '1.2.3'}
    assert len(calls) == 2


def test_refusal_fails_over_to_next_pool_entry(monkeypatch):
    """When the chosen proxy refuses the tunnel and the pool has another
    healthy GLOBAL entry, that entry is tried BEFORE any direct attempt —
    and a direct attempt is never made."""
    _reset_state(monkeypatch)
    monkeypatch.setattr(ver, '_pool_failover_proxies',
                        lambda: ['http://gw2.example.com:8080'])
    calls = []

    def fake_http_get(url, **kw):
        calls.append(kw)
        if 'proxies' not in kw:  # primary attempt via the refusing gateway
            raise requests.exceptions.ProxyError(
                'Unable to connect to proxy', OSError(
                    'Tunnel connection failed: 403 Forbidden'))
        return _FakeResponse()

    monkeypatch.setattr(ver, 'http_get', fake_http_get)

    payload, err = ver._fetch_latest_release_detailed()

    assert err is None
    assert payload == {'tag': 'v1.2.3', 'version': '1.2.3'}
    assert len(calls) == 2
    assert calls[1].get('proxies') == {
        'http': 'http://gw2.example.com:8080',
        'https': 'http://gw2.example.com:8080'}, (
        'the refusal must fail over to the pool entry, not to bare direct')
    assert calls[1].get('use_proxy', True) is not False, (
        'a pooled deployment must never attempt the hard-direct path')


def test_refusal_with_pool_never_goes_direct_and_reports_proxy_cause(
        monkeypatch):
    """All pool entries exhausted → the surfaced error names the ORIGINAL
    proxy refusal, not a downstream direct DNS failure (the 2026-10-04 bug:
    the cached 'Name or service not known' masked the real proxy cause)."""
    _reset_state(monkeypatch)
    monkeypatch.setattr(ver, '_pool_failover_proxies',
                        lambda: ['http://gw2.example.com:8080'])
    calls = []

    def fake_http_get(url, **kw):
        calls.append(kw)
        raise requests.exceptions.ProxyError(
            'Unable to connect to proxy', OSError(
                'Tunnel connection failed: 403 Forbidden'))

    monkeypatch.setattr(ver, 'http_get', fake_http_get)

    payload, err = ver._fetch_latest_release_detailed()

    assert payload is None
    assert err is not None and err['kind'] == 'network'
    assert 'Tunnel connection failed' in err['detail']
    assert all(call.get('use_proxy', True) is not False for call in calls), (
        'with a pool present, no use_proxy=False direct attempt is allowed')
    assert len(calls) == 2  # primary + one pool failover, then stop
