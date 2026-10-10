"""Public Model inheritance, scoped request names, and saved catalog repair."""
import copy
import json
from types import SimpleNamespace

import pytest

from lib.llm.body import build_body
from lib.llm.reasoning import ReasoningContractError
from lib.model_routing import empty_document, normalize_document, plan_legacy_migration
from lib.model_routing.catalog_identity import reconcile_catalog_identities
from lib.model_routing.inference import candidate_inference_metadata, resolve_inference_profile
from lib.model_routing.model_defaults import apply_model_defaults
from lib.provider_template_recipes import compile_provider_template_bundle

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('model_id,effort', [('kimi-k3', 'max'), ('grok-4.6', 'xhigh')])
def test_mainstream_templates_share_model_profile_and_presets(model_id, effort):
    providers = ('catpaw', 'meituan') if model_id == 'kimi-k3' else ('xai', 'meituan')
    bundles = [compile_provider_template_bundle(key, selected_model_ids=[model_id])
               for key in providers]
    assert bundles[0]['models'] == bundles[1]['models']
    for bundle in bundles:
        profile = resolve_inference_profile(model=bundle['models'][0],
            offering=bundle['offerings'][0], connection=bundle['connections'][0])
        assert profile['reasoning']['wire_format'] == 'reasoning_effort'
        for wire_name in (model_id, 'provider-only-wire-opaque-19'):
            body = build_body(wire_name, [{'role': 'user', 'content': 'hi'}],
                              thinking_depth='max', inference_profile=profile)
            assert body['reasoning_effort'] == effort
            assert 'enable_thinking' not in body
            assert 'temperature' not in body
        # Off on a cannot-disable model degrades to the cheapest legal rung
        # instead of failing the whole turn (the former whole-turn 500).
        off = build_body('wire', [{'role': 'user', 'content': 'hi'}],
                         thinking_depth='off', inference_profile=profile)
        assert off['reasoning_effort'] == 'low'
        assert off['_reasoning_resolution']['reason'] == \
            'disable_unavailable_fell_back_to_lowest'


@pytest.mark.parametrize('protocol', ['openai', 'anthropic', 'responses'])
@pytest.mark.parametrize('model_id,creator,effort', [('kimi-k3', 'moonshot', 'max'), ('grok-4.6', 'xai', 'xhigh')])
def test_public_preset_reaches_sealed_protocol_payload(monkeypatch, protocol, model_id, creator, effort):
    import json
    from lib.llm._sse_core import prepare_request
    monkeypatch.setattr('lib.token_counter.count_tokens', lambda *a, **kw: {'tokens': 20})
    monkeypatch.setattr('lib.tasks_pkg.compaction._tokens.resolve_model_context_limit', lambda *a, **kw: 200000)
    monkeypatch.setattr('lib.desktop.egress.route_request', lambda *a, **kw: 'direct')
    model = {'creator_id': creator, 'model_id': model_id}
    apply_model_defaults(model)
    body = build_body('opaque-provider-request', [{'role': 'user', 'content': 'hi'}],
        thinking_depth='max', inference_profile=model['inference_profile'])
    plan = prepare_request(body, base_url='https://example.invalid/v1', api_protocol=protocol)
    wire = json.loads(plan.body.payload)
    actual = (wire['output_config']['effort'] if protocol == 'anthropic' else
              wire['reasoning']['effort'] if protocol == 'responses' else wire['reasoning_effort'])
    assert actual == effort
    assert wire['model'] == 'opaque-provider-request'
    assert 'enable_thinking' not in wire


