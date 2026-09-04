"""Assistant serving-route presentation never mistakes auxiliary calls for fallback."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import native_module_path, runtime_section


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
OWNER = ROOT / 'frontend/src/conversation/presentation/turn-serving-route.ts'


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_serving_route_uses_response_authoring_round(tmp_path):
    owner_bundle = native_module_path('turn-serving-route.js', OWNER)
    harness = tmp_path / 'serving-route-harness.js'
    harness.write_text(r"""
const fs = require('fs');
global.window = global;
eval(fs.readFileSync(process.argv[2], 'utf8'));

const dispatch = (model, provider, key, tail) => ({
  usage: {_dispatch: {model, provider_id: provider, key, key_tail: tail}},
});
const compaction = (model, provider) => ({
  kind: 'compaction', tag: 'COMPACTION-L2',
  ...dispatch(model, provider, provider + '_summary', 'c999'),
});

const kimi = resolveTurnServingRoute({
  model: 'kimi-k3',
  lastRoundUsage: {round: 50, model: 'kimi-k3', tokensIn: 100000, tokensOut: 50},
  apiRounds: [
    {round: 50, tag: 'R50', ...dispatch('kimi-k3', 'sankuai', 'sankuai_key_0', 'k123')},
    compaction('gemini-3.5-flash-lite', 'gemini'),
  ],
});
const sol = resolveTurnServingRoute({
  model: 'gpt-5.6-sol',
  lastRoundUsage: {round: 29, model: 'gpt-5.6-sol', tokensIn: 90000, tokensOut: 40},
  apiRounds: [
    {round: 29, tag: 'R29', ...dispatch('gpt-5.6-sol', 'oauth_codex', 'codex_0', 's123')},
    compaction('gpt-5.6-terra', 'oauth_codex'),
  ],
});
const fallback = resolveTurnServingRoute({
  model: 'kimi-k3', fallbackModel: 'glm-5.3',
  lastRoundUsage: {
    round: 1, model: 'glm-5.3', resolvedModel: 'glm-5.3',
    providerId: 'zhipu', keyName: 'zhipu_0', keyTail: 'f123',
    tokensIn: 30000, tokensOut: 100,
  },
  apiRounds: [
    {round: 1, tag: 'R1', ...dispatch('kimi-k3', 'sankuai', 'sankuai_0', 'k123')},
    {round: 1, tag: 'R1-FALLBACK', ...dispatch('glm-5.3', 'zhipu', 'zhipu_0', 'f123')},
    compaction('gemini-3.5-flash-lite', 'gemini'),
  ],
});
const billed = resolveTurnServingRoute({
  model: 'kimi-k3',
  apiRounds: [
    {round: 1, tag: 'R1', ...dispatch('kimi-k3', 'sankuai', 'sankuai_0', 'k123')},
    {round: 1, tag: 'R1-BILLED', responseAuthoring: false,
      ...dispatch('gemini-discarded', 'gemini', 'g_0', 'd123')},
    compaction('gemini-3.5-flash-lite', 'gemini'),
  ],
});
const legacyFallback = resolveTurnServingRoute({
  model: 'kimi-k3', fallbackModel: 'glm-5.3',
  apiRounds: [
    {round: 1, model: 'glm-5.3', tag: 'R1-FALLBACK', usage: {prompt_tokens: 10}},
    {round: 1, model: 'gemini-summary', tag: 'COMPACTION-L2', usage: {prompt_tokens: 2}},
  ],
});
const snapshot = resolveTurnServingRoute({
  model: 'alpha',
  routeSnapshot: {
    contract_version: 'tofu.route-snapshot/v2',
    selected_model: {creator_id: 'creator', model_id: 'alpha'},
    actual_model: {creator_id: 'creator', model_id: 'beta'},
    provider_id: 'provider-b', offering_id: 'beta-b',
    deployment_id: 'beta-b-deployment', connection_id: 'connection-b',
    credential: {credential_id: 'credential-b', kind: 'api_key', key_hint: '***'},
    wire_model_id: 'beta-wire-b', transitions: [], degradation_reasons: [],
    preferred_provider_id: 'provider-a', provider_scoped_selection: null,
    recorded_at: 1,
  },
  lastRoundUsage: {
    model: 'stale-model', resolvedModel: 'stale-wire', providerId: 'stale-provider',
  },
});
const count = agentApiRoundCount([
  {tag: 'R1'},
  {tag: 'R1-BILLED', responseAuthoring: false},
  {kind: 'compaction', tag: 'COMPACTION-L2'},
]);
console.log(JSON.stringify({kimi, sol, fallback, billed, legacyFallback, snapshot, count}));
""", encoding='utf-8')
    run = subprocess.run(
        [shutil.which('node'), str(harness), owner_bundle],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
    routes = json.loads(run.stdout.strip().splitlines()[-1])

    assert routes['kimi'] == {
        'model': 'kimi-k3', 'logicalModel': 'kimi-k3',
        'providerId': 'sankuai', 'keyName': 'sankuai_key_0',
        'keyTail': 'k123', 'source': 'last-round',
    }
    assert routes['sol']['model'] == 'gpt-5.6-sol'
    assert routes['sol']['providerId'] == 'oauth_codex'
    assert routes['fallback']['model'] == 'glm-5.3'
    assert routes['fallback']['providerId'] == 'zhipu'
    assert routes['billed']['model'] == 'kimi-k3'
    assert routes['billed']['providerId'] == 'sankuai'
    assert routes['legacyFallback']['model'] == 'glm-5.3'
    assert routes['snapshot'] == {
        'model': 'beta-wire-b', 'logicalModel': 'alpha',
        'providerId': 'provider-b', 'keyName': 'credential-b',
        'keyTail': '', 'source': 'route-snapshot',
    }
    assert routes['count'] == 1


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_finish_bar_renders_response_route_not_trailing_compaction(tmp_path):
    owner_bundle = native_module_path('turn-serving-route.js', OWNER)
    finish_owner = runtime_section('ui/finish_info.js')
    function_start = finish_owner.index('function _formatTurnDuration(ms) {')
    function_end_marker = (
        '  if (parts.length === 0) return "";\n'
        '  return `<div class="message-finish">${parts.join("")}</div>`;\n'
        '}'
    )
    function_end = finish_owner.index(
        function_end_marker, function_start,
    ) + len(function_end_marker)
    finish_source = tmp_path / 'finish-route.js'
    finish_source.write_text(
        finish_owner[function_start:function_end], encoding='utf-8',
    )

    harness = tmp_path / 'finish-route-harness.js'
    harness.write_text(r"""
