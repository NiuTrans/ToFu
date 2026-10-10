"""Declared inference survives authority and both dispatch materializations."""
import copy
import json

import pytest

from lib.model_routing.inference import (
    profile_from_metadata, resolve_inference_profile, validate_inference_profile,
)
from lib.model_routing.domain import ModelRoutingError, parse_native_model_selection
from lib.model_routing.migration import plan_legacy_migration

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('reasoning', [
    {'supported_efforts': ['low'], 'disabled_effort': 'none'},
    {'supported_efforts': ['low'], 'effort_map': {'high': 'max'}},
    {'supported_efforts': [], 'support': 'supported', 'wire_format': 'reasoning_effort'},
])
def test_contradictory_declared_effort_fields_rejected_at_metadata_boundary(reasoning):
    with pytest.raises(ModelRoutingError):
        validate_inference_profile({'reasoning': reasoning})


def test_partial_overrides_binary_controls_and_portable_plugins_remain_valid():
    for reasoning in ({'effort_map': {'high': 'max'}}, {'disabled_effort': 'none'},
                      {'wire_format': 'future-plugin'},
                      {'support': 'supported', 'wire_format': 'thinking_type', 'supported_efforts': []}):
        assert validate_inference_profile({'reasoning': reasoning})['reasoning'] == reasoning
    resolved = resolve_inference_profile(
        model={'inference_profile': {'reasoning': {'supported_efforts': ['low', 'max'],
            'default_effort': 'max', 'effort_map': {'medium': 'low', 'ultra': 'max'}}}},
        offering={'inference_profile': {'reasoning': {'supported_efforts': ['low']}}})
    assert resolved['reasoning'] == {'supported_efforts': ['low'], 'effort_map': {'medium': 'low'}}


def test_narrowed_ladder_drops_unrepresentable_can_disable():
    """A provider catalog that narrows the effort ladder and drops the off-rung
    must not inherit an unrepresentable can_disable from the model's official
    defaults; otherwise the merged deployment profile cannot compile 'off'.
    """
    model = {'inference_profile': {'reasoning': {
        'can_disable': True, 'default_effort': 'medium', 'support': 'supported',
        'supported_efforts': ['none', 'low', 'medium', 'high', 'xhigh', 'max'],
        'wire_format': 'reasoning_effort'}, 'source': 'official'}}
    narrowed = {'inference_profile': {'reasoning': {
        'default_effort': 'medium', 'support': 'supported',
        'supported_efforts': ['low', 'medium', 'high', 'xhigh', 'max', 'ultra'],
        'wire_format': 'reasoning_effort'}, 'source': 'provider_catalog'}}
    resolved = resolve_inference_profile(model=model, offering=narrowed,
                                         connection={'protocol': 'openai_responses'}, deployment={})
    assert resolved['reasoning']['can_disable'] is False
    assert resolved['reasoning']['supported_efforts'] == ['low', 'medium', 'high', 'xhigh', 'max', 'ultra']

    # A narrowed ladder that retains an off-rung keeps can_disable.
    keep_none = {'inference_profile': {'reasoning': {
        'support': 'supported', 'supported_efforts': ['none', 'low', 'medium'],
        'default_effort': 'medium', 'wire_format': 'reasoning_effort'}}}
    retained = resolve_inference_profile(model=model, offering=keep_none,
                                         connection={'protocol': 'openai_responses'}, deployment={})
    assert retained['reasoning']['can_disable'] is True

    # An explicit provider/owner can_disable always wins over narrowing.
    explicit = {'inference_profile': {'reasoning': {
        'support': 'supported', 'supported_efforts': ['low', 'medium'],
        'default_effort': 'medium', 'wire_format': 'reasoning_effort',
        'can_disable': True}}}
    honored = resolve_inference_profile(model=model, offering=explicit,
                                        connection={'protocol': 'openai_responses'}, deployment={})
    assert honored['reasoning']['can_disable'] is True


def test_astra_ultra_is_exact_model_contract_without_widening_gpt56():
    from pathlib import Path
    from lib.provider_template_recipes import normalize_provider_template
    raw = json.loads((Path(__file__).parents[2] / 'lib/model_info/data/openai.json').read_text())
    recipes = {row['model_id']: row for row in normalize_provider_template(raw)['offering_recipes']}
    assert recipes['gpt-6-astra']['inference_profile']['reasoning']['supported_efforts'][-1] == 'ultra'
    assert recipes['gpt-5.6']['inference_profile']['reasoning']['supported_efforts'][-1] == 'max'