def test_provider_override_is_scoped_and_request_name_does_not_change_model(monkeypatch):
    import lib.provider_template_recipes as recipes
    model = {'creator_id': 'xai', 'model_id': 'grok-4.6', 'display_name': 'Owner Grok',
             'capabilities': ['text', 'vision', 'thinking'], 'context_window': 500000, 'quality_rank': 90}
    apply_model_defaults(model)
    before = copy.deepcopy(model)
    template = recipes.normalize_provider_template({'key': 'example', 'category': 'relay',
        'base_url': 'https://example.invalid', 'offering_recipes': [{'model_id': 'grok-4.6',
        'request_ids': ['actual-provider-request-name'],
        'inference_profile': {'reasoning': {'can_disable': True,
            'supported_efforts': ['none', 'low'], 'default_effort': 'low'}}}]})
    monkeypatch.setattr(recipes, 'load_provider_templates', lambda: [template])
    bundle = recipes.compile_provider_template_bundle('example', official_directory=[model])
    inherited = bundle['models'][0]
    assert inherited['inference_profile'] == before['inference_profile']
    assert inherited['display_name'] == 'Owner Grok'
    offering = bundle['offerings'][0]
    assert set(offering['capabilities']) - {'cheap'} == set(model['capabilities'])
    assert offering['context_window'] == 500000
    assert bundle['deployments'][0]['wire_model_id'] == 'actual-provider-request-name'
    assert bundle['deployments'][0]['probe_status'] == 'unprobed'
    profile = resolve_inference_profile(model=inherited, offering=offering)
    assert build_body('actual-provider-request-name', [{'role': 'user', 'content': 'hi'}], thinking_depth='off',
                      inference_profile=profile)['reasoning_effort'] == 'none'
    assert model == before


def test_existing_owner_profile_is_preserved_and_defaults_are_idempotent():
    model = {'creator_id': 'moonshot', 'model_id': 'kimi-k3',
             'inference_profile': {'reasoning': {'support': 'unsupported'}}}
    assert apply_model_defaults(model)
    assert model['inference_profile']['reasoning'] == {'support': 'unsupported', 'wire_format': 'none'}
    assert not apply_model_defaults(model)
    unknown = {'creator_id': 'moonshot', 'model_id': 'kimi-k3-future-preview'}
    assert not apply_model_defaults(unknown)
    assert 'inference_profile' not in unknown


def test_corrected_shipped_profile_replaces_only_exact_previous_defaults():
    from lib.model_routing import model_defaults
    data = json.loads(model_defaults._PATH.read_text())
    group = next(g for g in data['groups'] if 'claude-haiku-4.5' in g['model_ids'])
    previous = copy.deepcopy(group['previous_profiles'][0])
    model = {'creator_id': 'anthropic', 'model_id': 'claude-haiku-4.5', 'inference_profile': previous}
    apply_model_defaults(model)
    reasoning = model['inference_profile']['reasoning']
    assert reasoning['default_budget_tokens'] == 1024
    assert 'supported_efforts' not in reasoning
    assert 'enabled_type' not in reasoning
    assert not apply_model_defaults(model)
    owner = copy.deepcopy(previous)
    owner['source'] = 'configured'
    model['inference_profile'] = owner
    apply_model_defaults(model)
    assert model['inference_profile']['source'] == 'configured'
    assert model['inference_profile']['reasoning']['supported_efforts'] == owner['reasoning']['supported_efforts']


@pytest.mark.parametrize('creator,model_id,depths', [
    ('openai', 'gpt-5.5', ['none', 'low', 'medium', 'high', 'xhigh']),
    ('deepseek', 'deepseek-v4-flash', ['low', 'high', 'max']),
    ('deepseek', 'deepseek-v4-pro', ['low', 'high', 'max']),
    ('google', 'gemini-3.8-flash', ['low', 'medium', 'high']),
    ('google', 'gemini-3.5-flash-lite', ['minimal', 'low', 'medium', 'high']),
    ('anthropic', 'claude-sonnet-5', ['low', 'medium', 'high', 'xhigh', 'max']),
])
def test_mainstream_catalog_efforts_have_explicit_exact_defaults(creator, model_id, depths):
    model = {'creator_id': creator, 'model_id': model_id}
    assert apply_model_defaults(model)
    assert model['inference_profile']['reasoning']['supported_efforts'] == depths


