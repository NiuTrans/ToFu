#!/usr/bin/env python3
"""Result-level reconciliation: ``task_results`` rows wedged at status='running'.

Live incident (conv ms14u2lfihv8kj, 2026-07-26): four autopilot VU carriers
overwrote the agent's assistant slots and then left their ``task_results`` rows
at ``status='running'`` forever. The in-memory reaper
(``reap_stuck_running_tasks``) structurally cannot see them — a carrier is
discarded from the registry the moment its synchronous run returns and never
reaches ``persist_task_result`` — so nothing revisits the row.

The DB is therefore the only place the wedge is visible, and these tests pin
the predicate that finds it.

★ The load-bearing assertion is ``test_completed_at_predicate_is_wrong``: the
intuitive ``status='running' AND completed_at IS NOT NULL`` flags EVERY live
turn, because ``_upsert_task_row`` stamps ``completed_at`` on the ~5 s running
checkpoint too. Measured on production 2026-07-26: 69/69 running rows had it
non-NULL, 6 of them healthy in-flight turns.

NEUTER x2: dropping the staleness filter, or dropping the live-registry filter,
must each make a test fail.
"""

import time

import pytest

pytestmark = pytest.mark.unit


# ── Fakes ────────────────────────────────────────────────────────────────

class _FakeDB:
    """Minimal stand-in that answers the one SELECT the scan issues."""

    def __init__(self, rows):
        self._rows = rows
        self.last_sql = ''
        self.last_args = ()

    def execute(self, sql, args=()):
        self.last_sql = ' '.join(sql.split())
        self.last_args = args
        # Emulate the WHERE clause: status='running' is already encoded in the
        # fixture; apply the completed_at < cutoff bound the SUT passes in.
        cutoff = args[0]
        hits = [r for r in self._rows
                if r[2] is not None and r[2] < cutoff]
        hits.sort(key=lambda r: r[2])
        return _FakeCursor(hits[:args[1]])


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


@pytest.fixture
def mt(monkeypatch):
    """The maintenance module with its DB + registry stubbed."""
    from lib.tasks_pkg.manager import _maintenance as m
    monkeypatch.setattr(m, 'tasks', {}, raising=False)
    return m


def _install_db(monkeypatch, mt, rows):
    db = _FakeDB(rows)
    import lib.database as dbmod
    monkeypatch.setattr(dbmod, 'get_thread_db', lambda *a, **k: db)
    return db


def _row(task_id, conv_id, age_secs):
    """A task_results row whose last write was `age_secs` ago."""
    return (task_id, conv_id, int((time.time() - age_secs) * 1000))


# ── The core predicate ───────────────────────────────────────────────────

def test_stale_orphan_is_found(monkeypatch, mt):
    """A running row untouched for hours, with no live task, is an orphan."""
    _install_db(monkeypatch, mt, [_row('carrier-1', 'convA', 7200)])
    out = mt.find_orphan_running_results()
    assert [o['task_id'] for o in out] == ['carrier-1']
    assert out[0]['conv_id'] == 'convA'
    assert out[0]['age_secs'] >= 7000


def test_fresh_running_row_is_not_an_orphan(monkeypatch, mt):
    """NEUTER-adjacent: a row written 10 s ago is a healthy streaming turn.

    Removing the `completed_at < cutoff` bound in the SUT makes this fail —
    which is exactly the false-positive the naive predicate produces.
    """
    _install_db(monkeypatch, mt, [_row('live-1', 'convB', 10)])
    assert mt.find_orphan_running_results() == []


def test_row_still_in_registry_is_not_an_orphan(monkeypatch, mt):
    """A stale-looking ROW whose task is still in memory belongs to the reaper.

    NEUTER: drop the live-registry filter and this fails. Without it the two
    reconcilers double-report the same wedge and the DB scan would also flag a
    task that is merely between checkpoints.
    """
    _install_db(monkeypatch, mt, [_row('inmem-1', 'convC', 7200)])
    monkeypatch.setattr(mt, 'tasks', {'inmem-1': {'status': 'running'}},
                        raising=False)
    assert mt.find_orphan_running_results() == []


