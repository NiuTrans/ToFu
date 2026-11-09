"""Regression tests for tofu_search vertical search → http_client routing.

The vertical handlers previously issued raw ``requests.get`` calls with NO
proxy applied. After the search/fetch extraction — and the later split of the
flat ``vertical.py`` into a ``vertical/`` sub-package — every vertical lookup
goes through ``tofu_search.http_client.http_get`` (via the shared
``vertical.base`` seam: ``base.http_get`` / ``base._fetch_json``). ``http_get``
merges a default User-Agent and delegates to the package's network transport
port, which retains environment proxy policy and accepts host routing.

These tests pin that behaviour against the CURRENT package layout:
  1. A PyPI lookup (``vertical.pypi.search``) routes through ``http_get``
     (i.e. through ``http_client.request``), not a bare ``requests.get``,
     and the result still parses.
  2. The stock lookup (``vertical.stock.search``) lets the caller's custom
     User-Agent override http_get's default UA.
  3. Static guard: the shared HTTP seam (``vertical.base``) uses ``http_get``
     and no vertical handler module issues a bare ``requests.get(``.

Run:  pytest tests/proxy/test_vertical_proxy.py -m unit
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


@pytest.mark.unit
class TestVerticalUsesProxyClient:
    def test_pypi_lookup_routes_through_http_get(self, monkeypatch):
        """A PyPI lookup reaches the shared transport with its headers intact."""
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

        def _fake_request(method, url, **kwargs):
            assert method == 'GET'
            captured['url'] = url
            captured['headers'] = kwargs.get('headers', {})
            captured['timeout'] = kwargs.get('timeout')
            return _FakeResp()

        monkeypatch.setattr(hc, 'request', _fake_request)

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

        def _fake_request(method, url, **kwargs):
            assert method == 'GET'
            captured['headers'] = kwargs.get('headers', {})
            return _FakeResp()

        monkeypatch.setattr(hc, 'request', _fake_request)
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
