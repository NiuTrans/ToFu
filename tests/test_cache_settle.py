"""Tests for lib/llm_dispatch/cache_settle.py — cache write-visibility settle gate.

Guards the dominant floor-miss fix (Anthropic SDK #1451 write-visibility race):
a big request on a conversation whose PRIOR big round's stream ended <window ago
briefly waits so the prior cache WRITE is visible before this round reads the
prefix back. Pure timing logic — no DB, no network, no real sleep in the hot
assertions (we inject ``now`` and stub the sleeper).

Invariants under test:
  1. Rapid same-conv second request waits the REMAINDER of the window.
  2. Enough-elapsed second request waits ZERO (write already settled).
  3. FIRST request of a conv (no prior stream end) never waits.
  4. Sub-threshold (small) prefixes never wait.
  5. Different conversations don't gate each other.
  6. Wait is hard-capped by settle_max_wait_ms.
  7. Abort-aware: a tripped abort_check breaks the wait early.
  8. Env-off (TOFU_CACHE_SETTLE=0) → no wait, no recording.
  9. Adaptive: a long prior round (elapsed > window) waits zero.
 10. NEUTER: without recording the prior stream end, the race is NOT mitigated
     (the second rapid request would send immediately — proving record/measure
     is load-bearing).
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.unit

from lib.llm_dispatch import cache_settle as cs
from lib.token_counter.evidence import ADMITTED_INPUT_TOKENS_KEY


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Force a known env: gate ON, window 1500ms, cap 4000ms. Threshold is left
    UNSET so tests exercise the real default (30k) — the whole point of the
    fix is that the default fires on the observed ~120k miss class, which the
    old 150k-inherited bar skipped. Reset the recency map before AND after."""
    monkeypatch.setenv('TOFU_CACHE_SETTLE', '1')
    monkeypatch.setenv('TOFU_CACHE_SETTLE_MS', '1500')
    monkeypatch.setenv('TOFU_CACHE_SETTLE_MAX_MS', '4000')
    monkeypatch.delenv('TOFU_CACHE_SETTLE_THRESHOLD_TOKENS', raising=False)
    monkeypatch.delenv('TOFU_CACHE_SETTLE_WARM_WRITE_TOKENS', raising=False)
    cs._reset_settle_for_tests()
    yield
    cs._reset_settle_for_tests()


@pytest.fixture
def spy_sleep(monkeypatch):
    """Replace abortable_sleep so no test actually blocks; capture the wait."""
    calls = []

    def _fake(seconds, abort_check=None, interval=0.5):
        calls.append(seconds)
        # Honor abort semantics for the abort test: if abort fires, raise like
        # the real abortable_sleep would (it raises AbortedError). We emulate a
        # generic exception the caller does not catch, matching "break early".
        if abort_check and abort_check():
            raise _FakeAborted()

    monkeypatch.setattr('lib.llm._transport.abortable_sleep', _fake)
    return calls


class _FakeAborted(Exception):
    pass


# ── Prefix-size classification ───────────────────────────────────────────────

def test_estimate_counts_string_content():
    messages = [{'role': 'user', 'content': 'x' * 4000}]
    assert cs.estimate_prefix_tokens(messages) == 1000


def test_estimate_counts_block_text_and_thinking():
    messages = [{'role': 'assistant', 'content': [
        {'type': 'text', 'text': 'a' * 400},
        {'type': 'thinking', 'thinking': 'b' * 400},
    ]}]
    assert cs.estimate_prefix_tokens(messages) == 200


def test_estimate_counts_image_base64_payload():
    messages = [{'role': 'user', 'content': [
        {'type': 'image', 'source': {
            'type': 'base64', 'data': 'Z' * 4000}},
    ]}]
    assert cs.estimate_prefix_tokens(messages) == 1000


def test_estimate_accepts_body_dict_with_tools():
    body = {
        'messages': [{'role': 'user', 'content': 'hi'}],
        'tools': [{'name': 'x', 'description': 'd' * 400}],
    }
    assert cs.estimate_prefix_tokens(body) > 90