def test_resolution_is_detached_and_unsupported_clears_details():
    model = {'inference_profile': {'reasoning': {'support': 'supported', 'wire_format': 'thinking_type', 'default_budget_tokens': 100}}}
    connection = {'inference_profile': {'reasoning': {'can_disable': True}}}
    resolved = resolve_inference_profile(model=model, connection=connection)
    assert resolved['reasoning']['default_budget_tokens'] == 100
    resolved['reasoning']['default_budget_tokens'] = 200
    assert model['inference_profile']['reasoning']['default_budget_tokens'] == 100
    assert resolve_inference_profile(model=model, deployment={'inference_profile': {'reasoning': {'support': 'unsupported'}}})['reasoning'] == {'support': 'unsupported', 'wire_format': 'none'}
    assert resolve_inference_profile(model={'model_id': 'unknown'}) == {}


@pytest.mark.parametrize('profile', [
    {'secret': 'no'}, {'reasoning': {'default_budget_tokens': True}},
    {'reasoning': {'max_budget_tokens': 1000001}},
    {'reasoning': {'supported_efforts': ['low', 'low']}},
    {'reasoning': {'min_budget_tokens': 30, 'max_budget_tokens': 20}},
    {'sampling': {'temperature': float('nan')}},
    {'features': {str(i): 'supported' for i in range(65)}},
])
def test_invalid_profiles_fail_closed(profile):
    with pytest.raises(ModelRoutingError):
        validate_inference_profile(profile)


def test_metadata_declared_efforts_order_and_no_name_heuristics():
    assert profile_from_metadata({'model_id': 'gpt-reasoning'}) == {}
    profile = profile_from_metadata({'supported_reasoning_levels': [{'effort': 'high'}, 'low'], 'default_reasoning_level': 'low'})
    assert profile['reasoning']['supported_efforts'] == ['high', 'low']


def test_template_migration_save_reload_and_both_slots(monkeypatch):
    from tests.llm_dispatch.test_dispatcher_model_routing_v2 import _build
    from lib.model_routing import InMemoryModelRoutingRepository, OwnerBoundary
    from lib.model_routing.dispatch_adapter import mint_routed_slot_group, dispose_routed_slot_group
    import lib.llm_dispatch.ephemeral as ephemeral
    monkeypatch.setattr(ephemeral, '_preflight_enabled', lambda: False)

    template = {'providers': [{'id': 'explicit-provider', 'name': 'Explicit',
        'base_url': 'http://127.0.0.1:19876/v1', 'protocol': 'openai_responses',
        'responses_profile': 'compatible', 'thinking_format': 'none',
        'faces': {'special': {'base_url': 'http://127.0.0.1:19876/responses', 'responses_profile': 'openai', 'thinking_format': 'reasoning_effort'}},
        'models': [{'model_id': 'unknown-explicit-model', 'face': 'special',
            'supported_reasoning_levels': ['low', {'effort': 'high'}],
            'default_reasoning_level': 'high', 'max_output_tokens': 8192,
            'inference_profile': {'reasoning': {'can_disable': False}}}]}]}
    plan = plan_legacy_migration(template, official_directory=[{
        'creator_id': 'test-creator', 'model_id': 'unknown-explicit-model',
    }])
    assert not plan.blocking_issues
    document = json.loads(json.dumps(plan.document))
    assert 'inference_profile' not in document['models'][0]  # provider facts stay scoped
    from tests.support.model_registration import complete_registration
    document = complete_registration(document)
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(1)
    repository.compare_and_swap(boundary, document, expected_revision=0)
    loaded = repository.get(boundary).document
    dispatcher = _build(monkeypatch, loaded)
    assert len(dispatcher.slots) == 1
    pooled = dispatcher.slots[0]
    assert pooled.responses_profile == 'openai'
    assert pooled.inference_profile['reasoning']['supported_efforts'] == ['low', 'high']
    # Reuse an isolated pool; endpoint validation stays local, never dials it.
    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher', lambda: dispatcher)
    selection = parse_native_model_selection({'model': {'provider_id': loaded['providers'][0]['provider_id'], 'offering_id': loaded['offerings'][0]['offering_id']}})
    group = mint_routed_slot_group(repository, boundary, selection)
    try:
        scoped = group.primary.slot
        assert scoped.inference_profile == pooled.inference_profile
        assert scoped.responses_profile == pooled.responses_profile
        scoped.inference_profile['reasoning']['supported_efforts'].append('custom')
        assert pooled.inference_profile['reasoning']['supported_efforts'] == ['low', 'high']
    finally:
        dispose_routed_slot_group(group)


