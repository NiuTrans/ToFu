from __future__ import annotations

import pytest

from lib.billing.cost import MICRO_PER_USD, compute_request_cost
from lib.cost import compute_cost
from lib.pricing import build_rate_card, clear_provider_pricing, set_provider_pricing

pytestmark = pytest.mark.unit


def _display_micro(cost):
    return round(sum(cost[k] for k in (
        'inputCostUsd', 'outputCostUsd', 'cacheWriteCostUsd',
        'cacheReadCostUsd')) * MICRO_PER_USD)


def test_complete_prompt_selects_one_qwen_tier_for_every_component():
    usage = {
        'input_tokens': 10_000, 'output_tokens': 2_000,
        'cache_creation_input_tokens': 100_000,
        'cache_read_input_tokens': 100_000,
    }
    cost = compute_cost(usage, model_id='qwen3.5-plus')
    snap = cost['pricingSnapshot']
    assert snap['selectedPromptTokens'] == 210_000
    assert snap['tierId'] == 'ctx_256000'
    assert snap['rates']['input'] == 2.0
    assert snap['rates']['output'] == 12.0
    assert cost['cacheWriteCostCny'] == pytest.approx(0.2, abs=1e-6)
    assert cost['cacheReadCostCny'] == 0.0


def test_gpt56_long_context_multiplier_applies_to_the_whole_request():
    usage = {
        'prompt_tokens': 200_001,
        'completion_tokens': 1_000,
        'cache_creation_input_tokens': 36_000,
        'cache_read_input_tokens': 36_000,
    }
    cost = compute_cost(usage, model_id='gpt-5.6-sol')
    snap = cost['pricingSnapshot']
    assert snap['selectedPromptTokens'] == 272_001
    assert snap['tierId'] == 'ctx_gt_272000'
    assert snap['rates'] == {
        'input': 10.0,
        'output': 45.0,
        'cacheWriteMul': 1.25,
        'cacheReadMul': 0.10,
    }
    assert cost['inputCostUsd'] == pytest.approx(2.00001)
    assert cost['outputCostUsd'] == pytest.approx(0.045)
    assert cost['cacheWriteCostUsd'] == pytest.approx(0.45)
    assert cost['cacheReadCostUsd'] == pytest.approx(0.036)


def test_provider_context_tier_override_wins_and_uses_total_prompt():
    set_provider_pricing('tier-provider', 'tier-model', {
        'contextTiers': [
            {'id': 'small', 'maxPromptTokens': 100, 'input': 1,
             'output': 2, 'cacheReadMul': .5, 'cacheWriteMul': 1},
            {'id': 'large', 'maxPromptTokens': 1000, 'input': 10,
             'output': 20, 'cacheReadMul': .25, 'cacheWriteMul': 1.5},
        ]})
    try:
        usage = {'input_tokens': 50, 'output_tokens': 10,
                 'cache_read_input_tokens': 100,
                 'cache_creation_input_tokens': 100}
        cost = compute_cost(usage, model_id='tier-model',
                            provider_id='tier-provider')
        snap = cost['pricingSnapshot']
        assert snap['source'] == 'provider_override'
        assert snap['selectedPromptTokens'] == 250
        assert snap['tierId'] == 'large'
        assert snap['rates']['input'] == 10
        assert snap['rates']['output'] == 20
    finally:
        clear_provider_pricing('tier-provider')


def test_billing_adapter_preserves_raw_usage_and_matches_display():
    usage = {'prompt_tokens': 250, 'completion_tokens': 10,
             'cache_read_tokens': 200}
    set_provider_pricing('raw-provider', 'raw-model', {
        'contextTiers': [
            {'id': 'small', 'maxPromptTokens': 100, 'input': 1, 'output': 2},
            {'id': 'large', 'maxPromptTokens': 1000, 'input': 4,
             'output': 8, 'cacheReadMul': .5},
        ]})
    try:
        display = compute_cost(usage, 'raw-model', 'raw-provider')
        bill = compute_request_cost('raw-model', provider_id='raw-provider',
                                    raw_usage=usage, margin=0)
        assert bill.base_micro == _display_micro(display)
        assert bill.snapshot['selectedPromptTokens'] == 250
        assert bill.snapshot['tierId'] == 'large'
        assert bill.snapshot['totalMicro'] == bill.micro
    finally:
        clear_provider_pricing('raw-provider')


def test_rate_card_is_authoritative_and_exposes_flat_and_tier_rows():
    card = build_rate_card()
    assert card['models']['gpt-4o']['kind'] == 'flat'
    qwen = card['models']['qwen3.5-plus']
    assert qwen['kind'] == 'tiered'
    assert [t['maxPromptTokens'] for t in qwen['contextTiers']] == [
        128_000, 256_000, 1_000_000]
    assert 'input_per_mtok_micro' not in qwen


def test_unknown_model_default_estimate_is_preserved():
    cost = compute_cost({'prompt_tokens': 100, 'completion_tokens': 10},
                        model_id='unknown-contract-model')
    assert cost['pricingSource'] == 'default_estimate'
    assert cost['pricingSnapshot']['tierId'] is None
    assert cost['costUsd'] > 0
