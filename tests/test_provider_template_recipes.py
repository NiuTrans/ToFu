"""Executable contract for catalog-backed provider onboarding recipes."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from lib.provider_template_recipes import (
    RECIPE_VERSION,
    ProviderTemplateRecipeError,
    normalize_provider_template,
    offering_recipes,
    provider_from_template,
)
from tests._runtime_sections import runtime_section_path

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


@pytest.mark.skipif(not shutil.which('node'), reason='node not available')
def test_retained_templates_author_recipes_and_apply_through_adapter():
    templates_path = runtime_section_path(
        'settings/provider_templates.js', scope_prelude=False)
    actions_path = runtime_section_path(
        'settings/template_actions.js', scope_prelude=False)
    harness = r"""
const fs = require('fs');
global.window = global;
global.runtimeScope = global;
global.document = { querySelector: () => null };
global.t = key => key;
global.showConfirm = async () => true;
global._renderProvidersTab = () => {};
global._renderPresetsTab = () => {};
global._coldSortModels = models => models;
global._modelPricingCache = {};
global._serverConfig = {};
global._stgProviders = [];
global.isChatModel = () => true;

const templatesSource = fs.readFileSync(process.argv[1], 'utf8');
const actionsSource = fs.readFileSync(process.argv[2], 'utf8');
const probe = String.raw`
(async () => {
  const openrouter = _PROVIDER_TEMPLATES.find(row => row.key === 'openrouter');
  const bedrock = _PROVIDER_TEMPLATES.find(row => row.key === 'bedrock');
  const recipe = (template, modelId) =>
    _templateOfferingRecipes(template).find(row => row.model_id === modelId);
  const legacy = {
    key: 'legacy', models: [{ model_id: 'legacy-model', request_ids: ['wire-legacy'] }],
  };
  const normalized = _normalizeProviderTemplateRecipe(legacy);

  _PROVIDER_TEMPLATES.push(_normalizeProviderTemplateRecipe({
    key: 'adapter-probe', name: 'Adapter probe', base_url: 'https://probe.invalid/v1',
    offering_recipes: [{
      model_id: 'logical-model', request_ids: ['provider/wire-model'],
      capabilities: ['text'], rpm: 17,
    }],
  }));
  await addProviderFromTemplate('adapter-probe');
  const applied = global._stgProviders[0];
  const result = {
    authoredOnly: _PROVIDER_TEMPLATES.length >= 10
      && _PROVIDER_TEMPLATES.every(row => Array.isArray(row.offering_recipes))
      && _PROVIDER_TEMPLATES.every(row => !Object.hasOwn(row, 'models')),
    versioned: _PROVIDER_TEMPLATES.every(
      row => row.recipe_version === 'tofu.provider-offering-recipe/v1'),
    legacyNormalized: normalized.recipe_version === 'tofu.provider-offering-recipe/v1'
      && !Object.hasOwn(normalized, 'models')
      && normalized.offering_recipes[0].request_ids[0] === 'wire-legacy',
    openrouterOpenai: recipe(openrouter, 'gpt-5.6').request_ids[0]
      === 'openai/gpt-5.6',
    openrouterAnthropic: recipe(openrouter, 'claude-opus-4-6').request_ids[0]
      === 'anthropic/claude-opus-4.6',
    bedrockAnthropic: recipe(bedrock, 'claude-opus-4-6').request_ids[0]
      === 'us.anthropic.claude-opus-4-6-v1:0',
    appliedThroughAdapter: applied.models.length === 1
      && applied.models[0].model_id === 'logical-model'
      && applied.models[0].request_ids[0] === 'provider/wire-model'
      && applied.models[0].rpm === 17,
  };
  console.log(JSON.stringify(result));
})();
`;
(0, eval)(templatesSource + '\n' + actionsSource + '\n' + probe);
"""
    run = subprocess.run(
        [shutil.which('node'), '-e', harness, templates_path, actions_path],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == 0, (run.stdout or '') + (run.stderr or '')
    result = json.loads(run.stdout.strip().splitlines()[-1])
    assert result == {
        'authoredOnly': True,
        'versioned': True,
        'legacyNormalized': True,
        'openrouterOpenai': True,
        'openrouterAnthropic': True,
        'bedrockAnthropic': True,
        'appliedThroughAdapter': True,
    }
