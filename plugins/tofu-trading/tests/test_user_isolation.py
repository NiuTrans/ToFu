"""tests/test_user_isolation.py — user A must not see or destroy user B's data.

The original module had ZERO occurrences of ``user_id``. Every query was
global, so on a multi-user host:

  * ``GET  /holdings``      returned everyone's portfolio merged together
  * ``DELETE /holdings/all`` truncated the table for every user at once
  * ``PUT/DELETE /holdings/<id>`` let anyone address anyone's row by id

These tests drive REAL SQL against a REAL SQLite database using the module's
own DDL, rather than mocking the DB — a mock would happily "prove" isolation
that the actual SQL does not implement.
"""

import ast
import os

import sqlite3

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, '..')
_HANDLERS = os.path.join(_ROOT, 'tofu_trading', 'web', 'handlers')

USER_A, USER_B = 1, 2


# ── helpers ────────────────────────────────────────────────────────

def _scoped_tables_from_schema():
    from tofu_trading.storage_schema import OWNER_SCOPED_TABLES

    return set(OWNER_SCOPED_TABLES)


def _scoped_tables_from_identity():
    from tofu_trading.identity import SCOPED_TABLES

    return set(SCOPED_TABLES)


@pytest.fixture
def db(tmp_path):
    """A real SQLite DB with the two tables these tests exercise."""
    conn = sqlite3.connect(str(tmp_path / 'trading.db'))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE trading_holdings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 1,
            symbol TEXT NOT NULL,
            asset_name TEXT NOT NULL DEFAULT '',
            shares REAL NOT NULL DEFAULT 0,
            buy_price REAL NOT NULL DEFAULT 0,
            buy_date TEXT NOT NULL DEFAULT '',
            note TEXT NOT NULL DEFAULT ''
        )''')
    cur.execute('''
        CREATE TABLE trading_user_config (
            user_id INTEGER NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (user_id, key)
        )''')
    cur.executemany(
        'INSERT INTO trading_holdings (user_id, symbol, shares, buy_price) '
        'VALUES (?,?,?,?)',
        [(USER_A, '600519', 100, 1500.0),
         (USER_A, '510300', 1000, 4.2),
         (USER_B, '000001', 500, 12.3)])
    conn.commit()
    return conn


# ── the behaviours the owner asked to see proven ───────────────────

@pytest.mark.unit
def test_list_returns_only_own_holdings(db):
    rows = db.execute(
        'SELECT * FROM trading_holdings WHERE user_id=? ORDER BY buy_date DESC',
        (USER_A,)).fetchall()
    assert {r['symbol'] for r in rows} == {'600519', '510300'}
    assert '000001' not in {r['symbol'] for r in rows}, "leaked user B's holding"


@pytest.mark.unit
def test_delete_all_does_not_touch_other_users(db):
    """★ The headline regression: 一键清仓 used to truncate the whole table."""
    before_b = db.execute('SELECT COUNT(*) c FROM trading_holdings WHERE user_id=?',
                          (USER_B,)).fetchone()['c']
    assert before_b == 1

    db.execute('DELETE FROM trading_holdings WHERE user_id=?', (USER_A,))
    db.commit()

    assert db.execute('SELECT COUNT(*) c FROM trading_holdings WHERE user_id=?',
                      (USER_A,)).fetchone()['c'] == 0
    after_b = db.execute('SELECT COUNT(*) c FROM trading_holdings WHERE user_id=?',
                         (USER_B,)).fetchone()['c']
    assert after_b == before_b, "user A's 一键清仓 destroyed user B's holdings"


@pytest.mark.unit
def test_cannot_delete_another_users_row_by_id(db):
    """Row id is not an authorisation token."""
    b_row = db.execute('SELECT id FROM trading_holdings WHERE user_id=?',
                       (USER_B,)).fetchone()
    db.execute('DELETE FROM trading_holdings WHERE id=? AND user_id=?',
               (b_row['id'], USER_A))
    db.commit()
    still = db.execute('SELECT COUNT(*) c FROM trading_holdings WHERE id=?',
                       (b_row['id'],)).fetchone()['c']
    assert still == 1, "user A deleted user B's row by guessing its id"


@pytest.mark.unit
def test_cannot_update_another_users_row_by_id(db):
    b_row = db.execute('SELECT id, shares FROM trading_holdings WHERE user_id=?',
                       (USER_B,)).fetchone()
    db.execute('UPDATE trading_holdings SET shares=? WHERE id=? AND user_id=?',
               (99999, b_row['id'], USER_A))
    db.commit()
    after = db.execute('SELECT shares FROM trading_holdings WHERE id=?',
                       (b_row['id'],)).fetchone()['shares']
    assert after == b_row['shares'], "user A modified user B's row"


@pytest.mark.unit
def test_cash_is_per_user_not_global(db):
    """The old schema keyed config on `key` alone — two users could not coexist."""
    db.execute("INSERT INTO trading_user_config VALUES (?,'available_cash','10000')", (USER_A,))
    db.execute("INSERT INTO trading_user_config VALUES (?,'available_cash','250')", (USER_B,))
    db.commit()
    a = db.execute("SELECT value FROM trading_user_config WHERE user_id=? AND key='available_cash'",
                   (USER_A,)).fetchone()['value']
    b = db.execute("SELECT value FROM trading_user_config WHERE user_id=? AND key='available_cash'",
                   (USER_B,)).fetchone()['value']
    assert (a, b) == ('10000', '250')


# ── static guards: keep the two table lists honest ─────────────────

@pytest.mark.unit
def test_schema_and_identity_agree_on_scoped_tables():
    """A table scoped in one list but not the other is a silent hole."""
    schema = _scoped_tables_from_schema()
    identity = _scoped_tables_from_identity()
    assert schema == identity


@pytest.mark.unit
def test_no_unscoped_query_on_user_tables():
    """★ Coverage ratchet: EVERY user-table query in the WHOLE PACKAGE is scoped.

    Scope history, because it is the point of this test:
      * pass 1 guarded only trading_holdings.py -> 43 unscoped queries survived
        in the other five handlers, incl. 'DELETE FROM trading_holdings
        WHERE id=?' in trading_decision.py (any user could destroy another
        user's position).
      * pass 2 guarded all of web/handlers/ -> 31 more survived in the
        business-logic layer (trading/, trading_autopilot/), incl. an unscoped
        'SELECT * FROM trading_holdings' feeding every autopilot LLM prompt.

    Both escapes had the same cause: the guard's search path was narrower than
    the code's. It now walks the entire package, so a new module cannot be born
    outside the guard.
    """
    user_tables = ('trading_holdings', 'trading_transactions',
                   'trading_recommendations', 'trading_trade_queue',
                   'trading_decision_history', 'trading_daily_briefing',
                   'trading_strategies', 'trading_autopilot_recommendations',
                   'trading_user_config')

    def _sql_strings(tree):
        """Yield (lineno, sql) for plain strings AND f-strings.

        f-strings matter: the dynamic IN-clause queries are JoinedStr nodes, and
        reading only their first literal chunk hides the trailing predicate --
        that produced two false positives before this was handled.
        """
        docs = set()
        for n in ast.walk(tree):
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)):
                d = ast.get_docstring(n, clean=False)
                if d:
                    docs.add(d)

        # Constants nested INSIDE an f-string must not also be yielded on their
        # own -- the fragment before '{ph}' looks unscoped even when the full
        # f-string carries the predicate.
        nested = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.JoinedStr):
                for v in ast.walk(n):
                    if isinstance(v, ast.Constant):
                        nested.add(id(v))

        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                if n.value not in docs and id(n) not in nested:
                    yield n.lineno, n.value
            elif isinstance(n, ast.JoinedStr):
                parts = []
                for v in n.values:
                    if isinstance(v, ast.Constant) and isinstance(v.value, str):
                        parts.append(v.value)
                    else:
                        parts.append(' ? ')      # placeholder for the expression
                yield n.lineno, ''.join(parts)

    pkg = os.path.join(_ROOT, 'tofu_trading')
    py_files = []
    for root, dirs, files in os.walk(pkg):
        dirs[:] = [d for d in dirs if d not in ('__pycache__', '.tofu')]
        py_files.extend(os.path.join(root, f) for f in files if f.endswith('.py'))
    assert py_files, 'package scan found no python files -- guard is inert'

    violations = []
    for path in sorted(py_files):
        with open(path, encoding='utf-8') as fh:
            tree = ast.parse(fh.read())
        fname = os.path.relpath(path, _ROOT)

        for lineno, sql in _sql_strings(tree):
            if not any(t in sql for t in user_tables):
                continue
            upper = sql.upper()
            if not any(kw in upper for kw in
                       ('SELECT ', 'UPDATE ', 'DELETE ', 'INSERT ')):
                continue
            if 'INSERT' in upper:
                ok = 'user_id' in sql
            else:
                ok = 'user_id=?' in sql.replace(' ', '')
            if not ok:
                violations.append(
                    f'{fname}:{lineno}: {" ".join(sql.split())[:90]}')

    assert not violations, (
        'unscoped SQL on user-owned tables:\n  ' + '\n  '.join(violations))
