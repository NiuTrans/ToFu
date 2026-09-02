#!/usr/bin/env python3
"""Cache WRITE-VISIBILITY (cold-write) settle + detector attribution suite.

BACKGROUND (LIVE evidence, gathered this turn via real gateway probes — see the
JOURNAL entry):
  A freshly-WRITTEN Anthropic cache entry is not readable for ~15–20s after the
  write (the documented anthropic-sdk-python #1451 write-visibility race). In a
  fast tool loop the next round fires ~8s later — INSIDE that window — so it
  misses and re-writes the whole prefix, and cache_read pins at the static
  floor until the loop slows enough for the write to settle. Measured live:
    • resend identical COLD prefix every 4s → miss at t=3s, miss at t=10s,
      HIT at t≈16s;
    • settle sweep (cold write → wait S → continue) → MISS at S≤14s, HIT at S≥17s.
  The strip/thinking hypotheses were FALSIFIED first: sending an identical
  tool-loop body (with a signed thinking block in history) twice read the FULL
  prefix back on the 2nd send (cache_read == the whole prefix), so thinking
  blocks ARE cached in a pure tool_result loop.

THE DEFECT:
  lib/llm_dispatch/cache_settle.py already implements the settle gate and is
  wired into dispatch (api.py), BUT its window is 1500ms / cap 4000ms — ~10x
  too short to bridge the measured ~15–20s cold-write latency. On an 8s-gap
  loop elapsed(8s) > window(1.5s) → it waits ZERO and never helps.

THE FIX under test:
  A. cache_settle gains a COLD-WRITE-AWARE window. record_stream_end learns
     whether the finishing round was a COLD WRITE (large cache_write, ~0 read —
     the entry that actually needs time to become visible). Only THEN does the
     next same-conv send wait the long cold window (default 18000ms, capped at
     20000ms). A warm round (prefix already cached / merely extended) keeps the
     original short window so tool-loop throughput is not crippled. Env knobs:
     TOFU_CACHE_SETTLE_COLD_MS / TOFU_CACHE_SETTLE_COLD_MAX_MS.
  B. detect_cache_break names an in-window floor-miss cache_write_unsettled
     (its own bucket) instead of laundering it into upstream_identical /
     body_change: a byte-identical, same-routing read-collapse that arrives
     within the cold-write settle window since this conv's prior COLD write is
     the visibility race, NOT a server fault.

Run DIRECTLY (env-guarded):
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python tests/test_cache_write_unsettled.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

from lib.llm_dispatch import cache_settle as cs  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.setenv('TOFU_CACHE_SETTLE', '1')
    monkeypatch.setenv('TOFU_CACHE_SETTLE_MS', '1500')
    monkeypatch.setenv('TOFU_CACHE_SETTLE_MAX_MS', '4000')
    # The long cold BLOCK is opt-in (default OFF). Part-A tests below exercise
    # the ENABLED path, so turn it on here; test_cold_block_default_off_uses
    # _short_window overrides it back to prove the default.
    monkeypatch.setenv('TOFU_CACHE_SETTLE_COLD', '1')
    monkeypatch.setenv('TOFU_CACHE_SETTLE_COLD_MS', '18000')
    monkeypatch.setenv('TOFU_CACHE_SETTLE_COLD_MAX_MS', '20000')
    monkeypatch.delenv('TOFU_CACHE_SETTLE_THRESHOLD_TOKENS', raising=False)
    cs._reset_settle_for_tests()
    yield
    cs._reset_settle_for_tests()


@pytest.fixture
def spy_sleep(monkeypatch):
    calls = []

    def _fake(seconds, abort_check=None, interval=0.5):
        calls.append(seconds)

    monkeypatch.setattr('lib.llm._transport.abortable_sleep', _fake)
    return calls


BIG = 200_000
OBSERVED_MISS = 120_000


# ─────────────────────────────────────────────────────────────────────────────
#  Part A — cold-write-aware settle window
# ─────────────────────────────────────────────────────────────────────────────

def test_cold_write_uses_long_window(spy_sleep):
    """★ THE ROOT FIX (failing-first: pre-fix record_stream_end takes no
    cold arg and always uses the 1.5s window). After a COLD WRITE, a rapid
    second same-conv send waits the LONG cold window (18s), not 1.5s — because
    the just-written entry needs ~15–20s to become visible."""
    cs.record_stream_end('convCold', now=1000.0, cold_write=True)
    waited = cs.settle_before_send('convCold', BIG, now=1000.3)
    # remainder of the 18s cold window since the write 0.3s ago, capped at 20s
    assert 17.0 <= waited <= 18.0, (
        f'a rapid send after a COLD write must wait the long cold window '
        f'(~17.7s remainder of 18s), got {waited}')
    assert len(spy_sleep) == 1


def test_cold_block_default_off_uses_short_window(spy_sleep, monkeypatch):
    """★ THE OWNER GATE: the long cold BLOCK is OPT-IN. With TOFU_CACHE_SETTLE_COLD
    unset/0 (the default), a rapid send after a COLD write falls back to the
    ordinary SHORT 1.5s window — we accept the cheap re-write rather than stall
    the second round of nearly every conversation by ~18s."""
    monkeypatch.delenv('TOFU_CACHE_SETTLE_COLD', raising=False)
    cs.record_stream_end('convColdOff', now=1000.0, cold_write=True)
    waited = cs.settle_before_send('convColdOff', BIG, now=1000.3)
    assert 1.19 <= waited <= 1.21, (
        f'with the cold block OFF (default), a cold prior round must use the '
        f'short 1.5s window, not the 18s cold window — got {waited}')


def test_cold_block_off_is_the_default(spy_sleep, monkeypatch):
    """settle_cold_enabled() is False by default (no env set)."""
    monkeypatch.delenv('TOFU_CACHE_SETTLE_COLD', raising=False)
    assert cs.settle_cold_enabled() is False
    monkeypatch.setenv('TOFU_CACHE_SETTLE_COLD', '1')
    assert cs.settle_cold_enabled() is True


def test_warm_round_keeps_short_window(spy_sleep):
    """A WARM round (prefix already cached / merely extended) keeps the original
    short 1.5s window — we must NOT pad every tool-loop round by 18s."""
    cs.record_stream_end('convWarm', now=1000.0, cold_write=False)
    waited = cs.settle_before_send('convWarm', BIG, now=1000.3)
    assert 1.19 <= waited <= 1.21, (
        f'a warm round must keep the short 1.5s window, got {waited}')


def test_cold_window_hard_capped(spy_sleep):
    """The cold wait is hard-capped by TOFU_CACHE_SETTLE_COLD_MAX_MS so a clock
    skew / bogus timestamp can never stall a request unbounded."""
    cs.record_stream_end('convCap', now=1000.0, cold_write=True)
    # immediate resend → full 18s window, but cap is 20s so it is 18s here;
    # push the window above the cap to prove the cap bites.
    os.environ['TOFU_CACHE_SETTLE_COLD_MS'] = '60000'
    waited = cs.settle_before_send('convCap', BIG, now=1000.0)
    assert waited == 20.0, f'cold wait must be capped at 20s, got {waited}'


def test_cold_write_enough_elapsed_waits_zero(spy_sleep):
    """Adaptive: if the cold write already had the full cold window to settle
    (elapsed 19s > 18s window), the next send waits ZERO."""
    cs.record_stream_end('convCold', now=1000.0, cold_write=True)
    waited = cs.settle_before_send('convCold', BIG, now=1019.0)
    assert waited == 0.0
    assert spy_sleep == []


def test_cold_write_default_arg_is_warm(spy_sleep):
    """Back-compat: record_stream_end without cold_write behaves as WARM (short
    window) — existing callers that don't pass the flag are unchanged."""
    cs.record_stream_end('convDefault', now=1000.0)
    waited = cs.settle_before_send('convDefault', BIG, now=1000.3)
    assert 1.19 <= waited <= 1.21, (
        f'default (no cold flag) must keep the short window, got {waited}')


