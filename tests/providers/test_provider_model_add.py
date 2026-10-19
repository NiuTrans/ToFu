#!/usr/bin/env python3
"""Provider-side model supply: add-model staging + the always-on matrix.

Pins the retained owners ``settings/provider_render.js`` and
``settings/access_matrix.js``:

- 「+ 添加模型」on the matrix manage row stages confirmed offerings (catalog
  models) or pending-identity offerings (custom wire ids) plus their
  deployments into ``_stgModelRouting`` for the global 保存. New deployments
  omit ``enabled`` and honestly ship ``probe_status 'unprobed'``. Confirmed offerings also need a credential
  authorization grant per existing credential (routing refuses ungranted
  refs).
- The access matrix is the provider's always-on model manager: it renders
  even with zero offerings (otherwise a fresh provider could never add its
  first model), and the masked key hint input is content-sized via the
  ``size`` attribute instead of stretching across the whole card.
- Matrix root rows carry the supply-manager surface: state chips (待确认 /
  已下线), a meta line (Chinese capability chips, K/M-abbreviated context,
  structured 入/出 pricing with a 目录价 tag for catalog list prices), and
  row actions (probe row, add request name, remove offering). Disabled
  offerings dim and sink below usable ones; the manage row offers substring
  search over canonical + request names plus exclusive status filter chips
  with live counts. Request names render as tree rows with their own
  remove × and no separate enabled state, including legacy false inputs.
- Probes only update displayed diagnostics. Historical discovery snapshots
  with deployment_enabled:false remain non-actionable. Explicit Apply only
  removes flagged ModelRef grants, never changes deployments or evidence;
  snapshot ingest, polling and resume never change the routing draft.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from tests._runtime_sections import (
    native_module_path,
    ROOT,
    runtime_section,
    shipped_source_text,
)

pytestmark = pytest.mark.unit

NODE = shutil.which("node")
ZH_LOCALE = ROOT / "frontend" / "src" / "i18n" / "locales" / "zh.json"

HARNESS_PRELUDE = r"""
const fs = require('fs');
global.window = global;
global.runtimeScope = global;
global.escapeHtml = value => String(value == null ? '' : value)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');
// t() resolves against the SHIPPED zh locale, so a missing i18n key turns
// the rendered copy back into the raw key and fails the assertions below.
const ZH_FLAT = {};
(function flatten(node, prefix) {
  Object.keys(node).forEach(function(key) {
    var value = node[key];
    if (value && typeof value === 'object') flatten(value, prefix + key + '.');
    else ZH_FLAT[prefix + key] = String(value);
  });
})(JSON.parse(fs.readFileSync(process.argv[4], 'utf8')), '');
global.t = key => Object.prototype.hasOwnProperty.call(ZH_FLAT, key) ? ZH_FLAT[key] : key;
global._brandSvg = () => '<i></i>';
global._detectBrand = () => 'generic';
global.__alerts = [];
global.__toasts = [];
global.showAlert = message => { global.__alerts.push(String(message)); };
global.showToast = message => { global.__toasts.push(String(message)); };
global.showConfirm = async () => true;
global.showPrompt = async () => '';
global._stgPendingCredentialSecrets = {};
global._stgModelRoutingLoadError = '';
global._stgModelRoutingRevision = 1;
// The matrix re-attaches to a persisted probe via setTimeout(0): a
// never-resolving promise keeps the event loop quiet after console.log.
global.Api = { modelRouting: { get: async () => ({metadata_inheritance_supported: true}),
  probeCellsStatus: () => new Promise(() => {}) } };
global.__elements = {};
global.document = {
  getElementById: id => global.__elements[id] || null,
  querySelector: () => null,
  querySelectorAll: () => [],
};
global.freshRouting = () => ({
  contract_version: 'tofu.model-routing/v2',
  providers: [
    { provider_id: 'p1', name: 'Gateway', scope: 'owner', brand: 'generic' },
    { provider_id: 'p2', name: 'Empty', scope: 'owner', brand: 'generic' },
  ],
  provider_accesses: [
    { provider_access_id: 'p1-access', provider_id: 'p1', display_name: 'Gateway',
      enabled: true, quota_policy: {} },
    { provider_access_id: 'p2-access', provider_id: 'p2', display_name: 'Empty',
      enabled: true, quota_policy: {} },
  ],
  connections: [
    { connection_id: 'p1-conn', provider_access_id: 'p1-access',
      base_url: 'https://gw.example/v1', protocol: 'openai', enabled: true,
      extra_headers: {} },
    { connection_id: 'p2-conn', provider_access_id: 'p2-access',
      base_url: 'https://empty.example/v1', protocol: 'openai', enabled: true,
      extra_headers: {} },
  ],
  credentials: [
    { credential_id: 'cred-a', provider_access_id: 'p1-access', kind: 'api_key',
      key_hint: '2208…5409', enabled: true,
      authorization: { connection_ids: ['p1-conn'], models: [] }, quota_policy: {} },
  ],
  creators: [{ creator_id: 'meta', name: 'Meta' }],
  models: [
    { creator_id: 'meta', model_id: 'llama', display_name: 'Llama',
      capabilities: ['text'], context_window: 1000,
      list_pricing: { input: 1, output: 2, currency: 'USD' } },
    { creator_id: 'meta', model_id: 'esm', display_name: 'ESM',
      capabilities: ['text'], context_window: 500 },
    { creator_id: 'x', model_id: 'solo', display_name: 'Solo', capabilities: [] },
  ],
  offerings: [
    { offering_id: 'off-esm', provider_access_id: 'p1-access',
      identity_state: 'confirmed', model: { creator_id: 'meta', model_id: 'esm' },
      enabled: true, stale: false, capabilities: ['text'], context_window: 500,
      actual_pricing: { input: 5, output: 6, currency: 'USD' }, priority: 0 },
  ],
  deployments: [
    { deployment_id: 'dep-esm', offering_id: 'off-esm', connection_id: 'p1-conn',
      wire_model_id: 'esm', enabled: true, identity_confidence: 'high',
      probe_status: 'passed', priority: 0 },
  ],
});
global._stgModelRouting = global.freshRouting();
// Cross-section collaborators that live outside the two sections under
// test. Stubs keep the staged-add path honest without dragging core_panel /
// save_export into the shared lexical scope.
global._stgRoutingPersistChain = Promise.resolve();
global._persistModelRoutingNow = () => Promise.resolve(null);
eval(fs.readFileSync(process.argv[5], 'utf8'));
global.getManualMatrixProbe = () => global.manualMatrixProbe;
// provider_render and access_matrix share a lexical scope in the shipped
// bundle; eval them CONCATENATED so cross-file function/const bindings
// resolve exactly as they do in production (per-file eval hides them).
eval(fs.readFileSync(process.argv[2], 'utf8') + '\n;\n' +
     fs.readFileSync(process.argv[3], 'utf8'));
