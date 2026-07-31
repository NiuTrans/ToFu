"""rev-CAS guard for ``_persist_reconcile`` (whole-blob data-loss root fix).

Shape being locked: reconcile computes ``cleaned`` from a row read EARLIER and
persists it on a BACKGROUND task. If an append lands in between, an
unconditional whole-blob UPDATE erases it (conv ms3sfyrmn31omb: 13 VU appends,
8 survivors). The write must carry ``WHERE rev=?`` and stand down on a lost
race instead of overwriting.

Lightweight fake DB (per project convention: no real PG for retry/CAS logic).
"""
import pytest

pytestmark = pytest.mark.unit


class _Cur:
    def __init__(self, rowcount=1, row=None):
        self.rowcount = rowcount
        self._row = row

    def fetchone(self):
        return self._row


class FakeDB:
    """Models one conversations row with a trigger-bumped ``rev``."""

    def __init__(self, rev=7):
        self.rev = rev
        self.messages = None
        self.sql = []
        self.commits = 0

    def execute(self, sql, params=()):
        self.sql.append((sql, params))
        if sql.startswith('SELECT rev'):
            return _Cur(row={'rev': self.rev})
        if sql.startswith('UPDATE conversations'):
            if ' AND rev=?' in sql:
                if int(params[-1]) != self.rev:
                    return _Cur(rowcount=0)          # CAS lost → no row touched
                self.messages = params[0]
                self.rev += 1                         # bump trigger
                return _Cur(rowcount=1)
            self.messages = params[0]                 # unconditional legacy path
            self.rev += 1
            return _Cur(rowcount=1)
        return _Cur()

    def commit(self):
        self.commits += 1


@pytest.fixture
def persist(monkeypatch):
    import routes.conversations as rc
    monkeypatch.setattr(rc, 'notify_history_rewrite', lambda *a, **k: None,
                        raising=False)
    monkeypatch.setattr(rc, 'build_search_text', lambda m: '', raising=False)
    import lib.database.messages_rows as mr
    monkeypatch.setattr(mr, 'mirror_write_and_commit', lambda *a, **k: None,
                        raising=False)
    return rc._persist_reconcile


def test_cas_write_uses_rev_predicate(persist):
    db = FakeDB(rev=7)
    persist(db, 'c1', [{'role': 'user', 'content': 'a'}], {}, expected_rev=7)
    upd = [s for s, _ in db.sql if s.startswith('UPDATE conversations')]
    assert upd, 'no UPDATE issued'
    assert ' AND rev=?' in upd[0], f'CAS predicate missing: {upd[0]}'


def test_lost_race_stands_down_instead_of_overwriting(persist):
    """★ The data-loss assertion. A sibling appended (rev 7→9); the stale
    2-message verdict must NOT land."""
    db = FakeDB(rev=7)
    db.rev = 9                      # concurrent append bumped rev
    rv = persist(db, 'c1', [{'role': 'user', 'content': 'stale'}], {},
                 expected_rev=7)
    assert db.messages is None, 'stale array overwrote a newer row — data loss'
    assert rv == -1, f'lost CAS must report -1, got {rv}'


def test_legitimate_write_still_lands(persist):
    """Complement: CAS must not make honest writes impossible."""
    db = FakeDB(rev=7)
    rv = persist(db, 'c1', [{'role': 'user', 'content': 'fresh'}], {},
                 expected_rev=7)
    assert db.messages is not None, 'matching-rev write was dropped'
    assert rv != -1


def test_neuter_removing_cas_predicate_is_caught():
    """NEUTER: strip ``AND rev=?`` from the source → the guard above must fail."""
    import inspect
    import routes.conversations as rc
    src = inspect.getsource(rc._persist_reconcile)
    assert 'AND rev=?' in src, 'CAS predicate vanished from _persist_reconcile'
    assert 'expected_rev' in src
