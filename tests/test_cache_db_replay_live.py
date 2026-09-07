#!/usr/bin/env python3
"""Unit coverage for the OFFLINE machinery of debug/cache_db_replay_live.py —
the real-gateway A/B cache-replay harness.

The harness's headline value is a LIVE run (real gateway, real DB conv), which
cannot execute in CI. But its VERDICT logic is pure and must not silently rot,
because the whole point is a trustworthy A/B number:

  * ``_usage_tokens`` — pull cache_read/cache_write from the many usage key
    aliases the gateway returns.
  * ``_prefix_culprits`` — DROP the benign 'byte-field-len A→B' growth token
    (the prefix simply grew by the appended round) but KEEP a real
    '<bytes>key{field}' in-place mutation. This filter is load-bearing: it is
    what let the harness prove "prefix byte-STABLE yet still floor-collapses",
    which arbitrated the mid-anchor-is-net-negative finding.
  * ``_summarize`` — floor% + re-billed-write totals + culprit tally with the
    cold round 1 excluded. These are the exact numbers the A/B verdict prints.

Run directly (env-guarded):
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_cache_db_replay_live.py
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


_HARNESS_PATH = os.path.join(_ROOT, 'debug', 'cache_db_replay_live.py')


def _load_harness():
    """Import debug/cache_db_replay_live.py by path (debug/ is not a package).

    ``debug/`` is gitignored (every debug script is a local-only tool), so on a
    fresh checkout the harness may be absent. This test then SKIPS rather than
    erroring collection — it guards the verdict logic wherever the harness
    lives, without becoming a CI dependency on an untracked file.
    """
    spec = importlib.util.spec_from_file_location('_cache_db_replay_live',
                                                  _HARNESS_PATH)
    mod = importlib.util.module_from_spec(spec)
    if _ROOT not in sys.path:
        sys.path.insert(0, _ROOT)
    spec.loader.exec_module(mod)
    return mod


if not os.path.exists(_HARNESS_PATH):
    pytest.skip('debug/cache_db_replay_live.py absent (gitignored local tool)',
                allow_module_level=True)

H = _load_harness()


# ── _usage_tokens: alias resolution ────────────────────────────────────────

def test_usage_tokens_primary_keys():
    pt, cr, cw = H._usage_tokens({
        'prompt_tokens': 74095,
        'cache_read_tokens': 60000,
        'cache_creation_input_tokens': 12345,
    })
    assert (pt, cr, cw) == (74095, 60000, 12345)


def test_usage_tokens_alias_fallbacks():
    # Anthropic-native alias names the harness must also accept.
    pt, cr, cw = H._usage_tokens({
        'prompt_tokens': 10,
        'cache_read_input_tokens': 999,
        'cache_write_tokens': 42,
    })
    assert cr == 999 and cw == 42


def test_usage_tokens_none_safe():
    assert H._usage_tokens(None) == (0, 0, 0)
    assert H._usage_tokens({}) == (0, 0, 0)


# ── _prefix_culprits: the load-bearing growth-vs-mutation filter ────────────

def _fp(field_maps):
    """Build a wire_byte_field_prefix-shaped list: [{'key','fields':{f:hash}}]."""
    return [{'key': k, 'fields': dict(f)} for k, f in field_maps]


def test_prefix_culprits_drops_pure_growth_token():
    # Round k+1 is a strict superset (append) with an IDENTICAL shared prefix.
    prev = _fp([('assistant#0', {'content': 'a', '__order__': 'o'})])
    cur = _fp([('assistant#0', {'content': 'a', '__order__': 'o'}),
               ('tool#1', {'content': 'b', '__order__': 'o'})])
    culprits = H._prefix_culprits(prev, cur)
    # diff_byte_field_prefix would emit only 'byte-field-len 1→2' — the harness
    # MUST filter it out, leaving an empty list = prefix byte-STABLE.
    assert culprits == [], culprits


def test_prefix_culprits_keeps_real_in_place_mutation():
    # Same length, but an ALREADY-cached message's field changed byte value.
    prev = _fp([('assistant#0', {'content': 'HASH_A', '__order__': 'o'})])
    cur = _fp([('assistant#0', {'content': 'HASH_B', '__order__': 'o'})])
    culprits = H._prefix_culprits(prev, cur)
    assert culprits, 'a real in-place field mutation must NOT be filtered out'
    assert any('content' in c for c in culprits)
    assert all(not c.startswith('byte-field-len') for c in culprits)


def test_prefix_culprits_empty_on_missing_input():
    assert H._prefix_culprits(None, _fp([('a', {'x': '1'})])) == []
    assert H._prefix_culprits(_fp([('a', {'x': '1'})]), None) == []


# ── _summarize: the A/B verdict numbers ─────────────────────────────────────

def _row(rnd, cr, cw, floor, culprits=None):
    return {'round': rnd, 'msgs': rnd * 2, 'prompt': 2,
            'cache_read': cr, 'cache_write': cw, 'floor': floor,
            'culprits': culprits or []}


def test_summarize_excludes_cold_round1_and_counts_floor():
    rows = [
        _row(1, 0, 40000, True),        # cold — excluded from tallies
        _row(2, 30000, 45000, True),    # floor-collapse
        _row(3, 70000, 3000, False),    # hit
        _row(4, 28000, 50000, True),    # floor-collapse
    ]
    s = H._summarize('convX', 'current', rows)
    assert s['rounds'] == 3                       # round 1 excluded
    assert s['floor_rounds'] == 2                 # rounds 2 + 4
    assert s['floor_pct'] == pytest.approx(66.7, abs=0.1)
    # totals exclude the cold round: 45000+3000+50000, 30000+70000+28000
    assert s['total_write'] == 98000
    assert s['total_read'] == 128000


def test_summarize_none_when_too_few_rounds():
    assert H._summarize('c', 'drop', []) is None
    assert H._summarize('c', 'drop', [_row(1, 0, 1000, False)]) is None


def test_summarize_tallies_culprit_fields():
    rows = [
        _row(1, 0, 40000, True),
        _row(2, 30000, 45000, True, culprits=['<bytes>assistant#3{content}']),
        _row(3, 30000, 46000, True, culprits=['<bytes>assistant#3{content}',
                                              '<bytes>tool#5{__order__}']),
    ]
    s = H._summarize('c', 'current', rows)
    # '<bytes>key{field}' collapses to just the field name in the tally.
    assert s['culprits'].get('content') == 2
    assert s['culprits'].get('__order__') == 1


def test_summarize_drop_beats_current_shape():
    """The verdict must make a genuinely better arm look better: fewer floor
    rounds + lower total write. (Guards the direction the whole A/B reports.)"""
    current = [_row(1, 0, 40000, True)] + [_row(i, 28000, 50000, True)
                                           for i in range(2, 8)]
    drop = [_row(1, 0, 40000, True)] + [_row(i, 80000, 2000, False)
                                        for i in range(2, 8)]
    s_cur = H._summarize('c', 'current', current)
    s_drop = H._summarize('c', 'drop', drop)
    assert s_drop['floor_pct'] < s_cur['floor_pct']
    assert s_drop['total_write'] < s_cur['total_write']


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
