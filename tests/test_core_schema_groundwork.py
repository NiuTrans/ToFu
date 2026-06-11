"""Groundwork tests for the SQLAlchemy Core table-definition layer
(`lib/database/_core_schema.py`).

These prove the B2 thesis WITHOUT touching the live DB: a single Core
`Table` definition compiles to correct PostgreSQL AND SQLite DDL + DML,
including the `ON CONFLICT` upsert that `_sql_translate.py`'s `_PK_MAP`
hand-maintains today. The module is also asserted to be INERT — it must
not open a SQLAlchemy Engine or run any DDL (that wiring is a §10.3 schema
change pending sign-off).

Skips gracefully if SQLAlchemy is not installed.
"""

from __future__ import annotations

import pytest

sqlalchemy = pytest.importorskip('sqlalchemy')

import sqlalchemy as sa  # noqa: E402

from lib.database import _core_schema as cs  # noqa: E402

pytestmark = pytest.mark.unit


def _demo_table(name='cs_demo'):
    return cs.define_table(
        name,
        sa.Column('id', sa.Text, primary_key=True),
        sa.Column('conv_id', sa.Text, nullable=False),
        sa.Column('meta', cs.jsonb_column(), server_default=sa.text("'{}'")),
        sa.Column('created_at', sa.BigInteger),
        sa.Column('pinned', sa.Boolean, server_default=sa.text('false')),
    )


def test_dual_dialect_ddl_differs_correctly():
    t = _demo_table('cs_demo_ddl')
    both = cs.both_ddl(t)
    pg, lite = both['pg'], both['sqlite']
    # JSON type diverges by backend: JSONB on PG, TEXT on SQLite (the project
    # stores JSON as a string; TEXT gives the right SQLite affinity — see
    # jsonb_column docstring). Must NOT emit JSON on SQLite (NUMERIC affinity).
    assert 'JSONB' in pg, pg
    assert 'JSON' not in lite, lite
    assert 'TEXT' in lite, lite
    # Both define the same table + PK.
    assert 'CREATE TABLE cs_demo_ddl' in pg
    assert 'CREATE TABLE cs_demo_ddl' in lite
    assert 'PRIMARY KEY (id)' in pg
    assert 'PRIMARY KEY (id)' in lite


def test_identity_autoincrement_variant():
    t = cs.define_table(
        'cs_demo_serial',
        sa.Column('seq', sa.Integer, sa.Identity(), primary_key=True),
        sa.Column('val', sa.Text),
    )
    both = cs.both_ddl(t)
    # PG emits IDENTITY; SQLite uses plain INTEGER PRIMARY KEY (rowid autoinc).
    assert 'IDENTITY' in both['pg'], both['pg']
    assert 'IDENTITY' not in both['sqlite'], both['sqlite']


def test_paramstyle_per_dialect():
    t = _demo_table('cs_demo_param')
    pg_ins = str(t.insert().compile(dialect=cs._PG_DIALECT))
    lite_ins = str(t.insert().compile(dialect=cs._SQLITE_DIALECT))
    assert '%(id)s' in pg_ins, pg_ins          # PG pyformat
    assert '?' in lite_ins or ':id' in lite_ins, lite_ins  # SQLite qmark/named


def test_upsert_compiles_for_both_backends():
    t = _demo_table('cs_demo_upsert')
    pg = cs.upsert_sql(t, conflict_cols=['id'], dialect=cs._PG_DIALECT)
    lite = cs.upsert_sql(t, conflict_cols=['id'], dialect=cs._SQLITE_DIALECT)
    assert 'ON CONFLICT (id) DO UPDATE' in pg, pg
    assert 'ON CONFLICT (id) DO UPDATE' in lite, lite
    # Both reference the conflict-row pseudo-table `excluded` (PG accepts it
    # case-insensitively; SQLAlchemy emits lowercase for both dialects).
    assert 'excluded.conv_id' in pg.lower(), pg
    assert 'excluded.conv_id' in lite.lower(), lite


