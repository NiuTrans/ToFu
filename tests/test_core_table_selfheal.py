"""Tests for the missing-Core-table boot probe — the table-shaped twin of the
critical-column self-heal (tests/test_sqlite_critical_column_selfheal.py).

Pins the fix for the 2026-07-25 paper_podcasts incident: the table was
registered in ``_core_schema`` (and its create DDL added to the full-migration
path) WITHOUT bumping ``_SCHEMA_VERSION`` — so every existing deployment,
already at the current version, fast-pathed past the create DDL forever and
500'd UndefinedTable / no-such-table at runtime (the Podcast tab's lookup
endpoint collapsed on every call). Fresh test DBs always ran full DDL, which
is why CI never saw it.

``_missing_core_tables`` runs BEFORE the version fast-path on BOTH backends
and forces a full DDL pass when any always-on Core table is absent. The probe
list derives from the shared Core MetaData minus optional-domain tables
(``core_boot_table_names``), so a newly added Core table is covered
automatically.

Layers:
  1. probe unit (SQLite + PG, patched existence probes — no live DB);
  2. boot-list unit (paper_podcasts in, trading_config OUT);
  3. SQLite end-to-end: fresh init → DROP a Core table → re-init at the SAME
     version → the probe forces full DDL and the table is recreated;
  4. NEUTER negative control: with the probe amputated the same re-init
     fast-paths and the table stays missing (proves the probe is load-bearing,
     not incidental).

Run:  pytest tests/test_core_table_selfheal.py -m unit
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _Conn:
    """Minimal stand-in for the project's connection wrapper (._conn)."""
    def __init__(self, raw):
        self._conn = raw


# ═══ 1. probe unit — both backends, no live DB ═══

@pytest.mark.unit
class TestMissingCoreTablesProbe:
    def test_sqlite_flags_absent_tables(self, monkeypatch):
        import lib.database._schema_sqlite as ss
        from lib.database._core_schema._helpers import core_boot_table_names
        present = set(core_boot_table_names('sqlite')) - {'paper_podcasts'}
        monkeypatch.setattr(ss, '_table_exists', lambda conn, t: t in present)
        assert ss._missing_core_tables(_Conn(object())) == ['paper_podcasts']

    def test_sqlite_no_missing_when_all_present(self, monkeypatch):
        import lib.database._schema_sqlite as ss
        monkeypatch.setattr(ss, '_table_exists', lambda conn, t: True)
        assert ss._missing_core_tables(_Conn(object())) == []

    def test_sqlite_probe_failure_is_best_effort(self, monkeypatch):
        """A raising probe must not propagate — returns [] (never blocks boot)."""
        import lib.database._schema_sqlite as ss

        def _raise(conn, t):
            raise RuntimeError('sqlite_master unavailable')
        monkeypatch.setattr(ss, '_table_exists', _raise)
        assert ss._missing_core_tables(_Conn(object())) == []

    def test_pg_flags_absent_tables(self, monkeypatch):
        import lib.database._schema_pg as sp
        from lib.database._core_schema._helpers import core_boot_table_names
        present = set(core_boot_table_names('pg')) - {'paper_podcasts'}
        monkeypatch.setattr(sp, '_table_exists', lambda conn, t: t in present)
        assert sp._missing_core_tables(_Conn(object())) == ['paper_podcasts']

    def test_pg_no_missing_when_all_present(self, monkeypatch):
        import lib.database._schema_pg as sp
        monkeypatch.setattr(sp, '_table_exists', lambda conn, t: True)
        assert sp._missing_core_tables(_Conn(object())) == []

    def test_pg_probe_failure_is_best_effort(self, monkeypatch):
        import lib.database._schema_pg as sp

        def _raise(conn, t):
            raise RuntimeError('information_schema unavailable')
        monkeypatch.setattr(sp, '_table_exists', _raise)
        assert sp._missing_core_tables(_Conn(object())) == []


