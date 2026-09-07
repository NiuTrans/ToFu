"""The intel crawler must never carry SQLite writes across network work."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tofu_trading.trading import intel


pytestmark = pytest.mark.unit


class _FakeDB:
    def __init__(self):
        self.in_transaction = False
        self.commits = 0
        self.rollbacks = 0
        self._dirty = False
        self._transaction_pinned = False

    def begin(self):
        assert not self.in_transaction
        self.in_transaction = True

    def execute(self, _sql, _params=()):
        assert self.in_transaction, 'mutation escaped write_transaction()'
        return SimpleNamespace(rowcount=0)

    def commit(self):
        assert self.in_transaction
        self.in_transaction = False
        self.commits += 1

    def rollback(self):
        self.in_transaction = False
        self.rollbacks += 1


def test_snapshot_purge_commits_even_when_delete_matches_zero_rows():
    db = _FakeDB()
    assert intel._purge_expired_snapshot(db, 'market_news') == 0
    assert db.commits == 1
    assert db.in_transaction is False


def test_housekeeping_commits_even_when_every_delete_matches_zero_rows(
        monkeypatch):
    monkeypatch.setattr(
        intel, 'INTEL_SOURCES',
        {'market_news': {'decision_window_days': 7},
         'macro': {'decision_window_days': 30}})
    db = _FakeDB()

    assert intel.cleanup_stale_intel(db) == 0
    assert db.commits == 1
    assert db.in_transaction is False

