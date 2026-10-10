"""AA cost-first compaction routing never grants new access or claims fidelity."""
from copy import deepcopy
import time
from types import SimpleNamespace

import pytest

from lib.model_profiles import configured_model_profiles
from lib.tasks_pkg.compaction._summary_routing import select_summary_route

pytestmark = pytest.mark.unit


def _aa(score=50, **changes):
    return {'source': 'Artificial Analysis', 'match': 'exact',
            'intelligence': score, 'fetched_at': time.time(),
            'aa_slug': 'fixture-model', **changes}


def _profile(model, price=1, **changes):
    entry = {
        'model_id': model, 'capabilities': ['text'], 'context_window': 200_000,
        'aa_scores': _aa(),
        'pricing': {'input': price, 'output': price, 'currency': 'USD',
                    'unit': 'per_million_tokens'},
        'capability_profile': {
            'quality': 'heavy', 'roles': ['analyst', 'general'],
            'confidence': 1.0, 'evidence': 'operator',
        },
    }
    profile = configured_model_profiles(providers=[{'id': 'supplier', 'models': [entry]}])[0]
    profile['provenance'] = {
        'source': 'model_routing_v2', 'ownerUserId': 41, 'tenantId': 'tenant-a',
        'providerAccessId': 'access-a', 'offeringId': 'offering-' + model,
        'credentialsVerified': False, 'identityState': 'confirmed',
    }
    profile.update(changes)
    return profile


def _slot(model, **changes):
    fields = {
        'model': 'wire-' + model, 'logical_model': model,
        'provider_id': 'model-route:41:task-a', 'routing_provider_id': 'supplier',
        'routing_owner_user_id': 41, 'request_scoped': True,
        'route_offering_id': 'offering-' + model,
        'capabilities': {'text'}, 'context_window': 200_000,
        'max_output_tokens': 16_384, 'oauth': '',
    }
    fields.update(changes)
    return SimpleNamespace(**fields)


@pytest.fixture
def routes(monkeypatch):
    task = {
        '_userId': 41, 'model': 'main', '_pinned_provider_id': 'model-route:41:task-a',
        'config': {'compaction': {'summaryPolicy': 'qualified-cheapest'}},
        '_principalContext': {
            'kind': 'user', 'subject_id': 'user:41', 'owner_user_id': 41,
            'tenant_id': 'tenant-a', 'scopes': [],
        },
    }
    profiles = [_profile('main', 10), _profile('cheap', 1)]
    slots = [_slot('main'), _slot('cheap')]
    seen = []

    def owner_profiles(**kwargs):
        seen.append(kwargs)
        return profiles

    monkeypatch.setattr('lib.model_profiles.configured_model_profiles', owner_profiles)
    # No initialize(): discovery may only inspect already-authorized slots.
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    dispatcher = LLMDispatcher()
    dispatcher.slots = slots
    monkeypatch.setattr('lib.llm_dispatch.get_dispatcher', lambda: dispatcher)
    monkeypatch.setattr('lib.llm_dispatch.provider_pin.get_pinned_provider', lambda: None)
    monkeypatch.setattr('lib.context_limits.resolve_learned_context_limit', lambda p, m, w: w)
    return SimpleNamespace(task=task, profiles=profiles, slots=slots, seen=seen)


def _select(routes, **tokens):
    return select_summary_route(routes.task, tokens.get('input_tokens', 10_000),
                                tokens.get('output_tokens', 1_000))


def test_aa_policy_selects_cheaper_route_without_task_mutation(routes):
    before = deepcopy(routes.task)
    result = _select(routes)
    assert set(result) == {'modelId', 'providerId', 'policy', 'reason',
                           'projectedCostUsd', 'costBasis', 'selectionBasis'}
    assert result['modelId'] == 'cheap'
    assert result['providerId'] == 'model-route:41:task-a'
    assert result['costBasis']['providerId'] == 'supplier'
    assert result['projectedCostUsd'] == pytest.approx(0.011)
    assert result['reason'] == 'lowest_projected_cost_highest_aa'
    assert result['policy'] == 'aa-cheapest'
    assert result['selectionBasis'] == {
        'source': 'Artificial Analysis', 'metric': 'intelligence',
        'score': 50, 'match': 'exact', 'ranking': 'cost_asc_aa_desc',
        'eligibleRoutes': 1, 'fetchedAt': routes.profiles[1]['aaScores']['fetched_at'],
        'aaSlug': 'fixture-model',
    }
    assert routes.seen == [{'owner_user_id': 41, 'tenant_id': 'tenant-a'}]
    assert routes.task == before


@pytest.mark.parametrize('config', [
    {}, {'compaction': {}}, {'compaction': {'summaryPolicy': 'aa-cheapest'}},
    {'compaction': {'summaryPolicy': 'qualified-cheapest'}},
])
def test_aa_routing_is_default_and_legacy_alias_uses_same_policy(routes, config):
    routes.task['config'] = deepcopy(config)
    assert _select(routes)['modelId'] == 'cheap'
    assert _select(routes)['policy'] == 'aa-cheapest'


@pytest.mark.parametrize('policy', [None, 'main', 'cheapest', True, {}, []])
def test_explicit_main_and_invalid_policies_retain_main(routes, policy):
    routes.task['config']['compaction']['summaryPolicy'] = policy
    result = _select(routes)
    assert result['modelId'] == 'main'
    assert result['policy'] == 'main'
    assert result['selectionBasis'] == {}