def test_canonical_sections_endpoint_override_and_bounded_extras():
    profile = resolve_inference_profile(
        model={'inference_profile': {'reasoning': {'support': 'supported', 'wire_format': 'reasoning_effort'}, 'sampling': {'temperature': 'allow'}}},
        offering={'inference_profile': {'reasoning': {'can_disable': False}}},
        connection={'inference_profile': {'reasoning': {'wire_format': 'thinking_type', 'thinking_extra': {'budget_tokens': 1024}}, 'responses': {'feature_profile': 'openai'}}},
        deployment={'inference_profile': {'reasoning': {'can_disable': True}, 'sampling': {'temperature': 'omit', 'omit_params': ['top_p']}, 'responses': {'feature_profile': 'codex'}}})
    assert profile['reasoning']['wire_format'] == 'thinking_type'
    assert profile['reasoning']['can_disable'] is True
    assert profile['sampling'] == {'temperature': 'omit', 'omit_params': ['top_p']}
    assert profile['responses'] == {'feature_profile': 'codex'}
    for invalid in ({'nested': {}}, {'number': float('inf')}, {'text': 'x' * 257}):
        with pytest.raises(ModelRoutingError):
            validate_inference_profile({'reasoning': {'thinking_extra': invalid}})


def test_all_template_protocol_metadata_reaches_compiled_connections(monkeypatch):
    from lib.provider_template_recipes import compile_provider_template_bundle
    for protocol in ('openai', 'anthropic', 'openai_responses'):
        template = {'key': 'declared-profile', 'name': 'Declared', 'base_url': 'https://example.com/v1',
                    'protocol': protocol, 'responses_profile': 'compatible',
                    'inference_profile': {'sampling': {'temperature': 'omit'}},
                    'offering_recipes': [{'model_id': 'unknown-profile-model'}]}
        monkeypatch.setattr('lib.provider_template_recipes.load_provider_templates', lambda: [template])
        bundle = compile_provider_template_bundle(template['key'])
        connection = bundle['connections'][0]
        assert connection['protocol'] == protocol
        assert connection['inference_profile']['responses']['feature_profile'] == 'compatible'
        assert connection['inference_profile']['sampling']['temperature'] == 'omit'


@pytest.mark.parametrize('protocol', ['openai', 'anthropic', 'openai_responses'])
@pytest.mark.parametrize('location', ['faces', 'endpoints'])
@pytest.mark.parametrize('override', [{'can_disable': False}, {'support': 'unknown'}, {'support': 'unsupported'}])
def test_partial_face_profile_inherits_or_clears(protocol, location, override):
    from lib.provider_template_recipes import provider_from_template
    base = {'reasoning': {'support': 'supported', 'wire_format': 'reasoning_effort',
                          'supported_efforts': ['none', 'high'], 'default_effort': 'high',
                          'can_disable': True},
            'sampling': {'temperature': 'omit', 'omit_params': ['top_p']}}
    face = {'base_url': 'http://127.0.0.1:19876/alternate',
            'inference_profile': {'reasoning': override}}
    raw = {'key': 'partial-profile', 'base_url': 'http://127.0.0.1:19876/v1',
           'protocol': protocol, 'inference_profile': base,
           'offering_recipes': [{'model_id': 'exact-profile-model'}],
           location: {'alternate': face} if location == 'faces' else [face]}
    original = copy.deepcopy(raw)
    provider = provider_from_template(raw, raw['key'])
    plan = plan_legacy_migration({'providers': [provider]}, official_directory=[{
        'creator_id': 'test-creator', 'model_id': 'exact-profile-model'}])
    assert not plan.blocking_issues
    assert raw == original
    connections = {row['base_url']: row for row in plan.document['connections']}
    default = connections[raw['base_url']]
    alternate = connections[face['base_url']]
    assert default['inference_profile']['reasoning'] == base['reasoning']
    assert alternate['protocol'] == protocol
    profile = alternate['inference_profile']
    assert profile['sampling'] == base['sampling']
    if override.get('support') == 'unknown':
        assert profile['reasoning'] == {'support': 'unknown'}
    elif override.get('support') == 'unsupported':
        assert profile['reasoning'] == {'support': 'unsupported', 'wire_format': 'none'}
    else:
        assert profile['reasoning'] == {**base['reasoning'], **override}


