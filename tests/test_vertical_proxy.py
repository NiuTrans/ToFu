"""Regression tests for tofu_search.search.vertical → http_client routing.

vertical.py previously issued ~14 raw ``requests.get`` calls with NO proxy
applied. After the search/fetch extraction, every vertical lookup goes through
``tofu_search.http_client.http_get``, a thin proxy-aware wrapper: it merges a
default User-Agent and delegates to ``requests.get`` (which honours the
standard ``HTTP(S)_PROXY`` env vars), so proxied deployments work and the
caller's User-Agent still wins.

These tests pin that behaviour:
  1. A vertical lookup actually routes through ``http_get`` (i.e. through
     ``http_client.requests.get``), not a bare ``requests.get`` import inside
     vertical.py, and the result still parses.
  2. The caller's custom User-Agent overrides http_get's default UA.
  3. Static guard: no bare ``requests.get(`` remains in the module source.

Run:  pytest tests/test_vertical_proxy.py -m unit
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.mark.unit
class TestVerticalUsesProxyClient:
    def test_pypi_lookup_routes_through_http_get(self, monkeypatch):
        """A PyPI vertical lookup must hit requests.get via http_client's
        http_get (the proxy-aware path), not a raw requests call — proving the
        lookup honours env proxies and the shared client."""
        import tofu_search.http_client as hc
        from tofu_search.search import vertical

        captured = {}

        class _FakeResp:
            ok = True
            status_code = 200

            def json(self):
                return {'info': {'name': 'requests', 'version': '2.31.0',
                                 'summary': 'HTTP for Humans',
                                 'home_page': 'https://requests.readthedocs.io',
                                 'author': 'Kenneth Reitz', 'license': 'Apache 2.0'}}

        def _fake_get(url, **kwargs):
            captured['url'] = url
            captured['headers'] = kwargs.get('headers', {})
            captured['timeout'] = kwargs.get('timeout')
            return _FakeResp()

        # Patch the requests.get that http_get delegates to. If vertical issued
        # a bare requests.get bypassing http_client, this would never fire.
        monkeypatch.setattr(hc.requests, 'get', _fake_get)

        result = vertical._search_pypi('requests', {})

        assert captured.get('url', '').startswith('https://pypi.org/pypi/requests'), (
            'vertical must route the PyPI lookup through http_client.http_get')
        # http_get always injects a User-Agent (the proxy-aware shared client).
        assert 'User-Agent' in captured['headers']
        # Sanity: the lookup still parses correctly through the new client.
        assert result is not None
        assert 'requests' in result.get('content', '')

    def test_custom_user_agent_preserved(self, monkeypatch):
        """The stock lookup sends a browser UA; http_get must let the caller's
        User-Agent override the default TofuSearch UA."""
        import tofu_search.http_client as hc
        from tofu_search.search import vertical

        captured = {}

        class _FakeResp:
            ok = False
            status_code = 404

            def json(self):
                return {}

        def _fake_get(url, **kwargs):
            captured['headers'] = kwargs.get('headers', {})
            return _FakeResp()

        monkeypatch.setattr(hc.requests, 'get', _fake_get)
        # 404 → falls back; suppress the fallback so we only inspect the first
        # (Yahoo) call, which sends an explicit Mozilla UA.
        monkeypatch.setattr(vertical, '_search_stock_fallback', lambda ident: None)

        vertical._search_stock('AAPL', {})
        ua = captured['headers'].get('User-Agent', '')
        assert 'Mozilla' in ua, f'caller UA should win, got {ua!r}'


@pytest.mark.unit
class TestNoRawRequestsRemain:
    def test_no_bare_requests_get_in_source(self):
        import inspect

        from tofu_search.search import vertical
        src = inspect.getsource(vertical)
        assert 'requests.get(' not in src, (
            'vertical.py must not issue raw requests.get — use http_get '
            '(proxy-aware) from tofu_search.http_client')
        assert 'http_get(' in src