def test_disable_model_fallback_overrides_summary_opt_in(routes):
    routes.task['config']['disableModelFallback'] = True
    result = _select(routes)
    assert result['modelId'] == 'main'
    assert result['reason'] == 'disable_model_fallback'


def test_thread_pin_cannot_replace_task_route_scope(routes, monkeypatch):
    monkeypatch.setattr('lib.llm_dispatch.provider_pin.get_pinned_provider', lambda: 'foreign-pin')
    result = _select(routes)
    assert result['modelId'] == 'main'
    assert result['reason'] == 'conflicting_provider_pins'
    assert result['providerId'] == routes.task['_pinned_provider_id']
    assert routes.seen == []


@pytest.mark.parametrize('field,value', [
    ('routing_owner_user_id', 42), ('provider_id', 'model-route:41:other-task'),
    ('routing_provider_id', 'other-provider'), ('route_offering_id', 'other-offering'),
    ('logical_model', 'other-logical-model'), ('capabilities', {'embedding'}),
    ('max_output_tokens', 999), ('max_output_tokens', 0),
    ('context_window', 10_999), ('context_window', 0), ('oauth', 'codex'),
])
def test_slot_owner_pin_billing_identity_and_capacity_are_intersected(routes, field, value):
    setattr(routes.slots[1], field, value)
    assert _select(routes)['modelId'] == 'main'


@pytest.mark.parametrize('field,value', [
    ('ownerUserId', 42), ('tenantId', 'tenant-b'),
    ('providerAccessId', 'access-b'), ('offeringId', 'other-offering'), ('source', 'fixture'),
    ('identityState', 'pending_identity'),
])
def test_supply_profile_is_not_a_route_permission(routes, field, value):
    routes.profiles[1]['provenance'][field] = value
    assert _select(routes)['modelId'] == 'main'


def test_model_supply_without_execution_slot_is_never_selected(routes):
    routes.slots.pop()
    assert _select(routes)['modelId'] == 'main'


def test_slot_without_owner_profile_is_never_selected(routes):
    routes.profiles.pop()
    assert _select(routes)['modelId'] == 'main'


def test_another_tasks_ephemeral_slot_without_pin_is_not_available(routes):
    routes.task.pop('_pinned_provider_id')
    assert _select(routes)['modelId'] == 'main'


def test_global_owner_slot_keeps_provider_boundary(routes):
    routes.task.pop('_pinned_provider_id')
    for slot in routes.slots:
        slot.request_scoped = False
        slot.provider_id = 'supplier'
    assert _select(routes)['modelId'] == 'cheap'
    routes.slots[1].provider_id = 'other-execution-scope'
    assert _select(routes)['modelId'] == 'main'


@pytest.mark.parametrize('field,value', [
    ('autoSelectable', False), ('quality', 'light'), ('quality', 'unknown'),
    ('quality', None), ('roles', ['writer']), ('roles', []),
    ('confidence', 0.79), ('confidence', float('nan')),
    ('profileSource', 'unqualified'),
])
def test_exact_aa_proxy_replaces_ordinal_role_gate_for_compression(routes, field, value):
    routes.profiles[1][field] = value
    assert _select(routes)['modelId'] == 'cheap'


def test_cheaper_aa_candidate_need_not_match_main_ordinal_quality(routes):
    routes.profiles[0]['quality'] = 'frontier'
    routes.profiles[0]['roles'] = ['reviewer', 'worker']
    routes.profiles[1]['quality'] = 'light'
    assert _select(routes)['modelId'] == 'cheap'


def test_main_needs_identity_price_and_capacity_not_quality_or_aa(routes):
    routes.profiles[0].update(profileSource='unqualified', aaScores={})
    assert _select(routes)['modelId'] == 'cheap'


def test_learned_route_shrink_and_output_reserve_are_enforced(routes, monkeypatch):
    monkeypatch.setattr('lib.context_limits.resolve_learned_context_limit', lambda p, m, w: 10_999)
    assert _select(routes)['modelId'] == 'main'
    monkeypatch.setattr('lib.context_limits.resolve_learned_context_limit', lambda p, m, w: 11_000)
    assert _select(routes)['modelId'] == 'cheap'


def test_all_picker_reachable_slots_must_fit_not_just_one(routes):
    routes.slots.append(_slot('cheap', max_output_tokens=500))
    assert _select(routes)['modelId'] == 'main'


@pytest.mark.parametrize('opt_in', [False, True])
def test_subscription_never_buys_paid_api_summary_or_claims_token_cost(routes, opt_in):
    routes.slots[0].oauth = 'codex'
    if not opt_in:
        routes.task['config']['compaction'] = {}
    result = _select(routes)
    assert result['modelId'] == 'main'
    assert result['reason'] == 'subscription_route_preserved'
    assert result['projectedCostUsd'] is None


def test_token_weighted_cost_not_blended_price(routes):
    routes.profiles[0]['declaredPricing'].update(input=1, output=20)
    routes.profiles[1]['declaredPricing'].update(input=3, output=1)
    routes.profiles[1]['price'] = {'known': True, 'blendedUsdPerMTok': 0.0001}
    assert _select(routes, input_tokens=100_000, output_tokens=1_000)['modelId'] == 'main'
    assert _select(routes, input_tokens=1_000, output_tokens=10_000)['modelId'] == 'cheap'


