"""_ChatuiKmDocReader: km.internal.example.com doc URLs reroute to xuecheng-mcp.

Pins the fetch_url fix for the "KM doc URL returned the anonymous SSO login
wall / extension-config garbage" incident: a tofu-search SiteReader now
intercepts 学城 doc URLs and reads them via mcp__xuecheng__read_doc (user
identity) BEFORE the anonymous pipeline runs.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib.mcp  # noqa: E402
from lib.search_bridge import _ChatuiKmDocReader  # noqa: E402


class _FakeBridge:
    def __init__(self, result=None, error=None):
        self._result, self._error = result, error
        self.calls = []

    def call_tool(self, name, args, **kwargs):
        self.calls.append((name, args))
        if self._error is not None:
            raise self._error
        return self._result


def test_matches_only_km_doc_urls():
    r = _ChatuiKmDocReader()
    assert r.matches('https://km.internal.example.com/collabpage/2754681609')
    assert r.matches('https://km.internal.example.com/collabpage/2754681609#b-abc')
    assert r.matches('https://km.internal.example.com/xtable/2772033234')
    assert r.matches('https://km.internal.example.com/page/123')
    assert not r.matches('https://km.internal.example.com/')
    assert not r.matches('https://km.internal.example.com/space/abc')
    assert not r.matches('https://example.com/collabpage/123')
    assert not r.matches('not-a-url')


def test_read_reroutes_to_read_doc(monkeypatch):
    bridge = _FakeBridge(result='DOC MARKDOWN')
    monkeypatch.setattr(lib.mcp, 'get_bridge', lambda: bridge)
    url = 'https://km.internal.example.com/collabpage/2754681609'
    assert _ChatuiKmDocReader().read(url, max_chars=12345) == 'DOC MARKDOWN'
    assert bridge.calls == [(
        'mcp__xuecheng__read_doc', {'doc': url, 'max_chars': 12345})]


def test_read_falls_through_when_server_unconfigured(monkeypatch):
    bridge = _FakeBridge(error=ValueError('MCP server not connected: x'))
    monkeypatch.setattr(lib.mcp, 'get_bridge', lambda: bridge)
    r = _ChatuiKmDocReader()
    assert r.read('https://km.internal.example.com/collabpage/1') is None


def test_read_failure_returns_note_not_login_wall(monkeypatch):
    bridge = _FakeBridge(error=TimeoutError('timed out'))
    monkeypatch.setattr(lib.mcp, 'get_bridge', lambda: bridge)
    out = _ChatuiKmDocReader().read('https://km.internal.example.com/collabpage/1')
    assert out and 'read_doc' in out and '失败' in out
