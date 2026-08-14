#!/usr/bin/env python3
"""tests/test_shared_contention_backoff.py — shared-project 429 contention:
IMMEDIATE RETRY, no family backoff (owner directive 2026-08-03).

History: 2026-07-28 (pt_1a72b708098d446f) introduced a jittered, escalating
family parking (2s → doubling → 60s cap) for contention 429s, on the theory
that rotating our own keys is futile when the whole upstream project is
saturated by other tenants. 2026-08-03 the owner reversed the policy: a 429
gets NO backoff — the loop retries immediately and grabs the rate-limit
window the instant it resets (the bounded saturation escalation default was
retired the same day). ``note_shared_contention`` is now PURE TELEMETRY:
a per-(provider, model) streak counter plus a throttled log line.

Pinned here:

  1. note_shared_contention NEVER cools any slot and always returns 0.0 —
     repeated strikes can never resurrect the retired 2s→60s escalation
     (NEUTER: re-adding parking flips these red).
  2. The streak counter accumulates within the grace window and resets
     after a quiet window (30s).
  3. Log throttle: strikes 1-3 + every 100th at INFO, the rest DEBUG —
     a sustained storm costs ~1 line/30s, not ~3 lines/s.
  4. End-to-end through dispatch_stream: a contention 429 leaves the
     family unparked (the only cooldown is the slot's own 0.5s
     'rate_limit' steering) and the loop retries immediately.
  5. The per-cycle 429 loop log is DEBUG after the first 3 cycles
     (log-bloat guard for the immediate-retry era).
  6. Wait-label precedence + rpm-decay contracts UNCHANGED (the label
     function and the slot accounting are isolation-tested as before).

Run:  pytest tests/test_shared_contention_backoff.py -m unit
"""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

from tests._runtime_sections import ROOT as FRONTEND_ROOT

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

PROV = 'gw'


def _slot(model, key):
    from lib.llm_dispatch.slot import Slot
    return Slot(key_name=key, api_key='sk-test', model=model,
                capabilities={'text'}, provider_id=PROV)


def _dispatcher(slots):
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    disp = object.__new__(LLMDispatcher)
    disp._lock = threading.Lock()
    disp.slots = list(slots)
    disp.initialize = lambda: None
    disp._contention_strikes = {}
    return disp


@pytest.mark.unit
class TestNoFamilyBackoff:

    def test_no_parking_and_zero_window(self):
        s1, s2, other = (_slot('kimi-k3', 'k0'), _slot('kimi-k3', 'k1'),
                         _slot('qwen3.5-plus', 'k2'))
        disp = _dispatcher([s1, s2, other])
        window = disp.note_shared_contention(s1)
        assert window == 0.0, 'owner directive 2026-08-03: NO backoff'
        for s in (s1, s2, other):
            assert s.cooldown_until == 0.0, (
                'a contention note must never park ANY slot — immediate '
                'retry is the policy')
            assert s.cooldown_reason == ''

    def test_repeated_strikes_never_escalate(self):
        """NEUTER pin: the retired 2s→60s doubling cannot sneak back —
        25 consecutive strikes must park nothing, ever."""
        s = _slot('kimi-k3', 'k0')
        disp = _dispatcher([s])
        for _ in range(25):
            assert disp.note_shared_contention(s) == 0.0
            assert s.cooldown_until == 0.0
            assert s.cooldown_reason == ''
        assert disp._contention_strikes[(PROV, 'kimi-k3')][0] == 25

    def test_strikes_reset_after_quiet_window(self):
        s = _slot('kimi-k3', 'k0')
        disp = _dispatcher([s])
        for _ in range(3):
            disp.note_shared_contention(s)
        key = (PROV, 'kimi-k3')
        assert disp._contention_strikes[key][0] == 3
        # Simulate a healed project: quiet window + grace elapsed.
        disp._contention_strikes[key] = (3, time.time() - 31.0)
        disp.note_shared_contention(s)
        assert disp._contention_strikes[key][0] == 1

    def test_log_is_throttled(self, monkeypatch):
        """First 3 strikes + every 100th at INFO; the other 96 DEBUG."""
        s = _slot('kimi-k3', 'k0')
        disp = _dispatcher([s])
        infos, debugs = [], []
        monkeypatch.setattr('lib.llm_dispatch.dispatcher.logger.info',
                            lambda *a, **k: infos.append(a))
        monkeypatch.setattr('lib.llm_dispatch.dispatcher.logger.debug',
                            lambda *a, **k: debugs.append(a))
        for _ in range(100):
            disp.note_shared_contention(s)
        assert len(infos) == 4, (
            f'strikes 1-3 + strike 100 at INFO, got {len(infos)}')
        assert len(debugs) == 96

    def test_cooling_summary_has_no_contention_cause(self):
        """Nothing parks → the wait-label summary never sees 'contention'."""
        s = _slot('kimi-k3', 'k0')
        disp = _dispatcher([s])
        disp.note_shared_contention(s)
        assert 'contention' not in disp.cooling_cause_summary('text')

    def test_picker_not_steered_away_from_family(self):
        """Immediate retry: the picker keeps landing on the contended
        family — no parking means no fallback steering."""
        s1, other = _slot('kimi-k3', 'k0'), _slot('qwen3.5-plus', 'k1')
        other.latency_ema = 99999.0  # kimi wins on score deterministically
        disp = _dispatcher([s1, other])
        disp.note_shared_contention(s1)
        picked = disp._pick('text', None, None, None)
        assert picked is not None
        assert picked.model == 'kimi-k3', (
            'no family parking → the picker must NOT be steered away from '
            'the contended model')


