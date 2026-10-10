"""Own-route summarizer spend and warm-cache ROI, without network/model calls."""
from __future__ import annotations

from copy import deepcopy
import math

import pytest

from lib.tasks_pkg.compaction._cache_economics import _proactive_cache_economics
from lib.tasks_pkg.compaction._summary_cost import price_summary_call, summary_cost_details

pytestmark = pytest.mark.unit

PROVIDER = 'unit-summary-economics'
MODEL = 'unit-summary-model'
RATES = {'input': 2, 'output': 10, 'cacheReadMul': .1, 'cacheWriteMul': 1.25}


@pytest.fixture
def prices(monkeypatch):
    import lib.cost as cost
    import lib.pricing as pricing

    monkeypatch.setitem(pricing.PROVIDER_PRICING, PROVIDER, {})
    data = {'usdToCny': 7, 'inputPrice': 99, 'outputPrice': 999}
    monkeypatch.setattr(pricing, 'get_pricing_data', lambda: dict(data))
    monkeypatch.setattr(cost, 'get_pricing_data', lambda: dict(data))
    pricing.set_provider_pricing(PROVIDER, MODEL, dict(RATES))
    return pricing, data


def _usage(**extra):
    return {'prompt_tokens': 10_000, 'completion_tokens': 1_000, **extra}


def _task(cache_read=100_000):
    return {'id': 'economics-task', 'model': MODEL, 'provider_id': PROVIDER,
            '_compactionCacheObservation': {
                'taskId': 'economics-task', 'model': MODEL,
                'providerId': PROVIDER, 'cacheReadTokens': cache_read}}


def _economics(**kwargs):
    return _proactive_cache_economics(
        _task(), tokens_before=120_000, candidate_tokens=20_000, **kwargs)


@pytest.mark.parametrize('usage', [
    _usage(cache_read_tokens=6_000, cache_write_tokens=1_000),
    _usage(cache_read_tokens=0, cached_tokens=6_000, cache_write_tokens=1_000),
    _usage(prompt_tokens_details={'cached_tokens': 6_000}, cache_write_tokens=1_000),
    {'input_tokens': 3_000, 'output_tokens': 1_000,
     'cache_read_input_tokens': 6_000, 'cache_creation_input_tokens': 1_000},
    {'prompt_tokens': 10_000, 'input_tokens': 3_000, 'output_tokens': 1_000,
     'cache_read_input_tokens': 6_000, 'cache_creation_input_tokens': 1_000},
])
def test_summary_counts_input_output_and_both_cache_conventions(prices, usage):
    original = deepcopy(usage)
    result = price_summary_call(usage, model=MODEL, provider_id=PROVIDER)
    # 3k uncached * $2 + 1k output * $10 + 6k read * $.2 + 1k write * $2.5.
    assert result['costUsd'] == pytest.approx(.0197)
    assert result['known'] is True
    assert result['costBasis'] == 'metered_api_equivalent'
    assert result['pricingSource'] == 'provider_override'
    assert result['pricingSnapshot']['selectedPromptTokens'] == 10_000
    assert result['pricingSnapshot']['model'] == MODEL
    assert result['pricingSnapshot']['provider'] == PROVIDER
    assert usage == original


@pytest.mark.parametrize(('prompt', 'tier', 'expected'), [
    (20_000, 'base', .086), (20_001, 'premium', .244004),
])
def test_full_prompt_selects_summary_tier_for_every_component(prices, prompt, tier, expected):
    pricing, _ = prices
    pricing.set_provider_pricing(PROVIDER, MODEL, {'contextTiers': [
        {'id': 'base', 'maxPromptTokens': 200_000, **RATES},
        {'id': 'premium', 'maxPromptTokens': 1_000_000,
         'input': 4, 'output': 20, 'cacheReadMul': .2, 'cacheWriteMul': 1.5},
    ]})
    result = price_summary_call(
        {'input_tokens': prompt, 'output_tokens': 1_000,
         'cache_read_input_tokens': 180_000}, model=MODEL, provider_id=PROVIDER)
    assert result['costUsd'] == pytest.approx(expected)
    assert result['pricingSnapshot']['tierId'] == tier
    assert result['pricingSnapshot']['selectedPromptTokens'] == prompt + 180_000


