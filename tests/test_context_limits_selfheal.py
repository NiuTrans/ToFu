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


# ── source-aware resolution (expand-below-preset starvation pin) ───────
# A learned *expand* entry recorded when the static preset was smaller must
# never lower the effective window below today's preset: the compaction gate
# caps prompts below the pin, so no observation can ever climb out, and
# expand entries never expire — the mirror of the shrink-side deadlock.
# Live evidence: sankuai::kimi-k3 pinned at 383,727 (expand) while kimi-k3's
# real window is 1M (2026-07-26).

def test_resolve_expand_below_preset_does_not_lower_window():
    k = cl._key(P, M)
    cl._LEARNED[k] = 383727
    cl._META[k] = {'ts': time.time(), 'source': 'expand', 'strikes': 0}
    assert cl.resolve_learned_context_limit(P, M, PRESET) == PRESET


def test_resolve_expand_above_preset_still_wins():
    k = cl._key(P, M)
    cl._LEARNED[k] = 1_260_000
    cl._META[k] = {'ts': time.time(), 'source': 'expand', 'strikes': 0}
    assert cl.resolve_learned_context_limit(P, M, PRESET) == 1_260_000


def test_resolve_shrink_below_preset_still_wins():
    k = cl._key(P, M)
    cl._LEARNED[k] = 200278
    cl._META[k] = {'ts': time.time(), 'source': 'shrink', 'strikes': 0}
    assert cl.resolve_learned_context_limit(P, M, PRESET) == 200278


def test_resolve_legacy_entry_stays_absolute():
    # Hand-edited / pre-TTL values keep their historical absolute semantics.
    k = cl._key(P, M)
    cl._LEARNED[k] = 192614
    assert cl.resolve_learned_context_limit(P, M, PRESET) == 192614


def test_resolve_no_entry_returns_static():
    assert cl.resolve_learned_context_limit(P, M, PRESET) == PRESET


def test_get_context_limit_kimi_k3_unpinned():
    """kimi-k3 (real 1M window) must not be capped by a stale expand pin."""
    from lib.tasks_pkg.compaction import _get_context_limit, _get_static_context_limit

    task = {'config': {'model': 'kimi-k3'}, 'provider_id': 'sankuai'}
    assert _get_static_context_limit(task) == 1_000_000

    k = cl._key('sankuai', 'kimi-k3')
    cl._LEARNED[k] = 383727
    cl._META[k] = {'ts': time.time(), 'source': 'expand', 'strikes': 0}
    assert _get_context_limit(task) == 1_000_000