def test_cold_first_request_never_waits(spy_sleep):
    """A conv's FIRST request never waits even in cold mode (no prior write to
    settle behind) — perceived TTFT unaffected."""
    waited = cs.settle_before_send('convFresh', BIG, now=1000.0)
    assert waited == 0.0
    assert spy_sleep == []


def test_cold_sub_threshold_never_waits(spy_sleep):
    """A sub-threshold prefix never waits even after a cold write (a small-prefix
    miss is nearly free; never pad a trivial turn)."""
    cs.record_stream_end('convCold', now=1000.0, cold_write=True)
    waited = cs.settle_before_send('convCold', 5_000, now=1000.3)
    assert waited == 0.0


def test_observed_miss_gated_with_cold_window(spy_sleep):
    """The real ~120k production floor-miss, after a cold write, waits the long
    cold window (this is precisely the class the 1.5s window failed to fix)."""
    cs.record_stream_end('convCold', now=1000.0, cold_write=True)
    waited = cs.settle_before_send('convCold', OBSERVED_MISS, now=1000.5)
    assert 17.0 <= waited <= 18.0
    assert len(spy_sleep) == 1


# ─────────────────────────────────────────────────────────────────────────────
#  Part B — detector: name the in-window floor-miss cache_write_unsettled
# ─────────────────────────────────────────────────────────────────────────────

def _identical_body():
    from lib.tasks_pkg.wire_fingerprint import (
        canonical_messages, static_prefix_hash, wire_byte_prefix,
    )
    msgs = [{'role': 'system', 'content': 'STATIC SYSTEM'},
            {'role': 'user', 'content': 'hello'},
            {'role': 'assistant', 'content': 'hi there'}]
    return (msgs, canonical_messages(msgs), static_prefix_hash(msgs),
            wire_byte_prefix(msgs))


