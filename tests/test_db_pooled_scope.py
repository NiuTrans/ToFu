"""Bounded synchronous database leases never pin a worker thread."""

from __future__ import annotations

import pytest

from lib.database import _core

pytestmark = pytest.mark.unit


class _Conn:
    pass


def test_pooled_db_returns_connection_after_success(monkeypatch):
    conn = _Conn()
    returned = []
    monkeypatch.setattr(_core, '_pool_get', lambda: conn)
    monkeypatch.setattr(_core, '_pool_put', returned.append)

    with _core.pooled_db(_core.DOMAIN_SYSTEM) as leased:
        assert leased is conn

    assert returned == [conn]


def test_pooled_db_returns_connection_after_failure(monkeypatch):
    conn = _Conn()
    returned = []
    monkeypatch.setattr(_core, '_pool_get', lambda: conn)
    monkeypatch.setattr(_core, '_pool_put', returned.append)

    with pytest.raises(RuntimeError, match='boom'):
        with _core.pooled_db() as leased:
            assert leased is conn
            raise RuntimeError('boom')

    assert returned == [conn]
