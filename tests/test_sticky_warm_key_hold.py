"""tests/test_sticky_warm_key_hold.py — Warm-key hold on short 429 cooldown.

Background (real incident, conv mqwqyrrd on aws.claude-opus-4.8, 2026-06-28)
===========================================================================
Anthropic's prompt cache is keyed PER API key. When ``sankuai_key_0`` hits its
per-minute RPM limit, ``Slot.record_error(is_rate_limit=True)`` cools it for a
mere 0.5s. The conversation-sticky picker then correctly routes AROUND the
cooled key onto a cold one — but that cold key's server-side cache never held
this conversation's prefix, so the whole ~100K-token body is re-billed as a
fresh ``cache_creation`` (logged as ``cache_r=0`` full re-write at a 6-13s gap,
which RULES OUT TTL expiry — the gap is far under any cache TTL).

The economically-correct move is to briefly WAIT OUT the sub-second throttle so
the next pick lands back on the WARM key, rather than dodge it onto a cold one.
``dispatch_stream`` now does this (the "warm-key hold"): flag-gated
(``TOFU_CONV_STICKY_HOLD``), budget-capped (``TOFU_CONV_STICKY_HOLD_MS``), and
limited to ONE hold per dispatch call so a longer (failure/quota) cooldown can
never stall the loop — it falls through to the normal cold-key rebind.

These tests pin that behaviour against the REAL ``LLMDispatcher`` +
``dispatch_stream`` retry loop (only the network ``stream_chat`` and the wall
clock ``time.sleep`` are stubbed):

  - a SHORT 429 cooldown on the warm key → the loop HOLDS and the request lands
    BACK on the warm key (cache stays warm); the hold INFO line fires;
  - a LONG failure cooldown on the warm key → NO hold; the request rebinds to
    the cold key and the churn INFO line fires (cache re-write, as designed);
  - the hold is OFF when ``TOFU_CONV_STICKY_HOLD=0`` (reversible kill-switch).

Run:  pytest tests/test_sticky_warm_key_hold.py -v
"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_MODEL = 'sticky-hold-model'


@pytest.fixture
def dispatcher_two_keys():
    """Real LLMDispatcher with two slots for ONE alias model on two keys.

    key_0 has the lower latency so it wins the first (seeding) pick → it becomes
    the conv's warm/sticky key. key_1 is the cold second key.
    """
    os.environ['TOFU_EPHEMERAL_PREFLIGHT'] = '0'
    os.environ['TOFU_CONV_STICKY_ROUTING'] = '1'
    os.environ['TOFU_CONV_STICKY_HOLD'] = '1'
    os.environ.pop('TOFU_CONV_STICKY_HOLD_MS', None)

    from lib.llm_dispatch import conv_affinity
    from lib.llm_dispatch.factory import get_dispatcher
    from lib.llm_dispatch.slot import Slot

    conv_affinity.clear_conv_affinity()
    conv_affinity._conv_keys.clear()

    d = get_dispatcher()
    d.initialize()
    slot0 = Slot(key_name='key_0', api_key='sk-0', model=_MODEL,
                 capabilities={'text'}, provider_id='sankuai', latency_ema=10.0)
    slot1 = Slot(key_name='key_1', api_key='sk-1', model=_MODEL,
                 capabilities={'text'}, provider_id='sankuai', latency_ema=20.0)
    with d._lock:
        d.slots.extend([slot0, slot1])

    yield d, slot0, slot1

    conv_affinity.clear_conv_affinity()
    conv_affinity._conv_keys.clear()
    with d._lock:
        for s in (slot0, slot1):
            if s in d.slots:
                d.slots.remove(s)
    for k in ('TOFU_EPHEMERAL_PREFLIGHT', 'TOFU_CONV_STICKY_ROUTING',
              'TOFU_CONV_STICKY_HOLD', 'TOFU_CONV_STICKY_HOLD_MS',
              'TOFU_CONV_STICKY_HOLD_MAX_MS'):
        os.environ.pop(k, None)


def _fake_stream_capture(keys_called):
    """Return a stream_chat stub that records which api_key served each call."""
    def _fake_stream(body, **kwargs):
        keys_called.append(kwargs.get('api_key'))
        return 'ok', 'stop', {}
    return _fake_stream


def _drive(monkeypatch, dispatcher, conv_id, keys_called, sleeps,
           clear_cooldown_on_sleep=None):
    """Bind the conv, stub the clock + network, and run one dispatch_stream.

    ``clear_cooldown_on_sleep``: an optional Slot whose ``cooldown_until`` is
    reset to 0 when the loop sleeps — this models REAL wall-clock time passing
    (the sub-second throttle window expiring) WITHOUT actually sleeping, so the
    next pick correctly sees the warm key as eligible again.
    """
    from lib.llm_dispatch import api, conv_affinity

    monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)
    monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)

    def _fake_sleep(s, *a, **k):
        sleeps.append(s)
        # Model time passing: a sleep >= the slot's remaining cooldown clears it.
        if clear_cooldown_on_sleep is not None and s > 0:
            clear_cooldown_on_sleep.cooldown_until = 0.0

    monkeypatch.setattr('lib.llm_dispatch.api.time.sleep', _fake_sleep)

    import lib.llm as llm_mod
    monkeypatch.setattr(llm_mod, 'stream_chat', _fake_stream_capture(keys_called))

    conv_affinity.set_conv_affinity(conv_id)
    try:
        return api.dispatch_stream(
            [{'role': 'user', 'content': 'hi'}],
            prefer_model=_MODEL, strict_model=True,
            log_prefix='[t]', max_retries=2)
    finally:
        conv_affinity.clear_conv_affinity()


@pytest.mark.unit
class TestWarmKeyHold:
    def test_short_cooldown_holds_for_warm_key(self, dispatcher_two_keys,
                                               monkeypatch, caplog):
        """Warm key in a SHORT 429 cooldown → loop waits, lands BACK on it."""
        import logging

        from lib.llm_dispatch import conv_affinity
        d, slot0, slot1 = dispatcher_two_keys

        # Round 1 (real-life): seed affinity to key_0 by recording it warm.
        conv_affinity.record_conv_key('conv-hold', 'key_0')
        # Now key_0 takes a per-minute 429 → the real 0.5s rate-limit cooldown.
        slot0.record_error(is_rate_limit=True, error='HTTP 429')
        assert slot0.cooldown_until > time.time()  # cooling
        assert slot0.cooldown_until - time.time() <= 1.0  # short

        keys_called: list = []
        sleeps: list = []
        with caplog.at_level(logging.INFO, logger='lib.llm_dispatch.api'):
            with caplog.at_level(logging.INFO, logger='lib.llm_dispatch.dispatcher'):
                msg, finish, _usage = _drive(
                    monkeypatch, d, 'conv-hold', keys_called, sleeps,
                    clear_cooldown_on_sleep=slot0)

        assert msg == 'ok'
        # ★ The request must have landed on the WARM key (key_0 = sk-0), NOT
        #   the cold key_1 — proving the hold worked and the prompt cache stays
        #   warm. (record_error stubs the clock-sleep, so the cooldown does not
        #   actually elapse; the dispatcher re-reads cooldown_until each pick,
        #   so we clear it here to model the sub-second window passing.)
        # The hold fired (a sleep ~ the cooldown remaining, under budget).
        assert any(0 < s <= 1.5 for s in sleeps), (
            'expected a warm-key hold sleep, got sleeps=%r' % sleeps)
        # ★ Observable signal: the new INFO hold line fired.
        assert any('holding' in r.message and 'warm key key_0' in r.message
                   for r in caplog.records), (
            'expected warm-key hold INFO log, got: %r'
            % [r.message for r in caplog.records])
        # ★ And the served key is key_0 (the warm one), not key_1.
        assert keys_called == ['sk-0'], (
            'expected the request to land on the warm key sk-0, got %r'
            % keys_called)

    def test_long_cooldown_does_not_hold_and_rebinds(self, dispatcher_two_keys,
                                                     monkeypatch, caplog):
        """Warm key in a LONG failure cooldown → NO hold; rebind to cold key."""
        import logging

        from lib.llm_dispatch import conv_affinity
        d, slot0, slot1 = dispatcher_two_keys

        conv_affinity.record_conv_key('conv-long', 'key_0')
        # A long cooldown (e.g. consecutive-error backoff / quota) — 300s.
        slot0.cooldown_until = time.time() + 300.0

        keys_called: list = []
        sleeps: list = []
        with caplog.at_level(logging.INFO, logger='lib.llm_dispatch.api'):
            with caplog.at_level(logging.INFO, logger='lib.llm_dispatch.dispatcher'):
                msg, finish, _usage = _drive(
                    monkeypatch, d, 'conv-long', keys_called, sleeps)

        assert msg == 'ok'
        # ★ No warm-key hold sleep (300s >> 1.5s budget).
        assert not any(0 < s <= 1.5 for s in sleeps), (
            'must NOT hold for a long cooldown, got sleeps=%r' % sleeps)
        # ★ Rebound to the cold key key_1 (sk-1) — the only eligible slot.
        assert keys_called == ['sk-1'], (
            'expected rebind to cold key sk-1, got %r' % keys_called)
        # ★ Observable signal: the churn rebind INFO line fired.
        assert any('sticky key key_0 unavailable' in r.message
                   and 'rebinding to key_1' in r.message
                   for r in caplog.records), (
            'expected churn-rebind INFO log, got: %r'
            % [r.message for r in caplog.records])

    def test_hold_disabled_by_flag_rebinds(self, dispatcher_two_keys,
                                           monkeypatch):
        """TOFU_CONV_STICKY_HOLD=0 → no hold even on a short cooldown."""
        from lib.llm_dispatch import conv_affinity
        d, slot0, slot1 = dispatcher_two_keys

        os.environ['TOFU_CONV_STICKY_HOLD'] = '0'
        conv_affinity.record_conv_key('conv-off', 'key_0')
        slot0.record_error(is_rate_limit=True, error='HTTP 429')

        keys_called: list = []
        sleeps: list = []
        msg, finish, _usage = _drive(
            monkeypatch, d, 'conv-off', keys_called, sleeps)

        assert msg == 'ok'
        # Feature off → no warm-key hold; the picker rebinds to the cold key.
        assert not any(0 < s <= 1.5 for s in sleeps)
        assert keys_called == ['sk-1']

    def test_two_consecutive_rounds_stay_on_warm_key(self, dispatcher_two_keys,
                                                     monkeypatch):
        """The granularity the bug report ('reconstructed twice consecutively')
        actually lives at: TWO sequential dispatch_stream calls on the SAME conv,
        key_0 throttled on the first. The conv must land on key_0 BOTH times.

        Round 1: key_0 is in a short 429 cooldown → the warm-key hold waits it
        out and serves key_0 (seeding/refreshing affinity to key_0). Round 2: a
        FRESH dispatch_stream call (new _StreamRetryState) re-reads the
        process-global affinity map and must AGAIN prefer key_0 — proving the
        pin holds round-over-round, not just within a single call.
        """
        from lib.llm_dispatch import api, conv_affinity
        d, slot0, slot1 = dispatcher_two_keys

        monkeypatch.setattr(api, 'get_dispatcher', lambda: d)
        monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)

        keys_called: list = []

        import lib.llm as llm_mod
        monkeypatch.setattr(llm_mod, 'stream_chat',
                            _fake_stream_capture(keys_called))

        def _sleep_clears_slot0(s, *a, **k):
            # Model the sub-second throttle window expiring during the hold.
            if s > 0:
                slot0.cooldown_until = 0.0
        monkeypatch.setattr('lib.llm_dispatch.api.time.sleep', _sleep_clears_slot0)

        # The conv's prompt cache is already WARM on key_0 from an earlier
        # successful round (this is the precondition the bug report lives at —
        # not the very first round, which legitimately has no affinity yet).
        conv_affinity.record_conv_key('conv-multi', 'key_0')

        # Bind the conversation for the whole turn (run_task does this once per
        # task; both rounds run on the same worker thread).
        conv_affinity.set_conv_affinity('conv-multi')
        try:
            # ── Round 1: key_0 freshly 429'd (short cooldown). ──
            slot0.record_error(is_rate_limit=True, error='HTTP 429')
            assert slot0.cooldown_until > time.time()
            api.dispatch_stream(
                [{'role': 'user', 'content': 'r1'}],
                prefer_model=_MODEL, strict_model=True,
                log_prefix='[r1]', max_retries=2)

            # ── Round 2: a fresh dispatch call, no new error. ──
            api.dispatch_stream(
                [{'role': 'user', 'content': 'r2'}],
                prefer_model=_MODEL, strict_model=True,
                log_prefix='[r2]', max_retries=2)
        finally:
            conv_affinity.clear_conv_affinity()

        # ★ BOTH rounds served the warm key key_0 (sk-0) — the conversation did
        #   NOT reconstruct its prompt cache on a cold key on either round.
        assert keys_called == ['sk-0', 'sk-0'], (
            'expected both rounds on the warm key sk-0, got %r' % keys_called)
        # And the affinity map still points at key_0 after the turn.
        assert conv_affinity.get_preferred_key('conv-multi') == 'key_0'


    def test_escalated_hold_covers_contention_beyond_budget(
            self, dispatcher_two_keys, monkeypatch, caplog):
        """A concurrent sibling cools the conv's SOLE warm key for LONGER than
        the flat 1.5s budget but WITHIN the escalated ceiling → the hold must
        still wait it out and land BACK on the warm key (not cold-rebind).

        This is the mrne3bqe R4 >budget-contention gap: pre-fix the loop gave
        up at 1.5s and destroyed the prefix on a byte-identical round.
        """
        import logging

        from lib.llm_dispatch import conv_affinity
        d, slot0, slot1 = dispatcher_two_keys

        # Escalated ceiling well above the contention cooldown we set.
        os.environ['TOFU_CONV_STICKY_HOLD_MS'] = '1500'      # flat budget
        os.environ['TOFU_CONV_STICKY_HOLD_MAX_MS'] = '8000'  # escalated ceiling

        conv_affinity.record_conv_key('conv-esc', 'key_0')
        # 4s cooldown — ABOVE the 1.5s flat budget, BELOW the 8s ceiling.
        slot0.cooldown_until = time.time() + 4.0

        keys_called: list = []
        sleeps: list = []
        with caplog.at_level(logging.INFO, logger='lib.llm_dispatch.api'):
            msg, finish, _usage = _drive(
                monkeypatch, d, 'conv-esc', keys_called, sleeps,
                clear_cooldown_on_sleep=slot0)

        assert msg == 'ok'
        # Held for ~4s (between budget and ceiling) — the escalated window.
        assert any(1.5 < s <= 8.0 for s in sleeps), (
            'expected an ESCALATED warm-key hold (>budget, <=ceiling), '
            'got sleeps=%r' % sleeps)
        # Landed back on the WARM key, not the cold one.
        assert keys_called == ['sk-0'], (
            'escalated hold must land on warm sk-0, got %r' % keys_called)
        assert any('escalated hold' in r.message and 'warm key key_0' in r.message
                   for r in caplog.records), (
            'expected the escalated-hold INFO line, got: %r'
            % [r.message for r in caplog.records])

    def test_neuter_ceiling_equals_budget_rebinds_cold(
            self, dispatcher_two_keys, monkeypatch):
        """NEUTER negative control: set the escalated ceiling == flat budget so
        escalation is disabled. The same >budget (4s) contention cooldown must
        now FAIL to hold and cold-rebind — proving the escalated ceiling (not
        some other path) is what closes the gap in the test above."""
        from lib.llm_dispatch import conv_affinity
        d, slot0, slot1 = dispatcher_two_keys

        os.environ['TOFU_CONV_STICKY_HOLD_MS'] = '1500'
        os.environ['TOFU_CONV_STICKY_HOLD_MAX_MS'] = '1500'  # == budget → no escalation

        conv_affinity.record_conv_key('conv-neuter', 'key_0')
        slot0.cooldown_until = time.time() + 4.0  # > 1.5s budget/ceiling

        keys_called: list = []
        sleeps: list = []
        msg, finish, _usage = _drive(
            monkeypatch, d, 'conv-neuter', keys_called, sleeps,
            clear_cooldown_on_sleep=slot0)

        assert msg == 'ok'
        # No hold (4s > 1.5s ceiling) → cold rebind, exactly the pre-fix bug.
        assert not any(0 < s <= 8.0 for s in sleeps), (
            'neuter (ceiling==budget) must NOT hold a 4s cooldown, got %r' % sleeps)
        assert keys_called == ['sk-1'], (
            'neuter must cold-rebind to sk-1, got %r' % keys_called)

    def test_escalated_hold_still_fails_over_on_long_backoff(
            self, dispatcher_two_keys, monkeypatch):
        """Even with escalation on, a genuinely LONG backoff (300s >> 8s
        ceiling) must NOT hold — the task fails over to the cold key. The
        escalated hold widens the window; it is NOT a hard pin."""
        from lib.llm_dispatch import conv_affinity
        d, slot0, slot1 = dispatcher_two_keys

        os.environ['TOFU_CONV_STICKY_HOLD_MS'] = '1500'
        os.environ['TOFU_CONV_STICKY_HOLD_MAX_MS'] = '8000'

        conv_affinity.record_conv_key('conv-long-esc', 'key_0')
        slot0.cooldown_until = time.time() + 300.0  # genuine failure backoff

        keys_called: list = []
        sleeps: list = []
        msg, finish, _usage = _drive(
            monkeypatch, d, 'conv-long-esc', keys_called, sleeps)

        assert msg == 'ok'
        assert not any(0 < s <= 8.0 for s in sleeps), (
            'must NOT hold a 300s backoff even with escalation, got %r' % sleeps)
        assert keys_called == ['sk-1'], (
            'long backoff must fail over to cold sk-1, got %r' % keys_called)


@pytest.mark.unit
class TestRpmLimitRecovery:
    """The residual root cause: rpm_limit only ever DECAYED on 429 with no way
    back up, so a key that took transient throttling stayed permanently
    deprioritized (its score() RPM penalty never relaxed) and the warm key the
    hold protects became chronically cold-preferred. record_success now recovers
    rpm_limit toward a seeded ceiling.
    """

    def _slot(self, rpm=60.0):
        from lib.llm_dispatch.slot import Slot
        return Slot(key_name='k', api_key='sk', model='m',
                    capabilities={'text'}, rpm_limit=rpm)

    def test_429_decays_then_success_recovers(self):
        s = self._slot(rpm=60.0)
        assert s.rpm_limit_max == 60.0  # ceiling seeded from constructor

        # Several 429s ratchet the limit down (the decay that had no recovery).
        for _ in range(5):
            s.record_error(is_rate_limit=True, error='HTTP 429')
        decayed = s.rpm_limit
        assert decayed < 60.0
        assert decayed >= 5.0  # floored at 5

        # Sustained success recovers it multiplicatively, never above ceiling.
        prev = decayed
        for _ in range(50):
            s.record_success(latency_ms=100.0)
            assert s.rpm_limit >= prev          # monotonic up
            assert s.rpm_limit <= s.rpm_limit_max  # capped at ceiling
            prev = s.rpm_limit
        # With enough successes it climbs all the way back to the ceiling.
        assert abs(s.rpm_limit - 60.0) < 1e-6

    def test_recovery_capped_at_ceiling_never_overshoots(self):
        s = self._slot(rpm=30.0)
        # Already at ceiling → success must NOT push rpm_limit above it.
        for _ in range(20):
            s.record_success(latency_ms=50.0)
        assert s.rpm_limit == 30.0

    def test_recovery_gentler_than_decay(self):
        # One 429 (×0.8) then one success (×1.1) must NOT fully recover —
        # otherwise a chronically-throttled key ratchets back to full and
        # immediately overloads again.
        s = self._slot(rpm=100.0)
        s.record_error(is_rate_limit=True, error='HTTP 429')  # 100 → 80
        assert abs(s.rpm_limit - 80.0) < 1e-6
        s.record_success(latency_ms=100.0)                    # 80 → 88, not 100
        assert s.rpm_limit < 100.0
        assert abs(s.rpm_limit - 88.0) < 1e-6

    def test_set_rpm_ceiling_reseeds_both(self):
        # A benchmarked-DOWN value must lower the ceiling too, so recovery
        # cannot climb back toward a stale higher limit.
        s = self._slot(rpm=60.0)
        for _ in range(3):
            s.record_error(is_rate_limit=True, error='HTTP 429')
        s.set_rpm_ceiling(20.0)  # dispatcher reseed from benchmark
        assert s.rpm_limit == 20.0
        assert s.rpm_limit_max == 20.0
        for _ in range(50):
            s.record_success(latency_ms=100.0)
        assert s.rpm_limit <= 20.0  # never climbs above the new ceiling


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
