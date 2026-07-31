#!/usr/bin/env python3
"""tests/test_key_stats_model_exhaustion.py — per-(key, model) billing-stop
granularity guards (epic pt_69e9d6038c9344dc, 2026-07-28 incident).

The incident: sankuai_key_1 took an Aliyun ``insufficient_quota`` on
qwen3.5-plus (11:10:34) and ``mark_key_exhausted`` flipped the KEY-WIDE
flag — on an aggregating gateway where the same key also routes
kimi-k3→Moonshot, a qwen billing-stop cross-poisoned kimi capacity. And
the stop "didn't hold" in practice because all three keys carry a stale
manual ``override: true`` that silently wins over any billing-stop.

Pinned here:

  1. A model-named billing-stop blocks THAT model only — sibling models on
     the same key stay dispatchable (no cross-vendor poisoning).
  2. Callers that cannot name a model still get key-wide exhaustion
     (legacy contract preserved).
  3. A manual override still wins over per-model stops (user supremacy) —
     so nobody "fixes" the conflict by auto-clearing user state.
  4. Re-enabling a key (override True) clears per-model stops ("I topped
     up") — otherwise the next call would re-stop instantly.
  5. The stats rows expose ``exhausted_models`` (the UI badge's data) and
     the field survives a persist/reload round-trip.
  6. Dispatcher-level: the pick filter consults the model dimension — a
     slot whose (key, model) is stopped is skipped while the same key's
     other model is picked.

Run:  pytest tests/test_key_stats_model_exhaustion.py -m unit
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

PROV = 'gwtest'
KEY = 'gwtest_key_0'
PK = f'{PROV}::{KEY}'


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


@pytest.mark.unit
class TestPerModelBillingStop:

    def test_model_stop_blocks_only_that_model(self, fresh_stats):
        """The incident's core: qwen's quota-death must NOT poison kimi on
        the same key."""
        ks = fresh_stats
        ks.mark_key_exhausted(PROV, KEY, reason='insufficient_quota (aliyun)',
                              model='qwen3.5-plus')

        assert ks.is_key_enabled(PROV, KEY, model='qwen3.5-plus') is False
        assert ks.is_key_enabled(PROV, KEY, model='kimi-k3') is True, (
            'cross-vendor poisoning: a qwen billing-stop took kimi down too')
        # Key-wide reads (no model given) stay enabled — the key itself is
        # not exhausted.
        assert ks.is_key_enabled(PROV, KEY) is True
        row = ks.get_today_stats(PROV, KEY)
        assert row['exhausted'] is False
        assert row['exhausted_models'] == {
            'qwen3.5-plus': 'insufficient_quota (aliyun)'}

    def test_modelless_stop_stays_key_wide(self, fresh_stats):
        """Legacy contract: no model named → the whole key stops."""
        ks = fresh_stats
        # A healthy sibling keeps the last-resort guard out of this test —
        # last-resort deliberately promotes the sole remaining key, which is
        # a separate documented behavior.
        ks._list_siblings = lambda pid: [PK, f'{PROV}::gwtest_key_1']
        ks.record_outcome(PROV, 'gwtest_key_1', success=True)
        ks.mark_key_exhausted(PROV, KEY, reason='HTTP 402')

        assert ks.is_key_enabled(PROV, KEY) is False
        assert ks.is_key_enabled(PROV, KEY, model='kimi-k3') is False

    def test_override_still_wins_over_model_stop(self, fresh_stats):
        """User supremacy: a manual ON defeats a billing-stop (the conflict
        is surfaced in the UI instead of auto-clearing user state)."""
        ks = fresh_stats
        ks.mark_key_exhausted(PROV, KEY, reason='insufficient_quota',
                              model='qwen3.5-plus')
        ks.set_key_override(PROV, KEY, True)
        # Re-enable clears stops ("I topped up")…
        assert ks.is_key_enabled(PROV, KEY, model='qwen3.5-plus') is True
        # …and a FRESH stop after the override is also overridden.
        ks.mark_key_exhausted(PROV, KEY, reason='insufficient_quota again',
                              model='qwen3.5-plus')
        assert ks.is_key_enabled(PROV, KEY, model='qwen3.5-plus') is True, (
            'manual override must keep winning — do not "fix" the conflict '
            'by silently clearing user state')
        assert ks.get_today_stats(PROV, KEY)['exhausted_models'], (
            'the stop is still RECORDED — the UI surfaces the conflict '
            'from this field')

    def test_reenable_clears_model_stops(self, fresh_stats):
        ks = fresh_stats
        ks.mark_key_exhausted(PROV, KEY, reason='q', model='qwen3.5-plus')
        ks.set_key_override(PROV, KEY, True)
        assert ks.get_today_stats(PROV, KEY)['exhausted_models'] == {}

    def test_exhausted_models_survive_persist_reload(self, fresh_stats):
        ks = fresh_stats
        ks.mark_key_exhausted(PROV, KEY, reason='q', model='qwen3.5-plus')
        # Force a reload from disk.
        ks._cache['loaded'] = False
        assert ks.is_key_enabled(PROV, KEY, model='qwen3.5-plus') is False
        assert ks.is_key_enabled(PROV, KEY, model='kimi-k3') is True

    def test_get_all_stats_exposes_exhausted_models(self, fresh_stats):
        ks = fresh_stats
        ks.mark_key_exhausted(PROV, KEY, reason='q', model='qwen3.5-plus')
        snap = ks.get_all_stats()
        assert snap['keys'][PK]['exhausted_models'] == {'qwen3.5-plus': 'q'}


@pytest.mark.unit
class TestDispatcherModelGate:

    def _dispatcher(self, slots):
        import threading
        from lib.llm_dispatch.dispatcher import LLMDispatcher
        disp = object.__new__(LLMDispatcher)
        disp._lock = threading.Lock()
        disp.slots = list(slots)
        disp.initialize = lambda: None
        return disp

    def _slot(self, model, key=KEY):
        from lib.llm_dispatch.slot import Slot
        return Slot(key_name=key, api_key='sk-test', model=model,
                    capabilities={'text'}, provider_id=PROV)

    def test_pick_skips_stopped_model_keeps_sibling(self, fresh_stats):
        """End-to-end at the pick seam: the stopped (key, model) slot is
        filtered out while the same key's healthy model is picked."""
        ks = fresh_stats
        ks.mark_key_exhausted(PROV, KEY, reason='insufficient_quota',
                              model='qwen3.5-plus')
        disp = self._dispatcher([
            self._slot('qwen3.5-plus'),
            self._slot('kimi-k3'),
        ])
        picked = disp._pick('text', None, None, None)
        assert picked is not None
        assert picked.model == 'kimi-k3', (
            'the pick filter must consult the model dimension — a stopped '
            '(key, model) slot has to be skipped, not dispatched')

    def test_pick_returns_none_when_only_model_stopped(self, fresh_stats):
        ks = fresh_stats
        ks.mark_key_exhausted(PROV, KEY, reason='insufficient_quota',
                              model='qwen3.5-plus')
        disp = self._dispatcher([self._slot('qwen3.5-plus')])
        assert disp._pick('text', None, None, None) is None


