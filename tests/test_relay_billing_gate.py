"""tests/test_relay_billing_gate.py — Agent-only vs full-relay gating.

Covers ``lib.relay_config`` (the new single source of truth for relay
policy) and the ``billing_enabled`` no-op gate in
``lib.billing.request_flow``. The flag lets an operator run an
**agent-only** relay (users bring their own model keys; Tofu never
charges) vs a **full relay** (Tofu meters tokens + debits credits).

These are pure-unit tests — no DB, no HTTP. We drive ``relay.json`` by
monkeypatching the store path and toggle the env override directly.
"""

from __future__ import annotations

import json
import os

import pytest

import lib.relay_config as rc


@pytest.fixture()
def relay_file(tmp_path, monkeypatch):
    """Point lib.relay_config at a temp relay.json and clear the env."""
    p = tmp_path / 'relay.json'
    monkeypatch.setattr(rc, '_STORE_PATH', str(p))
    monkeypatch.delenv('TOFU_RELAY_BILLING', raising=False)

    def _write(d):
        p.write_text(json.dumps(d), encoding='utf-8')

    return _write


# ── relay_config defaults + file + env precedence ────────────────────

def test_billing_enabled_defaults_true_when_no_file(relay_file):
    # No file written → default True (backward compat with existing relays).
    assert rc.billing_enabled() is True
    assert rc.signup_enabled() is False


def test_billing_enabled_from_file_false(relay_file):
    relay_file({'billing_enabled': False})
    assert rc.billing_enabled() is False


def test_billing_enabled_from_file_true(relay_file):
    relay_file({'billing_enabled': True, 'signup_enabled': True})
    assert rc.billing_enabled() is True
    assert rc.signup_enabled() is True


def test_env_overrides_file(relay_file, monkeypatch):
    relay_file({'billing_enabled': True})
    monkeypatch.setenv('TOFU_RELAY_BILLING', '0')
    assert rc.billing_enabled() is False
    monkeypatch.setenv('TOFU_RELAY_BILLING', 'true')
    assert rc.billing_enabled() is True


def test_public_summary_shape(relay_file):
    relay_file({'billing_enabled': False, 'signup_enabled': True,
                'signup_default_role': 'user'})
    s = rc.public_summary()
    assert s == {'signup_enabled': True, 'billing_enabled': False,
                 'model_relay_enabled': True}
    # Must NOT leak the welcome-credit / default-role internals.
    assert 'signup_welcome_credit_micro' not in s


def test_missing_keys_fall_back_to_defaults(relay_file):
    relay_file({'signup_enabled': True})  # billing_enabled omitted
    assert rc.signup_enabled() is True
    assert rc.billing_enabled() is True   # default


# ── request_flow gate: reserve/settle no-op when billing disabled ────

def test_reserve_noop_when_billing_disabled(relay_file, monkeypatch):
    relay_file({'billing_enabled': False})
    from lib.billing import request_flow as rf

    # If reserve_for_task tried to actually reserve, it would import
    # lib.billing.reserve and hit the DB. Make that explode so a leak is
    # loud rather than silent.
    import lib.billing as billing
    monkeypatch.setattr(billing, 'reserve', _boom, raising=False)

    task = {'id': 'task_x'}
    got = rf.reserve_for_task(task, user_id='usr_abc', model='gpt-4o',
                              prompt_tokens=1000, max_completion_tokens=512)
    assert got == 0
    assert '_billing_reservation_micro' not in task


def test_reserve_runs_when_billing_enabled(relay_file, monkeypatch):
    relay_file({'billing_enabled': True})
    from lib.billing import request_flow as rf
    import lib.billing as billing

    calls = {}

    def _fake_estimate(model, *, prompt_tokens, max_completion_tokens,
                       headroom=1.5):
        return 1234

    def _fake_reserve(user_id, micro, *, ref_id, note=''):
        calls['reserve'] = (user_id, micro, ref_id)

    monkeypatch.setattr(billing, 'estimate_request_cost', _fake_estimate,
                        raising=False)
    monkeypatch.setattr(billing, 'reserve', _fake_reserve, raising=False)

    task = {'id': 'task_y'}
    got = rf.reserve_for_task(task, user_id='usr_abc', model='gpt-4o',
                              prompt_tokens=1000, max_completion_tokens=512)
    assert got == 1234
    assert task['_billing_reservation_micro'] == 1234
    assert calls['reserve'] == ('usr_abc', 1234, 'task_y')


