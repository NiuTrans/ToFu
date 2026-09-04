"""tests/test_run_id_uniqueness.py — run ids must survive two launches in one second.

THE DEFECT
----------
Five call sites built run ids as ``f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}"``.
That has SECOND resolution, and three of those ids land in ``TEXT NOT NULL
UNIQUE`` columns:

    trading_sim_sessions.session_id    <- llm_simulator.run_simulation
    trading_autopilot_cycles.cycle_id  <- trading_autopilot/cycle.py
                                          web/handlers/trading_tasks.py

Measured before the fix: two calls in the same second returned byte-identical
ids (``sim_20260729_170815`` twice), and the second INSERT raised
``sqlite3.IntegrityError: UNIQUE constraint failed``. In ``run_simulation`` that
exception was uncaught and propagated out of the entire function — the run died
with no session row and no message a user could read.

WHY THE GUARD IS SHAPED THIS WAY
--------------------------------
The obvious test — "call the minter twice, assert the ids differ" — is weak: it
passes even if the entropy is one hex character. So the guards below also:

  * drive the REAL ``run_simulation`` twice against one database, which is the
    scenario that actually crashed (and which was literally inexpressible in the
    test suite while the bug existed — a structural reason end-to-end coverage
    stayed missing);
  * insert minted ids into a genuinely UNIQUE column rather than trusting
    string inequality;
  * assert no source file rebuilds the bare timestamp format, so the next id
    added cannot quietly reintroduce the drift that caused this.

A retry-until-unique loop would also make these pass, and is explicitly not the
fix: it shrinks the race window instead of removing it, converting a
deterministic bug into an intermittent one.
"""

from __future__ import annotations

import datetime as dt
import math
import os
import random
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _strip_comments(text: str) -> str:
    """Drop Python line comments before scanning source text.

    The banned format is DISCUSSED at length in the comments explaining why it
    was removed, so a naive scan would flag exactly the code that documents the
    fix.
    """
    out = []
    for line in text.splitlines():
        quote = None
        cut = len(line)
        for i, ch in enumerate(line):
            if quote:
                if ch == quote:
                    quote = None
            elif ch in '"\'':
                quote = ch
            elif ch == '#':
                cut = i
                break
        out.append(line[:cut])
    return '\n'.join(out)


# ═══════════════════════════════════════════════════════════
#  1. The minter itself
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestMintRunId:
    def test_two_ids_in_the_same_second_differ(self):
        from tofu_trading.run_ids import mint_run_id

        ids = [mint_run_id('sim', uid=1) for _ in range(50)]
        assert len(set(ids)) == 50, 'collision within a single second'

    def test_ids_survive_a_unique_column(self):
        """String inequality is not the real requirement — the column is."""
        from tofu_trading.run_ids import mint_run_id

        conn = sqlite3.connect(':memory:')
        conn.execute('CREATE TABLE t (session_id TEXT NOT NULL UNIQUE)')
        for _ in range(200):
            conn.execute('INSERT INTO t VALUES (?)', (mint_run_id('sim', uid=1),))
        assert conn.execute('SELECT COUNT(*) FROM t').fetchone()[0] == 200

    def test_distinct_users_cannot_collide(self):
        """The old id carried no user id, so the collision crossed tenants."""
        from tofu_trading.run_ids import mint_run_id

        a = mint_run_id('autopilot', uid=1)
        b = mint_run_id('autopilot', uid=2)
        assert a != b
        assert '_u1_' in a and '_u2_' in b

    def test_background_workers_may_omit_uid(self):
        from tofu_trading.run_ids import mint_run_id

        ids = [mint_run_id('brain') for _ in range(50)]
        assert len(set(ids)) == 50
        assert all('_u' not in i.replace('_uuid', '') for i in ids)

    def test_keeps_a_readable_timestamp(self):
        """These ids show up in the UI and logs; 'which run was this' should be
        answerable at a glance. The timestamp is for humans, the uuid is what
        makes it unique."""
        from tofu_trading.run_ids import mint_run_id

        today = dt.datetime.now().strftime('%Y%m%d')
        assert mint_run_id('sim', uid=1).startswith(f'sim_{today}_')

    def test_entropy_is_not_trimmed_to_a_colliding_width(self):
        from tofu_trading.run_ids import RUN_ID_ENTROPY_HEX, mint_run_id

        assert RUN_ID_ENTROPY_HEX >= 8, (
            'entropy trimmed below 32 bits — collisions return under burst load')
        tail = mint_run_id('sim', uid=1).rsplit('_', 1)[-1]
        assert len(tail) == RUN_ID_ENTROPY_HEX

    def test_empty_prefix_is_rejected(self):
        from tofu_trading.run_ids import mint_run_id

        with pytest.raises(ValueError):
            mint_run_id('')


