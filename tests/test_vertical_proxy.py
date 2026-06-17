"""Regression tests for tofu_search vertical search → http_client routing.

The vertical handlers previously issued raw ``requests.get`` calls with NO
proxy applied. After the search/fetch extraction — and the later split of the
flat ``vertical.py`` into a ``vertical/`` sub-package — every vertical lookup
goes through ``tofu_search.http_client.http_get`` (via the shared
``vertical.base`` seam: ``base.http_get`` / ``base._fetch_json``). ``http_get``
is a thin proxy-aware wrapper that merges a default User-Agent and delegates to
``requests.get`` (which honours ``HTTP(S)_PROXY`` env vars), so proxied
deployments work and the caller's User-Agent still wins.

These tests pin that behaviour against the CURRENT package layout:
  1. A PyPI lookup (``vertical.pypi.search``) routes through ``http_get``
     (i.e. through ``http_client.requests.get``), not a bare ``requests.get``,
     and the result still parses.
  2. The stock lookup (``vertical.stock.search``) lets the caller's custom
     User-Agent override http_get's default UA.
  3. Static guard: the shared HTTP seam (``vertical.base``) uses ``http_get``
     and no vertical handler module issues a bare ``requests.get(``.

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
        from tofu_search.search.vertical import pypi

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

        # Patch the requests.get that http_get delegates to. If the handler
        # issued a bare requests.get bypassing http_client, this never fires.
        monkeypatch.setattr(hc.requests, 'get', _fake_get)

        result = pypi.search('requests', {})

        assert captured.get('url', '').startswith('https://pypi.org/pypi/requests'), (
            'vertical must route the PyPI lookup through http_client.http_get')
        # http_get always injects a User-Agent (the proxy-aware shared client).
        assert 'User-Agent' in captured['headers']
        # Sanity: the lookup still parses correctly through the new client.
        assert result is not None
        assert 'requests' in result.get('content', '')

    def test_custom_user_agent_preserved(self, monkeypatch):
        """The stock lookup sends an explicit browser UA; http_get must let the
        caller's User-Agent override the default TofuBot UA."""
        import tofu_search.http_client as hc
        from tofu_search.search.vertical import stock

        captured = {}

        class _FakeResp:
            ok = False
            status_code = 404
            text = ''

            def json(self):
                return {}

        def _fake_get(url, **kwargs):
            captured['headers'] = kwargs.get('headers', {})
            return _FakeResp()

        monkeypatch.setattr(hc.requests, 'get', _fake_get)
        # 404 → falls back; suppress the fallback so we only inspect the first
        # (Yahoo) call, which sends an explicit Mozilla UA.
        monkeypatch.setattr(stock, '_search_fallback', lambda ident: None)

        stock.search('AAPL', {})
        ua = captured['headers'].get('User-Agent', '')
        assert 'Mozilla' in ua, f'caller UA should win, got {ua!r}'


@pytest.mark.unit
class TestNoRawRequestsRemain:
    def test_http_seam_uses_http_get_not_raw_requests(self):
        """The shared vertical HTTP seam (base) must route through http_get,
        and no vertical handler module may issue a bare ``requests.get(``."""
        import importlib
        import inspect
        import pkgutil

        from tofu_search.search import vertical
        from tofu_search.search.vertical import base

        # The seam itself uses http_get.
        base_src = inspect.getsource(base)
        assert 'http_get(' in base_src, (
            'vertical.base must call http_get (proxy-aware) for all HTTP')

        # No handler module issues a bare requests.get( — they all go through
        # base.http_get / base._fetch_json.
        offenders = []
        for mod_info in pkgutil.iter_modules(vertical.__path__):
            mod = importlib.import_module(
                f'tofu_search.search.vertical.{mod_info.name}')
            if 'requests.get(' in inspect.getsource(mod):
                offenders.append(mod_info.name)
        assert not offenders, (
            f'these vertical modules issue a raw requests.get — use '
            f'base.http_get instead: {offenders}')