def test_cny_summary_retains_currency_exchange_and_provider_provenance(prices):
    pricing, _ = prices
    pricing.set_provider_pricing(PROVIDER, MODEL, {
        **RATES, 'input': 14, 'output': 70, 'currency': 'CNY'})
    result = price_summary_call(_usage(), model=MODEL, provider_id=PROVIDER)
    assert result['costUsd'] == pytest.approx(.03)
    assert result['pricingSnapshot']['currency'] == 'CNY'
    assert result['pricingSnapshot']['usdToCny'] == 7
    assert result['pricingSnapshot']['rates']['input'] == 14


def test_cheap_summary_uses_billing_components_not_rounded_display_price(prices):
    result = price_summary_call(
        {'prompt_tokens': 1, 'completion_tokens': 1}, model=MODEL, provider_id=PROVIDER)
    assert result['costUsd'] == pytest.approx(.000012)


def test_reasoning_only_output_follows_shared_cost_engine(prices):
    result = price_summary_call(
        {'prompt_tokens': 100, 'completion_tokens': 0, 'reasoning_tokens': 20},
        model=MODEL, provider_id=PROVIDER)
    assert result['costUsd'] == pytest.approx(.0004)


@pytest.mark.parametrize('usage', [
    None, {}, {'_dispatch': {'model': MODEL}}, {'total_tokens': 12_000},
    {'prompt_tokens': 5}, {'completion_tokens': 1},
    {'prompt_tokens': None, 'completion_tokens': 0},
    _usage(prompt_tokens=-1), _usage(completion_tokens=float('nan')),
    _usage(prompt_tokens=float('inf')), _usage(prompt_tokens='bad'),
    _usage(prompt_tokens=1.5), _usage(completion_tokens=True),
    _usage(cache_read_tokens=-1), _usage(cache_write_tokens=float('inf')),
    _usage(prompt_tokens_details={'cached_tokens': 'invalid'}),
    _usage(prompt_tokens_details='invalid'),
])
def test_missing_or_malformed_usage_is_unknown_not_free(prices, usage):
    result = price_summary_call(usage, model=MODEL, provider_id=PROVIDER)
    assert result['costUsd'] is None
    assert result['known'] is False
    assert result['costBasis'] == 'metered_api_equivalent'


def test_explicit_zero_counters_are_observed_zero_not_missing(prices):
    result = price_summary_call(
        {'prompt_tokens': 0, 'completion_tokens': 0}, model=MODEL, provider_id=PROVIDER)
    assert result['costUsd'] == 0
    assert result['known'] is True
    assert result['pricingSnapshot']['model'] == MODEL


@pytest.mark.parametrize('override', [
    {'currency': 'EUR'}, {'input': -1}, {'output': float('nan')},
    {'cacheReadMul': float('inf')}, {'cacheWriteMul': -1}, {'input': True},
])
def test_unverified_rates_never_prove_summary_price(prices, override):
    pricing, _ = prices
    pricing.set_provider_pricing(PROVIDER, MODEL, {**RATES, **override})
    assert price_summary_call(_usage(), model=MODEL, provider_id=PROVIDER)['costUsd'] is None


@pytest.mark.parametrize('model', ['', 'unregistered-economics-model', 'qwen-unregistered-economics'])
def test_unknown_model_or_estimated_provenance_is_not_known_spend(prices, model):
    assert price_summary_call(_usage(), model=model, provider_id=PROVIDER)['costUsd'] is None