const out = {};
(async () => {
%s
  console.log(JSON.stringify(out));
})().catch(error => {
  console.error(error && error.stack || error);
  process.exit(1);
});
"""


def _run_harness(tmp_path, scenario: str) -> dict:
    provider = tmp_path / "provider-render.js"
    provider.write_text(runtime_section("settings/provider_render.js"), encoding="utf-8")
    matrix = tmp_path / "access-matrix.js"
    matrix.write_text(runtime_section("settings/access_matrix.js"), encoding="utf-8")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS_PRELUDE % scenario, encoding="utf-8")
    result = subprocess.run(
        [NODE, str(harness), str(provider), str(matrix), str(ZH_LOCALE),
         native_module_path('.native/manual-matrix-probe.js',
                            ROOT / 'frontend/src/features/settings/manual-matrix-probe.ts')],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_stage_catalog_models_ship_routable_supply(tmp_path):
    out = _run_harness(
        tmp_path,
        r"""
  const added = await _stageV2AddModels('p1', [
    global._stgModelRouting.models.find(m => m.model_id === 'llama'),
    global._stgModelRouting.models.find(m => m.model_id === 'solo'),
  ], []);
  const llama = global._stgModelRouting.offerings.find(
    o => o.model && o.model.model_id === 'llama');
  const solo = global._stgModelRouting.offerings.find(
    o => o.model && o.model.model_id === 'solo');
  const llamaDeployment = global._stgModelRouting.deployments.find(
    d => d.offering_id === llama.offering_id);
  const duplicate = await _stageV2AddModels('p1', [
    global._stgModelRouting.models.find(m => m.model_id === 'llama'),
  ], []);
  const llamaCount = global._stgModelRouting.offerings.filter(
    o => o.model && o.model.model_id === 'llama').length;
  const grants = global._stgModelRouting.credentials[0].authorization.models;
  out.added = added;
  out.llamaOffering = {
    identity_state: llama.identity_state, enabled: llama.enabled,
    stale: llama.stale, capabilities: llama.capabilities,
    context_window: llama.context_window,
  };
  out.soloDefaults = {
    capabilities: solo.capabilities, context_window: solo.context_window };
  out.llamaDeployment = {
    wire_model_id: llamaDeployment.wire_model_id,
    connection_id: llamaDeployment.connection_id,
    enabled: llamaDeployment.enabled,
    identity_confidence: llamaDeployment.identity_confidence,
    probe_status: llamaDeployment.probe_status,
  };
  out.duplicate = duplicate;
  out.llamaCount = llamaCount;
  out.grants = grants;
  out.toasts = global.__toasts;
""",
    )
    assert out["added"] == 2
    offering = out["llamaOffering"]
    assert offering["identity_state"] == "confirmed"
    assert offering["enabled"] is True
    assert offering["stale"] is False
    # Creation asserts the catalog association, not successful transport.
    # JSON save payloads omit the retired per-request-name switch.
    deployment = out["llamaDeployment"]
    assert "enabled" not in deployment
    assert deployment["probe_status"] == "unprobed"
    assert deployment["identity_confidence"] == "high"
    assert deployment["wire_model_id"] == "llama"
    assert deployment["connection_id"] == "p1-conn"
    # Catalog facts are inherited; empty catalog facts fall back to defaults.
    assert offering["capabilities"] == ["text"]
    assert offering["context_window"] == 1000
    assert out["soloDefaults"] == {"capabilities": ["text"], "context_window": 128000}
    # Routing refuses confirmed refs without a credential grant: staging
    # must extend every existing credential of this access, deduped.
    assert out["grants"] == [
        {"creator_id": "meta", "model_id": "llama"},
        {"creator_id": "x", "model_id": "solo"},
    ]
    # Re-staging the same catalog model is a no-op (wire-id dedupe).
    assert out["duplicate"] == 0
    assert out["llamaCount"] == 1


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_stage_custom_model_pending_identity(tmp_path):
    out = _run_harness(
        tmp_path,
        r"""
  const added = await _stageV2AddModels('p1', [], ['deepseek-v4-flash']);
  const offering = global._stgModelRouting.offerings.find(
    o => o.identity_state === 'pending_identity');
  const deployment = global._stgModelRouting.deployments.find(
    d => d.offering_id === offering.offering_id);
  const duplicate = await _stageV2AddModels('p1', [], ['deepseek-v4-flash']);
  const customCount = global._stgModelRouting.offerings.filter(
    o => o.identity_state === 'pending_identity').length;
  out.added = added;
  out.offering = {
    identity_state: offering.identity_state,
    pending_model_id: offering.pending_model_id,
    enabled: offering.enabled,
    model: offering.model || null,
  };
  out.deployment = {
    wire_model_id: deployment.wire_model_id,
    identity_confidence: deployment.identity_confidence,
    probe_status: deployment.probe_status,
    enabled: deployment.enabled,
  };
  out.duplicate = duplicate;
  out.customCount = customCount;
  out.grants = global._stgModelRouting.credentials[0].authorization.models;