# ═══════════════════════════════════════════════════════════
#  2. No call site rebuilds the colliding format
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestNoHandRolledRunIds:
    _ID_SITES = (
        'tofu_trading/trading/llm_simulator.py',
        'tofu_trading/trading_autopilot/cycle.py',
        'tofu_trading/trading/brain/pipeline.py',
        'tofu_trading/web/handlers/trading_tasks.py',
        'tofu_trading/web/handlers/trading_brain.py',
    )

    def test_no_site_builds_a_bare_second_resolution_id(self):
        """Five copies of one format is what produced the bug.

        One of the six original sites (trading_tasks.py) already appended uuid4
        entropy and was correct, while the rest had drifted — so patching each
        by hand would have left the same drift in place for the next id added.
        """
        offenders = []
        for rel in self._ID_SITES:
            code = _strip_comments(open(os.path.join(_ROOT, rel), encoding='utf-8').read())
            for line in code.splitlines():
                if '%Y%m%d_%H%M%S' not in line:
                    continue
                # A timestamp is fine on its own; assigning it AS an id is not.
                if any(k in line for k in ('_id =', 'cycle_id', 'session_id', 'task_id')):
                    offenders.append(f'{rel}: {line.strip()}')
        assert not offenders, (
            'run id built from a bare second-resolution timestamp: '
            + '; '.join(offenders))

    def test_every_site_routes_through_the_minter(self):
        for rel in self._ID_SITES:
            code = _strip_comments(open(os.path.join(_ROOT, rel), encoding='utf-8').read())
            assert 'mint_run_id' in code, f'{rel} does not use the shared minter'

    def test_no_retry_until_unique_loop(self):
        """A retry loop shrinks the race window instead of removing it, and
        turns a deterministic bug into an intermittent one.

        Anchored on real control flow — a loop wrapping an INSERT or an
        IntegrityError handler — rather than on the words. ``run_ids.py``'s own
        docstring explains at length WHY retrying is banned, and a keyword scan
        flags exactly the file that documents the prohibition.
        """
        import ast

        for rel in self._ID_SITES + ('tofu_trading/run_ids.py',):
            src = open(os.path.join(_ROOT, rel), encoding='utf-8').read()
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.While, ast.For)):
                    continue
                body = ast.dump(node)
                if 'mint_run_id' in body and 'IntegrityError' in body:
                    pytest.fail(
                        f'{rel}: a loop retries id minting on IntegrityError — '
                        'that shrinks the race window instead of removing it')


# ═══════════════════════════════════════════════════════════
#  3. The scenario that actually crashed
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestTwoSimulationsInOneSecond:
    """Two runs against ONE database, back to back — the crashing scenario.

    This is the case the earlier end-to-end suite had to work around with a
    per-test database and an xfail marker, because the defect made it
    unrunnable. It is the real acceptance criterion for this ticket.
    """

    @pytest.fixture
    def sim_db(self, trading_connection_factory):
        conn = trading_connection_factory()
        from tofu_trading.trading.historical_data import _ensure_sim_tables
        _ensure_sim_tables(conn)
        yield conn
        conn.close()

    @staticmethod
    def _seed(db, symbol='600519', vol=0.30, seed=11, days=60):
        random.seed(seed)
        sigma = vol / math.sqrt(252)
        px = 10.0
        day = dt.date(2024, 1, 1)
        n = 0
        while n < days:
            prev = px
            px *= math.exp(random.gauss(-0.5 * sigma ** 2, sigma))
            if day.weekday() < 5:
                db.execute(
                    'INSERT OR REPLACE INTO trading_sim_prices'
                    ' (symbol, date, nav, open, close) VALUES (?, ?, ?, ?, ?)',
                    (symbol, day.strftime('%Y-%m-%d'), round(px, 4),
                     round(prev, 4), round(px, 4)))
                n += 1
            day += dt.timedelta(days=1)
        db.commit()

    @staticmethod
    def _stub(monkeypatch):
        import lib.llm_dispatch as LD
        import lib.llm_dispatch.api as LDA

        state = {'n': 0}

        def fake(*args, **kwargs):
            state['n'] += 1
            if state['n'] % 12 == 1:
                return ('<decisions>[{"action":"buy","symbol":"600519","amount":25000,'
                        '"confidence":95,"reason":"x"}]</decisions>', {})
            return ('<decisions>[]</decisions>', {})

        monkeypatch.setattr(LD, 'smart_chat', fake, raising=True)
        monkeypatch.setattr(LDA, 'smart_chat', fake, raising=True)

    def test_back_to_back_runs_both_succeed(self, sim_db, monkeypatch):
        from tofu_trading.trading.llm_simulator import SimulatorConfig, run_simulation

        self._seed(sim_db)
        self._stub(monkeypatch)

        results = []
        for _ in range(2):
            cfg = SimulatorConfig(symbols=['600519'], start_date='2024-01-01',
                                  end_date='2024-03-01', step_days=5,
                                  initial_capital=100000)
            results.append(run_simulation(sim_db, cfg, uid=1))

        assert all(r.get('status') == 'completed' for r in results), (
            f"a run did not complete: {[r.get('status') or r.get('error') for r in results]}")
        ids = [r['session_id'] for r in results]
        assert len(set(ids)) == 2, f'session ids collided: {ids}'

        rows = sim_db.execute(
            'SELECT COUNT(*) AS c FROM trading_sim_sessions').fetchone()
        assert dict(rows)['c'] == 2, 'both runs must have persisted a session row'

    def test_two_users_in_the_same_second_do_not_collide(self, sim_db, monkeypatch):
        """The old id carried no uid, so a shared host collided across tenants."""
        from tofu_trading.trading.llm_simulator import SimulatorConfig, run_simulation

        self._seed(sim_db)
        self._stub(monkeypatch)

        ids = []
        for uid in (1, 2):
            cfg = SimulatorConfig(symbols=['600519'], start_date='2024-01-01',
                                  end_date='2024-03-01', step_days=5,
                                  initial_capital=100000)
            res = run_simulation(sim_db, cfg, uid=uid)
            assert res.get('status') == 'completed'
            ids.append(res['session_id'])
        assert len(set(ids)) == 2, f'cross-tenant collision: {ids}'
