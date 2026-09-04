"""Executable contract for catalog-backed provider onboarding recipes."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from lib.provider_template_recipes import (
    RECIPE_VERSION,
    ProviderTemplateRecipeError,
    compile_provider_template_bundle,
    normalize_provider_template,
    offering_recipes,
    provider_from_template,
)
pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
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


def test_authored_recipe_preserves_exact_logical_and_wire_identity():
    normalized = normalize_provider_template(_recipe())
    assert normalized['recipe_version'] == RECIPE_VERSION
    assert 'models' not in normalized
    entry = normalized['offering_recipes'][0]
    assert entry['model_id'] == 'shared-model'
    assert entry['request_ids'] == ['vendor/shared-model-v2']
    assert entry['capabilities'] == ['text', 'thinking']


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
        if raw.get('category') != 'local':
            assert len(canonical['offering_recipes']) > 0, path.name


def test_deepseek_and_meituan_anchor_flash_to_the_same_model_identity():
    bundles = {
        key: compile_provider_template_bundle(
            key, selected_model_ids=['deepseek-v4-flash'])
        for key in ('deepseek', 'meituan')
    }
    expected_ref = {'creator_id': 'deepseek', 'model_id': 'deepseek-v4-flash'}
    for bundle in bundles.values():
        assert bundle['offerings'][0]['model'] == expected_ref

    assert [row['wire_model_id'] for row in bundles['deepseek']['deployments']] == [
        'deepseek-v4-flash',
    ]
    assert [row['wire_model_id'] for row in bundles['meituan']['deployments']] == [
        'deepseek-v4-flash',
        'deepseek-v4-flash-huawei',
    ]