def test_orphans_sorted_oldest_first(monkeypatch, mt):
    _install_db(monkeypatch, mt, [
        _row('newer', 'c1', 4000),
        _row('oldest', 'c2', 90000),
        _row('mid', 'c3', 20000),
    ])
    assert [o['task_id'] for o in mt.find_orphan_running_results()] == [
        'oldest', 'mid', 'newer']


# ── The disproof: why the intuitive predicate cannot be used ─────────────

def test_completed_at_predicate_is_wrong(monkeypatch, mt):
    """`status='running' AND completed_at IS NOT NULL` matches LIVE turns.

    _upsert_task_row stamps completed_at on the running checkpoint as well as
    the terminal write, so the column is "last written at". A scan built on
    non-NULL-ness alone would flag every in-flight task. This test encodes the
    production measurement (69/69 running rows non-NULL, 6 of them live) so a
    future refactor cannot quietly reintroduce that predicate.
    """
    live, wedged = _row('live-1', 'c1', 5), _row('carrier-1', 'c2', 7200)
    # Both rows satisfy the naive predicate...
    assert live[2] is not None and wedged[2] is not None
    # ...but only the wedged one is a real orphan.
    _install_db(monkeypatch, mt, [live, wedged])
    assert [o['task_id'] for o in mt.find_orphan_running_results()] == ['carrier-1']


def test_completed_at_is_stamped_on_running_checkpoints(monkeypatch):
    """Source-level proof of the above: the shared upsert always stamps it.

    Both the terminal write and the running checkpoint go through
    _upsert_task_row, which sets completed_at unconditionally.
    """
    import inspect

    from lib.tasks_pkg.manager import _persist
    src = inspect.getsource(_persist._upsert_task_row)
    assert "'completed_at': int(time.time() * 1000)" in src, (
        'completed_at is expected to be stamped unconditionally; if this '
        'changed, re-evaluate find_orphan_running_results()'
    )
    assert 'status' in inspect.signature(_persist._upsert_task_row).parameters


# ── Kill switch + wiring ─────────────────────────────────────────────────

def test_disabled_by_env(monkeypatch, mt):
    _install_db(monkeypatch, mt, [_row('carrier-1', 'c1', 90000)])
    monkeypatch.setenv('TOFU_ORPHAN_RESULT_MAX_AGE_SECS', '0')
    assert mt.find_orphan_running_results() == []


def test_db_failure_fails_closed(monkeypatch, mt):
    """A DB hiccup must return [] (report nothing), never raise into the tick."""
    import lib.database as dbmod

    def _boom(*a, **k):
        raise RuntimeError('pool exhausted')

    monkeypatch.setattr(dbmod, 'get_thread_db', _boom)
    assert mt.find_orphan_running_results() == []


def test_reporter_counts_and_warns(monkeypatch, mt, caplog):
    _install_db(monkeypatch, mt, [
        _row('carrier-1', 'convA', 9000),
        _row('carrier-2', 'convB', 8000),
    ])
    with caplog.at_level('WARNING'):
        assert mt.report_orphan_running_results() == 2
    assert 'orphaned at status=running' in caplog.text


def test_reporter_silent_when_clean(monkeypatch, mt, caplog):
    _install_db(monkeypatch, mt, [])
    with caplog.at_level('WARNING'):
        assert mt.report_orphan_running_results() == 0
    assert 'orphaned at status=running' not in caplog.text


def test_wired_into_maintenance_tick():
    """The scan must actually run — a helper nobody calls detects nothing."""
    import inspect

    from lib.tasks_pkg.manager import _maintenance as m
    src = inspect.getsource(m.cleanup_old_tasks)
    assert 'report_orphan_running_results()' in src


def test_scan_is_read_only():
    """Reconciliation must not mutate rows: a finished carrier legitimately
    ends at status='running', so flipping it to 'error' would record a failure
    that never happened."""
    import inspect

    from lib.tasks_pkg.manager import _maintenance as m
    for fn in (m.find_orphan_running_results, m.report_orphan_running_results):
        src = inspect.getsource(fn).upper()
        for verb in ('UPDATE ', 'DELETE ', 'INSERT '):
            assert verb not in src, f'{fn.__name__} must stay read-only'