@pytest.mark.parametrize('rate', [None, 0, -7, float('nan'), float('inf'), True, 'bad'])
def test_invalid_exchange_rate_is_unknown_for_cny(prices, rate):
    pricing, data = prices
    data['usdToCny'] = rate
    pricing.set_provider_pricing(PROVIDER, MODEL, {**RATES, 'currency': 'CNY'})
    assert price_summary_call(_usage(), model=MODEL, provider_id=PROVIDER)['costUsd'] is None
    economics = _economics()
    assert economics['pricing_known'] is False
    assert economics['rewrite_cost_usd'] is None
    assert math.isinf(economics['payback_rounds'])


def test_each_attempt_uses_own_route_and_tier_even_when_failed(prices):
    pricing, _ = prices
    other_model = MODEL + '-fallback'
    pricing.set_provider_pricing(PROVIDER, other_model, {'contextTiers': [
        {'id': 'base', 'maxPromptTokens': 10_000, **RATES},
        {'id': 'long', 'maxPromptTokens': 1_000_000, **RATES, 'input': 4, 'output': 20},
    ]})
    raw = {'prompt_tokens': 999_999, 'completion_tokens': 999_999, '_summaryCalls': [
        {'model': MODEL, 'provider_id': PROVIDER, 'failed': True, 'usage': _usage()},
        {'model': other_model, 'provider_id': PROVIDER, 'usage': _usage(prompt_tokens=20_000)},
    ]}
    original = deepcopy(raw)
    result = summary_cost_details(raw)
    assert result['known'] is True
    assert result['costUsd'] == pytest.approx(.03 + .1)
    assert result['calls'][1]['pricingSnapshot']['tierId'] == 'long'
    assert result['callCount'] == 2
    assert result['unknownCallCount'] == 0
    assert raw == original


def test_failed_attempt_without_usage_keeps_total_unknown_and_known_spend_visible(prices):
    result = summary_cost_details({'_summaryCalls': [
        {'model': MODEL, 'provider_id': PROVIDER, 'failed': True, 'usage': {}},
        {'model': MODEL, 'provider_id': PROVIDER, 'usage': _usage()},
    ]})
    assert result['costUsd'] is None
    assert result['known'] is False
    assert result['knownCostUsd'] == pytest.approx(.03)
    assert result['unknownCallCount'] == 1


@pytest.mark.parametrize('calls', [[], None, {}, [None], ['bad'], [1]])
def test_malformed_attempt_ledger_never_falls_back_to_aggregate_usage(prices, calls):
    result = summary_cost_details({**_usage(), '_summaryCalls': calls},
                                  fallback_model=MODEL, fallback_provider=PROVIDER)
    assert result['costUsd'] is None
    assert result['known'] is False


def test_attempt_ledger_sums_all_rows_but_bounds_receipt_details(prices):
    row = {'model': MODEL, 'provider_id': PROVIDER, 'usage': _usage()}
    result = summary_cost_details({'_summaryCalls': [deepcopy(row) for _ in range(20)]})
    assert result['costUsd'] == pytest.approx(.6)
    assert result['callCount'] == 20
    assert len(result['calls']) == 16
    rows = [deepcopy(row) for _ in range(20)] + [None]
    result = summary_cost_details({'_summaryCalls': rows})
    assert result['costUsd'] is None
    assert result['knownCostUsd'] == pytest.approx(.6)
    assert result['unknownCallCount'] == 1


def test_recorded_price_is_preserved_when_rate_card_changes(prices):
    pricing, _ = prices
    usage = _usage()
    cost = price_summary_call(usage, model=MODEL, provider_id=PROVIDER)
    pricing.set_provider_pricing(PROVIDER, MODEL, {**RATES, 'input': 200, 'output': 1000})
    result = summary_cost_details({'_summaryCalls': [
        {'model': MODEL, 'provider_id': PROVIDER, 'usage': usage, 'cost': cost}]})
    assert result['costUsd'] == pytest.approx(.03)


