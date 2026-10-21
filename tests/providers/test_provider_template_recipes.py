"""Executable contract for catalog-backed provider onboarding recipes."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from lib.provider.provider_template_recipes import (
    RECIPE_VERSION,
    ProviderTemplateRecipeError,
    compile_provider_template_bundle,
    normalize_provider_template,
    offering_recipes,
    provider_from_template,
)
pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / 'static' / 'provider_templates'


def _recipe(**changes):
    row = {
        'recipe_version': RECIPE_VERSION,
        'key': 'relay',
        'name': 'Relay',
        'base_url': 'https://relay.example/v1',
        'offering_recipes': [{
            'model_id': 'shared-model',
            'request_ids': ['vendor/shared-model-v2'],
            'capabilities': ['text', 'thinking'],
            'rpm': 60,
        }],
    }
    row.update(changes)
    return row


def test_metadata_defaults_are_conservative_and_keep_input_immutable():
    raw = _recipe()
    before = copy.deepcopy(raw)
    result = normalize_provider_template(raw)
    assert result['category'] == 'relay'
    assert result['catalog_mode'] == 'curated'
    assert 'catalog_source' not in result
    assert raw == before


@pytest.mark.parametrize('changes', [
    {'category': 'hosted-by-vendor'}, {'catalog_mode': 'guessed'},
    {'catalog_source': {'url': 'http://example.test', 'checked_at': '2026-09-08'}},
    {'catalog_source': {'url': 'https://example.test', 'checked_at': '2026-02-30'}},
    {'catalog_source': {'url': 'https://user:password@example.test', 'checked_at': '2026-09-08'}},
    {'catalog_source': {'url': 'https://example.test'}},
    {'catalog_mode': 'discovery', 'base_url': ''},
])
def test_invalid_category_or_provenance_is_rejected(changes):
    with pytest.raises(ProviderTemplateRecipeError):
        normalize_provider_template(_recipe(**changes))


def test_connection_only_discovery_compiles_without_guessing_models(monkeypatch):
    import lib.provider.provider_template_recipes as recipes
    template = normalize_provider_template(_recipe(
        catalog_mode='discovery', offering_recipes=[],
        catalog_source={'url': 'https://example.test/docs', 'checked_at': '2026-09-08'}))
    monkeypatch.setattr(recipes, 'load_provider_templates', lambda: [template])
    bundle = compile_provider_template_bundle('relay')
    assert bundle['connections'][0]['base_url'] == template['base_url']
    assert bundle['offerings'] == bundle['deployments'] == bundle['models'] == []
    assert bundle['credentials'][0]['secret_reference'] == ''
    assert 'template-secret-placeholder' not in json.dumps(bundle)


def test_api_metadata_is_generated_from_recipe_authority():
    from scripts.gen_api_v1_model_routing_contract import _load_contract
    from lib.generated_api_v1_model_routing import decode, ContractViolation
    authored = json.loads((ROOT / 'contracts/provider_offering_recipe_v1.schema.json').read_text())
    generated = _load_contract()['components']['schemas']['ProviderTemplate']['properties']
    for field in ('category', 'catalog_mode', 'catalog_source'):
        assert generated[field] == authored['properties'][field]
    decode('ProviderTemplate', normalize_provider_template(_recipe(catalog_mode='discovery')))
    with pytest.raises(ContractViolation):
        decode('ProviderTemplate', {'key': 'x', 'category': 'invented'})


def test_authored_recipe_preserves_exact_logical_and_wire_identity():
    normalized = normalize_provider_template(_recipe())
    assert normalized['recipe_version'] == RECIPE_VERSION
    assert 'models' not in normalized
    entry = normalized['offering_recipes'][0]
    assert entry['model_id'] == 'shared-model'
    assert entry['request_ids'] == ['vendor/shared-model-v2']
    assert entry['capabilities'] == ['text', 'thinking']


def test_relay_prices_never_become_official_list_prices(monkeypatch):
    import lib.provider.provider_template_recipes as recipes
    template = normalize_provider_template(_recipe(offering_recipes=[{
        'model_id': 'openai/gpt-test-provider-price-only-20260908',
        'capabilities': ['text'], 'pricing': {'input': 3, 'output': 7, 'currency': 'USD'},
    }]))
    monkeypatch.setattr(recipes, 'load_provider_templates', lambda: [template])
    bundle = compile_provider_template_bundle('relay')
    assert bundle['models'] and all('list_pricing' not in row for row in bundle['models'])
    assert bundle['offerings'][0]['actual_pricing']['input'] == 3
    assert bundle['offerings'][0]['actual_pricing']['output'] == 7


def test_official_operator_hosting_other_creators_cannot_set_their_list_prices(monkeypatch):
    import lib.provider.provider_template_recipes as recipes
    price = {'input': 2, 'output': 5, 'currency': 'USD'}
    template = normalize_provider_template(_recipe(category='official', brand='qwen',
        offering_recipes=[
            {'model_id': 'qwen-test-vendor-price', 'pricing': price},
            {'model_id': 'deepseek-test-hosted-price', 'pricing': price},
        ]))
    monkeypatch.setattr(recipes, 'load_provider_templates', lambda: [template])
    bundle = compile_provider_template_bundle('relay')
    own, hosted = [next(r for r in bundle['models'] if r['creator_id'] == family)
                   for family in ('alibaba', 'deepseek')]
    assert own['list_pricing']['input'] == 2
    assert 'list_pricing' not in hosted
    assert all(row['actual_pricing']['input'] == 2 for row in bundle['offerings'])


def test_template_seeds_never_claim_inference_probe_success(monkeypatch):
    import lib.provider.provider_template_recipes as recipes
    template = normalize_provider_template(_recipe(offering_recipes=[{
        'model_id': 'openai/gpt-test-unprobed-template', 'capabilities': ['text'],
    }]))
    monkeypatch.setattr(recipes, 'load_provider_templates', lambda: [template])
    bundle = compile_provider_template_bundle('relay')
    assert bundle['offerings'][0]['identity_state'] == 'confirmed'
    assert bundle['deployments']
    assert all('enabled' not in row and row['probe_status'] == 'unprobed'
               for row in bundle['deployments'])


def test_legacy_models_are_read_but_derived_alias_is_explicit():
    legacy = _recipe()
    legacy.pop('recipe_version')
    legacy['models'] = legacy.pop('offering_recipes')

    canonical = normalize_provider_template(legacy)
    assert canonical['recipe_version'] == RECIPE_VERSION
    assert 'models' not in canonical
    assert canonical['offering_recipes'][0]['model_id'] == 'shared-model'

    public = normalize_provider_template(legacy, include_legacy_models=True)
    assert public['models'] == public['offering_recipes']
    assert public['models'] is not public['offering_recipes']


def test_disagreeing_dual_fields_fail_loud():
    raw = _recipe(models=[{'model_id': 'other'}])
    with pytest.raises(ProviderTemplateRecipeError, match='disagree'):
        normalize_provider_template(raw)


def test_duplicate_provider_logical_offering_is_rejected():
    entry = _recipe()['offering_recipes'][0]
    raw = _recipe(offering_recipes=[entry, copy.deepcopy(entry)])
    with pytest.raises(ProviderTemplateRecipeError, match='duplicate'):
        offering_recipes(raw)


def test_provider_projection_is_legacy_transport_not_authored_authority():
    provider = provider_from_template(_recipe(), 'relay-installed')
    assert provider['id'] == 'relay-installed'
    assert 'offering_recipes' not in provider
    assert 'recipe_version' not in provider
    assert provider['models'][0]['model_id'] == 'shared-model'
    assert provider['models'][0]['request_ids'] == ['vendor/shared-model-v2']


def test_legacy_cost_is_removed_at_recipe_boundary():
    raw = _recipe(offering_recipes=[{
        'model_id': 'm', 'capabilities': ['text'], 'cost': 0.25,
    }])
    entry = offering_recipes(raw)[0]
    assert 'cost' not in entry
    assert entry['rpm'] == 30


def test_bundled_json_templates_use_recipe_authority():
    paths = sorted(TEMPLATES.glob('*.json'))
    assert paths
    for path in paths:
        raw = json.loads(path.read_text(encoding='utf-8'))
        assert raw['recipe_version'] == RECIPE_VERSION, path.name
        assert 'offering_recipes' in raw, path.name
        assert 'models' not in raw, path.name
        canonical = normalize_provider_template(raw, allow_legacy=False)
        assert canonical['key'] == raw['key']
        if raw.get('category') != 'local' and raw.get('catalog_mode') != 'discovery':
            assert len(canonical['offering_recipes']) > 0, path.name


def test_settings_templates_share_bootstrap_builtins():
    """Both pickers merge the same file-backed catalog (lib/model_info/data
    then static/provider_templates), so the bootstrap installer and the
    Settings picker must offer identical providers with identical models."""
    from bootstrap_pkg.providers import (
        _load_provider_templates as bootstrap_load,
    )

    from lib.provider.provider_template_recipes import load_provider_templates

    settings_rows = {row['key']: row for row in load_provider_templates()}
    bootstrap_rows = {
        row['key']: row for row in bootstrap_load()
        if row['key'] != 'custom'  # bootstrap-only form placeholder
    }
    assert set(bootstrap_rows) <= set(settings_rows)
    for key, bootstrap_row in bootstrap_rows.items():
        assert {m['model_id'] for m in bootstrap_row['models']} == {
            r['model_id'] for r in settings_rows[key]['offering_recipes']
        }, key
    # bootstrap-only rows stay out of the Settings picker.
    assert 'custom' not in settings_rows
    # Anthropic enters the Settings picker via the package static recipe
    # (native protocol), which overrides the protocol-less builtin stub.
    anthropic = settings_rows['anthropic']
    assert anthropic['protocol'] == 'anthropic'
    assert anthropic['offering_recipes'][0]['model_id'] == 'claude-opus-5-5'


def test_static_template_overrides_builtin_recipe_list():
    from lib.provider.provider_template_recipes import load_provider_templates

    templates = {row['key']: row for row in load_provider_templates()}
    static_glm = json.loads((TEMPLATES / 'glm.json').read_text(encoding='utf-8'))
    assert len(templates['glm']['offering_recipes']) == len(
        static_glm['offering_recipes'])
    # 20 recipes since gpt-6.1-sol joined the OpenAI template.
    assert len(templates['openai']['offering_recipes']) == 20


def test_loader_passes_static_overrides_to_the_feed_exclusion(
        monkeypatch, tmp_path):
    import lib.provider.provider_template_feed as feed
    from lib.provider.provider_template_recipes import load_provider_templates

    captured = {}
    sentinel = {'version': 2}
    monkeypatch.setattr(feed, 'load_cached_feed', lambda: sentinel)
    monkeypatch.setattr(feed, 'maybe_refresh_async', lambda *a: False)
    # Keep the user tier empty so only deployment/bundle layers are observed.
    monkeypatch.setattr('lib.config_dir.config_path',
                        lambda *parts: str(tmp_path))

    def merge(templates, cached, *, excluded_keys):
        assert cached is sentinel
        captured['keys'] = set(excluded_keys)
        return templates

    monkeypatch.setattr(feed, 'merge_feed_recipes', merge)
    load_provider_templates()
    expected = {json.loads(path.read_text())['key'] for path in TEMPLATES.glob('*.json')}
    # Release-tiered bundle templates are authored overrides too
    # (docs/SITE_PACKS.md §4); they exist only in internal checkouts.
    for tpl_dir in sorted(ROOT.glob('bundles/*/provider_templates')):
        expected |= {json.loads(path.read_text())['key']
                     for path in tpl_dir.glob('*.json')}
    assert captured['keys'] == expected


def test_every_shared_builtin_compiles_an_onboarding_bundle():
    from lib.provider.provider_template_recipes import load_provider_templates

    for row in load_provider_templates():
        recipes = row['offering_recipes']
        if not recipes:
            assert row.get('category') == 'local' or row.get('catalog_mode') == 'discovery'
            bundle = compile_provider_template_bundle(row['key'])
            assert bundle['connections'], row['key']
            assert bundle['offerings'] == [], row['key']
            assert bundle['deployments'] == [], row['key']
            continue
        # Compile the FULL recipe set — the shape the world-catalog sync
        # uses. Selecting a single recipe would miss cross-recipe identity
        # collisions (one logical model per template; wire spellings belong
        # in request_ids).
        bundle = compile_provider_template_bundle(row['key'])
        assert bundle['offerings'], row['key']
        assert bundle['connections'][0]['base_url'], row['key']


def test_yourprovider_v41_identity_keeps_its_v4_wire_names():
    bundles = {
        'deepseek': compile_provider_template_bundle('deepseek', selected_model_ids=['deepseek-v4-flash']),
        'yourprovider': compile_provider_template_bundle('yourprovider', selected_model_ids=['deepseek-v4.1-flash']),
    }
    assert bundles['deepseek']['offerings'][0]['model'] == {
        'creator_id': 'deepseek', 'model_id': 'deepseek-v4-flash'}
    assert bundles['yourprovider']['offerings'][0]['model'] == {
        'creator_id': 'deepseek', 'model_id': 'deepseek-v4.1-flash'}

    assert [row['wire_model_id'] for row in bundles['deepseek']['deployments']] == [
        'deepseek-v4-flash',
    ]
    assert sorted(
        row['wire_model_id'] for row in bundles['yourprovider']['deployments']
    ) == [
        'deepseek-flash',
        'deepseek-v4-flash',
        'deepseek-v4-flash-huawei',
        'deepseek-v4-flash-yourprovider',
        'deepseek-v4-flash-tencent',
    ]


def test_compiled_offerings_follow_their_model_authority():
    from lib.provider.provider_template_recipes import load_provider_templates
    for template in load_provider_templates():
        if template.get('category') == 'local':
            continue
        bundle = compile_provider_template_bundle(template['key'])
        models = {(m['creator_id'], m['model_id']): m for m in bundle['models']}
        for offering in bundle['offerings']:
            ref = offering.get('model')
            if ref:
                model = models[(ref['creator_id'], ref['model_id'])]
                assert offering['context_window'] <= model['context_window']
                if 'context_window' in offering.get('inherited_model_fields', []):
                    assert offering['context_window'] == model['context_window']


def test_bundle_model_rows_keep_the_recipe_context_window():
    # Provider context is constrained only by the resolved Model authority.
    bundle = compile_provider_template_bundle(
        'yourprovider', selected_model_ids=['Doubao-Seed-1.6'])
    model = next(
        row for row in bundle['models'] if row['model_id'] == 'Doubao-Seed-1.6')
    assert model['context_window'] == 256000
    assert bundle['offerings'][0]['context_window'] == 256000


def test_tencent_hunyuan_recipes_confirm_official_tencent_identity():
    # The creator-family taxonomy knows Hunyuan ids: every tencent recipe
    # must compile to a confirmed offering (never provider-scoped pending),
    # or the models stay invisible to the 模型配置 world-catalogue registry.
    bundle = compile_provider_template_bundle('tencent')
    assert all(row['identity_state'] == 'confirmed'
               for row in bundle['offerings'])
    tencent_ids = {
        row['model']['model_id'] for row in bundle['offerings']
        if (row.get('model') or {}).get('creator_id') == 'tencent'
    }
    assert {'hy4-preview', 'hy3-preview', 'hunyuan-2.0-thinking-20251109'} \
        <= tencent_ids


def test_template_add_survives_registry_rows_with_different_pricing_tiers():
    """Regression for the TokenHub template-add rejection
    'offering claims capabilities absent from the official model: [cheap]'.

    The world-catalogue registry row for glm-5/glm-5.1 comes from the zhipu
    template at creator list prices (outside the cheap bracket), while
    TokenHub's subsidised price book earns the tag.  A pricing-tier tag is
    not an identity capability: merging existing-rows-win exactly like the
    settings frontend must normalize the full document.
    """
    from lib.model_routing.domain import empty_document, normalize_document
    from lib.model_routing.world_catalog import _world_catalog_rows

    bundle = compile_provider_template_bundle('tencent')
    creators, models = _world_catalog_rows()
    document = empty_document()
    document['creators'] = [dict(row) for row in creators]
    document['models'] = [dict(row) for row in models]
    known_creators = {row['creator_id'] for row in document['creators']}
    known_models = {
        (row['creator_id'], row['model_id']) for row in document['models']}
    document['creators'] += [
        row for row in bundle['creators']
        if row['creator_id'] not in known_creators]
    document['models'] += [
        row for row in bundle['models']
        if (row['creator_id'], row['model_id']) not in known_models]
    document['providers'].append(bundle['provider'])
    document['provider_accesses'].append(bundle['provider_access'])
    for key in ('connections', 'credentials', 'offerings', 'deployments'):
        document[key] += bundle[key]
    for credential in document['credentials']:
        credential['secret_reference'] = 'placeholder'
        credential['key_hint'] = ''

    normalized = normalize_document(document)
    glm51 = next(row for row in normalized['offerings']
                 if (row.get('model') or {}).get('model_id') == 'glm-5.1')
    assert 'cheap' in glm51['capabilities']
    registry = next(row for row in normalized['models']
                    if row['model_id'] == 'glm-5.1')
    assert 'cheap' not in registry['capabilities']
