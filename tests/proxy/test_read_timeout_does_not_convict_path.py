#!/usr/bin/env python3
"""A slow upstream never convicts the egress path.

Root cause this guards (2026-10-21 boot): ``http_request`` reported EVERY
exception as a path failure. A pool entry owns a consecutive-failure budget
(2 fails → 60s cooldown), so on a deployment whose only egress is a single
pool entry, one slow upstream took the whole path out of service: the entry
cooled down, ``proxies_for`` then offered no proxy, and every other host fell
back to a direct path that resolves nothing on this class of host. The boot
window then showed an egress outage (self-update DNS failure, Artificial
Analysis read timeouts, registry probes) that had nothing to do with egress.

A read timeout means the request REACHED the peer — evidence about the
upstream, never about the path. Connect-phase failures still convict.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest \
        tests/proxy/test_read_timeout_does_not_convict_path.py -v
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import pytest
import requests

pytestmark = pytest.mark.unit


class _RecordingReport:
    def __init__(self):
        self.calls = []

    def __call__(self, url, ok, latency_ms=None, **kwargs):
        self.calls.append((url, ok))


@pytest.fixture
def reporting(monkeypatch):
    """Intercept the transport's path verdict without touching the network."""
    from lib.http_transport import client

    recorder = _RecordingReport()
    monkeypatch.setattr(client, '_report_outcome', recorder)
    return client, recorder


def _request_raising(client, monkeypatch, error):
    def boom(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(client, '_sync_request', boom)
    return client.http_request('GET', 'https://example.invalid/x', timeout=1)


def test_read_timeout_is_not_charged_to_the_egress_path(reporting, monkeypatch):
    client, recorder = reporting
    with pytest.raises(requests.exceptions.ReadTimeout):
        _request_raising(
            client, monkeypatch, requests.exceptions.ReadTimeout('slow body'))
    assert recorder.calls == [], (
        'a read timeout convicted the egress path: %r' % (recorder.calls,))


def test_connect_timeout_still_convicts(reporting, monkeypatch):
    client, recorder = reporting
    with pytest.raises(requests.exceptions.ConnectTimeout):
        _request_raising(
            client, monkeypatch, requests.exceptions.ConnectTimeout('no route'))
    assert recorder.calls and recorder.calls[-1][1] is False


def test_proxy_error_still_convicts(reporting, monkeypatch):
    client, recorder = reporting
    with pytest.raises(requests.exceptions.ProxyError):
        _request_raising(
            client, monkeypatch, requests.exceptions.ProxyError('407'))
    assert recorder.calls and recorder.calls[-1][1] is False


def test_success_reports_a_healthy_path(reporting, monkeypatch):
    client, recorder = reporting

    class _Resp:
        status_code = 200

    monkeypatch.setattr(client, '_sync_request',
                        lambda *_a, **_k: _Resp())
    client.http_request('GET', 'https://example.invalid/x', timeout=1)
    assert recorder.calls == [('https://example.invalid/x', True)]


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