def test_context_tiers_currency_and_cache_cost_apply_to_same_prompt(routes):
    from lib.pricing import DEFAULT_USD_CNY_RATE
    routes.profiles[1]['declaredPricing'] = {'contextTiers': [
        {'id': 'short', 'maxPromptTokens': 10_000, 'input': 1, 'output': 2,
         'currency': 'CNY', 'cacheReadMul': 0.1, 'cacheWriteMul': 1.25},
        {'id': 'long', 'maxPromptTokens': 100_000, 'input': 7, 'output': 14,
         'currency': 'CNY', 'cacheReadMul': 0.2, 'cacheWriteMul': 2},
    ]}
    result = _select(routes, input_tokens=10_001)
    assert result['modelId'] == 'cheap'
    basis = result['costBasis']
    assert basis['tierId'] == 'long'
    assert basis['cacheReadTokens'] == 0
    assert basis['cacheWriteTokens'] == 10_001
    assert basis['uncachedInputTokens'] == 0
    assert basis['rates'] == pytest.approx({'input': 7, 'output': 14, 'cacheRead': 1.4, 'cacheWrite': 14})
    assert result['projectedCostUsd'] == pytest.approx((10_001 * 14 + 1_000 * 14) / DEFAULT_USD_CNY_RATE / 1e6)
    assert _select(routes)['costBasis']['tierId'] == 'short'


def test_expensive_cache_write_prevents_false_cheap_route(routes):
    routes.profiles[1]['declaredPricing'].update(cache_read=0, cache_write=100)
    assert _select(routes)['modelId'] == 'main'


@pytest.mark.parametrize('pricing', [
    {}, {'input': 0, 'currency': 'USD'},
    {'input': float('nan'), 'output': 0, 'currency': 'USD'},
    {'input': -1, 'output': 0, 'currency': 'USD'},
    {'input': 0, 'output': float('inf'), 'currency': 'USD'},
    {'input': 0, 'output': 0, 'currency': 'EUR'},
    {'input': 0, 'output': 0, 'currency': 'USD', 'unit': 'per_token'},
    {'input': 0, 'output': 0, 'currency': 'USD', 'cache_write': -1},
    {'input': 0, 'output': 0, 'currency': 'USD', 'peak': {'mul': 2}},
    {'input': 1e308, 'output': 0, 'currency': 'USD', 'cacheReadMul': 1e308},
    {'contextTiers': [{'maxPromptTokens': 100_000, 'input': 0, 'currency': 'USD'}]},
])
def test_missing_or_invalid_prices_never_mean_free(routes, pricing):
    routes.profiles[1]['declaredPricing'] = pricing
    assert _select(routes)['modelId'] == 'main'


def test_explicit_zero_prices_are_valid_and_unknown_main_prices_prevent_switch(routes):
    routes.profiles[1]['declaredPricing'].update(input=0, output=0)
    assert _select(routes)['projectedCostUsd'] == 0
    routes.profiles[0]['declaredPricing'] = {}
    result = _select(routes)
    assert result['modelId'] == 'main'
    assert result['projectedCostUsd'] is None


def test_deterministic_tie_and_strict_savings(routes):
    routes.profiles.append(_profile('a-cheap', 1))
    routes.slots.append(_slot('a-cheap'))
    first = _select(routes)
    routes.slots.reverse()
    routes.profiles.reverse()
    assert _select(routes) == first
    assert first['modelId'] == 'a-cheap'
    for profile in routes.profiles:
        profile['declaredPricing'].update(input=10, output=10)
    assert _select(routes)['modelId'] == 'main'


@pytest.mark.parametrize('input_tokens,output_tokens', [(True, 1), (-1, 1), (1, 0), (1, '10')])
def test_bad_token_budgets_retain_main(routes, input_tokens, output_tokens):
    assert _select(routes, input_tokens=input_tokens, output_tokens=output_tokens)['modelId'] == 'main'


def test_missing_owner_and_repository_failures_do_not_expose_other_models(routes, monkeypatch):
    routes.task.pop('_userId')
    assert _select(routes)['modelId'] == 'main'
    assert routes.seen == []
    routes.task['_userId'] = 41

    def unavailable(**kwargs):
        raise RuntimeError('repository unavailable')

    monkeypatch.setattr('lib.model_profiles.configured_model_profiles', unavailable)
    result = _select(routes)
    assert result['modelId'] == 'main'
    assert result['reason'] == 'route_evidence_unavailable'


def test_task_principal_mismatch_is_not_authority(routes):
    routes.task['_principalContext']['owner_user_id'] = 42
    assert _select(routes)['modelId'] == 'main'
    assert routes.seen == []


def test_config_pin_mismatch_preserves_task_pin(routes):
    routes.task['config']['_pinned_provider_id'] = 'another-task'
    result = _select(routes)
    assert result['modelId'] == 'main'
    assert result['providerId'] == routes.task['_pinned_provider_id']
    assert result['reason'] == 'conflicting_provider_pins'


def test_unpinned_ambiguous_main_provider_is_not_guessed(routes):
    routes.task.pop('_pinned_provider_id')
    for slot in routes.slots:
        slot.request_scoped = False
        slot.provider_id = 'supplier'
    routes.slots.append(_slot('main', request_scoped=False, provider_id='another-pin'))
    assert _select(routes)['reason'] == 'main_route_unresolved'


def test_provider_restriction_cannot_hide_a_reachable_slot(routes):
    routes.task['config']['providerId'] = 'supplier'
    routes.slots.append(_slot('cheap', routing_provider_id='other-provider'))
    # Filtering this slot out before bucket qualification would be unsafe:
    # strict model+execution pin dispatch could still pick it.
    assert _select(routes)['modelId'] == 'main'


