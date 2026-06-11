"""Tests for lib.context_limits self-heal + anti-blip shrink hardening.

Covers the 2026-06-08 changes that broke the "wrongful shrink is permanent"
deadlock:
  * authoritative gateway-stated maximum is learned directly + immediately,
  * inferred big drops require N consecutive strikes before persisting,
  * shrink entries expire after a TTL and revert to the static preset,
  * expand entries are permanent (never TTL'd),
  * _parse_context_overflow extracts (requested, stated_max) from both
    error-string orderings.
"""

import time

import pytest

import lib.context_limits as cl
from lib.tasks_pkg.compaction import _parse_context_overflow


PRESET = 1_000_000
P, M = 'sankuai', 'deepseek-v4-pro'


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Isolate module state + disable disk persistence for each test."""
    monkeypatch.setattr(cl, '_LEARNED', {}, raising=True)
    monkeypatch.setattr(cl, '_META', {}, raising=True)
    monkeypatch.setattr(cl, '_persist', lambda: None, raising=True)
    yield


# ── parser ────────────────────────────────────────────────────────────

@pytest.mark.parametrize('text,expected', [
    ('prompt is too long: 210819 tokens > 200000 maximum', (210819, 200000)),
    ("This model's maximum context length is 1048565 tokens. However, "
     'you requested 1076791 tokens', (1076791, 1048565)),
    ('API HTTP 400: 432286 tokens exceeds', (432286, None)),
    ('maximum is 128000 tokens, you requested 150000 tokens', (150000, 128000)),
    ('', (None, None)),
])
def test_parse_context_overflow(text, expected):
    assert _parse_context_overflow(text) == expected


# ── strike gate (inferred big drop) ─────────────────────────────────────

def test_inferred_big_drop_held_until_second_strike():
    r1 = cl.learn_shrink_from_error(P, M, reported_tokens=210819, preset_limit=PRESET)
    assert r1 is None
    assert cl.lookup_learned_context_limit(P, M) is None

    r2 = cl.learn_shrink_from_error(P, M, reported_tokens=210819, preset_limit=PRESET)
    assert r2 and r2['new_limit'] == int(210819 * 0.95)
    assert cl.lookup_learned_context_limit(P, M) == int(210819 * 0.95)


def test_stale_strike_does_not_count_as_consecutive(monkeypatch):
    # First strike, then age it past the window → next is strike 1 again.
    cl.learn_shrink_from_error(P, M, reported_tokens=210819, preset_limit=PRESET)
    k = cl._key(P, M)
    cl._META[k]['ts'] = time.time() - (cl._STRIKE_WINDOW_SEC + 10)
    r = cl.learn_shrink_from_error(P, M, reported_tokens=210819, preset_limit=PRESET)
    assert r is None  # reset to strike 1, still held
    assert cl.lookup_learned_context_limit(P, M) is None


# ── authoritative stated max ────────────────────────────────────────────

def test_authoritative_below_preset_persists_immediately():
    r = cl.learn_shrink_from_error(P, M, reported_tokens=260000,
                                   preset_limit=PRESET, stated_max=200000)
    assert r and r['new_limit'] == 200000
    assert cl.lookup_learned_context_limit(P, M) == 200000


def test_authoritative_above_preset_is_noop():
    r = cl.learn_shrink_from_error(P, M, reported_tokens=1076791,
                                   preset_limit=PRESET, stated_max=1048565)
    assert r is None
    assert cl.lookup_learned_context_limit(P, M) is None


def test_small_shrink_no_gate():
    # Drop to 90% of preset is NOT a big drop (< _BIG_DROP_FACTOR) → immediate.
    r = cl.learn_shrink_from_error(P, M, reported_tokens=900000, preset_limit=PRESET)
    assert r and cl.lookup_learned_context_limit(P, M) == int(900000 * 0.95)


# ── TTL self-heal ───────────────────────────────────────────────────────

def test_expired_shrink_reverts_to_preset():
    k = cl._key(P, M)
    cl._LEARNED[k] = 200278
    cl._META[k] = {'ts': time.time() - (cl._SHRINK_TTL_SEC + 86400),
                   'source': 'shrink', 'strikes': 0}
    assert cl.lookup_learned_context_limit(P, M) is None
    assert k not in cl._LEARNED  # lazily dropped


def test_fresh_shrink_still_applies():
    k = cl._key(P, M)
    cl._LEARNED[k] = 200278
    cl._META[k] = {'ts': time.time(), 'source': 'shrink', 'strikes': 0}
    assert cl.lookup_learned_context_limit(P, M) == 200278


def test_expand_entry_is_permanent():
    k = cl._key(P, M)
    cl._LEARNED[k] = 660000
    cl._META[k] = {'ts': time.time() - 99 * 86400, 'source': 'expand', 'strikes': 0}
    assert cl.lookup_learned_context_limit(P, M) == 660000


def test_legacy_entry_without_meta_is_permanent():
    # Hand-edited / pre-TTL values have no metadata → treated as permanent.
    k = cl._key(P, M)
    cl._LEARNED[k] = 192614
    assert cl.lookup_learned_context_limit(P, M) == 192614


# ── expand path ─────────────────────────────────────────────────────────

def test_expand_only_above_preset():
    assert cl.learn_expand_from_success(P, M, 500_000, preset_limit=PRESET) is None
    r = cl.learn_expand_from_success(P, M, 1_200_000, preset_limit=PRESET)
    assert r and r['new_limit'] == int(1_200_000 * 1.05)