@pytest.mark.parametrize('protocol', ['openai', 'anthropic', 'openai_responses'])
def test_shipped_exact_contract_normalizes_migrates_and_binds(monkeypatch, protocol):
    from pathlib import Path
    from lib.provider_template_recipes import normalize_provider_template, provider_from_template
    from tests.llm_dispatch.test_dispatcher_model_routing_v2 import _build
    raw = json.loads((Path(__file__).parents[2] / 'lib/model_info/data/openai.json').read_text())
    contract = copy.deepcopy(raw['contract'])
    raw['protocol'] = protocol
    raw['base_url'] = 'http://127.0.0.1:19876/v1'
    raw['offering_recipes'] = [row for row in raw['offering_recipes']
                              if row['model_id'] in {'gpt-6-astra', 'gpt-5.4'}]
    raw['offering_recipes'].append({'model_id': 'gpt-6-astra-neighbor', 'capabilities': ['text']})
    before = copy.deepcopy(raw)
    normalized = normalize_provider_template(raw)
    assert raw == before
    assert normalized['contract'] == contract
    expected = {'support': 'supported', 'wire_format': 'reasoning_effort',
                'supported_efforts': contract['models']['gpt-6-astra']['reasoning_efforts'],
                'default_effort': contract['default_reasoning_effort'],
                'can_disable': 'none' in contract['reasoning_efforts']}
    assert normalized['offering_recipes'][0]['inference_profile']['reasoning'] == expected
    for recipe in normalized['offering_recipes'][1:]:
        assert 'inference_profile' not in recipe
    provider = provider_from_template(normalized, 'exact-contract')
    plan = plan_legacy_migration({'providers': [provider]}, official_directory=[
        {'creator_id': 'openai', 'model_id': row['model_id']} for row in provider['models']])
    assert not plan.blocking_issues
    models = {row['model_id']: row for row in plan.document['models']}
    assert 'inference_profile' not in models['gpt-6-astra-neighbor']
    assert models['gpt-5.4']['inference_profile']['source'] == 'official'
    # Activation requires an independently declared complete Model baseline.
    from tests.support.model_registration import complete_registration
    registered = complete_registration(plan.document)
    for row in registered['models']:
        if row['model_id'] == 'gpt-6-astra':
            row['inference_profile']['reasoning'] = copy.deepcopy(expected)
        elif row['model_id'] == 'gpt-6-astra-neighbor':
            row.pop('registration_metadata', None)
            row.pop('inference_profile', None)
    dispatcher = _build(monkeypatch, json.loads(json.dumps(registered)))
    slots = {slot.model: slot for slot in dispatcher.slots}
    resolved = slots['gpt-6-astra'].inference_profile['reasoning']
    assert all(resolved[key] == value for key, value in expected.items())
    assert slots['gpt-5.4'].inference_profile['reasoning']['support'] == 'supported'
    assert 'gpt-6-astra-neighbor' not in slots  # incomplete discoveries never activate


@pytest.mark.parametrize('override', [{'can_disable': True}, {'support': 'unknown'}, {'support': 'unsupported'}])
def test_contract_recipe_overrides_use_section_precedence(override):
    from lib.provider_template_recipes import normalize_provider_template
    raw = {'key': 'no-name-heuristics',
           'contract': {'models': {'arbitrary-id': {}}, 'reasoning_efforts': ['low', 'high'],
                        'default_reasoning_effort': 'low'},
           'offering_recipes': [{'model_id': 'arbitrary-id', 'inference_profile': {
               'reasoning': override, 'sampling': {'temperature': 'omit'}}}]}
    recipe = normalize_provider_template(raw)['offering_recipes'][0]
    profile = recipe['inference_profile']
    assert profile['sampling'] == {'temperature': 'omit'}
    if 'support' in override:
        expected = dict(override)
        if override['support'] == 'unsupported':
            expected['wire_format'] = 'none'
        assert profile['reasoning'] == expected
    else:
        assert profile['reasoning'] == {'support': 'supported', 'wire_format': 'reasoning_effort',
            'supported_efforts': ['low', 'high'], 'default_effort': 'low', 'can_disable': True}
    raw['offering_recipes'][0].pop('inference_profile')
    assert normalize_provider_template(raw)['offering_recipes'][0]['inference_profile']['reasoning']['can_disable'] is False