def test_estimate_reuses_admitted_prompt_tokens_without_traversal():
    class MustNotIterate(list):
        def __iter__(self):
            raise AssertionError('admitted prompt must not be rescanned')

    body = {
        ADMITTED_INPUT_TOKENS_KEY: 123_456,
        'messages': MustNotIterate([{'content': 'unreachable'}]),
        'tools': MustNotIterate([{'name': 'unreachable'}]),
    }

    assert cs.estimate_prefix_tokens(body) == 123_456


@pytest.mark.parametrize('invalid', [None, 0, -1, True, 1.5, '1000'])
def test_invalid_admitted_prompt_tokens_retain_estimator(invalid):
    body = {
        ADMITTED_INPUT_TOKENS_KEY: invalid,
        'messages': [{'role': 'user', 'content': 'x' * 4000}],
    }

    assert cs.estimate_prefix_tokens(body) == 1000


def test_estimate_malformed_returns_zero():
    assert cs.estimate_prefix_tokens(None) == 0
    assert cs.estimate_prefix_tokens(42) == 0


BIG = 200_000        # comfortably > default 30k threshold
SMALL = 5_000        # < 30k threshold → a trivial turn, never gated
OBSERVED_MISS = 120_000  # the real floor-miss size from production logs
                         # (cache 79.2k + write 40.9k) — MUST be gated now,
                         # yet sat BELOW the old 150k-inherited bar.


def test_rapid_second_request_waits_remainder(spy_sleep):
    """Invariant 1: prior stream ended 0.3s ago → wait ~1.2s (1.5 - 0.3)."""
    cs.record_stream_end('convA', now=1000.0)
    waited = cs.settle_before_send('convA', BIG, now=1000.3)
    assert 1.19 <= waited <= 1.21, f'expected ~1.2s remainder, got {waited}'
    assert len(spy_sleep) == 1 and abs(spy_sleep[0] - waited) < 1e-6


def test_generic_unmetered_warm_tail_is_bounded():
    assert cs.generic_cache_write_pending({
        'prompt_tokens': 100_000,
        'prompt_tokens_details': {'cached_tokens': 95_905},
    }) is False
    assert cs.generic_cache_write_pending({
        'prompt_tokens': 100_000,
        'prompt_tokens_details': {'cached_tokens': 95_904},
    }) is True
    assert cs.generic_cache_write_pending({
        'prompt_tokens': 100_000,
        'prompt_tokens_details': {'cached_tokens': 0},
    }) is True


def test_generic_metered_write_signal_is_authoritative():
    assert cs.generic_cache_write_pending({
        'input_tokens': 123,
        'cache_creation_input_tokens': 50_000,
        'cache_read_input_tokens': 0,
    }) is True
    assert cs.generic_cache_write_pending({
        'input_tokens': 123,
        'cache_creation_input_tokens': 0,
        'cache_read_input_tokens': 50_000,
    }) is False


@pytest.mark.parametrize('value', [None, False, -1, 'invalid'])
def test_malformed_metered_write_signal_remains_conservative(value):
    assert cs.generic_cache_write_pending({
        'input_tokens': 123,
        'cache_creation_input_tokens': value,
        'cache_read_input_tokens': 50_000,
    }) is True


@pytest.mark.parametrize('value', ['invalid', '0', '-1'])
def test_invalid_generic_warm_threshold_falls_back_safely(monkeypatch, value):
    monkeypatch.setenv('TOFU_CACHE_SETTLE_WARM_WRITE_TOKENS', value)
    assert cs.generic_cache_write_pending({
        'prompt_tokens': 100_000,
        'prompt_tokens_details': {'cached_tokens': 95_905},
    }) is False
    assert cs.generic_cache_write_pending({
        'prompt_tokens': 100_000,
        'prompt_tokens_details': {'cached_tokens': 95_904},
    }) is True


def test_explicit_no_pending_write_clears_generic_clock(spy_sleep):
    cs.record_stream_end('convA', now=1000.0, pending_write=True)
    cs.record_stream_end('convA', now=1000.1, pending_write=False)
    assert cs.settle_before_send('convA', BIG, now=1000.2) == 0.0
    assert spy_sleep == []


