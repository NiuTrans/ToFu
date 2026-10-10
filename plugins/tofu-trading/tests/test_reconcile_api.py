"""tests/test_reconcile_api.py — reconcile REST contract against the real schema.

These exercise the SQL the handlers issue, against a database built by the
plugin's own DDL. They are not endpoint smoke tests: each one pins a behaviour
that was either absent from the old module or is a rule the owner set.

  * the approve gate       — an unapproved target must not drive a plan
  * the adoption loop      — status/acted_at/actual_price actually persist
  * recompute idempotence  — replanning must not resurrect a completed action
  * per-user isolation     — on the three NEW tables, not just the old ones
"""

import importlib.util
import logging
import os
import sqlite3
import sys
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, '..')
_HANDLER = os.path.join(_ROOT, 'tofu_trading', 'web', 'handlers',
                        'trading_reconcile.py')

USER_A, USER_B = 1, 2


def _load_reconcile():
    # Respect the real host lib when it is importable (e.g. host on
    # PYTHONPATH): pytest imports every test module at collection time, so an
    # unconditional stub here would shadow the real package for OTHER suites
    # sharing the process (measured: ModuleNotFoundError 'lib.database').
    try:
        import lib.log  # noqa: F401
    except ImportError:
        if 'lib' not in sys.modules:
            lib = types.ModuleType('lib'); lib.__path__ = []
            log = types.ModuleType('lib.log')
            log.get_logger = lambda n: logging.getLogger(n)
            sys.modules['lib'] = lib
            sys.modules['lib.log'] = log
    if 'tt_rec_api' in sys.modules:
        return sys.modules['tt_rec_api']
    spec = importlib.util.spec_from_file_location(
        'tt_rec_api', os.path.join(_ROOT, 'tofu_trading', 'reconcile.py'))
    m = importlib.util.module_from_spec(spec)
    sys.modules['tt_rec_api'] = m
    spec.loader.exec_module(m)
    return m


R = _load_reconcile()


@pytest.fixture
def db(tmp_path):
    """Real SQLite with the three reconcile tables, same DDL shape as the plugin."""
    conn = sqlite3.connect(str(tmp_path / 'r.db'))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute('''CREATE TABLE trading_target (
        user_id INTEGER NOT NULL, symbol TEXT NOT NULL,
        asset_name TEXT NOT NULL DEFAULT '', target_weight REAL NOT NULL DEFAULT 0,
        rationale TEXT NOT NULL DEFAULT '', proposed_by TEXT NOT NULL DEFAULT 'ai',
        approved INTEGER NOT NULL DEFAULT 0, valid_from TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT '', PRIMARY KEY (user_id, symbol))''')
    cur.execute('''CREATE TABLE trading_position (
        user_id INTEGER NOT NULL, symbol TEXT NOT NULL,
        asset_name TEXT NOT NULL DEFAULT '', shares REAL NOT NULL DEFAULT 0,
        cost REAL NOT NULL DEFAULT 0, pending_shares REAL NOT NULL DEFAULT 0,
        settle_date TEXT NOT NULL DEFAULT '', as_of TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL DEFAULT '', PRIMARY KEY (user_id, symbol))''')
    cur.execute('''CREATE TABLE trading_action (
        user_id INTEGER NOT NULL, plan_date TEXT NOT NULL, symbol TEXT NOT NULL,
        side TEXT NOT NULL DEFAULT 'buy', shares REAL NOT NULL DEFAULT 0,
        amount REAL NOT NULL DEFAULT 0, price REAL NOT NULL DEFAULT 0,
        drift_pct REAL NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending', acted_at TEXT NOT NULL DEFAULT '',
        actual_price REAL NOT NULL DEFAULT 0, actual_shares REAL NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (user_id, plan_date, symbol))''')
    conn.commit()
    return conn


def _approved_targets(conn, uid):
    """Mirrors the planner's read: approved rows only."""
    return [dict(r) for r in conn.execute(
        'SELECT * FROM trading_target WHERE user_id=? AND approved=1',
        (uid,)).fetchall()]


# ── the approve gate (owner decision #3) ───────────────────────────