def test_detector_names_write_unsettled_not_upstream():
    """★ CORE (B). Body byte-identical, routing identical, the read collapsed,
    the previous round was a COLD WRITE and this round arrived within the
    cold-write settle window → the miss must be NAMED cache_write_unsettled,
    NEVER laundered into upstream_identical / server_side."""
    from lib.tasks_pkg.cache_tracking._state import _cache_states
    from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
    from lib.tasks_pkg.wire_fingerprint import routing_fingerprint

    _cache_states.clear()
    conv = 'writeunsettled'
    msgs, fp, st, wb = _identical_body()
    r_same = routing_fingerprint(key_hash='keyAAA',
                                 anthropic_beta='prompt-caching-2024',
                                 endpoint='https://gw/claude/messages')
    # R1: a COLD WRITE (large write, ~0 read).
    u1 = {'cache_read_tokens': 0, 'cache_creation_input_tokens': 120000,
          '_wire_fp': fp, '_wire_static': st, '_wire_bytes': wb,
          '_wire_routing': dict(r_same)}
    # R2: byte-identical prefix, arriving only ~8s later (within the window),
    # read collapsed to zero while re-writing the whole prefix (the exact live
    # probe signature: cold write then a fast follow-up misses entirely).
    u2 = {'cache_read_tokens': 0, 'cache_creation_input_tokens': 120000,
          '_wire_fp': fp, '_wire_static': st, '_wire_bytes': wb,
          '_wire_routing': dict(r_same), '_prev_cold_write_gap_s': 8.0}
    detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u1), user_id=1)
    r = detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u2), user_id=1)
    assert r is not None, 'expected a break (read collapsed after a cold write)'
    assert 'cache_write_unsettled' in r, (
        f'a byte-identical read-collapse within the cold-write settle window '
        f'must be named cache_write_unsettled — got: {r}')
    assert 'server_side' not in r, f'must NOT enter server_side: {r}'
    import json
    blob = json.dumps(r).lower()
    assert 'visib' in blob or 'settle' in blob or 'not yet visible' in blob, (
        f'the verdict must name the write-visibility race: {r}')


def test_detector_NEUTER_without_cold_gap_launders_to_upstream():
    """NEUTER control — proves the cold-gap signal is load-bearing. The SAME
    collapse, but WITHOUT the _prev_cold_write_gap_s marker (prior round was not
    a cold write, or the gap is unknown) → the miss launders back into the
    byte-identical upstream verdict and cache_write_unsettled is never named."""
    from lib.tasks_pkg.cache_tracking._state import _cache_states
    from lib.tasks_pkg.cache_tracking._detect import detect_cache_break
    from lib.tasks_pkg.wire_fingerprint import routing_fingerprint

    _cache_states.clear()
    conv = 'writeunsettled-neuter'
    msgs, fp, st, wb = _identical_body()
    r_same = routing_fingerprint(key_hash='keyAAA',
                                 anthropic_beta='prompt-caching-2024',
                                 endpoint='https://gw/claude/messages')
    u1 = {'cache_read_tokens': 260000, 'cache_creation_input_tokens': 8000,
          '_wire_fp': fp, '_wire_static': st, '_wire_bytes': wb,
          '_wire_routing': dict(r_same)}
    # No _prev_cold_write_gap_s marker → detector can't see the race.
    u2 = {'cache_read_tokens': 79615, 'cache_creation_input_tokens': 190000,
          '_wire_fp': fp, '_wire_static': st, '_wire_bytes': wb,
          '_wire_routing': dict(r_same)}
    detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u1), user_id=1)
    r = detect_cache_break(conv, msgs, None, 'claude-opus-4', usage=dict(u2), user_id=1)
    assert r is not None
    assert 'cache_write_unsettled' not in r, (
        f'NEUTER: without the cold-gap marker the write-visibility race MUST '
        f'NOT be named (this is exactly the blind spot the marker closes) — got: {r}')
    import json
    blob = json.dumps(r).lower()
    assert 'upstream cache miss' in blob, (
        f'NEUTER: without the cold-gap marker the miss launders to the '
        f'byte-identical upstream verdict — got: {r}')


def test_classify_verdict_maps_write_unsettled_bucket():
    """The single-source bucketer maps the new verdict key to its own bucket so
    live records + offline replay count it identically (never as upstream)."""
    from lib.tasks_pkg.cache_tracking._detect import (
        BUCKET_WRITE_UNSETTLED, classify_verdict,
    )
    assert classify_verdict(
        {'cache_write_unsettled': 'x'}) == BUCKET_WRITE_UNSETTLED


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
