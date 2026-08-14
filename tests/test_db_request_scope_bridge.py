"""Legacy get_thread_db callers must not pin request-worker connections."""

from __future__ import annotations

import pytest

from lib.database import _core

pytestmark = pytest.mark.unit


def test_get_thread_db_delegates_inside_app_context(monkeypatch):
    sentinel = object()
    calls = []
    monkeypatch.setattr(_core, '_has_app_context', lambda: True)
    monkeypatch.setattr(
        _core, 'get_db', lambda domain: calls.append(domain) or sentinel)

    got = _core.get_thread_db(_core.DOMAIN_CHAT)

    assert got is sentinel
    assert calls == [_core.DOMAIN_CHAT]


def test_get_thread_db_keeps_background_thread_contract(monkeypatch):
    class _Conn:
        _closed = False

    conn = _Conn()
    attr = f'db_{_core.DOMAIN_SYSTEM}'
    previous = getattr(_core._thread_local, attr, None)
    monkeypatch.setattr(_core, '_has_app_context', lambda: False)
    monkeypatch.setattr(_core, '_new_connection', lambda: conn)
    monkeypatch.setattr(_core, '_register_thread_conn', lambda *_a: None)
    monkeypatch.setattr(_core, '_test_connection', lambda value: value is conn)
    try:
        setattr(_core._thread_local, attr, None)
        assert _core.get_thread_db(_core.DOMAIN_SYSTEM) is conn
        assert _core.get_thread_db(_core.DOMAIN_SYSTEM) is conn
    finally:
        setattr(_core._thread_local, attr, previous)