@pytest.mark.parametrize('cost', [
    {}, None, {'costUsd': 0}, {'costUsd': -1}, {'costUsd': float('nan')},
    {'costUsd': float('inf')}, {'costUsd': None, 'reason': 'failed_usage'},
])
def test_invalid_or_unknown_stored_cost_is_never_repriced_as_known(prices, cost):
    result = summary_cost_details({'_summaryCalls': [
        {'model': MODEL, 'provider_id': PROVIDER, 'usage': _usage(), 'cost': cost}]})
    assert result['costUsd'] is None
    assert result['known'] is False


def test_summary_dispatch_identity_wins_over_selected_or_fallback_route(prices):
    raw = {**_usage(), '_dispatch': {'model': MODEL, 'provider_id': PROVIDER},
           '_summaryRoute': {'modelId': 'wrong-model', 'providerId': 'wrong-provider'}}
    result = summary_cost_details(raw, fallback_model='also-wrong')
    assert result['costUsd'] == pytest.approx(.03)
    assert result['calls'][0]['model'] == MODEL
    assert result['calls'][0]['providerId'] == PROVIDER
    raw.pop('_dispatch')
    raw['_summaryRoute'] = {'modelId': MODEL, 'providerId': PROVIDER}
    assert summary_cost_details(raw)['costUsd'] == pytest.approx(.03)


def test_all_in_payback_includes_summary_and_rewrite_separate_from_adoption(prices):
    summary = {**_usage(), '_dispatch': {'model': MODEL, 'provider_id': PROVIDER}}
    accounting = _economics(summary_usage=summary)
    projected = _economics(summary_cost={'costUsd': .03})
    adoption = _economics()
    # Warm replay saves (100k - 20k) * $2 * .1 / 1M = $.016 / round.
    # Cache rewrite costs 20k * $2 * 1.25 / 1M = $.05, summary costs $.03.
    assert accounting['savings_per_round_usd'] == pytest.approx(.016)
    assert accounting['rewrite_cost_usd'] == pytest.approx(.05)
    assert accounting['summary_cost_usd'] == pytest.approx(.03)
    assert accounting['total_cost_usd'] == pytest.approx(.08)
    assert accounting['summary_cost_tokens'] == pytest.approx(15_000)
    assert accounting['payback_rounds'] == pytest.approx(5)
    assert projected['payback_rounds'] == pytest.approx(5)
    assert adoption['summary_cost_usd'] == 0
    assert adoption['total_cost_usd'] == pytest.approx(.05)
    assert adoption['payback_rounds'] == pytest.approx(3.125)


@pytest.mark.parametrize('summary_args', [
    {'summary_usage_tokens': 10_000}, {'summary_usage': {}}, {'summary_usage': None},
    {'summary_usage': _usage()}, {'summary_cost': None}, {'summary_cost': {}},
    {'summary_cost': {'costUsd': float('nan')}}, {'summary_cost': {'costUsd': -1}},
    {'summary_cost': {'costUsd': float('inf')}}, {'summary_cost': {'costUsd': True}},
    {'summary_cost': {'costUsd': 0, 'known': False}},
    {'summary_cost': {'costUsd': 0, 'costBasis': 'subscription_cash'}},
    {'summary_usage': {}, 'summary_cost': {'costUsd': 0}},
])
def test_unknown_summary_never_uses_main_model_rate_or_assumes_zero(prices, summary_args):
    result = _economics(**summary_args)
    assert result['summary_cost_usd'] is None
    assert result['summary_cost_tokens'] is None
    assert result['summary_cost_known'] is False
    assert result['total_cost_usd'] is None
    assert result['rewrite_cost_usd'] == pytest.approx(.05)
    assert math.isinf(result['payback_rounds'])