def test_upsert_do_nothing():
    t = _demo_table('cs_demo_donothing')
    pg = cs.upsert_sql(t, conflict_cols=['id'], update_cols=[],
                       dialect=cs._PG_DIALECT)
    assert 'ON CONFLICT (id) DO NOTHING' in pg, pg


def test_upsert_sql_named_binds_both_backends():
    """upsert_sql must emit dict-bindable NAMED params on BOTH backends so one
    row-dict drives either — %(col)s on PG, :col on SQLite (NOT positional ?).
    This is the contract upsert() relies on for backend-agnostic binding."""
    t = _demo_table('cs_demo_named')
    pg = cs.upsert_sql(t, conflict_cols=['id'], dialect=cs._PG_DIALECT)
    lite = cs.upsert_sql(t, conflict_cols=['id'], dialect=cs._SQLITE_DIALECT)
    assert '%(id)s' in pg and '%(conv_id)s' in pg, pg
    assert ':id' in lite and ':conv_id' in lite, lite
    assert '?' not in lite, lite  # must be named, not positional


class _FakeDB:
    """Minimal db-shim wrapping a real sqlite3 conn, mimicking the project's
    connection wrapper (execute(sql, params) + commit)."""
    def __init__(self, conn):
        self._conn = conn
    def execute(self, sql, params=None):
        return self._conn.execute(sql, params or {})
    def commit(self):
        self._conn.commit()


def test_upsert_helper_insert_then_conflict_update(monkeypatch):
    """End-to-end upsert() on real SQLite: insert, then conflict→update via the
    SAME row-dict. Proves the single reusable call-site pattern."""
    import sqlite3
    from lib.database import _core
    monkeypatch.setattr(_core, '_BACKEND', 'sqlite', raising=False)
    cs._UPSERT_SQL_CACHE.clear()
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE kv (k TEXT PRIMARY KEY, v TEXT NOT NULL, n INTEGER NOT NULL)')
    t = cs.define_table('kv', sa.Column('k', sa.Text, primary_key=True),
                        sa.Column('v', sa.Text, nullable=False),
                        sa.Column('n', sa.Integer, nullable=False))
    db = _FakeDB(conn)
    cs.upsert(db, t, {'k': 'a', 'v': 'first', 'n': 1}, commit=True)
    assert conn.execute('SELECT v, n FROM kv WHERE k=?', ('a',)).fetchone() == ('first', 1)
    cs.upsert(db, t, {'k': 'a', 'v': 'second', 'n': 2}, commit=True)  # conflict
    assert conn.execute('SELECT v, n FROM kv WHERE k=?', ('a',)).fetchone() == ('second', 2)
    assert conn.execute('SELECT count(*) FROM kv').fetchone()[0] == 1  # updated, not duplicated


def test_upsert_helper_composite_pk_hotpath(monkeypatch):
    """The hot-path shape: composite PK + selective update_cols (task_events
    style). conflict_cols defaults to the full PK."""
    import sqlite3
    from lib.database import _core
    monkeypatch.setattr(_core, '_BACKEND', 'sqlite', raising=False)
    cs._UPSERT_SQL_CACHE.clear()
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE ev (tid TEXT, eid INTEGER, payload TEXT NOT NULL, '
                 'PRIMARY KEY (tid, eid))')
    t = cs.define_table('ev', sa.Column('tid', sa.Text), sa.Column('eid', sa.Integer),
                        sa.Column('payload', sa.Text, nullable=False),
                        sa.PrimaryKeyConstraint('tid', 'eid'))
    db = _FakeDB(conn)
    cs.upsert(db, t, {'tid': 'x', 'eid': 1, 'payload': 'p1'}, commit=True)
    cs.upsert(db, t, {'tid': 'x', 'eid': 1, 'payload': 'p2'}, commit=True)  # same PK
    cs.upsert(db, t, {'tid': 'x', 'eid': 2, 'payload': 'p3'}, commit=True)  # new PK
    rows = conn.execute('SELECT tid, eid, payload FROM ev ORDER BY eid').fetchall()
    assert rows == [('x', 1, 'p2'), ('x', 2, 'p3')]  # (x,1) updated; (x,2) inserted


