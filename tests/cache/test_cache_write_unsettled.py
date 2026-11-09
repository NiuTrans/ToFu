#!/usr/bin/env python3
"""Opt-in cold-write settle gate (lib/llm_dispatch/cache_settle.py) timing suite.

The gate is off by default (TOFU_CACHE_SETTLE / TOFU_CACHE_SETTLE_COLD). These cases pin
its windows, caps and first-request behaviour when an owner enables it.

Its premise, a ~15-20 s write-visibility window after a cold write, was refuted on
2026-11-08: after a cold write the next round's read rate falls with the gap instead of
recovering past the window, and single writes probed again at +150 s were not read. The
detector therefore no longer names ``cache_write_unsettled``; that attribution lives in
tests/cache/test_cache_sequential_cold_write_attribution.py and
docs/LLM_COST_OPTIMIZATION.md ("Cache-break attribution corrections").

Run DIRECTLY (env-guarded):
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python tests/cache/test_cache_write_unsettled.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

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


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
