"""PostgreSQL billing branch must match the DB layer's canonical ``pg`` name.

The live backend exports ``_BACKEND == 'pg'``.  The historical checks used the
unreachable string ``'postgresql'``, silently disabling SELECT ... FOR UPDATE
and the PG upsert branch in production.
"""

import pytest

pytestmark = pytest.mark.unit


class _Cursor:
    rowcount = 1

    def __init__(self, row=None):
        self._row = row

    def fetchone(self):
        return self._row


class _Db:
    def __init__(self, row=None):
        self.row = row
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return _Cursor(self.row)

    def begin(self):
        self.calls.append(('BEGIN_API', None))

    def commit(self):
        self.calls.append(('COMMIT_API', None))

    def rollback(self):
        self.calls.append(('ROLLBACK_API', None))


def test_pg_balance_read_takes_row_lock(monkeypatch):
    import lib.billing.wallet as wallet
    monkeypatch.setattr(wallet, '_BACKEND', 'pg')
    db = _Db((123,))
    assert wallet._read_balance(db, 'u1') == 123
    assert 'FOR UPDATE' in db.calls[0][0]


def test_sqlite_balance_read_does_not_use_pg_lock(monkeypatch):
    import lib.billing.wallet as wallet
    monkeypatch.setattr(wallet, '_BACKEND', 'sqlite')
    db = _Db((123,))
    assert wallet._read_balance(db, 'u1') == 123
    assert 'FOR UPDATE' not in db.calls[0][0]


def test_pg_write_boundary_uses_connection_api_not_sqlite_begin(monkeypatch):
    import lib.billing.wallet as wallet
    monkeypatch.setattr(wallet, '_BACKEND', 'pg')
    db = _Db()
    with wallet.write_transaction(db, label='billing pg branch'):
        pass
    assert db.calls == [('BEGIN_API', None), ('COMMIT_API', None)]