""",
    )
    assert out["added"] == 1
    # Custom ids are not catalog identity: they stay provider-local
    # pending_identity, never masquerade as a confirmed official model.
    assert out["offering"]["identity_state"] == "pending_identity"
    assert out["offering"]["pending_model_id"] == "deepseek-v4-flash"
    assert out["offering"]["model"] is None
    assert out["offering"]["enabled"] is True
    assert out["deployment"]["identity_confidence"] == "pending"
    assert out["deployment"]["probe_status"] == "unprobed"
    assert "enabled" not in out["deployment"]
    assert out["deployment"]["wire_model_id"] == "deepseek-v4-flash"
    # Pending offerings are provider-local: no credential grant is minted.
    assert out["grants"] == []
    assert out["duplicate"] == 0
    assert out["customCount"] == 1


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_price_falls_back_to_catalog_list_pricing(tmp_path):
    out = _run_harness(
        tmp_path,
        r"""
  await _stageV2AddModels('p1', [
    global._stgModelRouting.models.find(m => m.model_id === 'llama'),
    global._stgModelRouting.models.find(m => m.model_id === 'solo'),
  ], ['deepseek-v4-flash']);
  out.html = _renderAccessMatrix('p1');
""",
    )
    html = out["html"]
    # Provider-specific pricing wins; Model fallback is clearly a reference.
    assert '<span class="stg-v2-price-io">入</span>5' in html
    assert '<span class="stg-v2-price-io">出</span>6' in html
    # Missing actual_pricing falls back to the catalog list price, labeled.
    assert '<span class="stg-v2-price-io">入</span>1' in html
    assert '<span class="stg-v2-price-io">出</span>2' in html
    assert html.count('<span class="stg-v2-price-tag">参考价</span>') == 1
    assert '该服务商的实际价格尚未配置' in html
    assert '每百万 tokens' in html
    # Exactly the two priced rows carry a price bit — solo (neither source)
    # and the pending row render no price element at all, and the retired
    # manager's 「未设置价格」placeholder is gone for good.
    assert html.count('stg-mx-meta-bit stg-mx-price') == 2
    assert "未设置价格" not in html


@pytest.mark.parametrize('supported', [True, False])
@pytest.mark.skipif(not NODE, reason="node not installed")
def test_model_add_respects_server_inheritance_support(tmp_path, supported):
    scenario = r"""
  Api.modelRouting.get = async () => (SERVER_CAPABILITIES);
  await _stageV2AddModels('p1', [_stgModelRouting.models[0]], []);
  out.offering = _stgModelRouting.offerings.find(row => row.model.model_id === 'llama');
""".replace('SERVER_CAPABILITIES', json.dumps({'metadata_inheritance_supported': True} if supported else {}))
    offering = _run_harness(tmp_path, scenario)['offering']
    assert offering['context_window'] == 1000
    if supported:
        assert offering['inherited_model_fields'] == ['capabilities', 'context_window']
    else:
        assert 'inherited_model_fields' not in offering


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_matrix_always_rendered_and_key_input_usable(tmp_path):
    out = _run_harness(
        tmp_path,
        r"""
  const list = { innerHTML: '', querySelectorAll: () => [] };
  _renderModelRoutingProvidersTab(list);
  out.tabHtml = list.innerHTML;
  out.keyCardHtml = _renderV2KeyCard(
    { provider_id: 'p1' },
    { row: { kind: 'api_key', credential_id: 'cred-a', key_hint: '2208…5409',
             enabled: true, authorization: { connection_ids: [], models: [] } },
      index: 0 },
    0);
  out.emptyHintHtml = _renderV2KeyCard(
    { provider_id: 'p1' },
    { row: { kind: 'api_key', credential_id: 'cred-empty', key_hint: '',
             enabled: true, authorization: { connection_ids: [], models: [] } },
      index: 1 },
    1);