def test_missing_capacity_never_borrows_name_inference(routes, monkeypatch):
    routes.slots[1].context_window = 0
    monkeypatch.setattr('lib.llm_dispatch.context_policy.slot_context_window', lambda slot: 1_000_000)
    assert _select(routes)['modelId'] == 'main'
    routes.slots[1].context_window = 200_000
    routes.slots[0].max_output_tokens = 0
    assert _select(routes)['reason'] == 'main_capacity_unqualified'


def test_catalog_classifies_declared_explicit_and_aa_only_profiles():
    declared, aa_only, explicit = configured_model_profiles(providers=[{
        'id': 'supplier', 'models': [
            {'model_id': 'gpt-5.6-terra', 'capabilities': ['text'],
             'context_window': 200_000, 'pricing': {'input': 1, 'output': 2}},
            {'model_id': 'unknown-aa-leader', 'capabilities': ['text'],
             'aa_scores': {'intelligence': 999}},
            {'model_id': 'operator-model', 'capabilities': ['text'],
             'capability_profile': {'quality': 'heavy', 'roles': ['analyst', 'general'],
                                    'confidence': 1}},
        ],
    }])
    assert declared['profileSource'] == 'maintained_declared_rule'
    assert declared['contextWindow'] == 200_000
    assert aa_only['profileSource'] == 'unqualified'
    assert aa_only['autoSelectable'] is False
    assert aa_only['contextWindow'] is None
    assert explicit['profileSource'] == 'explicit_capability_profile'
    assert explicit['provenance'] == {'source': 'fixture', 'credentialsVerified': False}


def test_runtime_catalog_preserves_owner_offering_evidence_not_admission(monkeypatch):
    from lib.model_routing import OwnerBoundary
    raw_pricing = {'input': 0.1, 'output': 0.3, 'cache_read': 0.01,
                   'cache_write': 0.12, 'currency': 'CNY', 'unit': 'per_million_tokens'}
    boundary = OwnerBoundary.create(41, 'tenant-a')
    document = {
        'providers': [{'provider_id': 'supplier'}],
        'provider_accesses': [{'provider_access_id': 'access-a', 'provider_id': 'supplier', 'enabled': True}],
        'models': [{'creator_id': 'openai', 'model_id': 'gpt-5.6-terra'}],
        'deployments': [{'offering_id': 'offer-a'}],
        'offerings': [{
            'offering_id': 'offer-a', 'provider_access_id': 'access-a',
            'model': {'creator_id': 'openai', 'model_id': 'gpt-5.6-terra'},
            'enabled': True, 'identity_state': 'confirmed', 'capabilities': ['text'],
            'context_window': 128_000, 'actual_pricing': raw_pricing,
        }],
    }

    class Repository:
        def get(self, requested):
            assert requested == boundary
            return SimpleNamespace(revision=5, document=document)

    monkeypatch.setattr('lib.model_catalog.aa.cached_routing_scores', lambda models: {})
    profile, = configured_model_profiles(owner_user_id=41, tenant_id='tenant-a', repository=Repository())
    assert profile['profileSource'] == 'maintained_declared_rule'
    assert profile['contextWindow'] == 128_000
    assert profile['declaredPricing'] == raw_pricing
    assert profile['provenance'] == {
        'source': 'model_routing_v2', 'ownerUserId': 41, 'tenantId': 'tenant-a',
        'revision': 5, 'providerAccessId': 'access-a', 'offeringId': 'offer-a',
        'creatorId': 'openai', 'identityState': 'confirmed',
        'contextSource': 'offering', 'credentialsVerified': False,
    }


def test_summary_policy_is_explicit_in_config_contract():
    import json
    from pathlib import Path

    contract = json.loads((Path(__file__).resolve().parents[2] / 'contracts/config_knobs_v1.json').read_text())
    policy = contract['taskConfig']['compaction.summaryPolicy']
    assert policy['default'] == 'aa-cheapest'
    assert policy['values'] == ['main', 'aa-cheapest', 'qualified-cheapest']
    assert policy['aliases'] == {'qualified-cheapest': 'aa-cheapest'}
    assert 'qualityProxy' in policy['qualification']
    assert 'No price-band tolerance' in policy['qualification']['ranking']
    assert policy['owner'] == 'lib/tasks_pkg/compaction/_summary_routing.py'


def test_strict_picker_wire_collision_cannot_escape_qualification(routes):
    collision = _slot('different-logical')
    collision.model = 'cheap'
    routes.slots.append(collision)
    assert _select(routes)['modelId'] == 'main'


def test_worst_reachable_offering_not_cheapest_profile_controls_cost(routes):
    profile = _profile('cheap', 100)
    profile['provenance']['offeringId'] = 'expensive-offering'
    routes.profiles.append(profile)
    routes.slots.append(_slot('cheap', route_offering_id='expensive-offering'))
    assert _select(routes)['modelId'] == 'main'


def test_conflicting_tenant_identity_cannot_route(routes):
    routes.task['_tenant_id'] = 'other-tenant'
    assert _select(routes)['reason'] == 'conflicting_tenant_identity'
    assert routes.seen == []


