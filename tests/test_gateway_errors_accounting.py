#!/usr/bin/env python3
"""tests/test_gateway_errors_accounting.py — gateway-class failures (HTTP
502/503/504, upstream-vendor transients wrapped in 4xx, mid-stream SSE
errors) must be accounted SEPARATELY from key health (epic
pt_473805ad518b4ac4).

The incident (2026-08-03): the sankuai AIGC gateway had a ~2-hour 502 storm
(813 bare ``<html>`` error pages) that hit ALL ~20 models on ALL THREE keys
uniformly. Every failed attempt landed in ``record_outcome(failure)``,
crushing the daily success-rate column to 40% / 77% / 10% — and, worse,
TRIPPING the daily auto-disable gate (attempts >= 5, rate < 0.5): two of
three keys were auto-disabled for the day by an outage that had nothing to
do with key health. Only the third key's higher success volume plus the
last-resort guard kept service alive.

This mirrors the 2026-07-28 contention precedent
(tests/test_shared_contention_metrics.py): an error class that says
"the upstream is sick", never "this key is sick", gets its OWN counter.

Pinned here:

  1. Slot accounting: a gateway error increments ``gateway_errors`` (not
     ``total_errors``) and compensates the attempt out of
     ``total_requests`` — the success-rate column reflects genuine
     outcomes only. consecutive_errors still bumps (steer-away cooldown
     with reason 'upstream' is unchanged).
  2. key_stats: a gateway error feeds the NEW ``gateway_errors`` daily
     counter and NOTHING else — no failure stats (the auto-disable gate
     must never see a gateway storm), no 429 streak, no last_error
     clobber.
  3. A 500-hit gateway storm NEVER auto-disables the key (the incident's
     actual damage), while a genuine dead key (401 / endpoint
     unreachable — both unchanged paths) still counts as failure.
  4. Quota precedence: is_gateway + is_quota_exhausted stays a
     billing-stop — the gateway classification never launders the money
     signal.
  5. Plain per-key 429 accounting is UNCHANGED (still feeds the streak
     telemetry, never the gateway counter).
  6. The model-health fold + dispatcher slot snapshot carry
     ``gateway_errors`` to the Settings card.

Run:  pytest tests/test_gateway_errors_accounting.py -m unit
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

PROV = 'gwacct'
KEY = 'gwacct_key_0'
PK = f'{PROV}::{KEY}'


def _make_slot(model='qwen-plus', key=KEY):
    from lib.llm_dispatch.slot import Slot
    return Slot(key_name=key, api_key='sk-test-1234', model=model,
                capabilities={'text'}, provider_id=PROV)


@pytest.fixture
def key_stats_recorders(monkeypatch):
    """Capture key_stats calls; the real ones persist to disk."""
    rec = {'rate_limit': [], 'outcome': [], 'exhausted': [], 'gateway': []}
    monkeypatch.setattr(
        'lib.key_stats.record_rate_limit',
        lambda p, k, reason='': rec['rate_limit'].append((p, k)) or False)
    monkeypatch.setattr(
        'lib.key_stats.record_outcome',
        lambda p, k, success, error='': rec['outcome'].append((p, k, success)))
    monkeypatch.setattr(
        'lib.key_stats.mark_key_exhausted',
        lambda p, k, reason='', model='': rec['exhausted'].append((p, k)))
    monkeypatch.setattr(
        'lib.key_stats.record_gateway_error',
        lambda p, k, reason='': rec['gateway'].append((p, k, reason)) or False)
    return rec


@pytest.fixture
def fresh_stats(monkeypatch, tmp_path):
    """Isolate lib.key_stats onto a tmp stats file + known siblings."""
    import lib.key_stats as ks

    snapshot = {
        'day': ks._cache['day'],
        'stats': ks._cache['stats'],
        'overrides': ks._cache['overrides'],
        'loaded': ks._cache['loaded'],
    }
    monkeypatch.setattr(ks, '_STATS_PATH', str(tmp_path / 'key_stats.json'))
    monkeypatch.setattr(ks, '_list_siblings', lambda pid: [PK])
    ks._cache['day'] = ''
    ks._cache['stats'] = {}
    ks._cache['overrides'] = {}
    ks._cache['loaded'] = False
    yield ks
    ks._cache['day'] = snapshot['day']
    ks._cache['stats'] = snapshot['stats']
    ks._cache['overrides'] = snapshot['overrides']
    ks._cache['loaded'] = snapshot['loaded']


# ══════════════════════════════════════════════════════════
#  Slot.record_error — gateway accounting
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestSlotGatewayAccounting:

    def test_gateway_error_is_not_a_health_error(self, key_stats_recorders):
        slot = _make_slot()
        slot.record_request()
        slot.record_error(is_rate_limit=True, is_gateway=True,
                          error='HTTP 502: <html> bad gateway')
        assert slot.gateway_errors == 1
        assert slot.total_errors == 0, (
            'a sick gateway must stay OUT of the model health success-rate')
        assert slot.total_requests == 0, (
            'the gateway-failed attempt is compensated out of '
            'total_requests — neither a success nor a failure of THIS key')
        assert slot.consecutive_errors == 1, (
            'the brief steer-away cooldown ladder still applies')
        assert slot.cooldown_reason == 'upstream'
        assert key_stats_recorders['outcome'] == [], (
            'gateway failures must NOT land in the daily failure stats — '
            'that is what tripped the auto-disable gate on 2026-08-03')
        assert key_stats_recorders['rate_limit'] == [], (
            '…nor the consecutive-429 streak telemetry')
        assert len(key_stats_recorders['gateway']) == 1, (
            'the volume stays visible on its OWN counter')

    def test_success_rate_reflects_genuine_outcomes_only(
            self, key_stats_recorders):
        """The incident's shape: 9 gateway 502s + 1 real error + 2
        successes → the card must show 67%, not 18%."""
        slot = _make_slot()
        for _ in range(9):
            slot.record_request()
            slot.record_error(is_rate_limit=True, is_gateway=True,
                              error='HTTP 502')
        slot.record_request()
        slot.record_error(is_rate_limit=False, error='boom')
        for _ in range(2):
            slot.record_request()
            slot.record_success(100)
        assert slot.gateway_errors == 9
        assert slot.total_requests == 3
        assert slot.total_errors == 1
        assert abs(slot.success_rate - (1 - 1 / 3)) < 1e-9

    def test_plain_429_accounting_unchanged(self, key_stats_recorders):
        """Complement: the fix must NOT launder real per-key throttling."""
        slot = _make_slot()
        slot.record_request()
        slot.record_error(is_rate_limit=True, error='HTTP 429 generic')
        assert slot.gateway_errors == 0
        assert slot.total_errors == 1
        assert slot.total_requests == 1
        assert len(key_stats_recorders['rate_limit']) == 1
        assert key_stats_recorders['gateway'] == []

    def test_quota_precedence_over_gateway(self, key_stats_recorders):
        """is_gateway + is_quota_exhausted stays a billing-stop — the
        gateway reclassification must never launder the money signal."""
        slot = _make_slot()
        slot.record_request()
        slot.record_error(is_rate_limit=True, is_gateway=True,
                          is_quota_exhausted=True,
                          error='insufficient_quota')
        assert slot.gateway_errors == 0
        assert slot.total_errors == 1, (
            'a quota death IS genuine key health — it keeps counting')
        assert slot.cooldown_reason == 'quota'
        assert len(key_stats_recorders['exhausted']) == 1
        assert key_stats_recorders['gateway'] == []


# ══════════════════════════════════════════════════════════
#  key_stats daily accounting — the auto-disable gate is unreachable
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestDailyGatewayAccounting:

    def test_gateway_storm_never_auto_disables(self, fresh_stats):
        """The 2026-08-03 incident pin: 500 gateway failures, zero genuine
        outcomes → the daily auto-disable gate (attempts>=5, rate<0.5)
        must not even SEE the storm."""
        ks = fresh_stats
        for _ in range(500):
            ks.record_gateway_error(PROV, KEY, reason='HTTP 502: <html>')

        row = ks.get_today_stats(PROV, KEY)
        assert row['gateway_errors'] == 500
        assert row['total'] == 0, (
            'gateway errors must not enter the success-rate denominator')
        assert row['auto_disabled'] is False
        assert row['enabled'] is True
        assert row['exhausted'] is False

    def test_gateway_errors_do_not_pollute_success_rate(self, fresh_stats):
        ks = fresh_stats
        for _ in range(9):
            ks.record_gateway_error(PROV, KEY, reason='HTTP 503')
        ks.record_outcome(PROV, KEY, success=True)
        ks.record_outcome(PROV, KEY, success=True)
        ks.record_outcome(PROV, KEY, success=False, error='HTTP 500 boom')
        row = ks.get_today_stats(PROV, KEY)
        assert row['gateway_errors'] == 9
        assert row['success'] == 2 and row['failure'] == 1
        assert abs(row['success_rate'] - 2 / 3) < 1e-9, (
            'gateway volume must leave the genuine success rate untouched')

    def test_gateway_error_does_not_clobber_last_error(self, fresh_stats):
        """A bare ``<html>`` 502 body is diagnostic garbage — it must never
        hide the last REAL failure from the Settings card (the
        record_rate_limit precedent)."""
        ks = fresh_stats
        ks.record_outcome(PROV, KEY, success=False,
                          error='HTTP 401 real auth death')
        for _ in range(50):
            ks.record_gateway_error(PROV, KEY, reason='HTTP 502: <html>')
        row = ks.get_today_stats(PROV, KEY)
        assert row['last_error'] == 'HTTP 401 real auth death'
        assert row['gateway_errors'] == 50

    def test_gateway_errors_in_all_stats_projection(self, fresh_stats):
        ks = fresh_stats
        ks.record_gateway_error(PROV, KEY, reason='HTTP 504')
        snap = ks.get_all_stats()
        assert snap['keys'][PK]['gateway_errors'] == 1

    def test_genuine_failures_still_feed_the_safety_net(self, fresh_stats):
        """The dead-key safety net lives on the UNCHANGED classes: auth
        death (401/403) and endpoint-unreachable both record genuine
        failures. Gateway reclassification must not soften them."""
        ks = fresh_stats
        for _ in range(6):
            ks.record_outcome(PROV, KEY, success=False,
                              error='HTTP 401 key revoked')
        row = ks.get_today_stats(PROV, KEY)
        assert row['failure'] == 6
        assert row['auto_disabled'] is True, (
            'a genuinely dead key still trips the daily gate')


# ══════════════════════════════════════════════════════════
#  Projections — dispatcher snapshot + model-health fold
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestProjections:

    def test_slots_info_carries_gateway_errors(self):
        import threading
        from lib.llm_dispatch.dispatcher import LLMDispatcher

        slot = _make_slot()
        slot.gateway_errors = 7
        disp = object.__new__(LLMDispatcher)
        disp._lock = threading.Lock()
        disp.slots = [slot]
        disp.initialize = lambda: None
        row = disp.get_slots_info()[0]
        assert row['gateway_errors'] == 7

    def test_model_health_fold_carries_gateway_errors(self):
        from lib.dispatch_stats import aggregate_model_health
        out = aggregate_model_health([
            {'provider_id': 'sankuai', 'model': 'kimi-k3',
             'total_requests': 2, 'total_errors': 1, 'gateway_errors': 30},
            {'provider_id': 'sankuai', 'model': 'kimi-k3',
             'total_requests': 3, 'total_errors': 0, 'gateway_errors': 12},
        ])
        row = out['providers']['sankuai']['kimi-k3']
        assert row['gateway_errors'] == 42
        assert row['total_errors'] == 1
        assert row['success_rate'] == 0.8


# ══════════════════════════════════════════════════════════
#  Dispatch-loop integration — a 502 storm attempt then a success
# ══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestDispatchLoopIntegration:

    def test_gateway_502_then_success_feeds_only_the_gateway_counter(
            self, monkeypatch, key_stats_recorders):
        from lib.llm_dispatch import api
        from lib.llm_errors import RateLimitError
        from tests.test_vendor_transient_dispatch import _FakeDispatcher

        slot1 = _make_slot(key='gwacct_key_1')
        slot2 = _make_slot(key='gwacct_key_2')
        disp = _FakeDispatcher([slot1, slot2])
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
        monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)
        monkeypatch.setattr('lib.llm_dispatch.api.time.sleep',
                            lambda *_a, **_k: None)

        calls = {'n': 0}

        def _fake_stream(body, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RateLimitError('API HTTP 502: <html>',
                                     is_gateway=True, status_code=502)
            return 'ok', 'stop', {}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')

        assert msg == 'ok'
        assert calls['n'] == 2
        assert slot1.gateway_errors == 1
        assert slot1.total_errors == 0
        failures = [c for c in key_stats_recorders['outcome'] if not c[2]]
        assert failures == [], (
            'the 502 attempt must not land in the daily failure stats '
            '(the slot2 success is legitimate and unrelated)')
        assert len(key_stats_recorders['gateway']) == 1


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