@pytest.mark.unit
class TestWaitLabel:

    def _label(self, causes):
        from lib.llm_dispatch.retry_i18n import cooldown_wait_label
        return cooldown_wait_label(causes)

    def test_contention_wins(self):
        reason, status = self._label({'contention'})
        assert reason == 'Waiting for model (shared project limit)'
        assert status == 0, (
            'status 429 would swallow the token into the generic '
            'rate-limited detailKey — contention must ride the reason branch')

    def test_contention_beats_mixed_causes(self):
        reason, _ = self._label({'contention', 'error'})
        assert reason == 'Waiting for model (shared project limit)'

    def test_legacy_labels_unchanged(self):
        assert self._label({'rate_limit'}) == (
            'Waiting for model (rate-limited)', 429)
        assert self._label({'error'}) == (
            'Waiting for model (retry backoff)', 0)
        assert self._label(set()) == (
            'Waiting for model (rate-limited)', 429)

    def test_token_registered_with_i18n(self):
        from lib.llm_dispatch.retry_i18n import RETRY_REASON_KEYS
        key = RETRY_REASON_KEYS.get('Waiting for model (shared project limit)')
        assert key == 'stream.retryReason.waitingSharedProject'
        import json
        locales = FRONTEND_ROOT / 'frontend' / 'src' / 'i18n' / 'locales'
        packs = [json.loads((locales / f'{lang}.json').read_text(encoding='utf-8'))
                 for lang in ('en', 'zh')]
        assert all('stream.retryReason.waitingSharedProject' in pack
                   for pack in packs), (
            'missing i18n strings — the missing-translation tripwire would '
            'fire in production')

    def test_reasonkey_survives_phase_fields(self):
        from lib.llm_dispatch.retry_i18n import retry_phase_fields
        f = retry_phase_fields(
            model='kimi-k3', attempt=1,
            reason='Waiting for model (shared project limit)', status_code=0)
        assert f['detailKey'] == 'stream.phase.retryReason'
        assert f['detailArgs']['reasonKey'] == \
            'stream.retryReason.waitingSharedProject'


@pytest.mark.unit
class TestRpmLimitNotDecayed:

    def test_contention_skips_rpm_decay(self):
        s = _slot('kimi-k3', 'k0')
        before = s.rpm_limit
        s.record_request()
        s.record_error(is_rate_limit=True, is_shared_contention=True)
        assert s.rpm_limit == before, (
            'external saturation teaches the scorer nothing about this '
            "key's capacity — decaying rpm_limit is a false lesson")

    def test_plain_429_still_decays(self):
        s = _slot('kimi-k3', 'k0')
        before = s.rpm_limit
        s.record_request()
        s.record_error(is_rate_limit=True)
        assert s.rpm_limit < before, (
            'complement: a genuine per-key 429 still decays the estimate')