const fs = require('fs');
global.window = global;
global.runtimeScope = global;
global.escapeHtml = (value) => String(value ?? '')
  .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
global.t = (key, values = {}) => {
  if (key === 'finishInfo.modelRouteTag') {
    return `Routed ${values.from} → ${values.to}`;
  }
  if (key === 'finishInfo.modelRouteTip') {
    return `Selected ${values.from}; served by ${values.to}`;
  }
  return key;
};
global.Icon = () => '';
global._detectBrand = () => 'generic';
global._brandSvg = () => '';
global._isThinkingCapable = () => false;
global._providerDisplayName = (value) => String(value || '');
global.calcCostCny = () => ({costCny: 0});
global._subscriptionQuotaForMessage = () => null;
global.ConversationTurnStore = {
  finishPresentation: () => ({tone: 'success', label: 'Completed', detail: ''}),
};

eval(fs.readFileSync(process.argv[2], 'utf8'));
eval(fs.readFileSync(process.argv[3], 'utf8'));
const dispatch = (model, provider, key, tail) => ({
  usage: {_dispatch: {model, provider_id: provider, key, key_tail: tail}},
});
const base = {
  _turnStatus: 'completed', _turnSettlement: {},
  usage: {prompt_tokens: 10, completion_tokens: 5},
};
const kimi = renderFinishInfo({...base,
  model: 'kimi-k3',
  lastRoundUsage: {round: 50, model: 'kimi-k3', tokensIn: 10, tokensOut: 5},
  apiRounds: [
    {round: 50, tag: 'R50', ...dispatch('kimi-k3', 'sankuai', 'sankuai_0', '1234')},
    {kind: 'compaction', tag: 'COMPACTION-L2', ...dispatch('gemini-3.5-flash-lite', 'gemini', 'g_0', '9999')},
  ],
}, false);
const sol = renderFinishInfo({...base,
  model: 'gpt-5.6-sol',
  lastRoundUsage: {round: 29, model: 'gpt-5.6-sol', tokensIn: 10, tokensOut: 5},
  apiRounds: [
    {round: 29, tag: 'R29', ...dispatch('gpt-5.6-sol', 'oauth_codex', 'codex_0', '5678')},
    {kind: 'compaction', tag: 'COMPACTION-L2', ...dispatch('gpt-5.6-terra', 'oauth_codex', 'terra_0', '9999')},
  ],
}, false);
const fallback = renderFinishInfo({...base,
  model: 'kimi-k3', fallbackModel: 'glm-5.3',
  lastRoundUsage: {round: 1, model: 'glm-5.3', resolvedModel: 'glm-5.3',
    providerId: 'zhipu', keyName: 'zhipu_0', keyTail: 'abcd', tokensIn: 10, tokensOut: 5},
}, false);
const routed = renderFinishInfo({...base,
  model: 'deepseek-v4-pro',
  orchestration: {modelRoute: {
    selectedModel: 'kimi-k3', resolvedModel: 'deepseek-v4-pro',
    role: 'worker', tier: 'heavy', kind: 'role_tier',
  }},
}, false);
console.log(JSON.stringify({kimi, sol, fallback, routed}));
""", encoding='utf-8')
    run = subprocess.run(
        [shutil.which('node'), str(harness), owner_bundle, str(finish_source)],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
    rendered = json.loads(run.stdout.strip().splitlines()[-1])

    assert 'kimi-k3' in rendered['kimi']
    assert 'sankuai' in rendered['kimi']
    assert 'gemini' not in rendered['kimi']
    assert 'gpt-5.6-sol' in rendered['sol']
    assert 'gpt-5.6-terra' not in rendered['sol']
    assert 'glm-5.3' in rendered['fallback']
    assert 'zhipu' in rendered['fallback']
    assert 'finish-tag warn model-route' in rendered['routed']
    assert 'kimi-k3' in rendered['routed']
    assert 'deepseek-v4-pro' in rendered['routed']


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_model_first_picker_collapses_provider_supply_and_quarantines_pending(
        tmp_path):
    source = runtime_section('main/main_toolbar_ui.js')
    start = source.index('function _modelRoutingDropdownModels(documentValue) {')
    end = source.index('\n}\n\n/* Populate model dropdown', start) + 2
    owner = tmp_path / 'model-routing-picker.js'
    owner.write_text(source[start:end], encoding='utf-8')
    harness = tmp_path / 'model-routing-picker-harness.js'
    harness.write_text(r"""