@pytest.mark.unit
class TestAccountVsModelQuotaScope:
    """「402 → 整 key 停（账户语义）；429+insufficient_quota → 按模型停」
    粒度规则守卫（owner 2026-07-29 裁定）。

    The 2026-07-28 per-model contract was scoped to AMBIGUOUS
    ``insufficient_quota`` 429 bodies — on an aggregating gateway one
    vendor's quota-death must not poison sibling vendors routed through
    the same key. An HTTP 402 Payment Required is a different signal
    class: it is emitted by the gateway's OWN credit-validation layer
    (sankuai body: ext.error.source=AIGC, stage=validation) about the
    ACCOUNT's credit pool, so EVERY model on that key is dead and the
    honest stop is the key-wide ``exhausted`` flag. Per-model stops there
    just make each remaining model burn one live 402 before converging
    (43 model entries on the sankuai account, 12 stopped, ~30 more live
    402s queued for real users on 2026-07-29).
    """

    def _slot(self, model, key=KEY):
        from lib.llm_dispatch.slot import Slot
        return Slot(key_name=key, api_key='sk-test', model=model,
                    capabilities={'text'}, provider_id=PROV)

    def _healthy_sibling(self, ks, monkeypatch):
        """Keep the last-resort guard out of scope: one healthy sibling."""
        monkeypatch.setattr(ks, '_list_siblings',
                            lambda pid: [PK, f'{PROV}::gwtest_key_1'])
        ks.record_outcome(PROV, 'gwtest_key_1', success=True)

    def test_402_quota_flips_keywide_stop(self, fresh_stats, monkeypatch):
        """Account-level 402: key-wide exhausted, NO per-model noise, and
        EVERY model on the key is blocked (not just the observing one)."""
        ks = fresh_stats
        self._healthy_sibling(ks, monkeypatch)
        self._slot('kimi-k3').record_error(
            is_rate_limit=True, is_quota_exhausted=True,
            is_account_quota=True,
            error='HTTP 402: 您的Credit已耗尽')
        row = ks.get_today_stats(PROV, KEY)
        assert row['exhausted'] is True, (
            'HTTP 402 (account credit pool dead) must flip the KEY-WIDE '
            'exhausted flag, not a per-model stop')
        assert row['exhausted_models'] == {}, (
            'an account-level stop must not litter per-model stops — '
            'the key-wide flag already covers every model')
        assert ks.is_key_enabled(PROV, KEY) is False
        assert ks.is_key_enabled(PROV, KEY, model='qwen3.5-plus') is False, (
            'a 402 account stop kills every model on the key, including '
            'models that never saw the 402')

    def test_429_quota_stays_per_model(self, fresh_stats, monkeypatch):
        """The 2026-07-28 contract preserved: an ambiguous vendor-quota
        signal stops ONLY the observing model (no cross-vendor poison)."""
        ks = fresh_stats
        self._healthy_sibling(ks, monkeypatch)
        self._slot('qwen3.5-plus').record_error(
            is_rate_limit=True, is_quota_exhausted=True,
            is_account_quota=False,
            error='insufficient_quota (aliyun)')
        row = ks.get_today_stats(PROV, KEY)
        assert row['exhausted'] is False
        assert set(row['exhausted_models']) == {'qwen3.5-plus'}
        assert ks.is_key_enabled(PROV, KEY) is True
        assert ks.is_key_enabled(PROV, KEY, model='kimi-k3') is True, (
            '429-quota must NOT poison sibling vendors on the same key')


