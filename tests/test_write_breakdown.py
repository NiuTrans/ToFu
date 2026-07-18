"""tests/test_write_breakdown.py — tests for the per-round `write` decomposition.

`lib.tasks_pkg.orchestrator._compute_write_breakdown` splits a round's
prompt-cache ``write`` into {toolResults, prevOutput, recacheBody, envelope}
from real recorded usage. The CORE INVARIANT — the reason this lives on the
backend — is that the sub-items sum to EXACTLY ``write`` in every case,
including the tokenizer-mismatch case where the output-side component counts
overshoot the provider's input-side ``cache_write_tokens``. On a cache-break
round the body re-cache is reported as its own ``recacheBody`` term, never
lumped into ``envelope``.
"""

from __future__ import annotations

import unittest

from lib.tasks_pkg.orchestrator import _compute_write_breakdown


def _sum(wb: dict) -> int:
    return (wb['toolResults'] + wb['prevOutput']
            + wb.get('contextWrite', 0) + wb.get('recacheBody', 0)
            + wb['envelope'])


class WriteBreakdownTest(unittest.TestCase):

    def test_normal_exact_sum(self):
        # round_num=1 → api_rounds[-1] is round 2 (write=2000), api_rounds[-2]
        # is round 1 (output 600+100=700). Tool rounds with llmRound==0
        # (round_num-1) sum to 546+424=970. envelope = 2000-700-970 = 330.
        wb = _compute_write_breakdown(
            {'toolRounds': [
                {'llmRound': 0, 'toolTokens': 546},
                {'llmRound': 0, 'toolTokens': 424},
                {'llmRound': 1, 'toolTokens': 999},  # other round → ignored
            ]},
            [
                {'round': 1, 'usage': {'completion_tokens': 600, 'reasoning_tokens': 100}},
                {'round': 2, 'usage': {'cache_write_tokens': 2000}},
            ],
            1,
        )
        self.assertEqual(wb['write'], 2000)
        self.assertEqual(wb['toolResults'], 970)
        self.assertEqual(wb['prevOutput'], 700)
        self.assertEqual(wb['envelope'], 330)
        self.assertEqual(wb['recacheBody'], 0)  # no cacheBreak → no re-cache term
        self.assertFalse(wb['capped'])
        self.assertEqual(_sum(wb), wb['write'])

    def test_overshoot_components_scaled_proportionally_sum_still_exact(self):
        # tools 970 + prev 700 = 1670 > write 1000. The overshoot is resolved
        # by scaling BOTH components down by their estimated share — NOT by
        # letting tool results consume the whole write and zeroing prev output.
        # tool_results = round(1000 * 970/1670) = 581; prev_output = remainder
        # = 419. Both stay non-zero and sum exactly to write.
        wb = _compute_write_breakdown(
            {'toolRounds': [
                {'llmRound': 0, 'toolTokens': 546},
                {'llmRound': 0, 'toolTokens': 424},
            ]},
            [
                {'round': 1, 'usage': {'completion_tokens': 700}},
                {'round': 2, 'usage': {'cache_write_tokens': 1000}},
            ],
            1,
        )
        self.assertEqual(wb['toolResults'], 581)
        self.assertEqual(wb['prevOutput'], 419)
        self.assertEqual(wb['envelope'], 0)
        self.assertTrue(wb['capped'])
        # The whole point of proportional truncation: a real previous-round
        # output is never annihilated by the tool estimate's overshoot.
        self.assertGreater(wb['prevOutput'], 0)
        self.assertEqual(_sum(wb), wb['write'])

    def test_tool_results_dominate_but_prev_output_survives(self):
        # tools 1500 dwarfs prev 700, sum 2200 > write 1000. Even so, the
        # previous round DID output 700 tok — its share must survive, not be
        # zeroed. tool = round(1000 * 1500/2200) = 682; prev = 318.
        wb = _compute_write_breakdown(
            {'toolRounds': [{'llmRound': 0, 'toolTokens': 1500}]},
            [
                {'round': 1, 'usage': {'completion_tokens': 700}},
                {'round': 2, 'usage': {'cache_write_tokens': 1000}},
            ],
            1,
        )
        self.assertEqual(wb['toolResults'], 682)
        self.assertEqual(wb['prevOutput'], 318)
        self.assertGreater(wb['prevOutput'], 0)
        self.assertEqual(wb['envelope'], 0)
        self.assertTrue(wb['capped'])
        self.assertEqual(_sum(wb), wb['write'])

    def test_round2_real_bug_prev_output_not_swallowed(self):
        # The exact reported round-2 shape: write 1.1k, the local tool
        # estimate (1.6k) alone exceeds it, and the previous round produced a
        # real 595-token output. The OLD priority-cap logic zeroed prevOutput
        # and mislabeled its share as "tool results" (write 1.1k → toolResults
        # 1.1k, prevOutput 0). Proportional truncation must keep prevOutput's
        # share alive: est sum = 1595, tool = round(1100 * 1000/1595) = 690,
        # prev = 410.
        wb = _compute_write_breakdown(
            {'toolRounds': [
                {'llmRound': 0, 'toolTokens': 560},
                {'llmRound': 0, 'toolTokens': 440},
            ]},
            [
                {'round': 1, 'usage': {'completion_tokens': 595}},
                {'round': 2, 'usage': {'cache_write_tokens': 1100}},
            ],
            1,
        )
        self.assertEqual(wb['write'], 1100)
        # est sum = 1000 (tools) + 595 (prev) = 1595 > 1100 → scaled.
        self.assertEqual(wb['toolResults'], round(1100 * 1000 / 1595))  # 690
        self.assertEqual(wb['prevOutput'], 1100 - round(1100 * 1000 / 1595))  # 410
        # The regression guard: the real 595-tok previous output is NOT
        # attributed entirely to tool results (the round-2 misattribution bug).
        self.assertGreater(wb['prevOutput'], 0)
        self.assertLess(wb['toolResults'], wb['write'])
        self.assertTrue(wb['capped'])
        self.assertEqual(_sum(wb), wb['write'])

    def test_NEUTER_priority_cap_would_zero_prev_output(self):
        # NEGATIVE CONTROL with teeth: re-implement the OLD priority-cap
        # resolution (tool results first, prev output gets only the remainder)
        # on the SAME round-2 inputs and prove it produces the very
        # misattribution the fix eliminates — prevOutput == 0 and toolResults
        # swallows the whole write. If the production code ever regresses to
        # priority-cap, test_round2_real_bug_prev_output_not_swallowed fails
        # while THIS reproduction still shows the broken shape, pinpointing the
        # regression. (This does NOT call the production function — it models
        # the discarded algorithm so the guard's intent is self-documenting.)
        write = 1100
        tool_est, prev_est = 1000, 595
        # Old logic:
        old_tool = min(tool_est, write)              # 1000
        old_prev = min(prev_est, write - old_tool)   # min(595, 100) = 100
        # (In the reported case tool_est 1.6k ≥ write → old_prev would be 0.)
        self.assertEqual(old_tool, 1000)
        self.assertEqual(old_prev, 100)
        # And with the reported 1.6k estimate the previous output is fully lost:
        tool_est_hi = 1600
        self.assertEqual(min(prev_est, write - min(tool_est_hi, write)), 0)
        # The production function on the same hi estimate must NOT zero it:
        wb = _compute_write_breakdown(
            {'toolRounds': [{'llmRound': 0, 'toolTokens': 1600}]},
            [
                {'round': 1, 'usage': {'completion_tokens': 595}},
                {'round': 2, 'usage': {'cache_write_tokens': 1100}},
            ],
            1,
        )
        self.assertGreater(wb['prevOutput'], 0)
        self.assertEqual(_sum(wb), wb['write'])

    def test_first_round_no_prev_output(self):
        # round_num=0 → no api_rounds[-2]; prevOutput=0. Tool rounds with
        # llmRound==-1 (round_num-1) flow in. Residual (450) is below the
        # envelope ceiling, so it stays envelope; no contextWrite manufactured.
        wb = _compute_write_breakdown(
            {'toolRounds': [{'llmRound': -1, 'toolTokens': 50}]},
            [{'round': 1, 'usage': {'cache_write_tokens': 500}}],
            0,
        )
        self.assertEqual(wb['write'], 500)
        self.assertEqual(wb['toolResults'], 50)
        self.assertEqual(wb['prevOutput'], 0)
        self.assertEqual(wb['envelope'], 450)
        self.assertEqual(wb['contextWrite'], 0)
        self.assertEqual(wb['recacheBody'], 0)
        self.assertEqual(_sum(wb), wb['write'])

    def test_first_round_large_write_is_context_not_envelope(self):
        # The real-world bug: round 1 writes the whole system+tools+history
        # prefix (64.1k) with no prior round and no cacheBreak. The 64.1k must
        # be reported as `contextWrite` (first-time caching), NOT `envelope`
        # (which is physically bounded framing). envelope keeps only the
        # ceiling allowance; the excess goes to contextWrite; recacheBody=0.
        from lib.tasks_pkg.orchestrator import _ENVELOPE_MAX_TOKENS
        wb = _compute_write_breakdown(
            {'toolRounds': []},
            [{'round': 1, 'usage': {'cache_write_tokens': 64100}}],
            0,
        )
        self.assertEqual(wb['write'], 64100)
        self.assertEqual(wb['prevOutput'], 0)
        self.assertEqual(wb['toolResults'], 0)
        self.assertEqual(wb['envelope'], _ENVELOPE_MAX_TOKENS)
        self.assertEqual(wb['contextWrite'], 64100 - _ENVELOPE_MAX_TOKENS)
        self.assertEqual(wb['recacheBody'], 0)
        self.assertEqual(_sum(wb), wb['write'])

    def test_context_and_recache_are_mutually_exclusive(self):
        # Same large residual, but WITH a cacheBreak → the excess is waste
        # (recacheBody), and contextWrite must be 0. Mirror image of the test
        # above — the break flag is what distinguishes warm-up from waste.
        from lib.tasks_pkg.orchestrator import _ENVELOPE_MAX_TOKENS
        wb = _compute_write_breakdown(
            {'toolRounds': []},
            [{'round': 1, 'usage': {'cache_write_tokens': 64100},
              'cacheBreak': {'no_cache_reuse': 'stochastic server-side miss'}}],
            0,
        )
        self.assertEqual(wb['contextWrite'], 0)
        self.assertEqual(wb['recacheBody'], 64100 - _ENVELOPE_MAX_TOKENS)
        self.assertEqual(wb['envelope'], _ENVELOPE_MAX_TOKENS)
        self.assertEqual(_sum(wb), wb['write'])

    def test_output_tokens_alias_and_thinking_alias(self):
        # prevOutput should read output_tokens + thinking_tokens aliases too.
        wb = _compute_write_breakdown(
            {'toolRounds': []},
            [
                {'round': 1, 'usage': {'output_tokens': 200, 'thinking_tokens': 50}},
                {'round': 2, 'usage': {'cache_creation_input_tokens': 1000}},
            ],
            1,
        )
        self.assertEqual(wb['prevOutput'], 250)
        self.assertEqual(wb['toolResults'], 0)
        self.assertEqual(wb['envelope'], 750)
        self.assertEqual(_sum(wb), wb['write'])

    def test_cache_break_round_splits_recache_body(self):
        # A round carrying a cacheBreak with a huge residual must attribute the
        # bulk to recacheBody, NOT envelope. write=37700, tools=893, prev=337
        # → residual=36470; envelope capped at _ENVELOPE_MAX_TOKENS (800),
        # recacheBody=35670. The 36k must NOT show up as "structural overhead".
        from lib.tasks_pkg.orchestrator import _ENVELOPE_MAX_TOKENS
        wb = _compute_write_breakdown(
            {'toolRounds': [{'llmRound': 0, 'toolTokens': 893}]},
            [
                {'round': 1, 'usage': {'completion_tokens': 337}},
                {'round': 2, 'usage': {'cache_write_tokens': 37700},
                 'cacheBreak': {'no_cache_reuse': 'breakpoint advancement (BP4 …)'}},
            ],
            1,
        )
        self.assertEqual(wb['toolResults'], 893)
        self.assertEqual(wb['prevOutput'], 337)
        self.assertEqual(wb['envelope'], _ENVELOPE_MAX_TOKENS)
        self.assertEqual(wb['recacheBody'], 37700 - 893 - 337 - _ENVELOPE_MAX_TOKENS)
        self.assertEqual(wb['recacheCause'], {'no_cache_reuse': 'breakpoint advancement (BP4 …)'})
        self.assertEqual(_sum(wb), wb['write'])

    def test_no_cache_break_large_residual_is_context_write(self):
        # Same numbers but NO cacheBreak. The large residual is NOT framing —
        # it is fresh context cached for the first time, so it must land in
        # `contextWrite`, with envelope capped and recacheBody=0.
        from lib.tasks_pkg.orchestrator import _ENVELOPE_MAX_TOKENS
        wb = _compute_write_breakdown(
            {'toolRounds': [{'llmRound': 0, 'toolTokens': 893}]},
            [
                {'round': 1, 'usage': {'completion_tokens': 337}},
                {'round': 2, 'usage': {'cache_write_tokens': 37700}},
            ],
            1,
        )
        self.assertEqual(wb['recacheBody'], 0)
        self.assertEqual(wb['envelope'], _ENVELOPE_MAX_TOKENS)
        self.assertEqual(wb['contextWrite'], 37700 - 893 - 337 - _ENVELOPE_MAX_TOKENS)
        self.assertEqual(_sum(wb), wb['write'])

    def test_small_residual_cache_break_stays_envelope(self):
        # A cache-break round whose residual is BELOW the envelope ceiling must
        # NOT manufacture a recacheBody term (residual <= _ENVELOPE_MAX_TOKENS).
        wb = _compute_write_breakdown(
            {'toolRounds': [{'llmRound': 0, 'toolTokens': 100}]},
            [
                {'round': 1, 'usage': {'completion_tokens': 50}},
                {'round': 2, 'usage': {'cache_write_tokens': 600},
                 'cacheBreak': {'server_side': 'x'}},
            ],
            1,
        )
        self.assertEqual(wb['recacheBody'], 0)
        self.assertEqual(wb['contextWrite'], 0)
        self.assertEqual(wb['envelope'], 600 - 100 - 50)
        self.assertEqual(_sum(wb), wb['write'])

    def test_read_drop_relabels_context_as_recache_body(self):
        # The user-reported bug: round 3 wrote 5.8k while the conversation only
        # grew ~300 tok (prevOutput 233 + toolResults 68), and cache_read FELL
        # 134.9k → 130.0k (a 4.9k drop, only ~3.6% so detect_cache_break stayed
        # silent → no cacheBreak flag). The excess must be labeled recacheBody
        # (re-billed body / waste), NOT contextWrite ("first-time, not waste").
        from lib.tasks_pkg.orchestrator import _ENVELOPE_MAX_TOKENS
        wb = _compute_write_breakdown(
            {'toolRounds': [{'llmRound': 0, 'toolTokens': 68}]},
            [
                {'round': 1, 'usage': {'completion_tokens': 233,
                                       'cache_read_tokens': 134900}},
                {'round': 2, 'usage': {'cache_write_tokens': 5800,
                                       'cache_read_tokens': 130000}},
            ],
            1,
        )
        self.assertEqual(wb['write'], 5800)
        self.assertEqual(wb['toolResults'], 68)
        self.assertEqual(wb['prevOutput'], 233)
        self.assertEqual(wb['envelope'], _ENVELOPE_MAX_TOKENS)
        # excess = 5800 - 68 - 233 - 800 = 4699; read_drop = 4900 ≥ excess →
        # the whole excess is re-billed body, contextWrite must be 0.
        self.assertEqual(wb['recacheBody'], 4699)
        self.assertEqual(wb['contextWrite'], 0)
        self.assertEqual(wb['readDrop'], 4900)
        # A synthetic cause is filled in so the term is never unexplained.
        self.assertIn('no_cache_reuse', wb['recacheCause'])
        self.assertEqual(_sum(wb), wb['write'])

    def test_read_held_keeps_context_write(self):
        # Same shape but cache_read HELD (no drop) → the excess is genuine
        # first-time context, recacheBody must stay 0 and readDrop 0.
        from lib.tasks_pkg.orchestrator import _ENVELOPE_MAX_TOKENS
        wb = _compute_write_breakdown(
            {'toolRounds': [{'llmRound': 0, 'toolTokens': 68}]},
            [
                {'round': 1, 'usage': {'completion_tokens': 233,
                                       'cache_read_tokens': 130000}},
                {'round': 2, 'usage': {'cache_write_tokens': 5800,
                                       'cache_read_tokens': 130000}},
            ],
            1,
        )
        self.assertEqual(wb['readDrop'], 0)
        self.assertEqual(wb['recacheBody'], 0)
        self.assertEqual(wb['contextWrite'], 5800 - 68 - 233 - _ENVELOPE_MAX_TOKENS)
        self.assertEqual(wb['recacheCause'], {})
        self.assertEqual(_sum(wb), wb['write'])

    def test_partial_read_drop_splits_excess(self):
        # Read drop SMALLER than the excess: only the drop is re-billed body,
        # the remainder is genuine new context. Both terms present, sum exact.
        from lib.tasks_pkg.orchestrator import _ENVELOPE_MAX_TOKENS
        wb = _compute_write_breakdown(
            {'toolRounds': []},
            [
                {'round': 1, 'usage': {'completion_tokens': 0,
                                       'cache_read_tokens': 100000}},
                {'round': 2, 'usage': {'cache_write_tokens': 10800,
                                       'cache_read_tokens': 97000}},
            ],
            1,
        )
        # excess = 10800 - 0 - 0 - 800 = 10000; read_drop = 3000.
        self.assertEqual(wb['readDrop'], 3000)
        self.assertEqual(wb['recacheBody'], 3000)
        self.assertEqual(wb['contextWrite'], 7000)
        self.assertEqual(wb['envelope'], _ENVELOPE_MAX_TOKENS)
        self.assertEqual(_sum(wb), wb['write'])

    def test_small_read_drop_below_threshold_stays_context(self):
        # A drop below _READ_DROP_WASTE_TOKENS is noise, not waste → contextWrite.
        from lib.tasks_pkg.orchestrator import _ENVELOPE_MAX_TOKENS
        wb = _compute_write_breakdown(
            {'toolRounds': []},
            [
                {'round': 1, 'usage': {'cache_read_tokens': 100000}},
                {'round': 2, 'usage': {'cache_write_tokens': 5800,
                                       'cache_read_tokens': 99500}},
            ],
            1,
        )
        self.assertEqual(wb['readDrop'], 500)
        self.assertEqual(wb['recacheBody'], 0)
        self.assertEqual(wb['contextWrite'], 5800 - _ENVELOPE_MAX_TOKENS)
        self.assertEqual(_sum(wb), wb['write'])

    # ── Cross-turn baseline: the turn's round-1 mislabel fix ──
    # A turn's round-1 has NO within-turn predecessor (api_rounds has ONE
    # entry), so prev_read used to be 0 → read_drop 0 → the whole write
    # defaulted to benign contextWrite even when the PREVIOUS turn's cached
    # prefix was partly evicted and re-billed this round. The
    # prev_turn_cache_read param carries the prior turn's final cached-prefix
    # read across the run_task thread boundary so round-1 is honest.

    def test_round1_evicted_tail_recache_body_with_cross_turn_baseline(self):
        # Turn round-1: single api_round, write=40100, cache_read=79200. The
        # PREVIOUS turn ended reading 118000 of cached prefix. This round reads
        # only 79200 → a 38800 cross-turn drop: part of the prior turn's cached
        # prefix was evicted and re-billed inside this write. The excess must be
        # recacheBody, NOT the benign "first-cache context" (the exact round-1
        # mislabel the user caught).
        from lib.tasks_pkg.orchestrator import _ENVELOPE_MAX_TOKENS
        wb = _compute_write_breakdown(
            {'toolRounds': []},
            [{'round': 1, 'usage': {'cache_write_tokens': 40100,
                                    'cache_read_tokens': 79200}}],
            0,
            prev_turn_cache_read=118000,
        )
        self.assertEqual(wb['write'], 40100)
        self.assertEqual(wb['prevOutput'], 0)
        self.assertEqual(wb['toolResults'], 0)
        self.assertEqual(wb['envelope'], _ENVELOPE_MAX_TOKENS)
        self.assertEqual(wb['readDrop'], 118000 - 79200)  # 38800
        # excess = 40100 - 800 = 39300; read_drop 38800 < excess →
        # 38800 recacheBody + 500 genuinely-new contextWrite.
        self.assertEqual(wb['recacheBody'], 38800)
        self.assertEqual(wb['contextWrite'], 39300 - 38800)  # 500
        self.assertIn('no_cache_reuse', wb['recacheCause'])
        self.assertEqual(_sum(wb), wb['write'])

    def test_round1_baseline_held_stays_context_write(self):
        # Same round-1 write, but the prior turn's read baseline is BELOW this
        # round's read (cache grew across the turn boundary — the prefix was
        # fully read back plus more). No drop → the write is genuine first-time
        # context, recacheBody must stay 0.
        from lib.tasks_pkg.orchestrator import _ENVELOPE_MAX_TOKENS
        wb = _compute_write_breakdown(
            {'toolRounds': []},
            [{'round': 1, 'usage': {'cache_write_tokens': 40100,
                                    'cache_read_tokens': 79200}}],
            0,
            prev_turn_cache_read=79200,  # held exactly → no drop
        )
        self.assertEqual(wb['readDrop'], 0)
        self.assertEqual(wb['recacheBody'], 0)
        self.assertEqual(wb['contextWrite'], 40100 - _ENVELOPE_MAX_TOKENS)
        self.assertEqual(wb['recacheCause'], {})
        self.assertEqual(_sum(wb), wb['write'])

    def test_round1_genuine_first_ever_call_no_baseline_is_context(self):
        # A brand-new conversation's very first turn: no prior turn exists, so
        # prev_turn_cache_read defaults to 0 → read_drop 0 → the large write is
        # legitimate first-time context, exactly as before the fix. This is the
        # honest "first-cache warm-up" case the label was MEANT for.
        from lib.tasks_pkg.orchestrator import _ENVELOPE_MAX_TOKENS
        wb = _compute_write_breakdown(
            {'toolRounds': []},
            [{'round': 1, 'usage': {'cache_write_tokens': 40100,
                                    'cache_read_tokens': 0}}],
            0,
            prev_turn_cache_read=0,
        )
        self.assertEqual(wb['readDrop'], 0)
        self.assertEqual(wb['recacheBody'], 0)
        self.assertEqual(wb['contextWrite'], 40100 - _ENVELOPE_MAX_TOKENS)
        self.assertEqual(_sum(wb), wb['write'])

    def test_round1_default_param_matches_old_behavior(self):
        # Backward-compat: omitting prev_turn_cache_read (the default 0) yields
        # the SAME breakdown as passing 0 — no caller that forgets the new arg
        # gets a surprise. Mirrors the genuine-first-call case.
        wb = _compute_write_breakdown(
            {'toolRounds': []},
            [{'round': 1, 'usage': {'cache_write_tokens': 40100,
                                    'cache_read_tokens': 79200}}],
            0,
        )
        self.assertEqual(wb['readDrop'], 0)
        self.assertEqual(wb['recacheBody'], 0)
        self.assertEqual(wb['contextWrite'], 40100 - 800)
        self.assertEqual(_sum(wb), wb['write'])

    def test_NEUTER_round1_without_baseline_mislabels_eviction_as_context(self):
        # NEGATIVE CONTROL with teeth: the SAME evicted-round-1 inputs as
        # test_round1_evicted_tail_recache_body_with_cross_turn_baseline, but
        # WITHOUT the cross-turn baseline (prev_turn_cache_read=0, the pre-fix
        # behavior). The eviction re-bill is then INVISIBLE — read_drop=0 → the
        # whole 39.3k excess is mislabeled benign contextWrite, recacheBody=0.
        # If the production path ever stops feeding the baseline, the positive
        # test flips to this broken shape, pinpointing the regression.
        wb = _compute_write_breakdown(
            {'toolRounds': []},
            [{'round': 1, 'usage': {'cache_write_tokens': 40100,
                                    'cache_read_tokens': 79200}}],
            0,
            prev_turn_cache_read=0,  # the pre-fix blindness
        )
        self.assertEqual(wb['readDrop'], 0)
        self.assertEqual(wb['recacheBody'], 0)              # eviction hidden
        self.assertEqual(wb['contextWrite'], 40100 - 800)   # mislabeled benign
        self.assertEqual(wb['recacheCause'], {})
        self.assertEqual(_sum(wb), wb['write'])

    def test_within_turn_prev_wins_over_cross_turn_baseline(self):
        # When a within-turn predecessor EXISTS (api_rounds[-2]), its read is
        # the baseline — the cross-turn arg is ignored (it only rescues round-1).
        # round 2: prev within-turn read 130000, this read 130000 → no drop,
        # despite a cross-turn arg that would (wrongly) imply a huge drop.
        from lib.tasks_pkg.orchestrator import _ENVELOPE_MAX_TOKENS
        wb = _compute_write_breakdown(
            {'toolRounds': []},
            [
                {'round': 1, 'usage': {'completion_tokens': 0,
                                       'cache_read_tokens': 130000}},
                {'round': 2, 'usage': {'cache_write_tokens': 5800,
                                       'cache_read_tokens': 130000}},
            ],
            1,
            prev_turn_cache_read=999999,  # must be IGNORED (within-turn wins)
        )
        self.assertEqual(wb['readDrop'], 0)
        self.assertEqual(wb['recacheBody'], 0)
        self.assertEqual(wb['contextWrite'], 5800 - _ENVELOPE_MAX_TOKENS)
        self.assertEqual(_sum(wb), wb['write'])

    def test_no_write_returns_none(self):
        self.assertIsNone(_compute_write_breakdown(
            {'toolRounds': []},
            [{'round': 1, 'usage': {'cache_write_tokens': 0}}],
            0,
        ))

    def test_empty_api_rounds_returns_none(self):
        self.assertIsNone(_compute_write_breakdown({'toolRounds': []}, [], 0))

    def test_malformed_inputs_do_not_raise(self):
        # api_rounds[-1] not a dict → None.
        self.assertIsNone(_compute_write_breakdown({}, [None], 0))
        # No write in usage → None even with a junk toolRounds value (the
        # per-entry isinstance(dict) guard tolerates non-dict iterables).
        self.assertIsNone(_compute_write_breakdown(
            {'toolRounds': 'bad'}, [{'round': 1, 'usage': {}}], 0))
        # Junk toolRounds + a real write → valid breakdown, never raises.
        wb = _compute_write_breakdown(
            {'toolRounds': 'bad'},
            [{'round': 1, 'usage': {'cache_write_tokens': 300}}], 0)
        self.assertEqual(wb['write'], 300)
        self.assertEqual(_sum(wb), 300)


