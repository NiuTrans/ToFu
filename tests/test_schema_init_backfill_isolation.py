"""tests/test_schema_init_backfill_isolation.py — a DATA-heal step must never
be able to abort schema initialisation.

THE BUG (pt_2a8aed4d)
=====================
``lib/database/_schema_sqlite/__init__.py::init_db`` used to do, inline before
the DDL fast-path::

    from lib.paper.hash_backfill import backfill_paper_hash_canonical
    backfill_paper_hash_canonical(conn)

That import is the ONLY edge from schema init into the ``lib.paper`` → LLM/swarm
chain: importing ``lib.paper.hash_backfill`` runs ``lib/paper/__init__.py`` (an
eager barrel) which transitively loads the entire LLM-dispatch + swarm stack.
On 2026-07-26 a single merge-conflict marker in ``lib/llm_sanitize/_gateway.py``
made this line raise ``SyntaxError`` — and because it sat INSIDE ``init_db``'s
try, the ``except`` logged and re-raised, aborting ALL DDL. Boot crashed twice
(2× CRITICAL) and a serving process answered requests against a schema-less DB
for 22 minutes (~700 ``no such table`` errors).

THE FIX
=======
The implementation now lives at dependency-light ``lib.paper_hash_backfill``
and is wrapped in its OWN try/except: on ANY failure (import error / syntax
error in the paper chain / a backfill runtime error) log at ERROR and CONTINUE
with DDL. The heal is idempotent and re-runs on the next boot; schema init is a
correctness contract, data heal is not.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_schema_init_backfill_isolation.py -v
"""
from __future__ import annotations

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


class _BrokenPaperBackfill:
    """Stand-in for ``lib.paper.hash_backfill`` when the paper → LLM/swarm
    chain is broken (e.g. a merge-conflict marker). Any attribute access —
    including ``from ... import backfill_paper_hash_canonical`` — raises the
    same SyntaxError the real cascade produced."""

    def __getattr__(self, name):  # noqa: D401
        raise SyntaxError('<<<<<<< Updated upstream')


def _broken_import(monkeypatch):
    """Force the dependency-light schema import to raise."""
    monkeypatch.setitem(sys.modules, 'lib.paper_hash_backfill',
                        _BrokenPaperBackfill())
    monkeypatch.setitem(sys.modules, 'lib.paper.hash_backfill',
                        _BrokenPaperBackfill())


def _schema_meta_exists():
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
    ).fetchone()
    return row is not None


def test_init_db_survives_a_broken_paper_backfill_import(tmp_path, monkeypatch):
    """THE CORE CASE: the cascade reproduced. init_db must complete DDL even
    though importing the backfill raises."""
    _broken_import(monkeypatch)
    from lib.database import reset_sqlite_for_tests, restore_db_state

    # reset_sqlite_for_tests runs init_db() on a fresh temp DB. Before the fix
    # this raised SyntaxError (init_db aborted); after the fix it must succeed.
    snap = reset_sqlite_for_tests(os.path.join(str(tmp_path), 'tofu.db'))
    try:
        assert _schema_meta_exists(), (
            'schema_meta missing — a data-heal failure aborted schema DDL')
    finally:
        restore_db_state(snap)


def test_init_db_survives_a_backfill_runtime_error(tmp_path, monkeypatch):
    """A backfill that imports fine but raises at RUNTIME is equally unable to
    abort DDL."""
    import lib.paper.hash_backfill as hb

    def _boom(_conn):
        raise RuntimeError('paper library corrupt mid-heal')

    monkeypatch.setattr(hb, 'backfill_paper_hash_canonical', _boom)
    from lib.database import reset_sqlite_for_tests, restore_db_state

    snap = reset_sqlite_for_tests(os.path.join(str(tmp_path), 'tofu.db'))
    try:
        assert _schema_meta_exists()
    finally:
        restore_db_state(snap)


def test_init_db_still_runs_the_backfill_when_healthy(tmp_path, monkeypatch):
    """REGRESSION GUARD: the isolation must not silently DISABLE the heal —
    when nothing is broken the backfill is still invoked."""
    import lib.paper.hash_backfill as hb
    called = []
    monkeypatch.setattr(hb, 'backfill_paper_hash_canonical',
                        lambda conn: called.append(True))
    from lib.database import reset_sqlite_for_tests, restore_db_state

    snap = reset_sqlite_for_tests(os.path.join(str(tmp_path), 'tofu.db'))
    try:
        assert called, 'the healthy backfill was skipped — isolation went too far'
        assert _schema_meta_exists()
    finally:
        restore_db_state(snap)


def test_fresh_init_does_not_log_expected_missing_tables_as_errors(
        tmp_path, caplog):
    """Pre-DDL backfill probes are expected misses, not boot failures."""
    from lib.database import reset_sqlite_for_tests, restore_db_state

    caplog.clear()
    with caplog.at_level(logging.ERROR, logger='lib.database'):
        snap = reset_sqlite_for_tests(os.path.join(str(tmp_path), 'tofu.db'))
    try:
        false_alarms = [
            record.getMessage() for record in caplog.records
            if 'no such table: schema_meta' in record.getMessage()
            or 'no such table: paper_library' in record.getMessage()
        ]
        assert false_alarms == []
    finally:
        restore_db_state(snap)
