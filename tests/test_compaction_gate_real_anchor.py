#!/usr/bin/env python3
"""Compaction gate must decide on the REAL (provider-measured) yardstick, not
on unbounded estimates — the 2026-08-01 conv=mrxinirv misfire (epic
pt_18e9f7a6db664ff3).

Measured incident (app.log 2026-08-01 20:10:18): the gate counted
2,198,193 tokens ``via tiktoken+heuristic_floor`` and force-compacted a
5119-message conversation down to 33 — while the REAL prompt size one minute
earlier was 215,552 (CacheStats input=215552, hit=100%), i.e. ~22% of the
1M window. Two compounding defects:

  F0  ``record_usage`` in manager/_stream.py stored the UNCACHED RESIDUAL
      (``_prompt_tokens``) instead of the normalized FULL prompt
      (``_total_prompt_tokens``). On Anthropic-convention providers
      (input_tokens excludes cache) the usage_cache tier then reports ~2K on
      a 99%-hit warm round — the proactive gate can NEVER fire there
      (the "ball at 100% but no compaction" bug class).
  F1  When the exact tier cannot validate (task cold start / post-compaction
      prefix rewrite), estimate tiers take over and BOTH can diverge from
      reality ~10x on CJK-heavy content. Fix: (a) cap the heuristic floor at
      ``heuristic_floor_max_ratio()`` x the estimate count (the floor only
      exists to cover tiktoken's proven 0.66x under-count — nothing more);
      (b) clamp estimate-tier counts to the conversation's last REAL measured
      prompt x (1 + slack) — over-triggering destroys context lossily,
      under-triggering is bounded by the next round's fresh usage + L3.

Failing-first: test_record_usage_stores_total_not_residual,
test_mrxinirv_2010_misfire_repro, test_heuristic_floor_capped_at_ratio.

Run DIRECTLY (env-guarded):
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python tests/test_compaction_gate_real_anchor.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import threading  # noqa: E402
import time  # noqa: E402

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

_TOKENS_MOD = 'lib.tasks_pkg.compaction._tokens'
_ANCHOR_MOD = 'lib.tasks_pkg.compaction._real_anchor'


def _task(conv='c-anchor', model='kimi-k3'):
    # These tests isolate the provider-measured anchor guard.  The separately
    # tested economic working-set policy intentionally compacts at 128K by
    # default, so opt it out here; otherwise a 215K real anchor correctly trips
    # that independent policy and this suite no longer measures its namesake.
    return {
        'id': 't1234567',
        'convId': conv,
        'config': {
            'model': model,
            'compaction': {'workingSetTokens': 0},
        },
    }


def _clear_cooldown(conv):
    from lib.tasks_pkg.compaction._constants import _cooldown_lock, _summary_cooldowns
    with _cooldown_lock:
        _summary_cooldowns.pop(conv, None)


def _patch_counter(mp, tokens, method):
    import lib.token_counter as tc
    mp.setattr(tc, 'count_tokens',
               lambda *a, **kw: {'tokens': tokens, 'method': method},
               raising=True)


def _patch_estimate(mp, n):
    import lib.tasks_pkg.compaction._tokens as t
    mp.setattr(t, '_estimate_total_tokens', lambda _msgs: n, raising=True)


def _patch_anchor(mp, tokens, src):
    import lib.tasks_pkg.compaction._real_anchor as ra
    mp.setattr(ra, 'real_prompt_anchor', lambda conv_id, task=None: (tokens, src),
               raising=True)


# ═══════════════════════════════════════════════════════════════════════════
#  F0 — record_usage must store the FULL normalized prompt, not the residual
# ═══════════════════════════════════════════════════════════════════════════

def test_record_usage_stores_total_not_residual():
    """FAILING-FIRST (F0). Anthropic-convention usage: input_tokens=1809 is the
    UNCACHED residual; the real prompt is 1809+73472=75281. The usage_cache
    entry must hold 75281, else every warm Anthropic-convention conversation's
    gate reads ~2K and proactive compaction silently never fires."""
    import lib.tasks_pkg.manager._stream as strm
    from lib.token_counter.usage_cache import _lookup, invalidate

    conv = 'f0-anthropic-conv'
    invalidate(conv)
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(strm, 'dispatch_stream',
                   lambda body, **kw: (
                       {'content': 'ok', 'reasoning_content': '',
                        'tool_calls': []},
                       'stop',
                       {'input_tokens': 1809,
                        'cache_read_input_tokens': 73472,
                        'cache_creation_input_tokens': 0,
                        'output_tokens': 10}),
                   raising=True)
        mp.setattr(strm, 'make_task_abort_check', lambda _task: None)
        mp.setattr(strm, 'append_event', lambda *a, **kw: None, raising=True)
        mp.setattr(strm, 'checkpoint_task_partial', lambda *a, **kw: None,
                   raising=True)

        task = {'id': 't0anchor00', 'convId': conv, 'content': '',
                'thinking': '', 'content_lock': threading.Lock(),
                'config': {'model': 'kimi-k3'}, 'aborted': False}
        body = {'model': 'kimi-k3',
                'messages': [{'role': 'user', 'content': 'hi'}]}
        strm.stream_llm_response(task, body, tag='R1')

        entry = _lookup(conv)
        assert entry is not None, 'record_usage must have stored an entry'
        assert entry.prompt_tokens == 75281, (
            f'usage_cache must store the FULL prompt (75281), got '
            f'{entry.prompt_tokens} — the residual-only recording disables '
            f'the exact tier for Anthropic-convention providers')
    finally:
        mp.undo()
        invalidate(conv)


def test_record_usage_openai_convention_unchanged():
    """Guard: OpenAI-convention (prompt_tokens already includes cache) must be
    byte-identical before/after — total == prompt_tokens either way."""
    import lib.tasks_pkg.manager._stream as strm
    from lib.token_counter.usage_cache import _lookup, invalidate

    conv = 'f0-openai-conv'
    invalidate(conv)
    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(strm, 'dispatch_stream',
                   lambda body, **kw: (
                       {'content': 'ok', 'reasoning_content': '',
                        'tool_calls': []},
                       'stop',
                       {'prompt_tokens': 75281,
                        'prompt_tokens_details': {'cached_tokens': 73472},
                        'completion_tokens': 10}),
                   raising=True)
        mp.setattr(strm, 'make_task_abort_check', lambda _task: None)
        mp.setattr(strm, 'append_event', lambda *a, **kw: None, raising=True)
        mp.setattr(strm, 'checkpoint_task_partial', lambda *a, **kw: None,
                   raising=True)

        task = {'id': 't0anchor01', 'convId': conv, 'content': '',
                'thinking': '', 'content_lock': threading.Lock(),
                'config': {'model': 'kimi-k3'}, 'aborted': False}
        body = {'model': 'kimi-k3',
                'messages': [{'role': 'user', 'content': 'hi'}]}
        strm.stream_llm_response(task, body, tag='R1')

        entry = _lookup(conv)
        assert entry is not None
        assert entry.prompt_tokens == 75281, entry.prompt_tokens
    finally:
        mp.undo()
        invalidate(conv)


# ═══════════════════════════════════════════════════════════════════════════
#  F1 — estimate-tier counts are clamped to the REAL anchor
# ═══════════════════════════════════════════════════════════════════════════

def test_mrxinirv_2010_misfire_repro():
    """FAILING-FIRST (F1) — the 20:10 incident, end to end through the gate.

    Estimate tiers say 2,198,193 (tiktoken+heuristic_floor territory); the
    REAL last-measured prompt is 215,552. With the anchor clamp the gate must
    NOT fire (clamped to 215552x1.5=323,328 < 777,600 threshold). Pre-fix it
    fires — the misfire that destroyed a 22%-full conversation's context."""
    import lib.tasks_pkg.compaction._tokens as t

    conv = 'c-mrxinirv-repro'
    _clear_cooldown(conv)
    mp = pytest.MonkeyPatch()
    try:
        _patch_counter(mp, 2_198_193, 'tiktoken')
        _patch_estimate(mp, 2_198_193)
        _patch_anchor(mp, 215_552, 'usage_cache')
        fired = t._should_force_compact([{'role': 'user', 'content': 'x'}],
                                        _task(conv))
        assert fired is False, (
            'gate must trust the provider-measured anchor (215K real) over a '
            '10x-inflated estimate (2.19M) — the 20:10 misfire reproduces '
            'otherwise')
    finally:
        mp.undo()