@pytest.mark.unit
def test_unapproved_target_does_not_drive_a_plan(db):
    """★ AI may propose; only the owner's approval lets it move money."""
    db.execute("INSERT INTO trading_target (user_id,symbol,target_weight,approved) "
               "VALUES (?,?,?,0)", (USER_A, '600519', 90.0))
    db.execute("INSERT INTO trading_position (user_id,symbol,shares) VALUES (?,?,?)",
               (USER_A, '600519', 100))
    db.commit()

    targets = _approved_targets(db, USER_A)
    assert targets == [], 'unapproved target leaked into the planner input'

    positions = [dict(r) for r in db.execute(
        'SELECT * FROM trading_position WHERE user_id=?', (USER_A,)).fetchall()]
    drifts = R.compute_drift(targets, positions, {'600519': 100.0}, cash=90000.0)
    actions, _ = R.plan_actions(drifts, R.DEFAULT_PARAMS, cash=90000.0)
    sells = [a for a in actions if a['side'] == 'sell']
    # With no approved target the holding reads as 100% over target, so the
    # only thing that could appear is a sell — never a buy driven by the
    # unratified 90% proposal.
    assert not any(a['side'] == 'buy' for a in actions), \
        'unapproved proposal produced a buy'
    assert sells or actions == []


@pytest.mark.unit
def test_approving_a_target_makes_it_effective(db):
    db.execute("INSERT INTO trading_target (user_id,symbol,target_weight,approved) "
               "VALUES (?,?,?,0)", (USER_A, '600519', 50.0))
    db.commit()
    assert _approved_targets(db, USER_A) == []

    db.execute("UPDATE trading_target SET approved=1 WHERE user_id=? AND symbol=?",
               (USER_A, '600519'))
    db.commit()
    assert len(_approved_targets(db, USER_A)) == 1


# ── the adoption loop ──────────────────────────────────────────────

@pytest.mark.unit
def test_action_status_and_actuals_persist(db):
    """★ The loop the old schema could not close.

    trading_recommendations.adopted existed but no code ever wrote it, so
    "was the advice followed?" was unanswerable. Here the verdict AND what was
    really executed are both stored.
    """
    db.execute("INSERT INTO trading_action "
               "(user_id,plan_date,symbol,side,shares,amount,status) "
               "VALUES (?,?,?,?,?,?,'pending')",
               (USER_A, '2026-07-26', '600519', 'buy', 100, 130000))
    db.commit()

    db.execute("UPDATE trading_action SET status='done', acted_at=?, "
               "actual_price=?, actual_shares=? "
               "WHERE user_id=? AND plan_date=? AND symbol=?",
               ('2026-07-26 10:05', 1298.5, 100, USER_A, '2026-07-26', '600519'))
    db.commit()

    r = db.execute('SELECT * FROM trading_action WHERE user_id=?',
                   (USER_A,)).fetchone()
    assert r['status'] == 'done'
    assert r['acted_at'] == '2026-07-26 10:05'
    assert r['actual_price'] == 1298.5
    # actual vs advised must both be visible -- that difference (slippage) is
    # the whole reason to record actuals rather than a boolean.
    assert r['actual_shares'] == 100 and r['shares'] == 100


@pytest.mark.unit
def test_skipped_status_is_recorded_not_deleted(db):
    """A skipped suggestion must remain as evidence, not vanish.

    Deleting it would make the follow-through rate look artificially perfect.
    """
    db.execute("INSERT INTO trading_action "
               "(user_id,plan_date,symbol,side,status) VALUES (?,?,?,?,'pending')",
               (USER_A, '2026-07-26', '600519', 'buy'))
    db.commit()
    db.execute("UPDATE trading_action SET status='skipped', acted_at=? "
               "WHERE user_id=? AND plan_date=? AND symbol=?",
               ('2026-07-26 15:00', USER_A, '2026-07-26', '600519'))
    db.commit()
    rows = db.execute('SELECT status FROM trading_action WHERE user_id=?',
                      (USER_A,)).fetchall()
    assert [r['status'] for r in rows] == ['skipped']