def test_enough_elapsed_waits_zero(spy_sleep):
    """Invariant 2: prior stream ended 2s ago (> 1.5s window) → no wait."""
    cs.record_stream_end('convA', now=1000.0)
    waited = cs.settle_before_send('convA', BIG, now=1002.0)
    assert waited == 0.0
    assert spy_sleep == []


def test_first_request_never_waits(spy_sleep):
    """Invariant 3: no prior stream end recorded → the turn's first request
    sends immediately (perceived TTFT unaffected)."""
    waited = cs.settle_before_send('convFRESH', BIG, now=1000.0)
    assert waited == 0.0
    assert spy_sleep == []


def test_small_prefix_never_waits(spy_sleep):
    """Invariant 4: sub-threshold prefix never eats latency, even right after
    a prior stream end."""
    cs.record_stream_end('convA', now=1000.0)
    waited = cs.settle_before_send('convA', SMALL, now=1000.1)
    assert waited == 0.0
    assert spy_sleep == []


def test_different_convs_do_not_gate_each_other(spy_sleep):
    """Invariant 5: convB's send is not delayed by convA's recent stream end."""
    cs.record_stream_end('convA', now=1000.0)
    waited = cs.settle_before_send('convB', BIG, now=1000.1)
    assert waited == 0.0
    assert spy_sleep == []


def test_wait_hard_capped(spy_sleep, monkeypatch):
    """Invariant 6: a huge window is clamped by settle_max_wait_ms.

    Window 60s, cap 4s → an immediate second request waits at most 4s, never 60."""
    monkeypatch.setenv('TOFU_CACHE_SETTLE_MS', '60000')
    monkeypatch.setenv('TOFU_CACHE_SETTLE_MAX_MS', '4000')
    cs.record_stream_end('convA', now=1000.0)
    waited = cs.settle_before_send('convA', BIG, now=1000.0)
    assert waited == 4.0, f'expected 4.0s cap, got {waited}'


def test_abort_breaks_wait_early(monkeypatch):
    """Invariant 7: a tripped abort_check propagates out of the wait (the real
    abortable_sleep raises AbortedError; our fake raises to model the break)."""
    calls = []

    def _fake(seconds, abort_check=None, interval=0.5):
        calls.append(seconds)
        if abort_check and abort_check():
            raise _FakeAborted()

    monkeypatch.setattr('lib.llm._transport.abortable_sleep', _fake)
    cs.record_stream_end('convA', now=1000.0)
    with pytest.raises(_FakeAborted):
        cs.settle_before_send('convA', BIG, abort_check=lambda: True, now=1000.1)
    assert calls, 'abortable_sleep should have been entered'


def test_env_off_no_wait_no_record(spy_sleep, monkeypatch):
    """Invariant 8: TOFU_CACHE_SETTLE=0 → record is a no-op AND send never waits."""
    monkeypatch.setenv('TOFU_CACHE_SETTLE', '0')
    cs.record_stream_end('convA', now=1000.0)      # should be a no-op
    waited = cs.settle_before_send('convA', BIG, now=1000.1)
    assert waited == 0.0
    assert spy_sleep == []
    # And nothing was recorded even after re-enabling.
    monkeypatch.setenv('TOFU_CACHE_SETTLE', '1')
    assert cs.settle_before_send('convA', BIG, now=1000.1) == 0.0


def test_empty_conv_id_is_noop(spy_sleep):
    """Headless / no-identity path: empty conv_id never waits or records."""
    cs.record_stream_end('', now=1000.0)
    waited = cs.settle_before_send('', BIG, now=1000.1)
    assert waited == 0.0
    assert spy_sleep == []


def test_adaptive_long_prior_round_waits_zero(spy_sleep):
    """Invariant 9: a slow prior round (elapsed 5s > window) → zero wait.

    The write has had ample time to settle; adaptive means we don't pad."""
    cs.record_stream_end('convA', now=1000.0)
    waited = cs.settle_before_send('convA', BIG, now=1005.0)
    assert waited == 0.0
    assert spy_sleep == []