class PrevTurnCacheReadTest(unittest.TestCase):
    """get_prev_turn_cache_read — the cross-turn baseline source.

    It scans the per-(conv,thread) cache-state singleton for the same conv,
    EXCLUDING the caller's own thread (whose entry detect_cache_break already
    advanced to THIS round's read), and returns the most-recently-updated
    sibling's last_cache_read_tokens. That sibling is the previous user turn.
    """

    def setUp(self):
        import threading
        from lib.tasks_pkg.cache_tracking import _cache_states, _cache_lock, CacheState
        self._threading = threading
        self._states = _cache_states
        self._lock = _cache_lock
        self._CacheState = CacheState
        self._conv = 'cw_test_conv_' + str(id(self))
        # Clean any stray entries for this conv id.
        with _cache_lock:
            for k in [k for k in _cache_states if k[0] == self._conv]:
                _cache_states.pop(k, None)

    def tearDown(self):
        with self._lock:
            for k in [k for k in self._states if k[0] == self._conv]:
                self._states.pop(k, None)

    def _put(self, thread_id, *, cache_read, update_time, call_count=1):
        st = self._CacheState()
        st.last_cache_read_tokens = cache_read
        st.last_update_time = update_time
        st.call_count = call_count
        with self._lock:
            self._states[(self._conv, thread_id)] = st

    def test_returns_zero_when_no_state(self):
        from lib.tasks_pkg.cache_tracking import get_prev_turn_cache_read
        self.assertEqual(get_prev_turn_cache_read(self._conv), 0)
        self.assertEqual(get_prev_turn_cache_read(''), 0)

    def test_excludes_callers_own_thread(self):
        # The caller's own thread entry (this round's already-advanced read)
        # must NOT be returned — else read_drop collapses to 0 and the fix is a
        # no-op. Only a DIFFERENT-thread (prior-turn) sibling counts.
        from lib.tasks_pkg.cache_tracking import get_prev_turn_cache_read
        self_tid = self._threading.get_ident()
        # Self thread: this round's read (would be the no-op trap).
        self._put(self_tid, cache_read=79200, update_time=200.0)
        # No sibling yet → excluding self leaves nothing.
        self.assertEqual(get_prev_turn_cache_read(self._conv), 0)
        # Add a prior-turn sibling on a different thread.
        self._put(self_tid + 1, cache_read=118000, update_time=100.0)
        self.assertEqual(get_prev_turn_cache_read(self._conv), 118000)

    def test_picks_most_recent_sibling(self):
        from lib.tasks_pkg.cache_tracking import get_prev_turn_cache_read
        self_tid = self._threading.get_ident()
        self._put(self_tid + 1, cache_read=50000, update_time=100.0)   # older
        self._put(self_tid + 2, cache_read=118000, update_time=300.0)  # newest
        self._put(self_tid + 3, cache_read=90000, update_time=200.0)
        self.assertEqual(get_prev_turn_cache_read(self._conv), 118000)

    def test_ignores_cold_call_count_zero_siblings(self):
        # A sibling entry that never completed a round (call_count==0) carries
        # no real baseline — skip it even if it's the most recent.
        from lib.tasks_pkg.cache_tracking import get_prev_turn_cache_read
        self_tid = self._threading.get_ident()
        self._put(self_tid + 1, cache_read=118000, update_time=100.0, call_count=1)
        self._put(self_tid + 2, cache_read=5, update_time=999.0, call_count=0)
        self.assertEqual(get_prev_turn_cache_read(self._conv), 118000)


if __name__ == '__main__':
    unittest.main()