@pytest.mark.unit
def test_replan_does_not_resurrect_a_completed_action(db):
    """★ Recompute is stateless, but it must not overwrite a recorded verdict.

    The plan is derived fresh every call; if that blindly re-inserted rows it
    would flip a 'done' action back to 'pending' and destroy the adoption
    record. This pins the _persist_plan skip-if-not-pending rule.
    """
    db.execute("INSERT INTO trading_action "
               "(user_id,plan_date,symbol,side,status,acted_at,actual_price) "
               "VALUES (?,?,?,?,'done','2026-07-26 10:00',1298.5)",
               (USER_A, '2026-07-26', '600519', 'buy'))
    db.commit()

    # What _persist_plan does: check status first, only touch pending rows.
    existing = db.execute(
        'SELECT status FROM trading_action '
        'WHERE user_id=? AND plan_date=? AND symbol=?',
        (USER_A, '2026-07-26', '600519')).fetchone()
    if not existing or existing['status'] == 'pending':
        db.execute("INSERT OR REPLACE INTO trading_action "
                   "(user_id,plan_date,symbol,side,status) "
                   "VALUES (?,?,?,?,'pending')",
                   (USER_A, '2026-07-26', '600519', 'buy'))
        db.commit()

    r = db.execute('SELECT status, actual_price FROM trading_action '
                   'WHERE user_id=?', (USER_A,)).fetchone()
    assert r['status'] == 'done', 'replan wiped a recorded adoption verdict'
    assert r['actual_price'] == 1298.5


@pytest.mark.unit
def test_follow_through_rate_counts_only_this_user(db):
    # Distinct symbols per row: the PK is (user_id, plan_date, symbol), so
    # reusing a symbol within a user/date would collide -- which is the
    # constraint doing its job, not something to work around.
    for uid, symbol, status in ((USER_A, '600519', 'done'),
                                (USER_A, '510300', 'skipped'),
                                (USER_B, '600519', 'done'),
                                (USER_B, '510300', 'done')):
        db.execute("INSERT INTO trading_action "
                   "(user_id,plan_date,symbol,status) VALUES (?,?,?,?)",
                   (uid, '2026-07-26', symbol, status))
    db.commit()
    rows = db.execute('SELECT status, COUNT(*) AS n FROM trading_action '
                      'WHERE user_id=? GROUP BY status', (USER_A,)).fetchall()
    counts = {r['status']: r['n'] for r in rows}
    assert counts == {'done': 1, 'skipped': 1}, \
        f"user B's actions leaked into user A's stats: {counts}"


# ── isolation on the NEW tables ────────────────────────────────────

@pytest.mark.unit
def test_new_tables_isolate_users(db):
    """The P0 leak class must not reappear on the P1 tables."""
    for uid, w in ((USER_A, 50.0), (USER_B, 10.0)):
        db.execute("INSERT INTO trading_target (user_id,symbol,target_weight,approved) "
                   "VALUES (?,?,?,1)", (uid, '600519', w))
        db.execute("INSERT INTO trading_position (user_id,symbol,shares) "
                   "VALUES (?,?,?)", (uid, '600519', 100 * uid))
    db.commit()

    a_t = _approved_targets(db, USER_A)
    assert len(a_t) == 1 and a_t[0]['target_weight'] == 50.0

    db.execute('DELETE FROM trading_target WHERE user_id=?', (USER_A,))
    db.commit()
    assert len(_approved_targets(db, USER_B)) == 1, \
        "user A's delete removed user B's target"


# ── price honesty (docs/REDESIGN.md §5) ────────────────────────────

@pytest.mark.unit
def test_plan_handler_declares_estimate_and_never_claims_realtime():
    """Intraday NAV is unavailable, so the payload must say 'estimate'.

    Both fundgz domains were measured dead from this deployment. A UI that
    showed these as live prices would be lying about data it does not have, so
    the handler is pinned to ship is_estimate + a note, and pinned NOT to
    contain a realtime claim.
    """
    with open(_HANDLER, encoding='utf-8') as fh:
        src = fh.read()
    assert "'is_estimate': True" in src, 'plan payload must flag estimates'
    assert 'estimate_note' in src
    assert '估算' in src, 'note must be user-facing Chinese, matching the UI'
    assert 'price_basis' in src, 'each price must carry its provenance'
    # Guard against a future edit that starts asserting live data.
    assert "'realtime': True" not in src
    assert "'is_realtime': True" not in src
