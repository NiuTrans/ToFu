#!/usr/bin/env python3
"""tests/test_shared_contention_backoff.py — unified family backoff for
shared-project 429 contention (epic pt_1a72b708098d446f).

The incident (2026-07-28): every sankuai key terminates at the SAME
upstream Moonshot project, so the 0.5s-per-slot "rotate to the next key"
spin was futile against a project-level TPM limit — one task logged 429
retry cycle #19. The fix: a contention 429 parks the WHOLE (provider,
model) family for a jittered, escalating window (2s → 60s cap) while
other models/providers take over, and the HUD wait label tells the truth
("shared project limit", not fake 限流排队).

Pinned here:

  1. note_shared_contention cools EVERY slot of the (provider, model)
     family with reason 'contention' — and no other model's slots.
  2. The window escalates with consecutive strikes (2s → 4s → …),
     never exceeds the 60s cap, and resets after a quiet window+grace.
  3. Fallback works: with the kimi family parked, the picker lands on
     another model's slot (non-strict) instead of spinning on cooled keys.
  4. cooldown_wait_label: contention > rate-limit > backoff; contention
     rides status 0 so the reasonKey survives retry_phase_fields.
  5. The new token is registered in RETRY_REASON_KEYS and has i18n
     strings (the missing-translation tripwire must not fire in prod).
  6. Contention does NOT decay rpm_limit (external saturation teaches
     the scorer nothing about this key's capacity) — plain 429s still do.

Run:  pytest tests/test_shared_contention_backoff.py -m unit
"""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

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
class TestFamilyBackoff:

    def test_cools_whole_family_not_other_models(self):
        s1, s2, other = (_slot('kimi-k3', 'k0'), _slot('kimi-k3', 'k1'),
                         _slot('qwen3.5-plus', 'k2'))
        disp = _dispatcher([s1, s2, other])
        window = disp.note_shared_contention(s1)
        assert 1.5 <= window <= 2.5, f'first strike ≈ 2s (jittered), got {window}'
        for s in (s1, s2):
            assert s.cooldown_until > time.time() + 1.0
            assert s.cooldown_reason == 'contention'
        assert other.cooldown_until == 0.0, (
            'a contention window must never park OTHER models — that is '
            'the fallback path working')

    def test_window_escalates_and_caps(self, monkeypatch):
        # Pin jitter to 1.0 so the escalation band is deterministic —
        # comparing two jittered draws is a coin flip by construction.
        monkeypatch.setattr(
            'lib.llm_dispatch.dispatcher.random.uniform', lambda a, b: 1.0)
        s = _slot('kimi-k3', 'k0')
        disp = _dispatcher([s])
        w1 = disp.note_shared_contention(s)
        w2 = disp.note_shared_contention(s)
        assert w1 == 2.0
        assert w2 == 4.0, 'consecutive strikes double the window'
        for _ in range(20):
            w = disp.note_shared_contention(s)
        assert w == 60.0, 'the window NEVER exceeds the 60s cap'

    def test_window_is_jittered_within_band(self):
        s = _slot('kimi-k3', 'k0')
        disp = _dispatcher([s])
        seen = set()
        for _ in range(30):
            disp._contention_strikes.clear()   # force strike 1 every draw
            seen.add(round(disp.note_shared_contention(s), 3))
        assert all(1.5 <= w <= 2.5 for w in seen), (
            'first-strike window stays inside the ±25% jitter band')
        assert len(seen) > 1, 'the window is actually jittered (thundering-herd guard)'

    def test_strikes_reset_after_quiet_window(self):
        s = _slot('kimi-k3', 'k0')
        disp = _dispatcher([s])
        disp.note_shared_contention(s)
        disp.note_shared_contention(s)
        # Simulate a healed project: window + grace elapsed.
        key = (PROV, 'kimi-k3')
        strikes, until = disp._contention_strikes[key]
        disp._contention_strikes[key] = (strikes, time.time() - 31.0)
        w = disp.note_shared_contention(s)
        assert w <= 2.5, 'a quiet window+grace resets escalation to strike 1'
        assert disp._contention_strikes[key][0] == 1

    def test_cooling_cause_summary_reports_contention(self):
        s = _slot('kimi-k3', 'k0')
        disp = _dispatcher([s])
        disp.note_shared_contention(s)
        assert 'contention' in disp.cooling_cause_summary('text')

    def test_fallback_to_other_model(self):
        s1, other = _slot('kimi-k3', 'k0'), _slot('qwen3.5-plus', 'k1')
        disp = _dispatcher([s1, other])
        disp.note_shared_contention(s1)
        picked = disp._pick('text', None, None, None)
        assert picked is not None
        assert picked.model == 'qwen3.5-plus', (
            'with the contended family parked, the picker must land on a '
            'healthy OTHER model — not spin on cooled keys')


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
        import pathlib
        src = (pathlib.Path(__file__).resolve().parent.parent
               / 'static' / 'js' / 'i18n.js').read_text(encoding='utf-8')
        assert 'stream.retryReason.waitingSharedProject' in src, (
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

    def test_contention_429_parks_family_via_loop(self, monkeypatch):
        """End-to-end through dispatch_stream: one contention 429 → the
        whole (provider, model) family is parked, next pick gets the
        OTHER model."""
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
        assert s1.cooldown_reason == 'contention'
        assert s1.cooldown_until > time.time(), (
            'the family backoff must replace the 0.5s spin cycle')


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