@pytest.mark.parametrize('reverse', [False, True])
def test_complete_ranking_minimizes_cost_then_maximizes_aa_not_ordinal_quality(routes, reverse):
    routes.profiles[1].update(quality='light', aaScores=_aa(20))
    for model, price, score in [('expensive', 4, 99), ('tie-low', 1, 30),
                                ('tie-high', 1, 60)]:
        routes.profiles.append(_profile(model, price, quality='frontier', aaScores=_aa(score)))
        routes.slots.append(_slot(model))
    if reverse:
        routes.profiles.reverse()
        routes.slots.reverse()
    result = _select(routes)
    assert result['modelId'] == 'tie-high'
    assert result['projectedCostUsd'] == pytest.approx(.011)
    assert result['selectionBasis']['score'] == 60
    assert result['selectionBasis']['eligibleRoutes'] == 4


def test_ranking_uses_weakest_reachable_offering_aa_score(routes):
    routes.profiles[1]['aaScores'] = _aa(90)
    weaker = _profile('cheap', .5, aaScores=_aa(10))
    weaker['provenance']['offeringId'] = 'cheap-weaker'
    routes.profiles.append(weaker)
    routes.slots.append(_slot('cheap', route_offering_id='cheap-weaker'))
    routes.profiles.append(_profile('same-cost-stronger', 1, aaScores=_aa(50)))
    routes.slots.append(_slot('same-cost-stronger'))
    result = _select(routes)
    assert result['modelId'] == 'same-cost-stronger'
    assert result['selectionBasis']['score'] == 50


@pytest.mark.parametrize('field,value', [
    ('contextWindow', 10_999), ('contextWindow', None),
    ('contextWindow', True), ('contextWindow', float('inf')), ('enabled', False),
])
def test_aa_cannot_replace_candidate_capacity_and_enablement(routes, field, value):
    routes.profiles[1][field] = value
    assert _select(routes)['modelId'] == 'main'


@pytest.mark.parametrize('config_key', ['providerId', 'provider_id'])
def test_conflicting_provider_restrictions_do_not_discard_one_pin(routes, config_key):
    routes.task['provider_id'] = 'supplier'
    routes.task['config'][config_key] = 'other-supplier'
    result = _select(routes)
    assert result['modelId'] == 'main'
    assert result['reason'] == 'conflicting_provider_pins'


def test_billing_provider_and_execution_pin_can_name_same_route(routes):
    routes.task['provider_id'] = 'supplier'
    routes.task['config']['providerId'] = routes.task['_pinned_provider_id']
    assert _select(routes)['modelId'] == 'cheap'


def test_astra_physical_capacity_is_not_its_economic_price_threshold(routes):
    for profile, slot in zip(routes.profiles, routes.slots):
        profile['contextWindow'] = slot.context_window = 1_000_000
        rate = 10 if profile['modelId'] == 'main' else 1
        profile['declaredPricing'] = {'currency': 'USD', 'contextTiers': [
            {'id': 'base', 'maxPromptTokens': 272_000, 'input': rate, 'output': rate},
            {'id': 'long', 'maxPromptTokens': 1_000_000, 'input': rate * 2, 'output': rate * 2},
        ]}
    result = _select(routes, input_tokens=500_000, output_tokens=16_384)
    assert result['modelId'] == 'cheap'
    assert result['costBasis']['tierId'] == 'long'
    assert result['projectedCostUsd'] == pytest.approx(1.032768)
    assert _select(routes, input_tokens=990_000, output_tokens=16_384)['modelId'] == 'main'


@pytest.mark.parametrize('mutation', ['missing_input', 'duplicate_limit', 'beyond_limit', 'unit', 'peak'])
def test_ambiguous_or_incomplete_tier_card_cannot_qualify_candidate(routes, mutation):
    row = {'id': 'base', 'maxPromptTokens': 20_000, 'input': .1, 'output': .1}
    card = {'currency': 'USD', 'contextTiers': [row]}
    if mutation == 'missing_input':
        row.pop('input')
    elif mutation == 'duplicate_limit':
        card['contextTiers'].append({**row, 'id': 'conflicting', 'input': .2})
    elif mutation == 'beyond_limit':
        row['maxPromptTokens'] = 9_999
    elif mutation == 'unit':
        row['unit'] = 'per_thousand_tokens'
    else:
        row['peakMul'] = 2
    routes.profiles[1]['declaredPricing'] = card
    assert _select(routes)['modelId'] == 'main'


@pytest.mark.parametrize('rate', [None, 0, -7, True, float('nan'), float('inf')])
def test_invalid_projection_exchange_rate_cannot_qualify_candidate(routes, monkeypatch, rate):
    import lib.pricing as pricing
    monkeypatch.setattr(pricing, 'DEFAULT_USD_CNY_RATE', rate)
    routes.profiles[1]['declaredPricing']['currency'] = 'CNY'
    assert _select(routes)['modelId'] == 'main'


@pytest.mark.parametrize('card', [
    {'input': -1, 'output': 1}, {'input': True, 'output': 1},
    {'input': float('nan'), 'output': 1}, {'input': 1, 'output': float('inf')},
    {'input': 1, 'output': 1, 'currency': 'EUR'},
    {'input': 1, 'output': 1, 'unit': 'per_thousand_tokens'},
    {'input': 1, 'output': 1, '_pricingSource': 'default_estimate'},
])
def test_catalog_does_not_label_invalid_or_guessed_prices_known(card):
    profiles = configured_model_profiles(providers=[{'id': 'catalog-test', 'models': [{
        'model_id': 'catalog-test-model', 'capabilities': ['text'],
        'context_window': 100_000, 'pricing': card, 'cost': .1,
    }]}])
    assert profiles[0]['price'] == {
        'known': False, 'blendedUsdPerMTok': None, 'source': 'unknown'}
    assert profiles[0]['aaScores'] == {}


