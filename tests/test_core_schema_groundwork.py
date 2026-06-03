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
    # JSON type diverges by backend (JSONB on PG, JSON/TEXT on SQLite).
    assert 'JSONB' in pg, pg
    assert 'JSONB' not in lite, lite
    assert 'JSON' in lite, lite
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