def test_upsert_insert_cols_partial_uses_defaults(monkeypatch):
    """insert_cols restricts the INSERT to a subset; omitted columns fall to
    their schema DEFAULT (the feishu `conversations` shape that only sets some
    columns and relies on `settings` default + the search_tsv trigger)."""
    import sqlite3
    from lib.database import _core
    monkeypatch.setattr(_core, '_BACKEND', 'sqlite', raising=False)
    cs._UPSERT_SQL_CACHE.clear()
    conn = sqlite3.connect(':memory:')
    conn.execute("CREATE TABLE part1 (id TEXT, uid INTEGER, title TEXT NOT NULL, "
                 "extra TEXT NOT NULL DEFAULT 'def', PRIMARY KEY (id, uid))")
    t = cs.define_table('part1', sa.Column('id', sa.Text), sa.Column('uid', sa.Integer),
                        sa.Column('title', sa.Text, nullable=False),
                        sa.Column('extra', sa.Text, nullable=False, server_default='def'),
                        sa.PrimaryKeyConstraint('id', 'uid'))
    db = _FakeDB(conn)
    # Insert only id/uid/title — `extra` must take its DEFAULT, not error.
    cs.upsert(db, t, {'id': 'a', 'uid': 1, 'title': 'T1'},
              insert_cols=['id', 'uid', 'title'], commit=True)
    assert conn.execute('SELECT title, extra FROM part1 WHERE id=?', ('a',)).fetchone() == ('T1', 'def')


def test_upsert_insert_cols_partial_update_only_written(monkeypatch):
    """On conflict, a partial insert_cols upsert updates ONLY the columns it
    wrote (update_cols defaults to inserted-minus-conflict), leaving a
    previously-set, now-omitted column untouched."""
    import sqlite3
    from lib.database import _core
    monkeypatch.setattr(_core, '_BACKEND', 'sqlite', raising=False)
    cs._UPSERT_SQL_CACHE.clear()
    conn = sqlite3.connect(':memory:')
    conn.execute("CREATE TABLE part2 (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
                 "settings TEXT NOT NULL DEFAULT '{}')")
    t = cs.define_table('part2', sa.Column('id', sa.Text, primary_key=True),
                        sa.Column('title', sa.Text, nullable=False),
                        sa.Column('settings', sa.Text, nullable=False, server_default='{}'))
    db = _FakeDB(conn)
    # Seed a full row with non-default settings.
    conn.execute("INSERT INTO part2 (id, title, settings) VALUES ('a', 'orig', '{\"k\":1}')")
    conn.commit()
    # Partial upsert that only writes title → settings must be preserved.
    cs.upsert(db, t, {'id': 'a', 'title': 'updated'},
              insert_cols=['id', 'title'], conflict_cols=['id'], commit=True)
    row = conn.execute('SELECT title, settings FROM part2 WHERE id=?', ('a',)).fetchone()
    assert row == ('updated', '{"k":1}')  # title updated, settings untouched


def test_upsert_sql_cache_is_populated(monkeypatch):
    """Hot-path callers must not recompile: upsert() memoizes the compiled SQL."""
    import sqlite3
    from lib.database import _core
    monkeypatch.setattr(_core, '_BACKEND', 'sqlite', raising=False)
    cs._UPSERT_SQL_CACHE.clear()
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE c1 (k TEXT PRIMARY KEY, v TEXT NOT NULL)')
    t = cs.define_table('c1', sa.Column('k', sa.Text, primary_key=True),
                        sa.Column('v', sa.Text, nullable=False))
    db = _FakeDB(conn)
    cs.upsert(db, t, {'k': 'a', 'v': '1'}, commit=True)
    cs.upsert(db, t, {'k': 'b', 'v': '2'}, commit=True)
    assert len(cs._UPSERT_SQL_CACHE) == 1  # one compile shared across calls