# ═══ 2. boot-list unit ═══

@pytest.mark.unit
class TestCoreBootTableNames:
    def test_includes_paper_podcasts(self):
        """The incident table must be probed on every boot, both backends."""
        from lib.database._core_schema._helpers import core_boot_table_names
        assert 'paper_podcasts' in core_boot_table_names('sqlite')
        assert 'paper_podcasts' in core_boot_table_names('pg')

    def test_excludes_optional_domain_tables(self):
        """trading_config is created by the trading domain's registered
        initializer, NOT the always-on bootstrap — probing it would force a
        full DDL pass on EVERY boot of a vanilla (trading-disabled) install."""
        from lib.database._core_schema._helpers import (
            OPTIONAL_DOMAIN_TABLES, core_boot_table_names)
        for backend in ('sqlite', 'pg'):
            names = core_boot_table_names(backend)
            assert 'trading_config' not in names
            for name in names:
                assert name not in OPTIONAL_DOMAIN_TABLES

    def test_excludes_pg_only_tables_on_sqlite(self):
        """error_resolutions has NO SQLite CREATE by design (see the
        ERROR_RESOLUTIONS note in _tables.py) — probing it on SQLite would
        force a full DDL pass on EVERY boot of every SQLite install."""
        from lib.database._core_schema._helpers import (
            PG_ONLY_CORE_TABLES, core_boot_table_names)
        assert 'error_resolutions' in PG_ONLY_CORE_TABLES
        sqlite_names = core_boot_table_names('sqlite')
        pg_names = core_boot_table_names('pg')
        for name in PG_ONLY_CORE_TABLES:
            assert name not in sqlite_names
            assert name in pg_names

    def test_sorted_and_unique(self):
        from lib.database._core_schema._helpers import core_boot_table_names
        for backend in ('sqlite', 'pg'):
            names = core_boot_table_names(backend)
            assert names == sorted(set(names))

    def test_covers_every_registered_core_table(self):
        """A newly registered Core table lands in the probe automatically —
        the exact gap (registered but never created on existing deployments)
        this mechanism closes. Per-backend intentional exclusions are the
        ONLY tolerated gaps. Iterates the package-init SNAPSHOT (what
        _core_schema itself registered), not the live shared MetaData —
        test-defined tables are out of scope by design."""
        import lib.database._core_schema as _cs
        from lib.database._core_schema._helpers import (
            OPTIONAL_DOMAIN_TABLES, PG_ONLY_CORE_TABLES, core_boot_table_names)
        sqlite_names = set(core_boot_table_names('sqlite'))
        pg_names = set(core_boot_table_names('pg'))
        for table_name in _cs._CORE_REGISTERED_TABLES:
            if table_name in OPTIONAL_DOMAIN_TABLES:
                continue
            assert table_name in pg_names, \
                f'Core table {table_name} missing from the PG boot probe list'
            if table_name in PG_ONLY_CORE_TABLES:
                continue
            assert table_name in sqlite_names, \
                f'Core table {table_name} missing from the SQLite boot probe list'

    def test_define_table_pollution_does_not_leak_into_probe(self):
        """Regression pin for the groundwork-suite pollution incident:
        define_table() registers on the SHARED MetaData (its documented
        purpose — compile-only DDL fixtures), and without the snapshot
        guard every such table becomes a phantom 'missing' table that
        forces a full DDL pass on EVERY boot (caught live when
        test_core_schema_groundwork ran before this suite)."""
        import sqlalchemy as sa
        from lib.database._core_schema import define_table
        from lib.database._core_schema._helpers import (
            core_boot_table_names, metadata)
        define_table('zz_probe_pollution_demo',
                     sa.Column('id', sa.Text, primary_key=True))
        assert 'zz_probe_pollution_demo' in metadata.tables  # registered…
        for backend in ('sqlite', 'pg'):
            assert 'zz_probe_pollution_demo' not in core_boot_table_names(backend), \
                'test-defined table leaked into the boot probe list'

    def test_snapshot_is_frozen_at_package_init(self):
        """The snapshot covers exactly the tables _core_schema registers —
        a table added to _tables.py must appear (auto-coverage), and the
        snapshot itself must not grow with later define_table() calls."""
        import lib.database._core_schema as _cs
        assert 'paper_podcasts' in _cs._CORE_REGISTERED_TABLES
        assert 'zz_probe_pollution_demo' not in _cs._CORE_REGISTERED_TABLES