def test_settle_noop_when_billing_disabled(relay_file, monkeypatch):
    relay_file({'billing_enabled': False})
    from lib.billing import request_flow as rf
    import lib.billing as billing

    # settle / debit must never be called.
    monkeypatch.setattr(billing, 'settle', _boom, raising=False)
    monkeypatch.setattr(billing, 'debit', _boom, raising=False)

    task = {'id': 'task_z', 'usage': {'input_tokens': 100,
                                      'output_tokens': 200},
            '_billing_reservation_micro': 500}
    assert rf.settle_task(task, user_id='usr_abc', model='gpt-4o') is None


def test_reserve_and_settle_still_noop_without_user(relay_file):
    # Empty user_id short-circuits regardless of billing flag (personal
    # / open install).
    relay_file({'billing_enabled': True})
    from lib.billing import request_flow as rf
    assert rf.reserve_for_task({'id': 't'}, user_id='', model='m',
                               prompt_tokens=10) == 0
    assert rf.settle_task({'id': 't', 'usage': {}}, user_id='',
                          model='m') is None


def _boom(*a, **k):
    raise AssertionError('billing function called while billing disabled')


# ── pricing editor: margin-only (rate rows are read-only now) ────────
# As of the 2026-06-24 single-engine unification, save_prices was replaced
# by save_margin: per-model RATES are authoritative in lib/pricing.py and are
# NOT writable through billing (no second driftable table). The only tunable
# is the relay margin.

@pytest.fixture()
def pricing_file(tmp_path, monkeypatch):
    """Point lib.billing.pricing at a temp pricing.json."""
    import lib.billing.pricing as pr
    p = tmp_path / 'pricing.json'
    monkeypatch.setattr(pr, '_PRICING_PATH', str(p))
    # Reset the module cache so each test starts clean.
    monkeypatch.setattr(pr, '_cache', None, raising=False)
    monkeypatch.setattr(pr, '_cache_mtime', 0.0, raising=False)
    return pr


def test_save_margin_roundtrip(pricing_file):
    pr = pricing_file
    saved = pr.save_margin(0.25)
    assert saved['default_margin'] == 0.25
    # Hot reload: get_default_margin reflects the new value immediately.
    assert pr.get_default_margin() == 0.25
    # The seeded model rate rows are preserved untouched (read-only display).
    assert saved['models']['gpt-4o']['input_per_mtok_micro'] == 2_500_000
    assert pr.get_price('gpt-4o').input_per_mtok_micro == 2_500_000


def test_save_margin_preserves_rate_rows(pricing_file):
    # Saving the margin a second time must NOT mutate any rate row.
    pr = pricing_file
    pr.save_margin(0.10)
    before = pr.list_prices()['models']
    pr.save_margin(0.40)
    after = pr.list_prices()['models']
    assert before == after
    assert pr.get_default_margin() == 0.40


def test_pricing_views_do_not_expose_the_nested_live_cache(pricing_file):
    pr = pricing_file
    view = pr.list_prices()
    view['models']['gpt-4o']['input_per_mtok_micro'] = 1
    view['default_model']['output_per_mtok_micro'] = 2

    fresh = pr.list_prices()
    assert fresh['models']['gpt-4o']['input_per_mtok_micro'] == 2_500_000
    assert fresh['default_model']['output_per_mtok_micro'] == 15_000_000


def test_save_margin_merges_the_latest_disk_document(pricing_file):
    from lib.json_store import update_json_atomic

    pr = pricing_file
    pr.list_prices()  # populate the in-process cache with the old document

    def _operator_edit(cfg):
        cfg['operator_note'] = 'must survive margin update'
        cfg['models']['operator-model'] = {
            'input_per_mtok_micro': 123,
            'output_per_mtok_micro': 456,
        }
        return cfg

    update_json_atomic(pr._PRICING_PATH, _operator_edit, default={})
    saved = pr.save_margin(0.33)

    assert saved['default_margin'] == 0.33
    assert saved['operator_note'] == 'must survive margin update'
    assert saved['models']['operator-model']['output_per_mtok_micro'] == 456


def test_save_margin_rejects_negative(pricing_file):
    pr = pricing_file
    with pytest.raises(pr.PricingError):
        pr.save_margin(-0.5)


def test_save_margin_rejects_out_of_range(pricing_file):
    pr = pricing_file
    with pytest.raises(pr.PricingError):
        pr.save_margin(101.0)


def test_save_margin_rejects_non_numeric(pricing_file):
    pr = pricing_file
    with pytest.raises(pr.PricingError):
        pr.save_margin('free')


def test_save_prices_symbol_removed(pricing_file):
    # The old full-table writer must be gone (it was the drift source).
    pr = pricing_file
    assert not hasattr(pr, 'save_prices')


# ── model_relay_enabled: flag + scope strip + request guard ──────────

def test_model_relay_defaults_true(relay_file):
    assert rc.model_relay_enabled() is True


def test_model_relay_from_file_false(relay_file):
    relay_file({'model_relay_enabled': False})
    assert rc.model_relay_enabled() is False


