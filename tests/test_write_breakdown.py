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

    def test_overshoot_components_capped_sum_still_exact(self):
        # tools 970 + prev 700 = 1670 > write 1000. Must NOT print numbers that
        # exceed the total: toolResults=970, prevOutput=min(700,30)=30, env=0.
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
        self.assertEqual(wb['toolResults'], 970)
        self.assertEqual(wb['prevOutput'], 30)
        self.assertEqual(wb['envelope'], 0)
        self.assertTrue(wb['capped'])
        self.assertEqual(_sum(wb), wb['write'])

    def test_tool_results_alone_exceed_write(self):
        wb = _compute_write_breakdown(
            {'toolRounds': [{'llmRound': 0, 'toolTokens': 1500}]},
            [
                {'round': 1, 'usage': {'completion_tokens': 700}},
                {'round': 2, 'usage': {'cache_write_tokens': 1000}},
            ],
            1,
        )
        self.assertEqual(wb['toolResults'], 1000)
        self.assertEqual(wb['prevOutput'], 0)
        self.assertEqual(wb['envelope'], 0)
        self.assertTrue(wb['capped'])
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


if __name__ == '__main__':
    unittest.main()
