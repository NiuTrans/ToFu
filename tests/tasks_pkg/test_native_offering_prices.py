"""Native default settlement uses the owned offering admitted for this route."""
import pytest

from lib.model_routing.native_route import compile_native_route
from lib.pricing._provider import PROVIDER_PRICING
from lib.tasks_pkg.native_pricing import NativeRoutePricing
from lib.agent_core.native_model_control import ModelSettlement, NativePricingError
from lib.model_routing.repository import OwnerBoundary
from tests.model_routing.test_native_route import Repository, principal, selection
from tests.tasks_pkg.test_native_pricing import settlement, request

pytestmark = pytest.mark.unit


def metadata(repository, owner=7):
    route = compile_native_route(repository, repository.boundary, selection())
    return route.consume(principal(owner))[0]


def test_issued_offering_prices_win_over_conflicting_global_and_wire_alias_rows(monkeypatch):
    repository = Repository()
    monkeypatch.setitem(PROVIDER_PRICING, 'provider-a', {
        'alpha': {'input': 999, 'output': 999},
        'alpha-wire-a': {'input': 1000, 'output': 1000},
    })
    issued = metadata(repository)
    resolver = NativeRoutePricing(principal=principal(), margin=0, rates=lambda _: 7.24)
    value = resolver(settlement(), issued).values()
    assert (value['input'], value['output']) == (5, 10)


def test_same_provider_and_model_keep_independent_owner_price_snapshots():
    prices = []
    for owner, price in [(7, 3), (8, 11)]:
        repository = Repository()
        repository.boundary = OwnerBoundary.create(owner)
        repository.document['offerings'][0]['actual_pricing']['input'] = price
        issued = metadata(repository, owner)
        repository.document['offerings'][0]['actual_pricing']['input'] = 999
        resolver = NativeRoutePricing(principal=principal(owner), margin=0, rates=lambda _: 7.24)
        result = ModelSettlement(request(owner), b'{"prompt_tokens":100,"completion_tokens":2}', False)
        prices.append(resolver(result, issued).values()['input'])
    assert prices == [3, 11]


@pytest.mark.parametrize('base,read,write,expected', [(4, 0, 8, (0, 2)), (0, 0, 0, (0, 0))])
def test_declared_absolute_cache_prices_and_free_rates_survive_binding(base, read, write, expected):
    repository = Repository()
    repository.document['offerings'][0]['actual_pricing'].update(
        input=base, output=0, currency='CNY', cache_read=read, cache_write=write)
    result = NativeRoutePricing(principal=principal(), margin=0, rates=lambda _: 7.24)(
        settlement(), metadata(repository)).values()
    assert result['currency'] == 'CNY' and result['input'] == base and result['output'] == 0
    assert (result['cache_read_multiplier'], result['cache_write_multiplier']) == expected


@pytest.mark.parametrize('change', [
    {'input': 0, 'cache_read': 1}, {'input': 0, 'cache_write': 1},
    {'input': True}, {'output': -1}, {'unit': 'per_token'}, {'currency': 'unknown'},
    {'input': float('nan')}, {'output': float('inf')}, {'cache_read': False},
    {'input': 1e-320, 'cache_write': 1e308},
])
def test_unrepresentable_declared_price_refuses_at_route_binding(change):
    issued = metadata(Repository())
    issued['actual_pricing'].update(change)
    resolver = NativeRoutePricing(principal=principal(), margin=0)
    with pytest.raises(NativePricingError, match='not representable'):
        resolver.validate_binding(principal(), issued)


def test_explicit_owner_lookup_remains_authoritative_and_receives_its_original_contract():
    calls = []
    def lookup(owner, model, provider, *, at, prompt_tokens):
        calls.append((owner, model, provider, at, prompt_tokens))
        return {'input': 7, 'output': 9, 'currency': 'USD'}
    resolver = NativeRoutePricing(principal=principal(), margin=0, lookup=lookup,
        rates=lambda _: 7.24, clock=lambda: 100)
    issued = metadata(Repository())
    issued['actual_pricing'].update(input=0, cache_read=1)
    result = resolver(settlement(), issued).values()
    assert (result['input'], result['output']) == (7, 9)
    assert calls == [(principal(), 'alpha', 'provider-a', 100, 100)]


def test_price_snapshot_is_detached_for_the_owned_model_policy():
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from lib.agent_core.execution_budget import ExecutionBudget
    from lib.tasks_pkg.native_model_account import NativeModelAccountPolicy
    task = {'id': 'task', '_userId': 7, 'config': {}, 'abort_event': threading.Event()}
    task['_executionBudget'] = ExecutionBudget(task)
    issued = metadata(Repository())
    with ThreadPoolExecutor(max_workers=1) as executor:
        policy = NativeModelAccountPolicy(principal=principal(), task=task, executor=executor,
            pricing_resolver=NativeRoutePricing(principal=principal(), margin=0, rates=lambda _: 7.24))
        with policy.bind_pricing(principal=principal(), task_id='task', metadata=issued):
            issued['actual_pricing']['input'] = 999
            assert policy.prepare_pricing(settlement()).values()['input'] == 5
        assert policy.prepare_pricing is None