def test_model_relay_env_override(relay_file, monkeypatch):
    relay_file({'model_relay_enabled': True})
    monkeypatch.setenv('TOFU_RELAY_MODEL', '0')
    assert rc.model_relay_enabled() is False


def test_model_relay_independent_of_billing(relay_file):
    # The two flags must NOT imply each other.
    relay_file({'billing_enabled': False, 'model_relay_enabled': True})
    assert rc.billing_enabled() is False
    assert rc.model_relay_enabled() is True
    relay_file({'billing_enabled': True, 'model_relay_enabled': False})
    assert rc.billing_enabled() is True
    assert rc.model_relay_enabled() is False


def test_public_summary_includes_model_relay(relay_file):
    relay_file({'model_relay_enabled': False})
    s = rc.public_summary()
    assert s['model_relay_enabled'] is False
    assert set(s) == {'signup_enabled', 'billing_enabled',
                      'model_relay_enabled'}


def test_session_scopes_strip_chat_when_model_relay_off(relay_file):
    relay_file({'model_relay_enabled': False})
    from routes.api_v1.users import _session_scopes
    scopes = _session_scopes()
    assert 'chat' not in scopes          # operator pool withheld
    assert 'agents:run' in scopes        # BYO agent path granted instead
    assert 'providers' in scopes         # so the user can register an endpoint
    # Non-pool scopes survive.
    assert 'tasks' in scopes and 'conversations' in scopes


def test_session_scopes_keep_chat_when_model_relay_on(relay_file):
    relay_file({'model_relay_enabled': True})
    from routes.api_v1.users import _session_scopes
    scopes = _session_scopes()
    assert 'chat' in scopes


def _ctx(scopes):
    from lib.api_keys import AuthContext
    return AuthContext(key_id='k1', name='t', scopes=frozenset(scopes),
                       rate_limit_rpm=0, rate_limit_tpd=0)


def test_model_relay_guard_allows_byo_when_disabled(relay_file):
    relay_file({'model_relay_enabled': False})
    from routes.api_v1.auth import model_relay_guard
    # is_byo=True must pass even with model relay off — that's the point.
    assert model_relay_guard(is_byo=True) is None


def test_model_relay_guard_allows_when_enabled(relay_file):
    relay_file({'model_relay_enabled': True})
    from routes.api_v1.auth import model_relay_guard
    assert model_relay_guard(is_byo=False) is None


def test_guard_or_dispose_invariant(relay_file, monkeypatch):
    """The one-shot helper rejects ONLY when there is no BYO handle (pool
    request); a present handle is a BYO request and always passes. This
    is the invariant that makes the disposal branch unreachable — assert
    it so a future refactor that breaks it fails loudly here."""
    relay_file({'model_relay_enabled': False})
    import server  # noqa: F401 — installs Flask→Quart shim
    from server import app
    from lib.api_keys import AuthContext
    from quart import g
    import lib.llm_dispatch.ephemeral as eph

    disposed = []
    monkeypatch.setattr(eph, 'dispose_ephemeral_slot',
                        lambda h: disposed.append(h), raising=False)

    class _FakeHandle:
        handle_id = 'h_test'

    from routes.api_v1.auth import guard_model_relay_or_dispose

    async def _run():
        async with app.test_request_context('/api/v1/agent/run', method='POST'):
            g.auth_ctx = AuthContext(key_id='k', name='t',
                                     scopes=frozenset({'agents:run'}),
                                     rate_limit_rpm=0, rate_limit_tpd=0,
                                     user_id='usr_1')
            # Pool request (no handle) → rejected, nothing to dispose.
            assert guard_model_relay_or_dispose(None) is not None
            # BYO request (handle present) → allowed, slot survives.
            handle = _FakeHandle()
            assert guard_model_relay_or_dispose(handle) is None
            assert handle not in disposed

    import asyncio
    asyncio.new_event_loop().run_until_complete(_run())


def test_all_completion_surfaces_wire_the_guard():
    """Defense against a future 5th completion surface forgetting the gate.

    Every route module that resolves a BYO model AND can reach the
    operator slot pool must call ``guard_model_relay_or_dispose``. We
    assert the wiring by source inspection so adding a new surface
    without the guard fails CI loudly.
    """
    import inspect
    import server  # noqa: F401 — shim
    import routes.api_v1.chat as r_chat
    import routes.api_v1.agent_run as r_run
    import routes.compat_openai as r_oai
    import routes.compat_anthropic as r_ant

    for mod in (r_chat, r_run, r_oai, r_ant):
        src = inspect.getsource(mod)
        assert 'guard_model_relay_or_dispose(' in src, (
            f'{mod.__name__} no longer wires the model-relay guard')