@pytest.mark.unit
class TestDispatchIntegration:

    def test_contention_429_retries_immediately_without_parking(
            self, monkeypatch):
        """End-to-end through dispatch_stream: one contention 429 → NO
        family parking (only the slot's own 0.5s 'rate_limit' steering) →
        the loop retries immediately and succeeds."""
        from lib.llm_dispatch import api
        from lib.llm_errors import RateLimitError
        from tests.test_vendor_transient_dispatch import _FakeDispatcher

        s1, other = _slot('kimi-k3', 'k0'), _slot('qwen3.5-plus', 'k1')
        disp = _FakeDispatcher([s1, other])
        # The fake hands out queued slots; give it a real registry so the
        # loop's note_shared_contention call has somewhere to land.
        real = _dispatcher([s1, other])
        disp.note_shared_contention = real.note_shared_contention
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
        monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)
        monkeypatch.setattr('lib.llm_dispatch.api.time.sleep',
                            lambda *_a, **_k: None)
        monkeypatch.setattr('lib.key_stats.record_outcome',
                            lambda *a, **k: None)
        monkeypatch.setattr('lib.key_stats.record_rate_limit',
                            lambda *a, **k: False)

        calls = {'n': 0}

        def _fake_stream(body, **kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RateLimitError(
                    'API HTTP 429: reached project TPM rate limit',
                    status_code=429, is_shared_contention=True)
            return 'ok', 'stop', {}

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream)

        msg, finish, usage = api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}], log_prefix='[t]')

        assert msg == 'ok'
        assert s1.cooldown_reason != 'contention', (
            'immediate-retry policy: contention must NOT park the family')
        assert s1.cooldown_until <= time.time() + 0.6, (
            'the only cooldown left is the 0.5s per-slot steering from '
            'record_error — never a family window')

    def test_per_cycle_429_log_throttled_after_first_three(
            self, monkeypatch):
        """Log-bloat guard for the immediate-retry era: cycles 1-3 log at
        INFO, cycles 4+ at DEBUG (every 100th still surfaces at INFO)."""
        from lib.llm_dispatch import api
        from lib.llm_errors import RateLimitError
        from tests.test_429_saturation_escalation import (
            _FakeClock, _FakeDispatcher, _FakeSlot)

        slot = _FakeSlot()
        clock = _FakeClock()
        disp = _FakeDispatcher(slot)
        monkeypatch.setattr(api, 'get_dispatcher', lambda: disp)
        monkeypatch.setattr(api, 'time', clock)
        monkeypatch.setenv('TOFU_429_SATURATION_SECS', '0')

        calls = {'n': 0}

        def _chat(*a, **kw):
            calls['n'] += 1
            if calls['n'] <= 6:
                raise RateLimitError('slow down', status_code=429)
            return ('ok-text', {'completion_tokens': 2})

        monkeypatch.setattr('lib.llm.chat', _chat)
        infos, debugs = [], []
        monkeypatch.setattr(api.logger, 'info',
                            lambda *a, **k: infos.append(a))
        monkeypatch.setattr(api.logger, 'debug',
                            lambda *a, **k: debugs.append(a))

        content, usage = api.dispatch_chat(
            [{'role': 'user', 'content': 'hi'}],
            prefer_model='m1', strict_model=True, log_prefix='[T]')

        assert content == 'ok-text'
        rate_infos = [a for a in infos if '429 rate-limited on' in str(a[0])]
        rate_debugs = [a for a in debugs if '429 rate-limited on' in str(a[0])]
        assert len(rate_infos) == 3, (
            f'cycles 1-3 at INFO, got {len(rate_infos)}')
        assert len(rate_debugs) == 3, (
            f'cycles 4-6 at DEBUG, got {len(rate_debugs)}')


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