def test_anchor_clamp_still_fires_when_genuinely_huge():
    """The clamp must not silence REAL overflows: anchor 700K -> cap 1.05M,
    still above the 777.6K trigger -> compaction fires."""
    import lib.tasks_pkg.compaction._tokens as t

    conv = 'c-anchor-huge'
    _clear_cooldown(conv)
    mp = pytest.MonkeyPatch()
    try:
        _patch_counter(mp, 2_198_193, 'tiktoken')
        _patch_estimate(mp, 2_198_193)
        _patch_anchor(mp, 700_000, 'usage_cache')
        assert t._should_force_compact([{'role': 'user', 'content': 'x'}],
                                       _task(conv)) is True
    finally:
        mp.undo()


def test_no_anchor_preserves_legacy_floor():
    """No real anchor known (brand-new conv / cold process, no durable read):
    behaviour must be byte-identical to the legacy max(auth, heuristic)."""
    import lib.tasks_pkg.compaction._tokens as t

    conv = 'c-no-anchor'
    _clear_cooldown(conv)
    mp = pytest.MonkeyPatch()
    try:
        _patch_counter(mp, 2_198_193, 'tiktoken')
        _patch_estimate(mp, 2_198_193)
        _patch_anchor(mp, 0, 'none')
        assert t._should_force_compact([{'role': 'user', 'content': 'x'}],
                                       _task(conv)) is True
    finally:
        mp.undo()