# ═══ 3+4. SQLite end-to-end + NEUTER negative control ═══

@pytest.mark.unit
class TestMissingCoreTableSelfHealE2E:
    """Full-init on a fresh SQLite file, drop a Core table, re-init at the
    SAME schema version: the probe must force the full DDL pass that
    recreates it. Then the NEUTER: with the probe amputated, the identical
    re-init fast-paths and the table stays missing."""

    @pytest.fixture()
    def fresh_db(self, tmp_path):
        from lib.database import reset_sqlite_for_tests, restore_db_state
        snapshot = reset_sqlite_for_tests(str(tmp_path / 'probe.db'))
        try:
            yield tmp_path / 'probe.db'
        finally:
            restore_db_state(snapshot)

    @staticmethod
    def _table_exists(name):
        from lib.database._core import _new_connection
        conn = _new_connection()
        try:
            cur = conn._conn.cursor()
            cur.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (name,))
            return cur.fetchone() is not None
        finally:
            conn.close()

    @staticmethod
    def _drop(name):
        from lib.database._core import _new_connection
        conn = _new_connection()
        try:
            conn._conn.execute(f'DROP TABLE IF EXISTS {name}')
            conn._conn.commit()
        finally:
            conn.close()

    def test_reinit_recreates_dropped_core_table(self, fresh_db):
        """The incident shape: version ALREADY current, table absent."""
        from lib.database import init_db
        assert self._table_exists('paper_podcasts')  # fresh init created it
        self._drop('paper_podcasts')
        assert not self._table_exists('paper_podcasts')
        init_db()  # version unchanged (42 == 42) — probe must force full DDL
        assert self._table_exists('paper_podcasts'), \
            'version-current re-init did NOT recreate the dropped Core table'

    def test_NEUTER_without_probe_fastpath_leaves_table_missing(
            self, fresh_db, monkeypatch):
        """Amputate the probe (facade global consulted by init_db) and the
        SAME re-init must fast-path past the create DDL — proving the probe
        is load-bearing, not incidental. Restoring the probe then heals."""
        import lib.database._schema_sqlite as ss
        from lib.database import init_db
        self._drop('paper_podcasts')
        monkeypatch.setattr(ss, '_missing_core_tables', lambda conn: [])
        init_db()
        assert not self._table_exists('paper_podcasts'), \
            'probe amputated but the table was still recreated — the heal ' \
            'is coming from somewhere else (test is stale)'
        monkeypatch.undo()
        init_db()
        assert self._table_exists('paper_podcasts')

    def test_reinit_with_all_tables_present_is_fast_path(
            self, fresh_db, monkeypatch):
        """The healthy steady state must NOT pay a full DDL pass: with every
        Core table present the probe returns [] and init_db returns early
        (guarded here by patching the full-DDL entry points to explode —
        a fast-pathed init never reaches them)."""
        import lib.database._schema_sqlite as ss
        from lib.database import init_db
        monkeypatch.setattr(
            ss, '_init_chat_schema',
            lambda conn: (_ for _ in ()).throw(
                AssertionError('full DDL ran on a converged DB')))
        monkeypatch.setattr(
            ss, '_init_system_schema',
            lambda conn: (_ for _ in ()).throw(
                AssertionError('full DDL ran on a converged DB')))
        init_db()  # must fast-path silently


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-x', '-q', '-m', 'unit']))
