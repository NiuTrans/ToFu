"""Shutdown races for the owned MCP prewarm/auto-connect worker."""

from __future__ import annotations

import threading

import pytest


pytestmark = pytest.mark.unit


def test_stop_during_prewarm_prevents_late_connect(monkeypatch):
    import lib.mcp.client as client
    import lib.mcp.startup as startup

    entered = threading.Event()
    release = threading.Event()
    calls = []

    def prewarm():
        entered.set()
        release.wait(2.0)
        return []

    class Bridge:
        def connect_all(self):
            calls.append('connect')
            return {}

        def disconnect_all(self):
            calls.append('disconnect')

    monkeypatch.setattr(startup, '_worker', None)
    monkeypatch.setattr(client, 'prewarm_all_vendored', prewarm)
    monkeypatch.setattr(client, 'get_bridge', lambda: Bridge())

    assert startup.start_mcp_auto_connect({'one': {'enabled': True}})
    assert entered.wait(1.0)
    assert startup.stop_mcp_auto_connect(timeout=0.01) is False
    release.set()
    thread = startup._worker
    assert thread is not None
    thread.join(1.0)
    assert startup.stop_mcp_auto_connect(timeout=0.1) is True
    assert calls == []


def test_connect_finishing_after_stop_disconnects_itself(monkeypatch):
    import lib.mcp.client as client
    import lib.mcp.startup as startup

    connecting = threading.Event()
    release = threading.Event()
    calls = []

    class Bridge:
        def connect_all(self):
            calls.append('connect')
            connecting.set()
            release.wait(2.0)
            return {'server': ['tool']}

        def disconnect_all(self):
            calls.append('disconnect')

    monkeypatch.setattr(startup, '_worker', None)
    monkeypatch.setattr(client, 'prewarm_all_vendored', lambda: [])
    monkeypatch.setattr(client, 'get_bridge', lambda: Bridge())

    assert startup.start_mcp_auto_connect({'one': {'enabled': True}})
    assert connecting.wait(1.0)
    assert startup.stop_mcp_auto_connect(timeout=0.01) is False
    release.set()
    thread = startup._worker
    assert thread is not None
    thread.join(1.0)
    assert startup.stop_mcp_auto_connect(timeout=0.1) is True
    assert calls == ['connect', 'disconnect']


def test_duplicate_start_does_not_spawn_a_second_owner(monkeypatch):
    import lib.mcp.client as client
    import lib.mcp.startup as startup

    entered = threading.Event()
    release = threading.Event()

    def prewarm():
        entered.set()
        release.wait(2.0)
        return []

    monkeypatch.setattr(startup, '_worker', None)
    monkeypatch.setattr(client, 'prewarm_all_vendored', prewarm)
    assert startup.start_mcp_auto_connect({}) is True
    assert entered.wait(1.0)
    assert startup.start_mcp_auto_connect({}) is False
    release.set()
    assert startup.stop_mcp_auto_connect(timeout=1.0) is True