def test_heuristic_floor_capped_at_ratio():
    """FAILING-FIRST (F1a). Floor exists to cover tiktoken's proven 0.66x
    under-count — nothing more. heuristic 500K vs tiktoken 100K must cap at
    100K x 1.7 = 170K, not take 500K (5x over-count)."""
    import lib.tasks_pkg.compaction._tokens as t

    mp = pytest.MonkeyPatch()
    try:
        _patch_counter(mp, 100_000, 'tiktoken')
        _patch_estimate(mp, 500_000)
        _patch_anchor(mp, 0, 'none')
        total, method = t._count_tokens_authoritative(
            [{'role': 'user', 'content': 'x'}], _task())
        assert total == 170_000, (
            f'heuristic floor must cap at 1.7x the estimate tier, got {total}')
        assert 'heuristic_floor' in method, method
    finally:
        mp.undo()


def test_exact_tier_never_clamped():
    """A fresh usage_cache/api reading is the most exact number there is — a
    (possibly stale) anchor must never override it."""
    import lib.tasks_pkg.compaction._tokens as t

    mp = pytest.MonkeyPatch()
    try:
        _patch_counter(mp, 900_000, 'usage_cache')
        _patch_estimate(mp, 100_000)
        _patch_anchor(mp, 215_552, 'usage_cache')
        total, method = t._count_tokens_authoritative(
            [{'role': 'user', 'content': 'x'}], _task())
        assert total == 900_000, total
        assert method == 'usage_cache', method
    finally:
        mp.undo()


def test_anchor_flows_from_real_usage_cache_entry():
    """Wiring proof: a REAL usage_cache entry (post-F0 total) feeds the clamp
    without any patching of _real_anchor itself."""
    import lib.tasks_pkg.compaction._tokens as t
    from lib.token_counter.usage_cache import _UsageEntry

    mp = pytest.MonkeyPatch()
    try:
        import lib.token_counter.usage_cache as uc
        mp.setattr(uc, '_lookup', lambda conv_id: _UsageEntry(
            prompt_tokens=215_552, model='kimi-k3', ts=time.time(),
            message_count=40, tail_signature='sig'), raising=True)
        import lib.tasks_pkg.cache_tracking._persist as per
        mp.setattr(per, 'read_last_turn_cache_read', lambda _c: 0, raising=True)

        _patch_counter(mp, 2_198_193, 'tiktoken')
        _patch_estimate(mp, 2_198_193)
        total, method = t._count_tokens_authoritative(
            [{'role': 'user', 'content': 'x'}], _task())
        assert total == int(215_552 * 1.5), total
        assert 'anchor:usage_cache' in method, method
    finally:
        mp.undo()


