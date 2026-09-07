#!/usr/bin/env python3
"""tests/test_cache_write_reachability_by_convention.py — the write-gated
branches are LIVE on the anthropic-convention line.

Why this file exists
====================
``tests/test_cache_accounting_convention.py`` Defect 4 pinned a fact that has
since been shown to be SAMPLE-BIASED, not universal:

    "The sankuai AIGC gateway reports ``cache_write_tokens`` but pins it to 0
     on every model (231/231 rounds). Every predicate gated on a large
     ``cache_write`` therefore can never fire here."

That was measured over persisted ``round_usage`` events. Re-measured on the
CURRENT full table, ``round_usage`` is **3052/3052 openai-convention** and
covers only opus-5 / kimi-k3 / a stray 16 rounds of 4.7 — it **never sampled
4.6 or 4.8 at all**. So "every model" was really "every model this event type
happened to record", and the anthropic-convention line was invisible to it.

What the current data actually says (full ``task_results``, 27,189 rows,
23,980 carrying usage, 2026-07-26):

    convention   cache_write>0   cache_write==0
    -----------  --------------  --------------
    anthropic            10,938              23
    openai                  307          12,712

and running the REAL predicate — not just its write half —
``lib.tasks_pkg.floor_retry.is_floor_collapse`` fires **1732 times**
(1711 anthropic + 21 openai). It is emphatically NOT dead code.

★ The correction that matters most
----------------------------------
The ticket that opened this investigation claimed "4697 rounds exceed the
>20000 threshold", counting only ``cache_write > _FLOOR_WRITE_LO``. But
``is_floor_collapse`` is a CONJUNCTION:

    cw > _FLOOR_WRITE_LO (20_000)  AND  cr <= _FLOOR_READ_HI (90_000)

Checking one conjunct overstates reachability. The honest number is 1732, not
4697. This suite asserts the conjunction, so it cannot drift back into the
one-sided claim.

Scope — deliberately NOT a threshold change
-------------------------------------------
Per the epic: confirm reachability FIRST, discuss thresholds separately. These
tests only pin (a) which convention can carry metered writes, and (b) that the
predicate is reachable on that convention and still correctly quiet on a real
openai-wire payload. ``_FLOOR_WRITE_LO`` / ``_MIN_NO_REUSE_TOKENS`` are read,
never asserted to be "right" — tuning them needs its own evidence, and
``floor_retry_enabled()`` is an env gate that remains OFF by default for
reasons documented in ``floor_retry.py`` (per-request the resend is an
expected-cost LOSS).

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_cache_write_reachability_by_convention.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from lib.cost import normalize_usage, usage_cache_convention  # noqa: E402

pytestmark = pytest.mark.unit


def _anthropic_wire(uncached: int, cache_read: int, cache_write: int = 0) -> dict:
    """Anthropic-native usage: ``input_tokens`` is the UNCACHED residual.

    Same shape as the sibling suite's helper, kept local so the two files
    cannot silently drift into each other.
    """
    u = {
        'input_tokens': uncached,
        'output_tokens': 100,
        'cache_read_input_tokens': cache_read,
    }
    if cache_write:
        u['cache_creation_input_tokens'] = cache_write
    return u


def _openai_wire(prompt_tokens: int, cached: int) -> dict:
    """The gateway's OpenAI-compat shape — cache_write is present but always 0."""
    return {
        'prompt_tokens': prompt_tokens,
        'completion_tokens': 100,
        'cache_read_tokens': cached,
        'cache_write_tokens': 0,
        'cached_tokens': cached,
        'prompt_tokens_details': {'cached_tokens': cached},
        'total_tokens': prompt_tokens + 100,
    }


# ══════════════════════════════════════════════════════════
#  1. Metered writes DO reach us — on the anthropic wire
# ══════════════════════════════════════════════════════════

def test_anthropic_wire_carries_metered_cache_writes():
    """★ The premise correction. A real 4.8-shaped payload has cw > 0.

    Copied from a live ``task_results`` row (aws.claude-opus-4.8):
    input_tokens=4, cache_read=53350, cache_write=792333.
    """
    u = _anthropic_wire(uncached=4, cache_read=53350, cache_write=792333)
    assert usage_cache_convention(u) == 'anthropic'
    assert normalize_usage(u)['cache_write'] == 792333, (
        'the anthropic line DOES meter cache creation — the old '
        '"pinned to 0 on every model" premise was sample-biased')


def test_openai_wire_still_reports_no_write():
    """REGRESSION GUARD: the original observation stays true on ITS line.

    Defect 4 was not wrong about the openai-compat wire — it was wrong about
    the scope of "every model". Keep that half pinned.
    """
    u = _openai_wire(prompt_tokens=300000, cached=74000)
    assert usage_cache_convention(u) == 'openai'
    assert normalize_usage(u)['cache_write'] == 0


