"""Auxiliary summary quotes must retain their actual admitted offering rates."""
from copy import deepcopy

import pytest

from lib.tasks_pkg.compaction._summary_cost import (
    price_summary_call, summary_cost_details,
)

pytestmark = pytest.mark.unit


def _price(pricing, usage=None):
    return price_summary_call(
        usage or {'prompt_tokens': 20_000, 'completion_tokens': 2_000},
        model='fixture-summary', provider_id='fixture-paid-provider',
        declared_pricing=pricing,
    )


def test_declared_flat_offering_price_needs_no_ambient_registry():
    pricing = {'input': .1, 'output': .5, 'currency': 'USD',
               'unit': 'per_million_tokens'}
    before = deepcopy(pricing)
    cost = _price(pricing)
    assert cost['known'] is True
    assert cost['costUsd'] == pytest.approx(.003)
    assert cost['pricingSource'] == 'admitted_offering_price'
    assert cost['pricingSnapshot']['provider'] == 'fixture-paid-provider'
    assert pricing == before


def test_declared_offering_cost_survives_receipt_aggregation():
    usage = {'prompt_tokens': 20_000, 'completion_tokens': 2_000}
    cost = _price({'input': .1, 'output': .5, 'currency': 'USD'}, usage)
    receipt = summary_cost_details({'_summaryCalls': [{
        'model': 'fixture-summary', 'provider_id': 'fixture-paid-provider',
        'usage': usage, 'cost': cost,
    }]})
    assert receipt['known'] is True
    assert receipt['costUsd'] == pytest.approx(.003)
    assert receipt['unknownCallCount'] == 0


def test_declared_tier_uses_observed_full_prompt_and_absolute_cache_rate():
    pricing = {'currency': 'USD', 'contextTiers': [
        {'id': 'short', 'maxPromptTokens': 10_000, 'input': .1, 'output': .5,
         'cache_read': .01, 'cache_write': .125},
        {'id': 'long', 'maxPromptTokens': 100_000, 'input': .2, 'output': 1,
         'cache_read': .08, 'cache_write': .3},
    ]}
    usage = {'prompt_tokens': 20_000, 'completion_tokens': 2_000,
             'prompt_tokens_details': {'cached_tokens': 5_000}}
    cost = _price(pricing, usage)
    assert cost['known'] is True
    # 15k uncached at .2 + 5k cached at .08 + 2k output at 1 USD/MTok.
    assert cost['costUsd'] == pytest.approx(.0054)
    assert cost['pricingSnapshot']['tierId'] == 'long'
    assert cost['pricingSnapshot']['selectedPromptTokens'] == 20_000


@pytest.mark.parametrize('pricing', [
    {}, {'input': .1, 'currency': 'USD'},
    {'input': float('nan'), 'output': .5, 'currency': 'USD'},
    {'input': .1, 'output': .5, 'currency': 'EUR'},
    {'input': .1, 'output': .5, 'currency': 'USD', 'peakMul': 2},
])
def test_unknown_or_invalid_admitted_prices_never_become_free(pricing):
    cost = _price(pricing)
    assert cost['known'] is False
    assert cost['costUsd'] is None


def test_recorded_price_cannot_be_attributed_to_another_provider():
    usage = {'prompt_tokens': 20_000, 'completion_tokens': 2_000}
    cost = _price({'input': .1, 'output': .5, 'currency': 'USD'}, usage)
    receipt = summary_cost_details({'_summaryCalls': [{
        'model': 'fixture-summary', 'provider_id': 'foreign-provider',
        'usage': usage, 'cost': cost,
    }]})
    assert receipt['known'] is False
    assert receipt['costUsd'] is None
    assert receipt['calls'][0]['reason'] == 'invalid_cost'