def test_real_anchor_source_preference():
    """_real_anchor: fresh in-memory entry wins; durable lastTurnCacheRead is
    the restart-resilient fallback; nothing known -> (0, 'none')."""
    import lib.tasks_pkg.compaction._real_anchor as ra
    from lib.token_counter.usage_cache import _UsageEntry

    mp = pytest.MonkeyPatch()
    try:
        import lib.token_counter.usage_cache as uc
        import lib.tasks_pkg.cache_tracking._persist as per

        # in-memory preferred over durable
        mp.setattr(uc, '_lookup', lambda conv_id: _UsageEntry(
            prompt_tokens=300_000, model='kimi-k3', ts=time.time(),
            message_count=40, tail_signature='s'), raising=True)
        mp.setattr(per, 'read_last_turn_cache_read',
                   lambda _c, *, user_id: 215_552,
                   raising=True)
        task = {'_userId': 1}
        assert ra.real_prompt_anchor('c1', task) == (300_000, 'usage_cache')

        # durable fallback when memory is cold
        mp.setattr(uc, '_lookup', lambda conv_id: None, raising=True)
        assert ra.real_prompt_anchor(
            'c1', task) == (215_552, 'durable:lastTurnCacheRead')

        # nothing known
        mp.setattr(per, 'read_last_turn_cache_read',
                   lambda _c, *, user_id: 0, raising=True)
        assert ra.real_prompt_anchor('c1', task) == (0, 'none')

        # empty conv id never touches the stores
        assert ra.real_prompt_anchor('') == (0, 'none')
    finally:
        mp.undo()


# ═══════════════════════════════════════════════════════════════════════════
#  Constants — env-overridable FAIL-OPEN readers (§10.1 pattern)
# ═══════════════════════════════════════════════════════════════════════════

def test_floor_ratio_reader_env_override():
    import lib.tasks_pkg.compaction._constants as C

    mp = pytest.MonkeyPatch()
    try:
        mp.delenv('TOFU_COMPACT_FLOOR_MAX_RATIO', raising=False)
        assert C.heuristic_floor_max_ratio() == C._HEURISTIC_FLOOR_MAX_RATIO
        mp.setenv('TOFU_COMPACT_FLOOR_MAX_RATIO', '2.5')
        assert C.heuristic_floor_max_ratio() == 2.5
        mp.setenv('TOFU_COMPACT_FLOOR_MAX_RATIO', 'garbage')
        assert C.heuristic_floor_max_ratio() == C._HEURISTIC_FLOOR_MAX_RATIO
        mp.setenv('TOFU_COMPACT_FLOOR_MAX_RATIO', '99')
        assert C.heuristic_floor_max_ratio() == 5.0   # clamped
        mp.setenv('TOFU_COMPACT_FLOOR_MAX_RATIO', '0.1')
        assert C.heuristic_floor_max_ratio() == 1.0   # clamped
    finally:
        mp.undo()


def test_anchor_slack_reader_env_override():
    import lib.tasks_pkg.compaction._constants as C

    mp = pytest.MonkeyPatch()
    try:
        mp.delenv('TOFU_COMPACT_ANCHOR_SLACK', raising=False)
        assert C.real_anchor_slack() == C._REAL_ANCHOR_SLACK
        mp.setenv('TOFU_COMPACT_ANCHOR_SLACK', '1.0')
        assert C.real_anchor_slack() == 1.0
        mp.setenv('TOFU_COMPACT_ANCHOR_SLACK', 'junk')
        assert C.real_anchor_slack() == C._REAL_ANCHOR_SLACK
        mp.setenv('TOFU_COMPACT_ANCHOR_SLACK', '99')
        assert C.real_anchor_slack() == 3.0           # clamped
    finally:
        mp.undo()


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))
