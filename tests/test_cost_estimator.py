"""tests/test_cost_estimator.py — Token-cost estimation + budget gate."""

from __future__ import annotations

import unittest

import pytest

from lib.cost_estimator import _split_tokens, check_budget, estimate_usage_cost

pytestmark = pytest.mark.unit


class TestSplitTokens(unittest.TestCase):

    def test_anthropic_shape(self):
        # input_tokens excludes cache hits.
        u = {'input_tokens': 100,
             'cache_creation_input_tokens': 50,
             'cache_read_input_tokens': 200,
             'output_tokens': 75}
        self.assertEqual(_split_tokens(u), (100, 50, 200, 75))

    def test_openai_shape_input_includes_cache(self):
        # OpenAI: prompt_tokens already INCLUDES cached tokens.  The
        # split logic should subtract cache from input.
        u = {'prompt_tokens': 350,
             'cache_read_tokens': 200,
             'cache_write_tokens': 50,
             'completion_tokens': 75}
        # 350 > (200+50) → OpenAI path → uncached = 350 - 250 = 100.
        self.assertEqual(_split_tokens(u), (100, 50, 200, 75))

    def test_no_cache(self):
        u = {'prompt_tokens': 1000, 'completion_tokens': 500}
        self.assertEqual(_split_tokens(u), (1000, 0, 0, 500))

    def test_empty(self):
        self.assertEqual(_split_tokens({}), (0, 0, 0, 0))
        self.assertEqual(_split_tokens(None), (0, 0, 0, 0))


class TestEstimateUsageCost(unittest.TestCase):

    def test_unknown_model_zero(self):
        self.assertEqual(
            estimate_usage_cost({'input_tokens': 1000}, 'unknown-model'),
            0.0)

    def test_empty_inputs(self):
        self.assertEqual(estimate_usage_cost(None, 'claude-opus-4-7'), 0.0)
        self.assertEqual(estimate_usage_cost({}, 'claude-opus-4-7'), 0.0)
        self.assertEqual(estimate_usage_cost({'input_tokens': 100}, ''), 0.0)

    def test_claude_opus_pricing(self):
        # claude-opus-4-7: input $5/1M, output $25/1M, cacheRead 0.10x, cacheWrite 1.25x
        # 1M input + 1M output = $5 + $25 = $30
        u = {'input_tokens': 1_000_000, 'output_tokens': 1_000_000}
        cost = estimate_usage_cost(u, 'claude-opus-4-7')
        self.assertAlmostEqual(cost, 30.0, places=4)

    def test_claude_cache_discount_applied(self):
        # 1M cache reads at 0.10x of $5/1M = $0.50
        u = {'input_tokens': 0,
             'cache_read_input_tokens': 1_000_000,
             'output_tokens': 0}
        cost = estimate_usage_cost(u, 'claude-opus-4-7')
        self.assertAlmostEqual(cost, 0.50, places=4)

    def test_cache_write_premium(self):
        # 1M cache writes at 1.25x of $5/1M = $6.25
        u = {'input_tokens': 0,
             'cache_creation_input_tokens': 1_000_000,
             'output_tokens': 0}
        cost = estimate_usage_cost(u, 'claude-opus-4-7')
        self.assertAlmostEqual(cost, 6.25, places=4)


class TestCheckBudget(unittest.TestCase):

    def test_disabled_when_zero(self):
        exceeded, cost, reason = check_budget(
            {'id': 't1'}, {'input_tokens': 10_000_000},
            'claude-opus-4-7', max_budget_usd=0,
        )
        self.assertFalse(exceeded)
        self.assertEqual(cost, 0.0)
        self.assertEqual(reason, '')

    def test_disabled_when_negative(self):
        exceeded, *_ = check_budget(
            {'id': 't1'}, {'input_tokens': 10_000_000},
            'claude-opus-4-7', max_budget_usd=-1.0,
        )
        self.assertFalse(exceeded)

    def test_under_budget(self):
        # 100k input → $0.50
        exceeded, cost, reason = check_budget(
            {'id': 't1'},
            {'input_tokens': 100_000, 'output_tokens': 0},
            'claude-opus-4-7', max_budget_usd=1.0,
        )
        self.assertFalse(exceeded)
        self.assertAlmostEqual(cost, 0.5, places=4)
        self.assertEqual(reason, '')

    def test_over_budget(self):
        # 1M input + 1M output = $30 > $1
        exceeded, cost, reason = check_budget(
            {'id': 't1'},
            {'input_tokens': 1_000_000, 'output_tokens': 1_000_000},
            'claude-opus-4-7', max_budget_usd=1.0,
        )
        self.assertTrue(exceeded)
        self.assertAlmostEqual(cost, 30.0, places=4)
        self.assertIn('max_budget_usd', reason)
        self.assertIn('$30.0000', reason)

    def test_unknown_model_never_blocks(self):
        # No pricing data → cost is 0 → never exceeds.
        exceeded, cost, _ = check_budget(
            {'id': 't1'},
            {'input_tokens': 100_000_000},
            'unknown-model-xyz', max_budget_usd=0.01,
        )
        self.assertFalse(exceeded)
        self.assertEqual(cost, 0.0)


class TestTofuOptionsBudgetField(unittest.TestCase):

    def test_max_budget_usd_in_options(self):
        from lib.agent_options import TofuOptions
        opts = TofuOptions.from_cfg({'maxBudgetUsd': 5.0})
        self.assertEqual(opts.max_budget_usd, 5.0)
        # Round-trips through to_cfg
        self.assertEqual(opts.to_cfg().get('maxBudgetUsd'), 5.0)

    def test_max_budget_usd_in_openapi_schema(self):
        from lib.agent_options import TofuOptions
        schema = TofuOptions.openapi_schema()
        self.assertIn('maxBudgetUsd', schema['properties'])
        self.assertEqual(schema['properties']['maxBudgetUsd']['type'], 'number')


if __name__ == '__main__':
    unittest.main()