""",
    )
    tab = out["tabHtml"]
    # The matrix IS the model manager: every card embeds it inline, and a
    # provider with zero offerings still exposes 「+ 添加模型」 — otherwise
    # its first model could never be added through the UI.
    empty_card = tab[tab.index('data-provider-id="p2"') :]
    assert "stg-matrix" in empty_card
    assert "+ 添加模型" in empty_card
    assert "该服务商还没有模型供给" in empty_card
    # Layout fills the editor row via CSS, not a content size= attribute that
    # collapses empty stored hints to a 1-character sliver.
    assert 'size="' not in out["keyCardHtml"]
    assert "2208…5409" in out["keyCardHtml"]
    assert 'class="stg-keys-input"' in out["keyCardHtml"]
    empty = out["emptyHintHtml"]
    assert 'size="' not in empty
    assert "••••••••" in empty
    assert 'class="stg-keys-input"' in empty


def test_key_card_layout_stacks_secret_above_stats():
    css = (
        ROOT / "frontend" / "src" / "styles" / "application"
        / "06-file-changes-settings-shell.css"
    ).read_text(encoding="utf-8")
    card = css[css.index(".stg-key-card {") : css.index(".stg-key-card:hover {")]
    assert "flex-direction: column" in card
    stats = css[css.index(".stg-key-card-stats {") : css.index(".stg-key-card--blank")]
    assert "width: 100%" in stats
    assert "flex-wrap: wrap" in stats
    inp = css[
        css.index(".stg-key-card-edit > input.stg-keys-input {") :
        css.index(".stg-key-card-edit > input.stg-keys-input:hover {")
    ]
    assert "min-width: 12em" in inp
    generic = css[
        css.index(".stg-field input:not(.stg-keys-input),") :
        css.index(".stg-field input:not(.stg-keys-input):focus,")
    ]
    assert ".stg-keys-input" in generic


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_matrix_rows_state_chips_sort_filter_and_actions(tmp_path):
    out = _run_harness(
        tmp_path,
        r"""
  global._stgModelRouting.models.push(
    { creator_id: 'meta', model_id: 'old', display_name: 'Old',
      capabilities: ['vision'], context_window: 8, lifecycle: 'retired' });
  global._stgModelRouting.offerings.push(
    { offering_id: 'off-old', provider_access_id: 'p1-access',
      identity_state: 'confirmed', model: { creator_id: 'meta', model_id: 'old' },
      enabled: true, stale: false, capabilities: ['vision'], context_window: 8,
      priority: 0 },
    { offering_id: 'off-pend', provider_access_id: 'p1-access',
      identity_state: 'pending_identity', pending_model_id: 'custom-x',
      enabled: false, stale: false, capabilities: ['text'], context_window: 1000,
      priority: 0 });
  global._stgModelRouting.offerings.find(
    o => o.offering_id === 'off-esm').enabled = false;
  // A disabled request name under off-esm (the freshly-added shape: the
  // backend refuses to enable a deployment before a passed probe).
  global._stgModelRouting.deployments.push(
    { deployment_id: 'dep-esm-east', offering_id: 'off-esm',
      connection_id: 'p1-conn', wire_model_id: 'esm-east', enabled: false,
      identity_confidence: 'pending', probe_status: 'unprobed', priority: 100 });
  out.allHtml = _renderAccessMatrix('p1');
  _setMatrixFilter('p1', 'disabled');
  out.disabledHtml = _renderAccessMatrix('p1');
  _setMatrixFilter('p1', 'pending');
  out.pendingHtml = _renderAccessMatrix('p1');
  _setMatrixFilter('p1', 'all');
  _setMatrixQuery('p1', 'ESM-east');
  out.searchHtml = _renderAccessMatrix('p1');
  _setMatrixQuery('p1', '');
  out.ctxLabels = [_modelRoutingContextLabel(1000000), _modelRoutingContextLabel(128000),
    _modelRoutingContextLabel(8192), _modelRoutingContextLabel(16000), _modelRoutingContextLabel(500)];
