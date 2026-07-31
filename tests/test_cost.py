"""tests/test_cost.py — port-parity tests for lib.cost.compute_cost."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from lib.cost import compute_cost


def _patch_pricing(input_price=15.0, output_price=75.0,
                    cache_write_mul=1.25, cache_read_mul=0.10,
                    rate=7.24):
    return patch('lib.cost.get_pricing_data', return_value={
        'inputPrice': input_price, 'outputPrice': output_price,
        'cacheWriteMul': cache_write_mul, 'cacheReadMul': cache_read_mul,
        'usdToCny': rate,
    })


class ComputeCostTest(unittest.TestCase):

    def test_empty_usage_returns_none(self):
        self.assertIsNone(compute_cost(None))
        self.assertIsNone(compute_cost({}))
        self.assertIsNone(compute_cost('not a dict'))  # type: ignore[arg-type]

    def test_all_zero_returns_none(self):
        self.assertIsNone(compute_cost({
            'prompt_tokens': 0, 'completion_tokens': 0,
        }))

    def test_basic_openai_cost(self):
        with _patch_pricing(input_price=2.5, output_price=10.0):
            r = compute_cost(
                {'prompt_tokens': 1000, 'completion_tokens': 500},
                model_id='gpt-4o',
            )
        # MODEL_PRICING has gpt-4o at 2.5/10.0 — matches our patch.
        # input cost = 1000 * 2.5 / 1e6 = 0.0025; output = 500 * 10 / 1e6 = 0.005
        # total = 0.0075 USD
        self.assertAlmostEqual(r['costUsd'], 0.0075, places=4)
        self.assertEqual(r['inputTokens'], 1000)
        self.assertEqual(r['outputTokens'], 500)
        self.assertEqual(r['cacheWriteTokens'], 0)
        self.assertEqual(r['cacheReadTokens'], 0)

    def test_anthropic_cache_convention(self):
        # Anthropic: prompt_tokens (12) is just the uncached residual,
        # cache_creation_input_tokens=200, cache_read_input_tokens=800.
        # Total input = 12 + 200 + 800 = 1012.
        # Claude opus pricing $5/M input, write_mul=1.25, read_mul=0.10.
        r = compute_cost({
            'prompt_tokens': 12,
            'cache_creation_input_tokens': 200,
            'cache_read_input_tokens': 800,
            'completion_tokens': 100,
        }, model_id='claude-opus-4-7')
        # Uncached = 12, total_input = 1012.
        self.assertEqual(r['inputTokens'], 12)
        self.assertEqual(r['totalInputTokens'], 1012)
        self.assertEqual(r['cacheWriteTokens'], 200)
        self.assertEqual(r['cacheReadTokens'], 800)
        # Cache savings should be positive (cache is cheaper than full).
        self.assertGreater(r['cacheSavingsUsd'], 0)

    def test_openai_cache_convention(self):
        # OpenAI: prompt_tokens=1000 is the TOTAL.
        # cache_read=300 means 300 of those 1000 came from cache.
        # Uncached = 1000 - 0 - 300 = 700, total_input = 1000.
        r = compute_cost({
            'prompt_tokens': 1000,
            'cache_read_tokens': 300,
            'completion_tokens': 50,
        }, model_id='gpt-4o')
        self.assertEqual(r['inputTokens'], 700)
        self.assertEqual(r['totalInputTokens'], 1000)
        self.assertEqual(r['cacheReadTokens'], 300)

    def test_thinking_tokens_promoted_to_output_when_zero(self):
        r = compute_cost({
            'prompt_tokens': 100,
            'completion_tokens': 0,
            'reasoning_tokens': 500,
        }, model_id='gpt-4o')
        # JS impl: when out=0 and think_tok>0, out := think_tok.
        self.assertEqual(r['outputTokens'], 500)
        self.assertEqual(r['thinkingTokens'], 500)

    def test_qwen_uses_cny_tiered(self):
        # qwen-plus tiers: input  [(128K, 0.8), (256K, 2.4), (1M, 4.8)]
        #                  output [(128K, 2.0), (256K, 20.0), (1M, 48.0)]
        # 100K tokens fits tier 1 (≤128K threshold).
        r = compute_cost({
            'prompt_tokens': 100_000,
            'completion_tokens': 100_000,
        }, model_id='qwen-plus')
        # Input  = 100K × 0.8/M = 0.08 CNY (tier 1)
        # Output = 100K × 2.0/M = 0.20 CNY (tier 1)
        # Total = 0.28 CNY.
        self.assertAlmostEqual(r['costCny'], 0.28, places=2)
        self.assertEqual(r['cacheWriteCostCny'], 0.0)
        self.assertEqual(r['cacheReadCostCny'], 0.0)

    def test_qwen_high_tier(self):
        # 1M tokens → tier 3 rates: input 4.8/M, output 48/M.
        # Total = 4.8 + 48 = 52.8 CNY.
        r = compute_cost({
            'prompt_tokens': 1_000_000,
            'completion_tokens': 1_000_000,
        }, model_id='qwen-plus')
        self.assertAlmostEqual(r['costCny'], 52.8, places=1)

    def test_qwen_tier_kicks_in_above_threshold(self):
        # qwen3.5-flash tier 1 ≤128K: 0.2 CNY/M input.
        # Tier 2 ≤256K: 0.8 CNY/M input.
        # 200K input tokens should price at the tier-2 rate.
        r = compute_cost({
            'prompt_tokens': 200_000,
            'completion_tokens': 0,
        }, model_id='qwen3.5-flash')
        # At tier 2: 200K * 0.8/M = 0.16 CNY.
        self.assertAlmostEqual(r['inputCostCny'], 0.16, places=4)

    def test_unknown_model_uses_pricing_data_defaults(self):
        with _patch_pricing(input_price=1.0, output_price=5.0):
            r = compute_cost({
                'prompt_tokens': 1_000_000,
                'completion_tokens': 0,
            }, model_id='no-such-model-anywhere')
        self.assertAlmostEqual(r['costUsd'], 1.0, places=4)

    def test_returns_full_keys(self):
        r = compute_cost({'prompt_tokens': 100, 'completion_tokens': 50},
                          model_id='gpt-4o')
        for key in ('costUsd', 'costCny', 'inputTokens', 'outputTokens',
                     'cacheWriteTokens', 'cacheReadTokens', 'thinkingTokens',
                     'inputCostCny', 'outputCostCny', 'cacheWriteCostCny',
                     'cacheReadCostCny', 'cacheSavingsCny', 'cacheSavingsUsd'):
            self.assertIn(key, r, f'missing key: {key}')

    def test_provider_override_beats_global(self):
        from lib.pricing import set_provider_pricing, clear_provider_pricing
        set_provider_pricing('test-provider', 'shared-model', {
            'input': 100.0, 'output': 200.0,
            'cacheWriteMul': 1.0, 'cacheReadMul': 0.0,
        })
        try:
            r = compute_cost(
                {'prompt_tokens': 1_000_000, 'completion_tokens': 0},
                model_id='shared-model',
                provider_id='test-provider',
            )
            # 1M tokens × $100/M = $100.
            self.assertAlmostEqual(r['costUsd'], 100.0, places=2)
        finally:
            clear_provider_pricing('test-provider')


class NormalizeUsageTest(unittest.TestCase):
    """Pure key-aliasing helper — no cache-convention math."""

    def test_openai_keys(self):
        from lib.cost import normalize_usage
        u = normalize_usage({'prompt_tokens': 10, 'completion_tokens': 5})
        self.assertEqual(u['input'], 10)
        self.assertEqual(u['output'], 5)

    def test_anthropic_keys(self):
        from lib.cost import normalize_usage
        u = normalize_usage({
            'input_tokens': 7, 'output_tokens': 3,
            'cache_creation_input_tokens': 20,
            'cache_read_input_tokens': 40,
            'thinking_tokens': 9,
        })
        self.assertEqual(u['input'], 7)
        self.assertEqual(u['output'], 3)
        self.assertEqual(u['cache_write'], 20)
        self.assertEqual(u['cache_read'], 40)
        self.assertEqual(u['thinking'], 9)

    def test_null_and_nondict(self):
        from lib.cost import normalize_usage
        for bad in (None, {}, 'x', 123):
            u = normalize_usage(bad)  # type: ignore[arg-type]
            self.assertEqual(u, {'input': 0, 'output': 0, 'cache_write': 0,
                                 'cache_read': 0, 'thinking': 0})

    def test_string_values_coerced(self):
        from lib.cost import normalize_usage
        u = normalize_usage({'prompt_tokens': '15', 'completion_tokens': None})
        self.assertEqual(u['input'], 15)
        self.assertEqual(u['output'], 0)

    def test_openai_key_wins_when_both_present(self):
        # A dict carries ONE convention, but if both are present the primary
        # (OpenAI) key is read first — matches the legacy `a or b` order.
        from lib.cost import normalize_usage
        u = normalize_usage({'prompt_tokens': 100, 'input_tokens': 999})
        self.assertEqual(u['input'], 100)

    def test_matches_legacy_inline_expression(self):
        """Parity: normalize_usage must equal the old inline
        ``int(usage.get('prompt_tokens') or usage.get('input_tokens') or 0)``
        for representative usage dicts (both conventions)."""
        from lib.cost import normalize_usage
        samples = [
            {'prompt_tokens': 10, 'completion_tokens': 5},
            {'input_tokens': 7, 'output_tokens': 3},
            {'input_tokens': 0, 'output_tokens': 0},
            {},
        ]
        for s in samples:
            legacy_in = int(s.get('prompt_tokens') or s.get('input_tokens') or 0)
            legacy_out = int(s.get('completion_tokens')
                             or s.get('output_tokens') or 0)
            u = normalize_usage(s)
            self.assertEqual(u['input'], legacy_in, s)
            self.assertEqual(u['output'], legacy_out, s)

    def test_neuter_alias_table_breaks_anthropic_read(self):
        """Double-neuter: dropping the Anthropic alias makes an Anthropic-shape
        usage dict read as 0 input — proving the fallback key is load-bearing.
        """
        import lib.cost as cost_mod
        original = cost_mod._USAGE_KEY_ALIASES
        anthropic = {'input_tokens': 42, 'output_tokens': 8}
        # Baseline: reads correctly.
        self.assertEqual(cost_mod.normalize_usage(anthropic)['input'], 42)
        try:
            # NEUTER: only the OpenAI key remains for 'input'.
            cost_mod._USAGE_KEY_ALIASES = dict(original)
            cost_mod._USAGE_KEY_ALIASES['input'] = ('prompt_tokens',)
            self.assertEqual(
                cost_mod.normalize_usage(anthropic)['input'], 0,
                'neutered alias table should fail to read input_tokens')
        finally:
            cost_mod._USAGE_KEY_ALIASES = original
        # Restored: reads correctly again.
        self.assertEqual(cost_mod.normalize_usage(anthropic)['input'], 42)


# The exact usage payload the sankuai gateway returned for kimi-k3 on a warm
# (second, byte-identical) request — probed live 2026-07-24. Note the hit is
# reported as cached_tokens / prompt_tokens_details.cached_tokens /
# effectiveCachedTokens while the canonical cache_read_tokens is pinned at 0.
KIMI_WARM_USAGE = {
    'effectiveCachedTokens': 3328,
    'completion_tokens': 16,
    'prompt_tokens': 3367,
    'total_tokens': 3383,
    'completion_tokens_details': {'reasoning_tokens': 13},
    'prompt_tokens_details': {'cached_tokens': 3328, 'audio_tokens': 0,
                              'image_tokens': 0, 'video_tokens': 0,
                              'text_tokens': 0},
    'cache_write_tokens': 0,
    'cache_read_tokens': 0,
    'input_tokens': 0,
    'output_tokens': 0,
    'output_tokens_details': None,
    'cached_tokens': 3328,
}


class MultiVendorUsageTest(unittest.TestCase):
    """normalize_usage must read cache/thinking under every probed vendor
    spelling — not just the two canonical ones."""

    def test_kimi_gateway_shape(self):
        from lib.cost import normalize_usage
        u = normalize_usage(dict(KIMI_WARM_USAGE))
        self.assertEqual(u['input'], 3367)
        self.assertEqual(u['output'], 16)
        self.assertEqual(u['cache_read'], 3328)
        self.assertEqual(u['cache_write'], 0)
        self.assertEqual(u['thinking'], 13)  # nested completion_tokens_details

    def test_openai_nested_cached_tokens(self):
        from lib.cost import normalize_usage
        u = normalize_usage({'prompt_tokens': 1000, 'completion_tokens': 50,
                             'prompt_tokens_details': {'cached_tokens': 400}})
        self.assertEqual(u['cache_read'], 400)

    def test_deepseek_shape(self):
        from lib.cost import normalize_usage
        u = normalize_usage({'prompt_tokens': 1000, 'completion_tokens': 50,
                             'prompt_cache_hit_tokens': 700,
                             'prompt_cache_miss_tokens': 300})
        self.assertEqual(u['cache_read'], 700)

    def test_gemini_shape(self):
        from lib.cost import normalize_usage
        u = normalize_usage({'prompt_tokens': 1000, 'completion_tokens': 50,
                             'cached_content_token_count': 600})
        self.assertEqual(u['cache_read'], 600)

    def test_explicit_zero_canonical_does_not_shadow_vendor(self):
        # The gateway pins cache_read_tokens=0 while reporting the real hit as
        # cached_tokens — the falsy canonical key must fall through, not win.
        from lib.cost import normalize_usage
        u = normalize_usage({'cache_read_tokens': 0, 'cached_tokens': 900,
                             'prompt_tokens': 1000})
        self.assertEqual(u['cache_read'], 900)

    def test_no_double_count_when_flat_and_nested_agree(self):
        # cached_tokens and prompt_tokens_details.cached_tokens are the SAME
        # number reported twice — first truthy (flat) wins, never summed.
        from lib.cost import normalize_usage
        u = normalize_usage({'cached_tokens': 500,
                             'prompt_tokens_details': {'cached_tokens': 500}})
        self.assertEqual(u['cache_read'], 500)

    def test_canonical_spelling_still_wins(self):
        from lib.cost import normalize_usage
        u = normalize_usage({'cache_read_input_tokens': 800,
                             'cached_tokens': 999})
        self.assertEqual(u['cache_read'], 800)

    def test_neuter_vendor_aliases_blinds_kimi_hit(self):
        """NEUTER: stripping the vendor aliases + nested table must drop the
        kimi hit back to 0 — proving they (not luck) carry the read."""
        import lib.cost as cost_mod
        orig_flat = cost_mod._USAGE_KEY_ALIASES
        orig_nested = cost_mod._USAGE_NESTED_ALIASES
        self.assertEqual(cost_mod.normalize_usage(dict(KIMI_WARM_USAGE))['cache_read'],
                         3328)
        try:
            cost_mod._USAGE_KEY_ALIASES = dict(orig_flat)
            cost_mod._USAGE_KEY_ALIASES['cache_read'] = (
                'cache_read_tokens', 'cache_read_input_tokens')
            cost_mod._USAGE_NESTED_ALIASES = {}
            self.assertEqual(
                cost_mod.normalize_usage(dict(KIMI_WARM_USAGE))['cache_read'], 0,
                'neutered alias tables should miss the kimi cache hit')
        finally:
            cost_mod._USAGE_KEY_ALIASES = orig_flat
            cost_mod._USAGE_NESTED_ALIASES = orig_nested
        self.assertEqual(cost_mod.normalize_usage(dict(KIMI_WARM_USAGE))['cache_read'],
                         3328)


class CanonicalizeUsageCacheKeysTest(unittest.TestCase):

    def test_stamps_kimi_hit(self):
        from lib.cost import canonicalize_usage_cache_keys
        u = dict(KIMI_WARM_USAGE)
        out = canonicalize_usage_cache_keys(u)
        self.assertIs(out, u)  # in-place, same dict returned
        self.assertEqual(u['cache_read_tokens'], 3328)
        self.assertNotIn('cache_write_tokens_set', u)
        self.assertEqual(u['cache_write_tokens'], 0)  # untouched (no write)

    def test_idempotent(self):
        from lib.cost import canonicalize_usage_cache_keys
        u = dict(KIMI_WARM_USAGE)
        canonicalize_usage_cache_keys(u)
        snapshot = dict(u)
        canonicalize_usage_cache_keys(u)
        self.assertEqual(u, snapshot)

    def test_anthropic_untouched(self):
        from lib.cost import canonicalize_usage_cache_keys
        u = {'input_tokens': 12, 'cache_creation_input_tokens': 200,
             'cache_read_input_tokens': 800, 'cached_tokens': 999}
        canonicalize_usage_cache_keys(u)
        self.assertNotIn('cache_read_tokens', u)
        self.assertNotIn('cache_write_tokens', u)

    def test_nondict_passthrough(self):
        from lib.cost import canonicalize_usage_cache_keys
        self.assertIsNone(canonicalize_usage_cache_keys(None))
        self.assertEqual(canonicalize_usage_cache_keys('x'), 'x')  # type: ignore[arg-type]


class KimiComputeCostTest(unittest.TestCase):
    """End-to-end: the kimi warm-round payload must bill the cached portion at
    the kimi-k3 cacheReadMul (0.10), not full input price."""

    def test_kimi_warm_round_cost(self):
        r = compute_cost(dict(KIMI_WARM_USAGE), model_id='kimi-k3')
        # OpenAI convention: prompt_tokens (3367) includes the 3328 cached.
        self.assertEqual(r['inputTokens'], 39)
        self.assertEqual(r['totalInputTokens'], 3367)
        self.assertEqual(r['cacheReadTokens'], 3328)
        # kimi-k3 pricing: input $2.76/M, cacheReadMul 0.10 → the cached share
        # costs 3328 * 2.76 * 0.10 / 1e6 ≈ $0.00092 instead of ≈ $0.00919.
        self.assertAlmostEqual(r['cacheReadCostUsd'],
                               3328 * 2.76 * 0.10 / 1e6, places=6)
        self.assertGreater(r['cacheSavingsUsd'], 0.008)

    def test_migrated_consumers_see_kimi_hit(self):
        """The migrated direct-read sites must observe the kimi cache hit."""
        from lib.tasks_pkg.floor_retry import _cache_tokens
        self.assertEqual(_cache_tokens(dict(KIMI_WARM_USAGE)), (3328, 0))

        from lib.llm_dispatch.cache_settle import is_cold_write
        # kimi: read-only hit, no write → never a cold write.
        self.assertFalse(is_cold_write(dict(KIMI_WARM_USAGE)))
        # Regression guard for the migration: a genuine Anthropic cold write
        # (big creation, negligible read) must still be detected.
        self.assertTrue(is_cold_write({'cache_creation_input_tokens': 50000,
                                       'cache_read_input_tokens': 0}))


if __name__ == '__main__':
    unittest.main()