def test_main_tier_crossing_uses_actual_cache_replay_and_summary_own_price(prices):
    pricing, _ = prices
    main_model = MODEL + '-main'
    pricing.set_provider_pricing(PROVIDER, main_model, {'contextTiers': [
        {'id': 'base', 'maxPromptTokens': 250_000,
         'input': 1, 'output': 2, 'cacheReadMul': .1, 'cacheWriteMul': 1.25},
        {'id': 'premium', 'maxPromptTokens': 1_000_000,
         'input': 2, 'output': 3, 'cacheReadMul': .1, 'cacheWriteMul': 1.25},
    ]})
    task = _task(cache_read=280_000)
    task['model'] = task['_compactionCacheObservation']['model'] = main_model
    summary = {**_usage(), '_dispatch': {'model': MODEL, 'provider_id': PROVIDER}}
    result = _proactive_cache_economics(
        task, tokens_before=300_000, candidate_tokens=200_000, summary_usage=summary)
    assert result['crosses_pricing_tier'] is True
    assert result['pricing_before']['tier_id'] == 'premium'
    assert result['pricing_after']['tier_id'] == 'base'
    assert result['savings_per_round_usd'] == pytest.approx(.036)
    assert result['rewrite_cost_usd'] == pytest.approx(.25)
    assert result['summary_cost_usd'] == pytest.approx(.03)
    assert result['total_cost_usd'] == pytest.approx(.28)
    assert result['payback_rounds'] == pytest.approx(.28 / .036)


def test_dropping_uncached_tail_does_not_claim_future_cache_savings(prices):
    result = _proactive_cache_economics(
        _task(cache_read=50_000), tokens_before=120_000, candidate_tokens=60_000)
    assert result['dropped_tokens'] == 60_000
    assert result['cache_replay_tokens_before'] == result['cache_replay_tokens_after'] == 50_000
    assert result['savings_per_round_usd'] == 0
    assert math.isinf(result['payback_rounds'])


@pytest.mark.parametrize('override', [
    {'currency': 'EUR'}, {'input': -2}, {'input': float('nan')},
    {'cacheReadMul': float('inf')}, {'cacheWriteMul': -1},
])
def test_unverified_main_route_yields_unknown_dollars_and_payback(prices, override):
    pricing, _ = prices
    pricing.set_provider_pricing(PROVIDER, MODEL, {**RATES, **override})
    result = _economics(summary_cost={'costUsd': .03})
    assert result['pricing_known'] is False
    assert result['summary_cost_usd'] == pytest.approx(.03)
    assert result['rewrite_cost_usd'] is None
    assert result['savings_per_round_usd'] is None
    assert result['total_cost_usd'] is None
    assert math.isinf(result['payback_rounds'])


def test_cny_main_route_and_config_provider_use_verified_usd_rates(prices):
    pricing, _ = prices
    pricing.set_provider_pricing(PROVIDER, MODEL, {
        **RATES, 'input': 14, 'output': 70, 'currency': 'CNY'})
    task = _task()
    task['config'] = {'model': task.pop('model'), 'providerId': task.pop('provider_id')}
    original = deepcopy(task)
    result = _proactive_cache_economics(
        task, tokens_before=120_000, candidate_tokens=20_000, summary_cost={'costUsd': .03})
    assert result['payback_rounds'] == pytest.approx(5)
    assert result['pricing_before']['currency'] == 'CNY'
    assert result['pricing_before']['usd_to_cny'] == 7
    assert result['pricing_before']['input_rate_usd'] == 2
    assert task == original


def test_zero_priced_main_route_has_known_zero_cost_but_no_financial_payback(prices):
    pricing, _ = prices
    pricing.set_provider_pricing(PROVIDER, MODEL, {**RATES, 'input': 0, 'output': 0})
    result = _economics(summary_cost={'costUsd': .03})
    assert result['pricing_known'] is True
    assert result['rewrite_cost_usd'] == 0
    assert result['savings_per_round_usd'] == 0
    assert result['total_cost_usd'] == pytest.approx(.03)
    assert math.isinf(result['payback_rounds'])


@pytest.mark.parametrize('mutation', ['source', 'currency', 'model', 'provider', 'basis'])
def test_stored_price_must_match_route_currency_and_api_equivalent_provenance(prices, mutation):
    cost = price_summary_call(_usage(), model=MODEL, provider_id=PROVIDER)
    if mutation == 'basis':
        cost['costBasis'] = 'subscription_cash'
    else:
        cost['pricingSnapshot'][mutation] = 'not-the-call-evidence'
    result = summary_cost_details({'_summaryCalls': [
        {'model': MODEL, 'provider_id': PROVIDER, 'usage': _usage(), 'cost': cost}]})
    assert result['costUsd'] is None
    assert result['knownCostUsd'] is None
    assert result['unknownCallCount'] == 1