# ══════════════════════════════════════════════════════════
#  2. is_floor_collapse is REACHABLE — and it's a conjunction
# ══════════════════════════════════════════════════════════

def test_floor_collapse_fires_on_a_real_anthropic_payload():
    """★ The branch is LIVE. 1711 anthropic-convention rows fire it in prod."""
    from lib.tasks_pkg import floor_retry as fr
    u = _anthropic_wire(uncached=4, cache_read=53350, cache_write=792333)
    assert fr.is_floor_collapse(u) is True, (
        'is_floor_collapse must fire on a metered-write floor collapse; '
        'it is not dead code on the anthropic line')


def test_floor_collapse_needs_BOTH_conjuncts_not_just_the_write():
    """★ Guards the counting error that opened this investigation.

    A big write with a HEALTHY read is not a collapse. Counting only
    ``cw > _FLOOR_WRITE_LO`` reported 4697 reachable rounds; the real
    conjunction yields 1732. This test makes the write-only reading fail.
    """
    from lib.tasks_pkg import floor_retry as fr
    big_write_healthy_read = _anthropic_wire(
        uncached=4, cache_read=250000, cache_write=792333)
    assert normalize_usage(big_write_healthy_read)['cache_write'] > 20000
    assert fr.is_floor_collapse(big_write_healthy_read) is False, (
        'a large write with a large read is a healthy warm round, NOT a '
        'floor collapse — the predicate is a conjunction, so reachability '
        'must never be estimated from the write threshold alone')


def test_floor_collapse_stays_quiet_on_the_openai_wire():
    """Unchanged behaviour on the write-blind line (the sibling PIN's case)."""
    from lib.tasks_pkg import floor_retry as fr
    assert fr.is_floor_collapse(_openai_wire(300000, 74000)) is False


# ══════════════════════════════════════════════════════════
#  3. no_reuse / partial_no_reuse are reachable too
# ══════════════════════════════════════════════════════════

def test_no_reuse_fires_when_writes_are_metered():
    """★ The second PIN's branch is also live on the anthropic line."""
    from lib.tasks_pkg.cache_tracking._detect import _classify_break
    _api_break, no_reuse, _partial = _classify_break(
        call_count=5, was_compaction=False,
        prev_cache_read=200000, cache_read=0,
        prev_cache_write=0, cache_write=792333,
        prev_prefix_tokens=200000,
    )
    assert no_reuse is True, (
        'no_reuse must fire when the wire actually meters a large write')


def test_no_reuse_still_quiet_without_metered_writes():
    """REGRESSION GUARD: the openai-wire case the sibling PIN protects."""
    from lib.tasks_pkg.cache_tracking._detect import _classify_break
    api_break, no_reuse, partial = _classify_break(
        call_count=5, was_compaction=False,
        prev_cache_read=200000, cache_read=0,
        prev_cache_write=0, cache_write=0,
        prev_prefix_tokens=200000,
    )
    assert no_reuse is False
    assert partial is False
    assert api_break is True, 'api_break remains the read-driven signal'


def test_compaction_still_suppresses_no_reuse():
    """A compaction legitimately rewrites the prefix — not a defect."""
    from lib.tasks_pkg.cache_tracking._detect import _classify_break
    _api_break, no_reuse, _partial = _classify_break(
        call_count=5, was_compaction=True,
        prev_cache_read=200000, cache_read=0,
        prev_cache_write=0, cache_write=792333,
        prev_prefix_tokens=200000,
    )
    assert no_reuse is False


# ══════════════════════════════════════════════════════════
#  4. The rule that keeps this from regressing
# ══════════════════════════════════════════════════════════

def test_reachability_is_a_property_of_the_convention_not_the_model():
    """The dividing line is the USAGE CONVENTION, not the model name.

    Same numbers, two key spellings: only the residual (anthropic) spelling
    can carry a metered write. Any future "model X can't do writes" claim
    should be re-expressed as "wire X doesn't meter writes" and re-measured
    per convention — that is what the 231/231 sample missed.
    """
    anthropic_like = _anthropic_wire(uncached=4, cache_read=53350,
                                     cache_write=792333)
    openai_like = _openai_wire(prompt_tokens=300000, cached=74000)
    assert usage_cache_convention(anthropic_like) == 'anthropic'
    assert usage_cache_convention(openai_like) == 'openai'
    assert normalize_usage(anthropic_like)['cache_write'] > 0
    assert normalize_usage(openai_like)['cache_write'] == 0


def main():
    raise SystemExit(pytest.main([__file__, '-v']))


if __name__ == '__main__':
    main()