def test_NEUTER_without_recording_race_not_mitigated(spy_sleep):
    """NEUTER: if the prior stream end is NEVER recorded (simulating the
    pre-fix state where nothing tracks write visibility), a rapid second big
    request sends IMMEDIATELY — i.e. the race is NOT mitigated. This proves the
    record_stream_end + measure path is load-bearing: with it (see
    test_rapid_second_request_waits_remainder) the same timing waits ~1.2s."""
    # No cs.record_stream_end(...) call at all.
    waited = cs.settle_before_send('convA', BIG, now=1000.3)
    assert waited == 0.0, (
        'NEUTER expectation: without a recorded prior stream end there is '
        'nothing to settle behind → immediate send (race unmitigated)')
    assert spy_sleep == []


def test_clock_skew_backwards_is_safe(spy_sleep):
    """Robustness: if 'now' is earlier than the recorded end (clock went back),
    treat elapsed as 0 and wait the full window (capped) — never negative."""
    cs.record_stream_end('convA', now=1000.0)
    waited = cs.settle_before_send('convA', BIG, now=999.0)  # 1s in the past
    assert waited == 1.5, f'expected full 1.5s window on backward clock, got {waited}'


def test_rerecord_refreshes_baseline(spy_sleep):
    """A fresh stream end resets the measurement baseline for the next send."""
    cs.record_stream_end('convA', now=1000.0)
    cs.record_stream_end('convA', now=1010.0)  # newer round ends later
    # Measured from 1010.0, not 1000.0.
    waited = cs.settle_before_send('convA', BIG, now=1010.5)
    assert 0.99 <= waited <= 1.01, f'expected ~1.0s from the newer baseline, got {waited}'


def test_async_rapid_second_request_waits_remainder(monkeypatch):
    """Async path mirrors the sync one: rapid second big request waits ~1.2s
    via async_abortable_sleep (event-loop-friendly, no blocking)."""
    import asyncio
    calls = []

    async def _fake(seconds, abort_check=None, interval=0.5):
        calls.append(seconds)

    monkeypatch.setattr('lib.llm._transport.async_abortable_sleep', _fake)
    cs.record_stream_end('convA', now=1000.0)
    waited = asyncio.run(
        cs.async_settle_before_send('convA', BIG, now=1000.3))
    assert 1.19 <= waited <= 1.21, f'expected ~1.2s remainder, got {waited}'
    assert len(calls) == 1 and abs(calls[0] - waited) < 1e-6


def test_async_first_request_never_waits(monkeypatch):
    """Async path: no prior stream end → no wait (async sleeper never entered)."""
    import asyncio
    calls = []

    async def _fake(seconds, abort_check=None, interval=0.5):
        calls.append(seconds)

    monkeypatch.setattr('lib.llm._transport.async_abortable_sleep', _fake)
    waited = asyncio.run(cs.async_settle_before_send('convFRESH', BIG, now=1000.0))
    assert waited == 0.0
    assert calls == []


def test_default_threshold_stays_below_retired_shared_threshold():
    """The same-conversation settle threshold must stay below the obsolete
    150k cross-conversation value that skipped every ~120-140k real miss."""
    assert cs.settle_threshold_tokens() == 30_000
    assert cs.settle_threshold_tokens() < 150_000


def test_observed_miss_size_is_gated_under_default(spy_sleep):
    """THE bug this revision fixes: a 120k-token prefix (the real production
    floor-miss size) fires the settle wait under the DEFAULT threshold. With
    the old 150k-inherited bar this returned 0 (gate skipped it entirely)."""
    cs.record_stream_end('convA', now=1000.0)
    waited = cs.settle_before_send('convA', OBSERVED_MISS, now=1000.3)
    assert 1.19 <= waited <= 1.21, (
        f'120k prefix must be gated under the 30k default, got {waited}')
    assert len(spy_sleep) == 1


def test_env_override_threshold_still_respected(spy_sleep, monkeypatch):
    """The override still works: bump the threshold above 120k → the same 120k
    prefix is no longer gated (proves the knob, and that the 120k firing above
    is threshold-driven not accidental)."""
    monkeypatch.setenv('TOFU_CACHE_SETTLE_THRESHOLD_TOKENS', '150000')
    cs.record_stream_end('convA', now=1000.0)
    waited = cs.settle_before_send('convA', OBSERVED_MISS, now=1000.3)
    assert waited == 0.0
    assert spy_sleep == []