def test_missing_usage_never_trusts_even_a_valid_stored_zero(prices):
    cost = price_summary_call(
        {'prompt_tokens': 0, 'completion_tokens': 0}, model=MODEL, provider_id=PROVIDER)
    result = summary_cost_details({'_summaryCalls': [
        {'model': MODEL, 'provider_id': PROVIDER, 'usage': {}, 'cost': cost}]})
    assert result['costUsd'] is None
    assert result['knownCostUsd'] is None


def test_subscription_execution_is_api_equivalent_not_a_cash_charge(prices, monkeypatch):
    pricing, _ = prices
    subscription = PROVIDER + '-subscription'
    monkeypatch.setitem(pricing.PROVIDER_PRICING, subscription, {MODEL: dict(RATES)})
    result = summary_cost_details({**_usage(), '_dispatch': {
        'model': MODEL, 'provider_id': subscription, 'access_mode': 'subscription'}})
    assert result['costUsd'] == pytest.approx(.03)
    assert result['costBasis'] == 'metered_api_equivalent'
    assert result['calls'][0]['pricingSnapshot']['provider'] == subscription
    assert not {'cashChargeUsd', 'subscriptionChargeUsd'} & result.keys()


def test_unknown_main_price_never_falls_back_to_token_only_payback(prices, monkeypatch):
    pricing, _ = prices
    monkeypatch.setattr(pricing, 'lookup_pricing', lambda *args, **kwargs: None)
    result = _economics(summary_cost={'costUsd': .03})
    assert result['pricing_known'] is False
    assert result['rewrite_cost_usd'] is None
    assert result['savings_per_round_usd'] is None
    assert result['total_cost_usd'] is None
    assert result['summary_cost_usd'] == pytest.approx(.03)
    assert math.isinf(result['payback_rounds'])


def test_failed_summary_attempt_remains_in_all_in_payback(prices):
    result = _economics(summary_usage={'_summaryCalls': [
        {'model': MODEL, 'provider_id': PROVIDER, 'failed': True, 'usage': _usage()},
        {'model': MODEL, 'provider_id': PROVIDER, 'usage': _usage()},
    ]})
    assert result['summary_cost_usd'] == pytest.approx(.06)
    assert result['total_cost_usd'] == pytest.approx(.11)
    assert result['payback_rounds'] == pytest.approx(.11 / .016)


def test_malformed_stored_currency_never_breaks_receipt_aggregation(prices):
    cost = price_summary_call(_usage(), model=MODEL, provider_id=PROVIDER)
    cost['pricingSnapshot']['currency'] = ['USD']
    result = summary_cost_details({'_summaryCalls': [
        {'model': MODEL, 'provider_id': PROVIDER, 'usage': _usage(), 'cost': cost}]})
    assert result['costUsd'] is None
    assert result['knownCostUsd'] is None
    assert result['known'] is False


@pytest.mark.parametrize('mutation', [
    'missing_input', 'missing_output', 'null_input', 'boolean_input',
    'duplicate_limit', 'invalid_limit', 'beyond_limit', 'unit',
])
def test_raw_tier_normalization_cannot_turn_missing_prices_into_free_spend(prices, mutation):
    pricing, _ = prices
    row = {'id': 'base', 'maxPromptTokens': 1_000_000, **RATES}
    card = {'contextTiers': [row]}
    if mutation == 'missing_input':
        row.pop('input')
    elif mutation == 'missing_output':
        row.pop('output')
    elif mutation == 'null_input':
        row['input'] = None
    elif mutation == 'boolean_input':
        row['input'] = False
    elif mutation == 'duplicate_limit':
        card['contextTiers'].append({**row, 'id': 'ambiguous', 'input': 4})
    elif mutation == 'invalid_limit':
        row['maxPromptTokens'] = True
    elif mutation == 'beyond_limit':
        row['maxPromptTokens'] = 9_999
    else:
        row['unit'] = 'per_thousand_tokens'
    pricing.set_provider_pricing(PROVIDER, MODEL, card)
    result = price_summary_call(_usage(), model=MODEL, provider_id=PROVIDER)
    assert result['known'] is False
    assert result['costUsd'] is None
    assert _economics()['pricing_known'] is False