""",
    )
    html = out["allHtml"]
    # 已启用排最前：off-old 是唯一启用行，须在停用的 esm / custom-x 之前；
    # 停用行之间按 id 字母序（custom-x 在 esm 前）。
    assert html.index('data-id="old"') < html.index('data-id="custom-x"')
    assert html.index('data-id="custom-x"') < html.index('data-id="esm"')
    # 停用只暗化，不用删除线冒充「已删除」。
    assert "stg-mx-row model-off is-root" in html
    # 删除线只留给目录已下线（retired）的模型：芯片 + 悬停说明。
    assert "is-retired" in html
    assert '<span class="stg-mx-chip retired" title="官方目录已将该模型标记为下线，建议移除">已下线</span>' in html
    assert 'stg-mx-mid retired' in html
    # 待确认供给：芯片 + 悬停说明，行斜纹（无 ModelRef 可授权/探测）。
    locale = json.loads(ZH_LOCALE.read_text())
    assert ('<span class="stg-mx-chip pending" title="' + locale['settings.matrixPendingHint'] +
            '">' + locale['settings.matrixPendingChip'] + '</span>') in html
    assert "stg-mx-model root logical model-off pending" in html
    # 上下文按 K/M 缩写：500 与 8 保持原样。
    assert "上下文 500" in html
    assert "上下文 8" in html
    assert out["ctxLabels"] == ["1M", "128K", "8.2K", "16K", "500"]
    # 能力芯片中文标签，原始 capability 名保留在 title。
    assert 'title="text">文本</span>' in html
    assert 'title="vision">视觉</span>' in html
    # 请求名渲染为树形子行：未启用的带状态芯片与自己的删除 ×。
    assert "stg-mx-model alias" in html
    assert "esm-east" in html
    assert "未启用" not in html
    assert "wire-off" not in html
    assert "_removeV2Alias(1)" in html
    # Both configured and provider-scoped request names retain probe cells.
    assert "_probeMatrixScope(this.dataset.providerId,'row',&quot;esm-east&quot;)" in html
    assert "'cell',0,&quot;esm-east&quot;" in html
    assert html.count('stg-mx-cell logical') >= 3
    assert "'cell',0,&quot;custom-x&quot;" not in html  # no invented deployment
    # Root 行动作：探测行 + 添加请求名 + 移除供给（垃圾桶，不是 ✕）。
    assert "_addV2Alias(this.dataset.providerId,'off-esm')" in html
    assert "_removeV2Offering(this.dataset.providerId,'off-esm')" in html
    assert ">✕</button>" not in html
    # Manage row：搜索框（匹配 canonical + 请求名）与四枚状态筛选芯片（含计数）。
    assert 'id="stgMatrixSearch-p1"' in html
    assert "_setMatrixQuery(this.dataset.providerId,this.value)" in html
    assert ">全部 <em>3</em>" in html
    assert ">已启用 <em>1</em>" in html
    assert ">已停用 <em>1</em>" in html
    assert '>' + locale['settings.matrixFilterPending'] + ' <em>1</em>' in html
    # 状态筛选：disabled 只剩停用行（含其请求名），pending 只剩待确认行。
    disabled = out["disabledHtml"]
    assert 'data-id="esm"' in disabled
    assert "esm-east" in disabled
    assert 'data-id="old"' not in disabled
    assert "custom-x" not in disabled
    pending = out["pendingHtml"]
    assert "custom-x" in pending
    assert 'data-id="old"' not in pending
    assert 'data-id="esm"' not in pending
    # 搜索大小写不敏感，且命中禁用的请求名（esm-east 属于 off-esm）。
    search = out["searchHtml"]
    assert "esm-east" in search
    assert 'data-id="esm"' in search
    assert 'data-id="old"' not in search
    assert "custom-x" not in search


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_probe_pass_waits_for_explicit_apply(tmp_path):
    out = _run_harness(
        tmp_path,
        r"""
  global._stgModelRouting.deployments.push(
    { deployment_id: 'dep-esm-east', offering_id: 'off-esm',
      connection_id: 'p1-conn', wire_model_id: 'esm-east', enabled: false,
      identity_confidence: 'pending', probe_status: 'unprobed', priority: 100 });
  // A FAILING verdict on the disabled id changes nothing and stays
  // non-actionable (recommend_disable never re-arms client-side either).
  _ingestProbeSnapshot('p1', {
    probe_schema_version: 1, status: 'done', total: 2, done_count: 2,
    cells: {
      '0::esm': { key_idx: 0, root_model_id: 'esm', model_id: 'esm',
        status: 'ok', detail: 'HTTP 200', probe_surface: 'chat',
        proof: 'generated_text', recommend_disable: false },
      '0::esm-east': { key_idx: 0, root_model_id: 'esm', model_id: 'esm-east',
        status: 'not_found', detail: 'HTTP 404', probe_surface: 'chat',
        recommend_disable: true, deployment_enabled: false },
    },
    summary: { ok: 1, disable: 0 },
  });
  const failDep = global._stgModelRouting.deployments.find(
    x => x.deployment_id === 'dep-esm-east');
  out.failDep = { enabled: failDep.enabled, probe_status: failDep.probe_status };
  out.failCell = _stgMatrixProbe['p1'].cells['0::esm-east'];
  out.failToasts = global.__toasts.slice();
  global.__toasts = [];
  const beforePass = JSON.stringify(global._stgModelRouting);
  // A pass updates the displayed verdict, not the routing draft.
  const passed = {
    probe_schema_version: 3, status: 'done', total: 2, done_count: 2,
    cells: {
      '0::esm': { key_idx: 0, root_model_id: 'esm', model_id: 'esm',
        status: 'ok', detail: 'HTTP 200', probe_surface: 'chat',
        proof: 'generated_text', recommend_disable: false },
      '0::esm-east': { key_idx: 0, root_model_id: 'esm', model_id: 'esm-east',
        status: 'ok', detail: 'HTTP 200', probe_surface: 'chat',
        proof: 'generated_text', recommend_disable: false },
    },
    summary: { ok: 2, disable: 0 },
  };
  _ingestProbeSnapshot('p1', passed);
  _ingestProbeSnapshot('p1', passed); // repeated poll/resume data stays inert
  out.draftUnchanged = JSON.stringify(global._stgModelRouting) === beforePass;
  out.passCell = _stgMatrixProbe['p1'].cells['0::esm-east'];
  out.passToasts = global.__toasts.slice();
  out.beforeApplyHtml = _renderAccessMatrix('p1');
  _applyMatrixRecommendations('p1');
  const dep = global._stgModelRouting.deployments.find(
    x => x.deployment_id === 'dep-esm-east');
  out.appliedDep = { enabled: dep.enabled, probe_status: dep.probe_status,
    identity_confidence: dep.identity_confidence };
  out.appliedToasts = global.__toasts.slice();
  out.grants = global._stgModelRouting.credentials[0].authorization.models;
  out.afterHtml = _renderAccessMatrix('p1');
  const afterApply = JSON.stringify(global._stgModelRouting);
  _applyMatrixRecommendations('p1');
  out.repeatUnchanged = JSON.stringify(global._stgModelRouting) === afterApply;
  out.repeatToast = global.__toasts[global.__toasts.length - 1];