@pytest.mark.parametrize('cheap_output_limit', [8_192, 32_768])
def test_summary_transport_capacity_is_not_the_priced_projection(routes, cheap_output_limit):
    routes.slots[0].max_output_tokens = 128_000
    routes.slots[1].max_output_tokens = cheap_output_limit
    # The old 2.2K-only preflight bought a route dispatch could not use.
    assert select_summary_route(routes.task, 10_000, 2_200)['modelId'] == 'cheap'
    result = select_summary_route(
        routes.task, 10_000, 2_200, capacity_output_tokens=65_536)
    assert result['modelId'] == 'main'
    # Transport headroom is not forecast output and never billed as input.
    assert result['projectedCostUsd'] == pytest.approx(.122)
    routes.slots[1].max_output_tokens = 65_536
    result = select_summary_route(
        routes.task, 10_000, 2_200, capacity_output_tokens=65_536)
    assert result['modelId'] == 'cheap'
    assert result['projectedCostUsd'] == pytest.approx(.0122)


def test_optional_compaction_prices_separate_input_and_capacity_before_payback(
        routes, monkeypatch):
    import lib.tasks_pkg.compaction._layer2._compact as compact
    routes.slots[0].max_output_tokens = 128_000
    routes.slots[1].max_output_tokens = 8_192
    routes.task.update(id='preflight-capacity', convId='preflight-capacity')
    messages = [
        {'role': 'system', 'content': 'rules'},
        {'role': 'user', 'content': 'preserve the original objective'},
        {'role': 'assistant', 'content': 'cold evidence ' * 10_000},
        {'role': 'user', 'content': 'continue the current task'},
        {'role': 'assistant', 'content': 'recent evidence'},
    ]
    original = deepcopy(messages)
    monkeypatch.setattr(compact, '_projected_summary_usage_tokens',
                        lambda *a, **k: 12_200)
    seen = []

    def payback(task, *, tokens_before, candidate_tokens, **kwargs):
        cost = kwargs['summary_cost']['costUsd']
        seen.append(cost)
        # A near-cutoff .05 budget admits the old cheap quote, not the
        # capacity-qualified main route. Neither route may be purchased.
        return {'dropped_tokens': tokens_before - candidate_tokens,
                'cache_read_tokens': 0, 'cache_rewrite_tokens': candidate_tokens,
                'rewrite_cost_tokens': candidate_tokens,
                'payback_rounds': 1 if cost < .05 else 100,
                'payback_limit_rounds': 2}

    monkeypatch.setattr(compact, '_proactive_cache_economics', payback)
    def no_dispatch(*args, **kwargs):
        pytest.fail('An unprofitable summary must not be dispatched')
    monkeypatch.setattr(compact, '_generate_query_aware_summary', no_dispatch)
    monkeypatch.setattr(compact, '_archive_transcript', no_dispatch)
    meta = {}
    compact.execute_compact_tool(
        messages, task=routes.task, _proactive_economic=True,
        preserve_budget_tokens=1,
        _result_meta=meta, _compaction_skip_archive=True)
    assert seen == [pytest.approx(.122)]
    assert meta['reason'] == 'cache_negative'
    assert messages == original


@pytest.mark.parametrize('evidence', [
    None, {}, {'intelligence': 99}, _aa(None), _aa(True), _aa('99'),
    _aa(-1), _aa(float('nan')), _aa(float('inf')),
    _aa(99, source='another-index'), _aa(99, match='fuzzy'),
    _aa(99, match='base-model'),
    {'source': 'Artificial Analysis', 'match': 'exact', 'coding': 99},
])
def test_missing_ambiguous_or_invalid_aa_never_qualifies_an_alternate(routes, evidence):
    routes.profiles[1]['aaScores'] = evidence
    result = _select(routes)
    assert result['modelId'] == 'main'
    assert result['reason'] == 'no_eligible_cheaper_aa_route'
    assert result['selectionBasis'] == {}


def test_zero_aa_is_known_not_missing(routes):
    routes.profiles[1]['aaScores'] = _aa(0)
    assert _select(routes)['selectionBasis']['score'] == 0


def test_tiny_price_increase_is_not_a_hidden_near_cheapest_tier(routes):
    routes.profiles[1]['aaScores'] = _aa(1)
    routes.profiles.append(_profile('near-cheap', 1.000001, aaScores=_aa(100)))
    routes.slots.append(_slot('near-cheap'))
    assert _select(routes)['modelId'] == 'cheap'


def test_candidate_with_unknown_aa_is_excluded_not_assumed_zero(routes):
    routes.profiles[1]['aaScores'] = {}
    routes.profiles.append(_profile('next-priced-with-aa', 2, aaScores=_aa(40)))
    routes.slots.append(_slot('next-priced-with-aa'))
    assert _select(routes)['modelId'] == 'next-priced-with-aa'


def test_equal_to_main_cost_does_not_buy_higher_aa(routes):
    routes.profiles[1]['declaredPricing'].update(input=10, output=10)
    routes.profiles[1]['aaScores'] = _aa(100)
    routes.profiles[0]['aaScores'] = _aa(1)
    assert _select(routes)['modelId'] == 'main'


