#!/usr/bin/env python3
"""D2 transaction-pooling session-state compatibility (Epic pt_6879b628).

Under PgBouncer transaction pooling a server backend is returned to the pool at
every COMMIT, so a connect-time ``SET SESSION`` (statement_timeout /
idle_in_transaction_session_timeout) is a double bug: it silently no-ops for the
setter (the next transaction may land on a different server backend that never
saw the SET) AND it LEAKS the modified GUC to the next unrelated pool borrower.

The pooler-safe form applies the timeouts as server-startup GUCs via the libpq
``options`` parameter (they ride the server backend for its whole pooled
lifetime and are part of the pool key, so they never leak) and SKIPS the
connect-time SET SESSION.

Gated behind ``TOFU_PG_VIA_POOLER`` — OFF (single-box default) is byte-identical
to legacy (SET SESSION emitted, no ``options``); ON is the transaction-safe form.

Run with ``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`` (autoload pulls a spurious vispy
GL-ES import error at collection in this env).
"""

import inspect
import re

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def core():
    import lib.database._core as c
    return c


# ── env parser ───────────────────────────────────────────────────────────────

def test_via_pooler_default_off(core, monkeypatch):
    monkeypatch.delenv('TOFU_PG_VIA_POOLER', raising=False)
    assert core._pg_via_pooler() is False


@pytest.mark.parametrize('val', ['1', 'true', 'TRUE', 'yes', 'on', 'On'])
def test_via_pooler_truthy(core, monkeypatch, val):
    monkeypatch.setenv('TOFU_PG_VIA_POOLER', val)
    assert core._pg_via_pooler() is True


@pytest.mark.parametrize('val', ['0', 'false', 'no', 'off', '', '  '])
def test_via_pooler_falsy(core, monkeypatch, val):
    monkeypatch.setenv('TOFU_PG_VIA_POOLER', val)
    assert core._pg_via_pooler() is False


# ── plan: flag OFF is byte-identical to legacy ────────────────────────────────

def test_plan_off_is_legacy(core):
    plan = core._pg_session_setup_plan(False)
    assert plan['emit_set_session'] is True
    assert plan['options'] is None


# ── plan: flag ON is the transaction-safe form ────────────────────────────────

def test_plan_on_uses_startup_options_not_set_session(core):
    plan = core._pg_session_setup_plan(True)
    assert plan['emit_set_session'] is False
    opts = plan['options']
    assert opts, 'pooler mode must supply libpq startup options'
    assert '-c statement_timeout=' in opts
    assert '-c idle_in_transaction_session_timeout=' in opts
    # carries the SAME numeric values as the legacy SET SESSION so behaviour
    # (the actual timeouts enforced) is unchanged — only the mechanism differs.
    assert str(core._STATEMENT_TIMEOUT_MS) in opts
    assert str(core._IDLE_IN_TRANSACTION_S) in opts


def test_plan_on_options_is_valid_libpq_form(core):
    opts = core._pg_session_setup_plan(True)['options']
    pairs = re.findall(r'-c (\S+)=(\S+)', opts)
    keys = {k for k, _ in pairs}
    assert keys == {'statement_timeout', 'idle_in_transaction_session_timeout'}, \
        'exactly the two timeout GUCs, each as a -c flag'


# ── wiring: the connect path actually consults the plan (DB-free source guard) ─

def test_connect_path_wires_the_plan(core):
    src = inspect.getsource(core._new_pg_connection)
    # The connect path resolves (dsn, plan) via _pg_connect_target, which for a
    # non-admin connection computes the plan from _pg_via_pooler() (asserted
    # directly in test_target_normal_pooler_on_uses_startup_options).
    assert '_pg_connect_target(admin)' in src, \
        'the connect path must resolve dsn+plan from the env-driven target helper'
    assert "_session_plan['emit_set_session']" in src, \
        'the SET SESSION block must be gated on the plan'
    assert "_session_plan['options']" in src, \
        'the connect kwargs must take options from the plan'


# ── NC: neuter the plan to always-legacy → the pooler-safe contract breaks ─────

def test_NC_plan_guard_is_load_bearing(core):
    """If _pg_session_setup_plan ignored via_pooler (always legacy), the
    flag-ON pooler-safe contract would break — prove that assertion bites."""
    orig = core._pg_session_setup_plan
    core._pg_session_setup_plan = lambda via: {'options': None, 'emit_set_session': True}
    try:
        with pytest.raises(AssertionError):
            plan = core._pg_session_setup_plan(True)
            assert plan['emit_set_session'] is False, \
                'pooler mode must NOT emit SET SESSION (leak/no-op defect)'
            assert plan['options'], 'pooler mode must carry startup options'
    finally:
        core._pg_session_setup_plan = orig