const fs = require('fs');
eval(fs.readFileSync(process.argv[2], 'utf8'));
const documentValue = {
  contract_version: 'tofu.model-routing/v2',
  providers: [
    {provider_id: 'alpha', name: 'Alpha'},
    {provider_id: 'beta', name: 'Beta'},
  ],
  provider_accesses: [
    {provider_access_id: 'access-alpha', provider_id: 'alpha', enabled: true},
    {provider_access_id: 'access-beta', provider_id: 'beta', enabled: true},
  ],
  models: [{creator_id: 'creator', model_id: 'official'}],
  offerings: [
    {offering_id: 'alpha-official', provider_access_id: 'access-alpha',
     enabled: true, stale: false, identity_state: 'confirmed',
     model: {creator_id: 'creator', model_id: 'official'}, capabilities: ['text']},
    {offering_id: 'beta-official', provider_access_id: 'access-beta',
     enabled: true, stale: false, identity_state: 'confirmed',
     model: {creator_id: 'creator', model_id: 'official'}, capabilities: ['text']},
    {offering_id: 'beta-pending', provider_access_id: 'access-beta',
     enabled: true, stale: false, identity_state: 'pending_identity',
     pending_model_id: 'preview-wire', capabilities: ['text']},
  ],
  deployments: [
    {offering_id: 'alpha-official', enabled: true},
    {offering_id: 'beta-official', enabled: true},
    {offering_id: 'beta-pending', enabled: true},
  ],
};
console.log(JSON.stringify(_modelRoutingDropdownModels(documentValue)));
""", encoding='utf-8')
    result = subprocess.run(
        [shutil.which('node'), str(harness), str(owner)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)

    automatic = [row for row in rows if not row['provider_id']]
    assert [(row['creator_id'], row['model_id']) for row in automatic] == [
        ('creator', 'official')]
    official = [row for row in rows if row['model_id'] == 'official']
    assert len(official) == 1
    assert [(option['provider_id'], option['offering_id'])
            for option in official[0]['provider_options']] == [
                ('alpha', 'alpha-official'), ('beta', 'beta-official')]
    pending = [row for row in rows if row['pending_identity']]
    assert [(row['provider_id'], row['offering_id']) for row in pending] == [
        ('beta', 'beta-pending')]
    assert all('connection_id' not in row and 'deployment_id' not in row
               for row in rows)


def test_settings_runtime_has_one_v2_provider_editor():
    manifest = (
        ROOT / 'frontend/src/runtime/sections/manifest.json'
    ).read_text(encoding='utf-8')
    renderer = runtime_section('settings/provider_render.js')
    assert 'settings/provider_render.js' in manifest
    for legacy in (
        'settings/providers/access_matrix.js',
        'settings/local_endpoints.js',
        'settings/provider_faces.js',
        'settings/template_actions.js',
        'settings/model_edit.js',
    ):
        assert legacy not in manifest
    for term in ('服务商', '接入配置', '接入点', '凭证', '模型供给', '上游部署标识'):
        assert term in renderer
    assert '<details class="stg-v2-advanced">' not in renderer
    assert '_stgProviderManagerLimit = 80' in renderer
    assert 'async function _showTemplateMenu' in renderer
    assert 'async function _openTemplateWizard' in renderer
    assert 'async function addProvider' in renderer
    assert "provider.brand === 'oauth'" in renderer
    assert 'codex|chatgpt|openai' in renderer