""",
    )
    # 失败只留判决，不启用、不弹提示；禁用格的 recommend_disable 被拔除。
    assert out["failDep"] == {"enabled": False, "probe_status": "unprobed"}
    assert out["failCell"]["status"] == "not_found"
    assert out["failCell"]["recommend_disable"] is False
    assert out["failCell"]["deployment_enabled"] is False
    assert out["failToasts"] == []
    # Probe completion/replay is read-only and silent. A fresh pass on an
    # ungranted wire surfaces the explicit Apply action (the grant only
    # changes on that deliberate click — never on ingest/poll/resume).
    assert out["draftUnchanged"] is True
    assert out["passCell"]["status"] == "ok"
    assert out["passToasts"] == []
    assert "stg-mx-probe-pip ok" in out["beforeApplyHtml"]
    assert "stg-mx-apply" in out["beforeApplyHtml"]  # enable-only IS actionable
    # Apply mints the grant but never touches the deployment row's persisted
    # enabled/probe/identity diagnostics — those are the probe's own evidence.
    assert out["appliedDep"] == {"enabled": False, "probe_status": "unprobed",
                                 "identity_confidence": "pending"}
    assert out["appliedToasts"] == ["已应用：移除 1 项模型授权 — 保存后生效。"]
    assert out["grants"] == [{"creator_id": "meta", "model_id": "esm"}]
    assert "stg-mx-apply" not in out["afterHtml"]  # nothing left to apply
    assert out["repeatUnchanged"] is True
    assert out["repeatToast"] == "没有需要应用的推荐"


@pytest.mark.skipif(not NODE, reason="node not installed")
@pytest.mark.parametrize("entry_point", ["start", "poll", "resume"])
def test_probe_lifecycle_never_changes_routing_or_toasts(tmp_path, entry_point):
    scenario = r"""
  global._stgModelRouting.deployments.push(
    { deployment_id: 'dep-new', offering_id: 'off-esm', connection_id: 'p1-conn',
      wire_model_id: 'esm-new', enabled: false, identity_confidence: 'pending',
      probe_status: 'unprobed', priority: 100 });
  const snapshot = {
    probe_schema_version: 3, status: 'done', total: 1, done_count: 1,
    cells: { '0::esm-new': {
      key_idx: 0, root_model_id: 'esm', model_id: 'esm-new', status: 'ok',
      proof: 'generated_text', probe_surface: 'chat', deployment_enabled: false,
      recommend_disable: false } }, summary: { ok: 1, disable: 0 },
  };
  const before = JSON.stringify(global._stgModelRouting);
  global._stgModelRoutingSavedJson = before;
  let pendingTimer;
  global.setTimeout = callback => { pendingTimer = callback; return 1; };
  global.clearTimeout = () => {};
  global.__elements.stgProviderList = { innerHTML: '', querySelectorAll: () => [] };
  _stgMatrixProbeAttached.p1 = true;
  _stgMatrixProbeAttached.p2 = true;
  global.Api.modelRouting.probeCellsStart = async () => snapshot;
  global.Api.modelRouting.probeCellsStatus = async () => snapshot;
  if (ENTRY_POINT === 'start') _runMatrixProbe('p1', false, { model_ids: ['esm-new'] });
  else if (ENTRY_POINT === 'resume') _resumeMatrixProbe('p1');
  else { _pollMatrixProbe('p1'); pendingTimer(); }
  await new Promise(resolve => setImmediate(resolve));
  out.unchanged = JSON.stringify(global._stgModelRouting) === before;
  out.toasts = global.__toasts;
  out.status = _stgMatrixProbe.p1.cells['0::esm-new'].status;
  out.html = _renderAccessMatrix('p1');
"""
    out = _run_harness(tmp_path, scenario.replace("ENTRY_POINT", json.dumps(entry_point)))
    # Ingest/poll/resume are read-only: no routing mutation, no toast. But a
    # fresh ok on an ungranted wire IS actionable, so the explicit Apply
    # action surfaces — the grant still changes only on that deliberate click.
    assert out["unchanged"] is True
    assert out["toasts"] == []
    assert out["status"] == "ok"
    assert "stg-mx-apply" in out["html"]


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_apply_mixed_recommendations_preserves_scope_and_grant_policy(tmp_path):
    out = _run_harness(tmp_path, r"""
  // Distinct grant objects per credential — a shared reference would alias
  // the in-place retune across credentials and fake a cross-provider leak.
  global._stgModelRouting.credentials[0].authorization.models = [{ creator_id: 'meta', model_id: 'esm' }];
  global._stgModelRouting.credentials.push({
    credential_id: 'cred-b', provider_access_id: 'p1-access', kind: 'api_key',
    enabled: true, authorization: { connection_ids: ['p1-conn'], models: [] },
  }, {
    credential_id: 'cred-other', provider_access_id: 'p2-access', kind: 'api_key',
    enabled: true, authorization: { connection_ids: ['p2-conn'], models: [{ creator_id: 'meta', model_id: 'esm' }] },
  });
  global._stgModelRouting.offerings.push({
    ...global._stgModelRouting.offerings[0], offering_id: 'off-other',
    provider_access_id: 'p2-access',
  });
  for (const wire of ['esm-pass', 'esm-fail', 'esm-unknown']) {
    global._stgModelRouting.deployments.push({
      deployment_id: wire, offering_id: 'off-esm', connection_id: 'p1-conn',
      wire_model_id: wire, enabled: false, probe_status: 'unprobed',
      identity_confidence: 'pending', priority: 100,
    });
  }
  const other = { ...global._stgModelRouting.deployments[1],
    deployment_id: 'other-pass', offering_id: 'off-other', connection_id: 'p2-conn' };
  global._stgModelRouting.deployments.push(other);
  // Symmetric apply: a fresh ok on an ungranted wire mints a grant, a flagged
  // wire on a granted credential adds a per-wire exclusion. Legacy
  // deployment_enabled:false markers stay non-actionable for REMOVAL only.
  const cell = (key_idx, model_id, status, recommend_disable, deployment_enabled) => ({
    key_idx, root_model_id: 'esm', model_id, status, recommend_disable,
    deployment_enabled, probe_surface: 'chat', proof: 'generated_text',
  });
  const before = JSON.stringify(global._stgModelRouting);
  _ingestProbeSnapshot('p1', {
    probe_schema_version: 3, status: 'done', total: 5, done_count: 5,
    cells: {
      '0::esm': cell(0, 'esm', 'not_found', true, true),
      '0::esm-pass': cell(0, 'esm-pass', 'ok', false, false),
      '1::esm-pass': cell(1, 'esm-pass', 'ok', false, false),
      '0::esm-fail': cell(0, 'esm-fail', 'not_found', true, false),
      '0::esm-unknown': cell(0, 'esm-unknown', 'unverified', false, false),
    }, summary: { ok: 2, disable: 2 },
  });
  out.beforeUnchanged = JSON.stringify(global._stgModelRouting) === before;
  out.beforeHtml = _renderAccessMatrix('p1');
  _applyMatrixRecommendations('p1');
  out.deployments = global._stgModelRouting.deployments;
  out.grants = global._stgModelRouting.credentials.map(c => c.authorization.models);
  out.toasts = global.__toasts;