def test_one_reachable_offering_missing_aa_disqualifies_whole_candidate(routes):
    profile = _profile('cheap', .5, aaScores={})
    profile['provenance']['offeringId'] = 'missing-aa-offering'
    routes.profiles.append(profile)
    routes.slots.append(_slot('cheap', route_offering_id='missing-aa-offering'))
    assert _select(routes)['modelId'] == 'main'


def test_best_reachable_main_price_not_expensive_main_member_controls_savings(routes):
    profile = _profile('main', .1)
    profile['provenance']['offeringId'] = 'less-expensive-main'
    routes.profiles.append(profile)
    routes.slots.append(_slot('main', route_offering_id='less-expensive-main'))
    assert _select(routes)['modelId'] == 'main'


def test_pending_main_identity_cannot_authorize_aa_selection(routes):
    routes.profiles[0]['provenance']['identityState'] = 'pending_identity'
    assert _select(routes)['reason'] == 'main_route_unresolved'


@pytest.mark.parametrize('fresh', [False, True])
def test_real_cached_aa_projection_is_fresh_exact_and_does_not_fetch(routes, monkeypatch, fresh):
    from lib.model_catalog import aa

    def no_fetch(*args, **kwargs):
        pytest.fail('Summary routing must never fetch AA inline')

    monkeypatch.setattr(aa, '_fetch_dataset', no_fetch)
    monkeypatch.setattr(aa, '_memo', {
        'fetched_at': time.time() if fresh else 1,
        'models': aa._parse_dataset({'data': [{
            'name': 'Cheap', 'slug': 'cheap',
            'evaluations': {'artificial_analysis_intelligence_index': 60},
        }]}),
    })
    scores = aa.cached_routing_scores([{'model_id': 'cheap'}, {'model_id': 'cheap-fast'}])
    assert '::cheap-fast' not in scores
    routes.profiles[1]['aaScores'] = scores.get('::cheap', {})
    result = _select(routes)
    assert result['modelId'] == ('cheap' if fresh else 'main')


@pytest.mark.parametrize('mutation', ['unchanged', 'higher_aa_tie', 'price', 'missing_aa'])
def test_real_aa_route_is_revalidated_before_optional_summary_spending(routes, monkeypatch, mutation):
    import lib.llm_dispatch as dispatch
    from lib.llm.stream_result import ProviderStreamResult
    from lib.tasks_pkg.compaction._constants import (
        _SUMMARY_MAX_TOKENS, _SUMMARY_DISPATCH_MAX_TOKENS,
    )
    from lib.tasks_pkg.compaction._layer2 import _summary as summary
    from lib.tasks_pkg.compaction._tokens import _estimate_total_tokens

    for slot in routes.slots:
        slot.max_output_tokens = 128_000
    source = [{'role': 'user', 'content': 'Preserve the original complete source.'}]
    original = deepcopy(source)
    # Match production prompt counting while keeping archive/formatting I/O hermetic.
    monkeypatch.setattr(summary, '_codex_subscription_provider', lambda task: '')
    monkeypatch.setattr(summary, '_archived_prior_receipt', lambda task: '')
    monkeypatch.setattr(summary, '_summary_input_char_budget', lambda task: 12000)
    monkeypatch.setattr(summary, '_build_summary_user_content', lambda **kwargs: 'fixed summary input')
    prompt = [{'role': 'system', 'content': summary._SUMMARY_SYSTEM_PROMPT},
              {'role': 'user', 'content': 'fixed summary input'}]
    expected = select_summary_route(routes.task, _estimate_total_tokens(prompt), _SUMMARY_MAX_TOKENS,
                                    capacity_output_tokens=_SUMMARY_DISPATCH_MAX_TOKENS)
    assert expected['modelId'] == 'cheap'
    if mutation == 'higher_aa_tie':
        routes.profiles.append(_profile('aa-tie-winner', 1, aaScores=_aa(80)))
        routes.slots.append(_slot('aa-tie-winner', max_output_tokens=128_000))
    elif mutation == 'price':
        routes.profiles[1]['declaredPricing'].update(input=2, output=2)
    elif mutation == 'missing_aa':
        routes.profiles[1]['aaScores'] = {}
    calls = []

    def stream(*args, **kwargs):
        calls.append(kwargs)
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': '### Objective\nComplete summary.'},
            'stop', {'prompt_tokens': 10, 'completion_tokens': 10})

    monkeypatch.setattr(dispatch, 'dispatch_stream', stream)
    usage = {}
    result = summary._generate_query_aware_summary(
        source, 'continue', task=routes.task, expected_route=expected,
        on_delta=lambda chunk: None, usage_out=usage)
    assert source == original
    if mutation == 'unchanged':
        assert result == '### Objective\nComplete summary.'
        assert len(calls) == 1
        assert calls[0]['prefer_model'] == 'cheap'
        assert calls[0]['strict_model'] is True
        assert calls[0]['thinking_enabled'] is False
        assert usage['_summaryRoute']['selectionBasis']['score'] == 50
    else:
        assert result is None
        assert calls == []