def test_provider_can_narrow_ladder_without_repeating_public_default():
    model = {'creator_id': 'moonshot', 'model_id': 'kimi-k3'}
    apply_model_defaults(model)
    profile = resolve_inference_profile(model=model, offering={
        'inference_profile': {'reasoning': {'supported_efforts': ['low']}}})
    assert profile['reasoning']['supported_efforts'] == ['low']
    assert 'default_effort' not in profile['reasoning']


def test_catalog_default_input_cannot_exceed_its_memory_budget(monkeypatch, tmp_path):
    from lib.model_routing import model_defaults
    oversized = tmp_path / 'oversized.json'
    oversized.write_bytes(b' ' * (262144 + 1))
    monkeypatch.setattr(model_defaults, '_PATH', oversized)
    with pytest.raises(ValueError, match='256 KiB'):
        model_defaults.apply_model_defaults({'creator_id': 'xai', 'model_id': 'grok-4.6'})


def test_discovery_with_only_a_name_inherits_public_metadata():
    from lib.model_routing.discovered_provider import build_discovered_provider_bundle
    model = {'creator_id': 'xai', 'model_id': 'grok-4.6', 'display_name': 'Grok',
        'capabilities': ['text', 'vision', 'thinking'], 'context_window': 500000, 'quality_rank': 90}
    apply_model_defaults(model)
    bundle = build_discovered_provider_bundle(provider_id='relay', display_name='Relay', brand='generic',
        base_url='https://example.invalid', models=[{'model_id': 'grok-4.6'}], official_directory=[model])
    offering = bundle['offerings'][0]
    assert offering['identity_state'] == 'confirmed'
    assert offering['capabilities'] == sorted(model['capabilities'])
    assert offering['context_window'] == model['context_window']
    assert bundle['models'][0]['inference_profile'] == model['inference_profile']
    assert bundle['deployments'][0]['probe_status'] == 'unprobed'


@pytest.mark.parametrize('wire_name', ['kimi-k3', 'gpt-6-astra', 'grok-4.6', 'claude-opus-5'])
def test_empty_v2_profile_never_invokes_name_heuristics(monkeypatch, wire_name):
    import lib.llm._reasoning_legacy as legacy
    monkeypatch.setattr(legacy, 'apply_legacy', lambda *a, **kw: pytest.fail('legacy name heuristic called'))
    candidate = SimpleNamespace(model={'model_id': wire_name}, offering={},
                                connection={'protocol': 'openai'}, deployment={})
    profile = candidate_inference_metadata(candidate)['inference_profile']
    body = build_body(wire_name, [{'role': 'user', 'content': 'hi'}], thinking_depth='default', inference_profile=profile)
    assert 'reasoning_effort' not in body and 'thinking' not in body
    with pytest.raises(ReasoningContractError, match='unsupported_reasoning'):
        build_body(wire_name, [{'role': 'user', 'content': 'hi'}], thinking_depth='max', inference_profile=profile)


def pending_document():
    doc = plan_legacy_migration({'providers': [{'id': 'relay', 'base_url': 'https://example.invalid',
        'api_keys': ['test-placeholder'], 'models': [{'model_id': 'opaque-discovered'}]}]}).document
    doc['creators'].append({'creator_id': 'xai', 'name': 'xAI'})
    doc['models'].append({'creator_id': 'xai', 'model_id': 'grok-4.6', 'display_name': 'Grok',
        'capabilities': ['text', 'thinking'], 'context_window': 500000, 'quality_rank': 90})
    doc['offerings'][0]['pending_model_id'] = 'grok-4.6'
    doc['deployments'][0]['wire_model_id'] = 'grok-4.6'
    doc['deployments'][0]['probe_status'] = 'unprobed'
    return doc