@pytest.mark.unit
class TestDayRolloverRecovery:
    """「credit 按日赋予 ⇒ credit 停机次日自动恢复」机制守卫
    (owner requirement, 2026-07-29 sankuai_key_2 402 storm).

    The mechanism: billing-stops live in the DAY-SCOPED ``stats`` map, so
    ``_ensure_fresh_unlocked`` resetting stats at the calendar boundary IS
    the auto-restart — no scheduler, no cron, no manual re-enable needed.
    Manual ``overrides`` are the asymmetry: they persist by design (user
    supremacy), which is also why a manual override must never be used as
    a stopgap for credit exhaustion (it would NOT come back on its own).
    """

    DAY1 = '2026-07-29'
    DAY2 = '2026-07-30'

    def test_model_credit_stop_auto_recovers_next_day(self, fresh_stats,
                                                      monkeypatch):
        """A 402 per-model stop blocks the model TODAY and is gone
        TOMORROW — the daily credit grant finds the key dispatchable again
        with zero human action."""
        ks = fresh_stats
        monkeypatch.setattr(ks, '_today', lambda: self.DAY1)
        ks.mark_key_exhausted(PROV, KEY, reason='您的Credit已耗尽',
                              model='kimi-k3')
        assert ks.is_key_enabled(PROV, KEY, model='kimi-k3') is False

        monkeypatch.setattr(ks, '_today', lambda: self.DAY2)
        assert ks.is_key_enabled(PROV, KEY, model='kimi-k3') is True, (
            'credit stops must auto-recover at day rollover — the daily '
            'credit grant must not require a manual re-enable')
        assert ks.get_today_stats(PROV, KEY)['exhausted_models'] == {}, (
            'the stop record itself must reset with the day, not linger')

    def test_keywide_credit_stop_auto_recovers_next_day(self, fresh_stats,
                                                        monkeypatch):
        """Same contract for the key-wide ``exhausted`` flag (callers that
        cannot name a model): dead today, back tomorrow."""
        ks = fresh_stats
        monkeypatch.setattr(ks, '_today', lambda: self.DAY1)
        # Healthy sibling keeps last-resort promotion out of this test.
        monkeypatch.setattr(ks, '_list_siblings',
                            lambda pid: [PK, f'{PROV}::gwtest_key_1'])
        ks.record_outcome(PROV, 'gwtest_key_1', success=True)
        ks.mark_key_exhausted(PROV, KEY, reason='HTTP 402 credit exhausted')
        assert ks.is_key_enabled(PROV, KEY) is False

        monkeypatch.setattr(ks, '_today', lambda: self.DAY2)
        assert ks.is_key_enabled(PROV, KEY) is True, (
            'key-wide credit exhaustion must also auto-recover at rollover')

    def test_manual_override_does_not_auto_recover(self, fresh_stats,
                                                   monkeypatch):
        """The asymmetry that caused the 2026-07-29 incident: a MANUAL
        override persists across rollover BY DESIGN (user supremacy). Pin
        it so nobody "helps" by auto-clearing user state — and so a manual
        disable is never mistaken for a day-scoped credit stop."""
        ks = fresh_stats
        monkeypatch.setattr(ks, '_today', lambda: self.DAY1)
        ks.set_key_override(PROV, KEY, False)
        assert ks.is_key_enabled(PROV, KEY) is False

        monkeypatch.setattr(ks, '_today', lambda: self.DAY2)
        assert ks.is_key_enabled(PROV, KEY) is False, (
            'manual overrides persist across day rollover by design — '
            'do not auto-clear them (user supremacy)')
        assert ks.get_today_stats(PROV, KEY)['override'] is False


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