@pytest.mark.parametrize('same_owner', [True, False])
def test_real_v2_offerings_carry_exact_aa_and_actual_prices_into_selection(routes, monkeypatch, same_owner):
    from lib.model_catalog import aa
    from lib.model_routing import (
        InMemoryModelRoutingRepository, OwnerBoundary, empty_document,
    )
    import lib.model_profiles as profiles_module

    owner = OwnerBoundary.create(41, 'tenant-a')
    repository = InMemoryModelRoutingRepository()
    document = empty_document()
    document['creators'] = [{'creator_id': 'fixture', 'name': 'Fixture'}]
    document['providers'] = [{'provider_id': 'supplier', 'name': 'Supplier', 'scope': 'owner'}]
    document['provider_accesses'] = [{
        'provider_access_id': 'access-a', 'provider_id': 'supplier', 'enabled': True,
        'quota_policy': {},
    }]
    document['connections'] = [{
        'connection_id': 'connection-a', 'provider_access_id': 'access-a',
        'base_url': 'https://supplier.example/v1', 'protocol': 'openai',
        'enabled': True, 'priority': 0,
    }]
    aa_rows = []
    for model, price, score in [('main', 10, 90), ('cheap', 1, 30), ('cheap-aa', 1, 50)]:
        ref = {'creator_id': 'fixture', 'model_id': model}
        document['models'].append({
            **ref, 'display_name': model, 'quality_rank': 0,
            'capabilities': ['text'], 'context_window': 200_000,
            'list_pricing': {'input': price, 'output': price, 'currency': 'USD',
                             'unit': 'per_million_tokens'},
            'inference_profile': {'reasoning': {'support': 'unsupported'}},
            'registration_metadata': {
                'release_date': '2026-01-01', 'checked_at': '2026-10-01',
                'source_urls': ['https://supplier.example/model-fixtures'],
            },
        })
        document['offerings'].append({
            'offering_id': 'offering-' + model, 'provider_access_id': 'access-a',
            'identity_state': 'confirmed', 'model': ref, 'enabled': True,
            'capabilities': ['text'], 'context_window': 200_000, 'priority': 0,
            'actual_pricing': {'input': price, 'output': price, 'currency': 'USD',
                               'unit': 'per_million_tokens'},
        })
        document['deployments'].append({
            'deployment_id': 'deployment-' + model, 'offering_id': 'offering-' + model,
            'connection_id': 'connection-a', 'wire_model_id': 'wire-' + model,
            'priority': 0, 'probe_status': 'unprobed', 'identity_confidence': 'high',
        })
        aa_rows.append({'name': model, 'slug': model, 'model_creator': {'name': 'Fixture'},
                        'evaluations': {'artificial_analysis_intelligence_index': score}})
    repository.compare_and_swap(owner, document, expected_revision=0)
    monkeypatch.setattr(aa, '_memo', {'fetched_at': time.time(),
                                    'models': aa._parse_dataset({'data': aa_rows})})
    routes.slots.append(_slot('cheap-aa'))
    routes.task['config'] = {}
    seen = []

    def owner_profiles(**kwargs):
        seen.append(kwargs)
        return configured_model_profiles(repository=repository, **kwargs)

    monkeypatch.setattr(profiles_module, 'configured_model_profiles', owner_profiles)
    profiles = configured_model_profiles(owner_user_id=41, tenant_id='tenant-a',
                                         repository=repository)
    assert all(profile['profileSource'] == 'unqualified' for profile in profiles)
    assert all(profile['aaScores']['match'] == 'exact' for profile in profiles)
    assert profiles[1]['declaredPricing']['input'] == 1
    if not same_owner:
        routes.task['_userId'] = 42
        routes.task['_principalContext'].update(owner_user_id=42, subject_id='user:42')
    result = _select(routes)
    assert seen == [{'owner_user_id': 41 if same_owner else 42, 'tenant_id': 'tenant-a'}]
    assert result['modelId'] == ('cheap-aa' if same_owner else 'main')
    if same_owner:
        assert result['selectionBasis']['score'] == 50
        assert result['projectedCostUsd'] == pytest.approx(.011)
    else:
        assert result['reason'] == 'main_route_unresolved'


@pytest.mark.parametrize('failure', ['length', 'content_filter', 'tool_calls',
                                      'unverified', 'error', 'cancel'])
def test_aa_selected_route_failure_never_returns_partial_summary_or_mutates_source(routes, monkeypatch, failure):
    import lib.llm_dispatch as dispatch
    from lib.llm.stream_result import ProviderStreamResult
    from lib.tasks_pkg.compaction._layer2 import _summary as summary

    routes.task['config'] = {}
    routes.task['aborted'] = False  # Live abort wire exists before dispatch.
    for slot in routes.slots:
        slot.max_output_tokens = 128_000
    source = [{'role': 'user', 'content': 'Original binding constraint and complete history.'}]
    original = deepcopy(source)
    monkeypatch.setattr(summary, '_codex_subscription_provider', lambda task: '')
    monkeypatch.setattr(summary, '_archived_prior_receipt', lambda task: '')
    monkeypatch.setattr(summary, '_summary_input_char_budget', lambda task: 12000)
    calls = []

    def failed(*args, **kwargs):
        calls.append(kwargs)
        if failure == 'error':
            raise RuntimeError('synthetic provider failure')
        if failure == 'cancel':
            routes.task['aborted'] = True
        finish = (failure if failure in {'length', 'content_filter', 'tool_calls'}
                  else None if failure == 'unverified' else 'stop')
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': 'Incomplete summary; must not replace history.'},
            finish, {'prompt_tokens': 10, 'completion_tokens': 10})

    monkeypatch.setattr(dispatch, 'dispatch_stream', failed)
    result = summary._generate_query_aware_summary(
        source, 'continue', task=routes.task, on_delta=lambda chunk: None)
    assert result is None
    assert source == original
    assert len(calls) == 1
    assert calls[0]['prefer_model'] == 'cheap'
    assert calls[0]['strict_model'] is True