@pytest.mark.parametrize('provider_id', [None, '', 'unknown-provider', 'model-route:41:task-a'])
def test_known_model_table_does_not_prove_actual_summary_provider_price(prices, monkeypatch, provider_id):
    pricing, _ = prices
    monkeypatch.setitem(pricing.MODEL_PRICING, MODEL, dict(RATES))
    result = price_summary_call(_usage(), model=MODEL, provider_id=provider_id)
    assert result['costUsd'] is None
    assert result['known'] is False


def test_same_model_uses_actual_provider_price_and_preserves_execution_pin(prices, monkeypatch):
    pricing, _ = prices
    other = PROVIDER + '-actual'
    monkeypatch.setitem(pricing.PROVIDER_PRICING, other, {MODEL: {**RATES, 'input': 4, 'output': 20}})
    usage = {**_usage(), '_summaryRoute': {'modelId': MODEL, 'providerId': 'model-route:41:task-a'},
             '_dispatch': {'model': MODEL, 'provider_id': other}}
    original = deepcopy(usage)
    result = summary_cost_details(usage, fallback_model=MODEL, fallback_provider=PROVIDER)
    assert result['costUsd'] == pytest.approx(.06)
    assert result['calls'][0]['providerId'] == other
    assert usage == original


@pytest.mark.parametrize(('counter', 'expected'), [
    ('cache_read_tokens', .0320002), ('cache_write_tokens', .0550025),
])
def test_hybrid_cache_counters_follow_shared_residual_convention(prices, counter, expected):
    # The shared engine recognizes prompt_tokens carrying a residual when the
    # cache is larger. Preserve that provider convention instead of guessing
    # that the usage is malformed or silently excluding the observed cache.
    result = price_summary_call(_usage(**{counter: 10_001}), model=MODEL, provider_id=PROVIDER)
    assert result['known'] is True
    assert result['costUsd'] == pytest.approx(expected)
    assert result['pricingSnapshot']['selectedPromptTokens'] == 20_001


def test_stored_snapshot_cannot_claim_a_different_prompt_tier(prices):
    cost = price_summary_call(_usage(), model=MODEL, provider_id=PROVIDER)
    cost['pricingSnapshot']['selectedPromptTokens'] = 1
    result = summary_cost_details({'_summaryCalls': [
        {'model': MODEL, 'provider_id': PROVIDER, 'usage': _usage(), 'cost': cost}]})
    assert result['costUsd'] is None
    assert result['calls'][0]['reason'] == 'invalid_cost'


@pytest.mark.parametrize('cache_rate', [.5, 1])
def test_absolute_cache_rate_cannot_be_silently_ignored(prices, cache_rate):
    pricing, _ = prices
    pricing.set_provider_pricing(PROVIDER, MODEL, {**RATES, 'cache_read': cache_rate})
    result = price_summary_call(_usage(cache_read_tokens=5_000), model=MODEL, provider_id=PROVIDER)
    assert result['costUsd'] is None
    assert _economics()['pricing_known'] is False


def test_consistent_explicit_absolute_and_multiplier_cache_rates_are_known(prices):
    pricing, _ = prices
    pricing.set_provider_pricing(PROVIDER, MODEL, {**RATES, 'cache_read': .2, 'cache_write': 2.5})
    result = price_summary_call(_usage(cache_read_tokens=5_000), model=MODEL, provider_id=PROVIDER)
    assert result['costUsd'] == pytest.approx(.021)
