#!/usr/bin/env python3
"""D2 direct-DSN admin lane (Epic pt_6879b628) — closes the audit #4/#5 trap.

Multi-statement DDL migrations (``_schema_pg.init_db``) and the corruption
self-heal ``VACUUM (FULL)`` / ``REINDEX`` (``_core.heal_toast_corruption``)
CANNOT run through a transaction pooler: VACUUM FULL is illegal inside a pooled
transaction block, and the DDL ``SET SESSION`` + restore straddles a connection
recycle. With ``TOFU_PG_VIA_POOLER=1`` set, routing those admin paths through
the pooler would ACTIVELY BREAK them.

The admin lane connects to a DIRECT PG endpoint (``TOFU_PG_DIRECT_DSN``, default
= the normal DSN) with a REAL session (SET SESSION, never the pooler
startup-options form). With pooling OFF it is byte-identical to today (admin
uses the same DSN + session setup as a normal connection).

Run with ``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`` (autoload trips a spurious vispy
GL-ES import at collection in this env).
"""

import inspect

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def core():
    import lib.database._core as c
    return c


# ── _pg_admin_dsn: default falls back to normal DSN, override honored ──────────

def test_admin_dsn_defaults_to_normal(core, monkeypatch):
    monkeypatch.delenv('TOFU_PG_DIRECT_DSN', raising=False)
    assert core._pg_admin_dsn() == core.PG_DSN


def test_admin_dsn_override(core, monkeypatch):
    monkeypatch.setenv('TOFU_PG_DIRECT_DSN', 'host=real-pg port=5432 dbname=tofu')
    assert core._pg_admin_dsn() == 'host=real-pg port=5432 dbname=tofu'


# ── _pg_connect_target: the pure routing decision ─────────────────────────────

def test_target_normal_pooler_off_is_legacy(core, monkeypatch):
    monkeypatch.delenv('TOFU_PG_VIA_POOLER', raising=False)
    dsn, plan = core._pg_connect_target(admin=False)
    assert dsn == core.PG_DSN
    assert plan['emit_set_session'] is True and plan['options'] is None


def test_target_admin_pooler_off_byte_identical_to_normal(core, monkeypatch):
    """With pooling OFF, an admin connection must be IDENTICAL to a normal one
    (same DSN, same session setup) — zero behaviour change for single-box."""
    monkeypatch.delenv('TOFU_PG_VIA_POOLER', raising=False)
    monkeypatch.delenv('TOFU_PG_DIRECT_DSN', raising=False)
    assert core._pg_connect_target(admin=True) == core._pg_connect_target(admin=False)


def test_target_normal_pooler_on_uses_startup_options(core, monkeypatch):
    monkeypatch.setenv('TOFU_PG_VIA_POOLER', '1')
    dsn, plan = core._pg_connect_target(admin=False)
    assert dsn == core.PG_DSN
    assert plan['emit_set_session'] is False
    assert plan['options'] and '-c statement_timeout=' in plan['options']


def test_target_admin_pooler_on_bypasses_pooler(core, monkeypatch):
    """The load-bearing case: pooling ON + admin → connect DIRECT with a real
    session (SET SESSION), NOT the pooler startup-options."""
    monkeypatch.setenv('TOFU_PG_VIA_POOLER', '1')
    monkeypatch.setenv('TOFU_PG_DIRECT_DSN', 'host=real-pg port=5432 dbname=tofu')
    dsn, plan = core._pg_connect_target(admin=True)
    assert dsn == 'host=real-pg port=5432 dbname=tofu'
    assert plan['emit_set_session'] is True, 'admin must use a real session'
    assert plan['options'] is None, 'admin must NOT ship pooler startup-options'


def test_target_admin_pooler_on_default_direct_dsn(core, monkeypatch):
    """Even without an explicit direct DSN, admin+pooler-on must fall back to
    the normal DSN with a real session — never the pooler options form."""
    monkeypatch.setenv('TOFU_PG_VIA_POOLER', '1')
    monkeypatch.delenv('TOFU_PG_DIRECT_DSN', raising=False)
    dsn, plan = core._pg_connect_target(admin=True)
    assert dsn == core.PG_DSN
    assert plan['options'] is None and plan['emit_set_session'] is True


# ── wiring guards (DB-free source inspection) ─────────────────────────────────

def test_new_pg_connection_takes_admin_and_uses_target(core):
    sig = inspect.signature(core._new_pg_connection)
    assert 'admin' in sig.parameters and sig.parameters['admin'].default is False
    src = inspect.getsource(core._new_pg_connection)
    assert '_pg_connect_target(admin)' in src, 'connect path must route via the target helper'
    assert 'psycopg2.connect(PG_DSN' not in src, \
        'must connect to the resolved _dsn, never the hardcoded PG_DSN'
    assert 'psycopg2.connect(_dsn' in src


def test_admin_factories_exist(core):
    assert callable(core._new_pg_admin_connection)
    assert callable(core._new_admin_connection)


def test_ddl_init_routed_through_admin(core):
    src = inspect.getsource(core.init_db)
    assert '_new_pg_admin_connection' in src, \
        'schema DDL init must use the admin (pooler-bypassing) connection'


def test_toast_heal_routed_through_admin(core):
    src = inspect.getsource(core.heal_toast_corruption)
    assert '_new_admin_connection()' in src, \
        'VACUUM/REINDEX self-heal must use the admin (pooler-bypassing) connection'


# ── NC: neuter the routing to ignore `admin` → the bypass contract breaks ──────

def test_NC_admin_routing_is_load_bearing(core, monkeypatch):
    monkeypatch.setenv('TOFU_PG_VIA_POOLER', '1')
    orig = core._pg_connect_target
    core._pg_connect_target = lambda admin=False: (
        core.PG_DSN, core._pg_session_setup_plan(core._pg_via_pooler()))
    try:
        with pytest.raises(AssertionError):
            _dsn, plan = core._pg_connect_target(admin=True)
            assert plan['options'] is None, \
                'admin must bypass the pooler (no startup-options)'
            assert plan['emit_set_session'] is True
    finally:
        core._pg_connect_target = orig