""")
    assert out["beforeUnchanged"] is True
    # Two actionable changes: retune cred-a (exclude `esm`) + mint cred-b.
    assert "应用推荐 (2)" in out["beforeHtml"]
    # Apply never touches the deployment rows' persisted diagnostics.
    deployments = {row["deployment_id"]: row for row in out["deployments"]}
    for key in ("esm-pass", "esm-fail", "esm-unknown", "other-pass"):
        assert deployments[key]["enabled"] is False
        assert deployments[key]["probe_status"] == "unprobed"
    # cred-a: `esm` wire flagged (deployment_enabled:true → actionable) gets a
    #   per-wire exclusion; `esm-fail` carries the legacy marker so its disable
    #   is NOT applied (stays reachable). cred-b: fresh ok on `esm-pass` mints
    #   the grant. cred-other (different provider) is untouched.
    assert out["grants"] == [
        [{"creator_id": "meta", "model_id": "esm",
          "excluded_wire_model_ids": ["esm"]}],
        [{"creator_id": "meta", "model_id": "esm"}],
        [{"creator_id": "meta", "model_id": "esm"}],
    ]
    assert out["toasts"] == ["已应用：移除 2 项模型授权 — 保存后生效。"]


@pytest.mark.skipif(not NODE, reason="node not installed")
@pytest.mark.parametrize("status", ["running", "error"])
def test_incomplete_probe_cannot_enable_request_names(tmp_path, status):
    out = _run_harness(tmp_path, r"""
  const dep = global._stgModelRouting.deployments[0];
  dep.enabled = false;
  dep.probe_status = 'unprobed';
  dep.identity_confidence = 'pending';
  const before = JSON.stringify(global._stgModelRouting);
  _ingestProbeSnapshot('p1', {
    status: PROBE_STATUS, cells: { '0::esm': {
      key_idx: 0, root_model_id: 'esm', model_id: 'esm', status: 'ok',
      proof: 'generated_text', recommend_disable: false,
    } }, summary: { ok: 1, disable: 0 },
  });
  out.html = _renderAccessMatrix('p1');
  _applyMatrixRecommendations('p1');
  out.unchanged = JSON.stringify(global._stgModelRouting) === before;
""".replace("PROBE_STATUS", json.dumps(status)))
    assert out["unchanged"] is True
    assert "stg-mx-apply" not in out["html"]


def test_matrix_toolbar_static_contract():
    matrix = runtime_section("settings/access_matrix.js")
    # Manage row: search + exclusive status buckets + 添加模型 picker trigger.
    assert "_setMatrixQuery(this.dataset.providerId,this.value)" in matrix
    assert "_setMatrixFilter(this.dataset.providerId,\\'" in matrix
    assert "_openProviderModelPicker(this.dataset.providerId)" in matrix
    # Root-row supply actions: add request name + remove offering; alias remove.
    assert "_addV2Alias(this.dataset.providerId,\\'" in matrix
    assert "_removeV2Offering(this.dataset.providerId,\\'" in matrix
    assert "_removeV2Alias(" in matrix
    # Probe toolbar + per-scope probes stay.
    for action in (
        "_runMatrixProbe(this.dataset.providerId,",
        "_applyMatrixRecommendations(this.dataset.providerId)",
        "_clearMatrixProbe(this.dataset.providerId)",
        "_setMatrixAttempts(this.dataset.providerId,this.value)",
        "_probeMatrixScope(this.dataset.providerId,\\'col\\',",
    ):
        assert action in matrix
    # The picker implementation stays in provider_render.js on the delegated
    # data-tofu-action contract.
    source = runtime_section("settings/provider_render.js")
    for action in (
        "_closeProviderModelPicker()",
        "_stageV2CustomModelFromPicker()",
        "_stageV2CheckedCatalogModels()",
        "_filterProviderModelPicker(this.value)",
        "_toggleProviderModelPick(this.dataset.modelKey,this.checked)",
    ):
        assert "data-tofu-action" in source and action in source
    # The retired manager overlay is gone for good.
    assert "_renderProviderManagerModelRows" not in source
    assert "_stgMatrixOpen" not in source
    assert "stg-v2-model-identity" not in source
    css = shipped_source_text("frontend/src/styles/settings/07-access-matrix.css")
    for rule in (
        ".stg-mx-manage",
        ".stg-matrix .stg-mx-search",
        ".stg-mx-chip.pending",
        ".stg-mx-chip.retired",
        ".stg-mx-meta",
        ".stg-mx-rowacts",
        ".stg-mx-act.danger",
        ".stg-mx-model.root.pending",
        ".stg-mx-empty",
    ):
        assert rule in css
    shared = shipped_source_text("frontend/src/styles/settings/06-model-routing-v2.css")
    assert ".stg-v2-filter-chip" in shared
    assert ".stg-v2-price-tag" in shared
    # Dead manager chrome must not linger in the shipped stylesheets.
    assert "stg-mcard" not in shared
    assert "stg-provider-v2-foot" not in shared
    assert "stg-v2-model-row" not in shared


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_all_wire_pool_logical_head_and_shared_grants(tmp_path):
    out = _run_harness(tmp_path, r"""
  global._stgModelRouting.deployments = ['esm-east', 'esm-west'].map((wire, i) => ({
    deployment_id: 'dep-' + i, offering_id: 'off-esm', connection_id: 'p1-conn',
    wire_model_id: wire, enabled: false, identity_confidence: 'pending', probe_status: 'unprobed',
  }));
  const before = JSON.stringify(global._stgModelRouting.deployments);
  const entry = _matrixModelRows(_modelRoutingProviderContext('p1'))[0];
  out.pool = _matrixRowPool(entry);
  out.logical = _matrixIsPureLogical(entry);
  out.search = _matrixGroupMatches(entry, 'esm-west', 'all');
  _toggleMatrixAccess('p1', 0, 0);
  out.onHtml = _renderAccessMatrix('p1');
  out.grantsAfterOn = JSON.parse(JSON.stringify(global._stgModelRouting.credentials[0].authorization.models));
  _toggleMatrixAccess('p1', 0, 0);
  out.offHtml = _renderAccessMatrix('p1');
  out.grantsAfterOff = JSON.parse(JSON.stringify(global._stgModelRouting.credentials[0].authorization.models));
  out.deploymentsUnchanged = before === JSON.stringify(global._stgModelRouting.deployments);
