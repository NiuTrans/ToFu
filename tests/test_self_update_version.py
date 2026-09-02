"""tests/test_self_update_version.py — GitHub tags API failure handling.

Pins the two production fixes for the proxy-403 spam on api.github.com:

  1. A proxy that refuses the CONNECT tunnel (ProxyError / "Tunnel connection
     failed: 403") is retried DIRECTLY once, so the tags check still succeeds
     when only the corporate proxy is broken for GitHub.
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.self_update import _version as ver  # noqa: E402

pytestmark = pytest.mark.unit


def _reset_state(monkeypatch):
    monkeypatch.setenv('TOFU_UPDATE_CHECK_MIN_INTERVAL_S', '60')
    ver._failure_state.update({'ts': 0.0, 'error': None})


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
