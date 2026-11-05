"""Declared cache rates survive context-tier resolution.

A price fact may be split across tables: ``MODEL_PRICING`` declares a model's
cache multipliers, while the legacy CNY table (``QWEN_PRICING_CNY``) declares
only per-context input/output tiers. Resolution must never replace a declared
rate with a value no source declared: hy4-preview's documented ¥0.3 cache hit
(5% of ¥6 input) was billed as free because the tier translator wrote a
``cacheReadMul`` of 0.0 over the model row.
"""

from __future__ import annotations

import pytest

from lib.pricing import MODEL_PRICING, QWEN_PRICING_CNY, build_rate_card, lookup_pricing

pytestmark = pytest.mark.unit

# Documented resolver defaults for a rate no source declares.
_DEFAULTS = {'cacheReadMul': 0.10, 'cacheWriteMul': 1.25}
_TIERED = sorted(set(QWEN_PRICING_CNY) - {'_default'})


def _declared(model_id: str, field: str) -> float:
    for source in (QWEN_PRICING_CNY.get(model_id) or {}, MODEL_PRICING.get(model_id) or {}):
        if field in source:
            return float(source[field])
    return _DEFAULTS[field]


@pytest.mark.parametrize('model_id', _TIERED)
def test_every_context_tier_keeps_the_declared_cache_rates(model_id):
    tiers = lookup_pricing(model_id, prompt_tokens=0)['contextTiers']
    assert tiers
    for tier in tiers:
        resolved = lookup_pricing(model_id, prompt_tokens=tier['maxPromptTokens'])
        assert resolved['selectedTier']['id'] == tier['id']
        for field in _DEFAULTS:
            assert resolved[field] == pytest.approx(_declared(model_id, field)), (model_id, tier['id'], field)


def test_rate_card_tiers_show_the_declared_cache_rates():
    card = build_rate_card()['models']
    for model_id in _TIERED:
        for tier in card[model_id]['contextTiers']:
            for field in _DEFAULTS:
                assert tier[field] == pytest.approx(_declared(model_id, field)), (model_id, field)


def test_hy4_preview_cache_hits_cost_the_documented_rate():
    from lib.cost import compute_cost

    row = lookup_pricing('hy4-preview', prompt_tokens=1_000_000)
    assert (row['currency'], row['input'], row['cacheReadMul']) == ('CNY', 6.0, 0.05)
    cost = compute_cost({'prompt_tokens': 1_000_000, 'completion_tokens': 0,
                         'cache_read_input_tokens': 1_000_000}, model_id='hy4-preview')
    assert cost['cacheReadCostCny'] == pytest.approx(0.3)


def test_unpriced_family_estimate_uses_its_declared_rates_not_free_cache_reads():
    row = lookup_pricing('qwen-unlisted-test', prompt_tokens=0)
    assert row['_pricingSource'] == 'qwen_default_estimate'
    assert row['cacheWriteMul'] == pytest.approx(QWEN_PRICING_CNY['_default']['cacheWriteMul'])
    assert row['cacheReadMul'] == pytest.approx(_DEFAULTS['cacheReadMul'])