""")
    assert out['pool'] == ['esm-east', 'esm-west']
    assert out['logical'] and out['search'] and out['deploymentsUnchanged']
    # Per-wire toggle: enabling mints one shared grant reaching both wires.
    assert out['grantsAfterOn'] == [{'creator_id': 'meta', 'model_id': 'esm'}]
    assert out['onHtml'].count('stg-mx-cell on') == 2
    assert 'stg-mx-cell off' not in out['onHtml']
    # Toggling the primary wire off excludes ONLY that wire; the sibling wire
    # stays on (a bare toggle resolves to the row's primary wire `esm-east`).
    assert out['grantsAfterOff'] == [{'creator_id': 'meta', 'model_id': 'esm',
                                      'excluded_wire_model_ids': ['esm-east']}]
    assert out['offHtml'].count('stg-mx-cell on') == 1
    assert out['offHtml'].count('stg-mx-cell off') == 1
    assert out['onHtml'].count('stg-mx-cell logical') == 1
    assert '未启用' not in out['onHtml'] and 'wire-off' not in out['onHtml']
    assert 'data-id="esm"' in out['onHtml']


@pytest.mark.skipif(not NODE, reason="node not installed")
@pytest.mark.parametrize('status', ['ok', 'rate_limited', 'not_found', 'unauthorized', 'error'])
def test_probe_outcomes_observational_apply_only_removes_grants(tmp_path, status):
    out = _run_harness(tmp_path, r"""
  const ref = { creator_id: 'meta', model_id: 'esm' };
  global._stgModelRouting.credentials[0].authorization.models = [ref];
  const dep = global._stgModelRouting.deployments[0];
  dep.enabled = false;
  dep.identity_confidence = 'pending';
  dep.probe_status = 'unprobed';
  const before = JSON.stringify(global._stgModelRouting);
  const flagged = PROBE_STATUS === 'not_found' || PROBE_STATUS === 'error';
  _ingestProbeSnapshot('p1', {
    probe_schema_version: 3, status: 'done', total: 1, done_count: 1,
    cells: { '0::esm': { key_idx: 0, root_model_id: 'esm', model_id: 'esm',
      status: PROBE_STATUS, proof: 'generated_text', probe_surface: 'chat',
      recommend_disable: flagged } }, summary: { ok: 0, disable: flagged ? 1 : 0 },
  });
  out.observational = before === JSON.stringify(global._stgModelRouting);
  const evidence = JSON.stringify(_stgMatrixProbe['p1']);
  const deployments = JSON.stringify(global._stgModelRouting.deployments);
  const offerings = JSON.stringify(global._stgModelRouting.offerings);
  _applyMatrixRecommendations('p1');
  out.evidenceUnchanged = evidence === JSON.stringify(_stgMatrixProbe['p1']);
  out.deploymentsUnchanged = deployments === JSON.stringify(global._stgModelRouting.deployments);
  out.offeringsUnchanged = offerings === JSON.stringify(global._stgModelRouting.offerings);
  out.grants = global._stgModelRouting.credentials[0].authorization.models;
""".replace('PROBE_STATUS', json.dumps(status)))
    assert out['observational'] and out['evidenceUnchanged']
    assert out['deploymentsUnchanged'] and out['offeringsUnchanged']
    assert out['grants'] == ([] if status in ('not_found', 'error') else [
        {'creator_id': 'meta', 'model_id': 'esm'}])


@pytest.mark.skipif(not NODE, reason="node not installed")
def test_manual_alias_has_explicit_identity_but_no_probe_or_switch(tmp_path):
    out = _run_harness(tmp_path, r"""
  global.showPrompt = async () => 'esm-manual';
  await _addV2Alias('p1', 'off-esm');
  out.deployment = JSON.parse(JSON.stringify(global._stgModelRouting.deployments.at(-1)));
""")
    assert out['deployment']['wire_model_id'] == 'esm-manual'
    assert out['deployment']['identity_confidence'] == 'high'
    assert out['deployment']['probe_status'] == 'unprobed'
    assert 'enabled' not in out['deployment']