def test_saved_exact_identity_binding_preserves_probe_ids_and_authority():
    doc = pending_document()
    original = copy.deepcopy(doc)
    denied = copy.deepcopy(doc['credentials'][0])
    denied.update(credential_id='no-connection', authorization={'connection_ids': [], 'models': []})
    doc['credentials'].append(denied)
    assert reconcile_catalog_identities(doc) == 1
    assert doc['offerings'][0]['offering_id'] == original['offerings'][0]['offering_id']
    assert doc['deployments'][0]['deployment_id'] == original['deployments'][0]['deployment_id']
    assert doc['deployments'][0]['probe_status'] == 'unprobed'
    assert doc['credentials'][0]['authorization']['models'] == [{'creator_id': 'xai', 'model_id': 'grok-4.6'}]
    assert denied['authorization']['models'] == []
    assert reconcile_catalog_identities(doc) == 0
    normalize_document(doc)


@pytest.mark.parametrize('ambiguous', [False, True])
def test_binding_does_not_guess_future_or_ambiguous_names(ambiguous):
    doc = pending_document()
    if ambiguous:
        doc['models'].append({**doc['models'][0], 'creator_id': 'other'})
    else:
        doc['offerings'][0]['pending_model_id'] = 'grok-4.6-future'
    original = copy.deepcopy(doc)
    assert reconcile_catalog_identities(doc) == 0
    assert doc == original


def test_binding_preserves_explicit_wire_exclusion():
    doc = pending_document()
    auth = doc['credentials'][0]['authorization']
    auth['models'].append({'creator_id': 'xai', 'model_id': 'grok-4.6',
                           'excluded_wire_model_ids': ['grok-4.6']})
    before = copy.deepcopy(auth)
    assert reconcile_catalog_identities(doc) == 1
    assert auth == before


def test_new_identity_grant_does_not_open_existing_other_wires():
    doc = pending_document()
    offering = copy.deepcopy(doc['offerings'][0])
    offering.update(offering_id='other-supply', identity_state='confirmed',
                    model={'creator_id': 'xai', 'model_id': 'grok-4.6'})
    offering.pop('pending_model_id')
    doc['offerings'].append(offering)
    doc['deployments'].append({**doc['deployments'][0], 'deployment_id': 'other-deployment',
        'offering_id': 'other-supply', 'wire_model_id': 'other-private-wire'})
    assert reconcile_catalog_identities(doc) == 1
    assert doc['credentials'][0]['authorization']['models'][0]['excluded_wire_model_ids'] == ['other-private-wire']
    normalize_document(doc)


def test_world_sync_repairs_existing_identity_and_profile_once(monkeypatch):
    from lib.model_routing import InMemoryModelRoutingRepository, OwnerBoundary
    from lib.model_routing import world_catalog
    doc = pending_document()
    model = copy.deepcopy(doc['models'][0])
    apply_model_defaults(model)
    monkeypatch.setattr(world_catalog, '_world_catalog_rows', lambda: (doc['creators'], [model]))
    monkeypatch.setattr(world_catalog, '_retired_model_keys', lambda: set())
    repository, owner = InMemoryModelRoutingRepository(), OwnerBoundary.create(331)
    repository.compare_and_swap(owner, doc, expected_revision=0)
    result = world_catalog.sync_world_model_catalog(boundary=owner, repository=repository)
    # Exact defaults are now applied at authority normalization, before sync.
    assert result['identities_bound'] == 1
    installed = repository.get(owner).document
    assert installed['models'][0]['inference_profile'] == model['inference_profile']
    assert installed['offerings'][0]['identity_state'] == 'confirmed'
    from lib.model_routing.controls import reasoning_controls
    rows = reasoning_controls(installed)
    scoped = next(r for r in rows if r['offering_id'] == doc['offerings'][0]['offering_id'])
    assert 'xhigh' in scoped['depths']
    assert 'max' not in scoped['depths']
    again = world_catalog.sync_world_model_catalog(boundary=owner, repository=repository)
    assert again['revision'] == result['revision']
