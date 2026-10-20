"""Concurrent proxy attempts retain request-owned configuration and context."""

import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar

import pytest
import requests

from tofu_search import SearchConfig
from tofu_search.config import bind_call_config, get_config
from tofu_search.search import _common
from tofu_search.search.proxy_mode import ProxyModeManager

pytestmark = pytest.mark.unit


def test_raced_proxy_attempts_retain_the_callers_context(monkeypatch):
    request_marker = ContextVar('search_request_marker', default='unbound')
    cfg = SearchConfig(proxy_url='http://primary.invalid:8123',
                       proxy_fallback_urls=['http://alternate.invalid:8123'],
                       proxy_dual_attempt=True, proxy_race=True,
                       min_request_interval_ms=0)
    monkeypatch.setattr(_common, 'engine_circuit', _common._EngineCircuit())
    monkeypatch.setattr(_common, 'proxy_mode_manager', ProxyModeManager())
    rendezvous = threading.Barrier(2)
    observed = []

    def get(url, **kwargs):
        if kwargs.get('proxies', {}).get('https') == cfg.proxy_url:
            raise requests.exceptions.ProxyError('primary unavailable')
        observed.append((get_config().proxy_url, request_marker.get()))
        rendezvous.wait(timeout=2)
        response = requests.Response()
        response.status_code = 200
        response._content = b'result'
        return response

    monkeypatch.setattr(_common.search_session, 'get', get)
    with ThreadPoolExecutor(max_workers=2) as pool:
        monkeypatch.setattr(_common, '_race_executor', lambda: pool)
        token = request_marker.set('owned-by-this-request')
        try:
            with bind_call_config(cfg):
                results = _common.http_search_get(
                    name='ContextProbe', url='https://search.invalid/', params={},
                    query='synthetic context probe',
                    parser=lambda response: [{'url': 'https://example.org/result'}])
        finally:
            request_marker.reset(token)
    assert results
    assert observed == [(cfg.proxy_url, 'owned-by-this-request')] * 2