def test_upsert_do_nothing_rowcount_canary(monkeypatch):
    """DO NOTHING upsert (update_cols=[]) must report rowcount=1 on a real
    insert and rowcount=0 on a duplicate (conflict) — the exact semantics the
    task_events event-log collision canary depends on. Must hold on SQLite
    (this test) and PG (verified live in the prototype). retry=False is
    required so the cursor (and its rowcount) is returned."""
    import sqlite3
    from lib.database import _core
    monkeypatch.setattr(_core, '_BACKEND', 'sqlite', raising=False)
    cs._UPSERT_SQL_CACHE.clear()
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE ev2 (tid TEXT, eid INTEGER, payload TEXT NOT NULL, '
                 'PRIMARY KEY (tid, eid))')
    t = cs.define_table('ev2', sa.Column('tid', sa.Text), sa.Column('eid', sa.Integer),
                        sa.Column('payload', sa.Text, nullable=False),
                        sa.PrimaryKeyConstraint('tid', 'eid'))
    db = _FakeDB(conn)
    row = {'tid': 'x', 'eid': 1, 'payload': '{}'}
    cur = cs.upsert(db, t, row, conflict_cols=['tid', 'eid'],
                    insert_cols=['tid', 'eid', 'payload'], update_cols=[],
                    commit=True, retry=False)
    assert cur.rowcount == 1, 'insert must report rowcount=1'
    cur = cs.upsert(db, t, row, conflict_cols=['tid', 'eid'],
                    insert_cols=['tid', 'eid', 'payload'], update_cols=[],
                    commit=True, retry=False)
    assert cur.rowcount == 0, 'duplicate (conflict) must report rowcount=0 (the canary)'
    cur = cs.upsert(db, t, {'tid': 'x', 'eid': 2, 'payload': '{}'},
                    conflict_cols=['tid', 'eid'],
                    insert_cols=['tid', 'eid', 'payload'], update_cols=[],
                    commit=True, retry=False)
    assert cur.rowcount == 1, 'new PK must report rowcount=1'
    assert conn.execute('SELECT count(*) FROM ev2').fetchone()[0] == 2  # no dup row


def test_active_dialect_follows_backend(monkeypatch):
    from lib.database import _core
    monkeypatch.setattr(_core, '_BACKEND', 'pg', raising=False)
    assert cs._active_dialect() is cs._PG_DIALECT
    monkeypatch.setattr(_core, '_BACKEND', 'sqlite', raising=False)
    assert cs._active_dialect() is cs._SQLITE_DIALECT


def test_module_is_inert_no_engine():
    """Groundwork must NOT create an Engine or run DDL on import/use.
    We assert the module exposes no Engine/Connection objects."""
    import inspect
    src = inspect.getsource(cs)
    # No engine creation in the module source.
    assert 'create_engine' not in src, (
        '_core_schema.py must not create a SQLAlchemy Engine — execution '
        'goes through the existing get_db() connection, not SQLAlchemy.'
    )
    # The private MetaData must not be bound to any engine.
    assert cs.metadata.bind is None if hasattr(cs.metadata, 'bind') else True


def test_ddl_for_uses_active_backend(monkeypatch):
    from lib.database import _core
    t = _demo_table('cs_demo_active')
    monkeypatch.setattr(_core, '_BACKEND', 'pg', raising=False)
    assert 'JSONB' in cs.ddl_for(t)
    monkeypatch.setattr(_core, '_BACKEND', 'sqlite', raising=False)
    assert 'JSONB' not in cs.ddl_for(t)
