#!/usr/bin/env python3
"""tests/test_frontend_devices_tab.py — RWA P4b-1:Settings → Devices 页.

钉住的事(每一处都是「页面静默消失」类事故的高发点):
  * 装配链:tab 按钮(index.html data-tab="devices")→ SETTINGS_PANEL:devices
    标记 → static/settings_panels/devices.html(id=settingsTab_devices)→
    settings/devices.js 在 _BUNDLE_FILES → core_panel 的 switchSettingsTab
    钩子 → Api.desktop 域命中三端点 → i18n 键存在;
  * 行为(jsdom):agents/自动授权渲染、revoke 后刷新；不暴露 mint/
    copy 工作流;NEUTER:摘掉 core_panel 钩子 → 切页签不填充。

Run isolated: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_frontend_devices_tab.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests._jsdom import JS_DIR, ROOT, run_harness

pytestmark = pytest.mark.unit

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEVICES_TS = Path(PROJECT_ROOT) / 'frontend/src/features/settings/devices.ts'
SETTINGS_TS = Path(PROJECT_ROOT) / 'frontend/src/features/settings.ts'
ESBUILD = Path(PROJECT_ROOT) / 'node_modules/.bin/esbuild'


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


# ══════════════════════════════════════════════════════════════════════
#  1. 装配链静态钉
# ══════════════════════════════════════════════════════════════════════

def test_tab_button_and_panel_marker_in_index():
    html = _read(os.path.join(PROJECT_ROOT, 'index.html'))
    assert 'data-tab="devices"' in html, 'index.html 缺 Devices 页签按钮'
    assert '<!-- SETTINGS_PANEL:devices -->' in html, (
        'index.html 缺 SETTINGS_PANEL:devices 标记 —— 面板不会被注入')


def test_panel_fragment_exists_with_matching_id():
    frag = _read(os.path.join(PROJECT_ROOT, 'static', 'settings_panels',
                              'devices.html'))
    assert 'id="settingsTab_devices"' in frag, (
        '片段 id 必须是 settingsTab_devices(switchSettingsTab 按此约定寻址)')
    for el in ('devicesAgentsList', 'devicesTokensList'):
        assert el in frag, f'面板缺关键元素 #{el}'
    for retired in ('devicesMintBtn', 'devicesMintedBox',
                    'devicesMintedToken', 'devicesMintName'):
        assert retired not in frag, f'手工凭据流程泄回面板: #{retired}'


def test_devices_js_in_bundle_list():
    assert "import './settings/devices';" in SETTINGS_TS.read_text(), (
        '原生 settings/devices.ts 未接入 Vite settings 入口')


def test_switch_hook_delegates():
    src = _read(os.path.join(JS_DIR, 'settings', 'core_panel.js'))
    assert "tabId === 'devices'" in src and '_populateDevicesTab()' in src, (
        'core_panel.switchSettingsTab 缺 devices 填充钩子 —— 切到页签白屏')


def test_api_domain_keeps_inventory_and_revoke_endpoints():
    src = _read(os.path.join(JS_DIR, 'api.js'))
    assert "get('/api/v1/desktop/devices'" in src
    assert '/api/v1/desktop/token/${encodeURIComponent(keyId)}' in src, (
        'Api.desktop 缺授权撤销端点')


def test_i18n_keys_present_both_langs():
    locale_dir = Path(PROJECT_ROOT) / 'frontend/src/i18n/locales'
    locales = [json.loads((locale_dir / f'{lang}.json').read_text())
               for lang in ('zh', 'en')]
    for key in ('settings.tabDevices', 'devices.tokensDesc',
                'devices.revoke', 'devices.empty'):
        assert all(key in locale and locale[key] for locale in locales), \
            f'i18n 缺双语键 {key}'


# ══════════════════════════════════════════════════════════════════════
#  2. 行为(jsdom 真驱)
# ══════════════════════════════════════════════════════════════════════

_DEVICES_BODY = r"""
(async () => {
const { setup } = require(process.env.JSDOM_HARNESS);

const FRAG = process.argv[4];
const fs = require('fs');
const html = '<!DOCTYPE html><body>' + fs.readFileSync(FRAG, 'utf8') + '</body>';

const calls = { get: [], del: [] };
const fixture = {
  agents: [
    { agent_id: 'aaaaaaaabbbb', name: 'macbook', platform: 'darwin',
      share_roots: [{ name: 'myapp', path: '/code/myapp' }], online: true },
    { agent_id: 'ccccccccdddd', name: 'winbox', platform: 'win32',
      share_roots: [], online: false },
  ],
  tokens: [ { id: 'k_1', name: 'bridge-mac', created_at: 1785000000,
              scopes: ['agents:bridge'] } ],
};

const { check, report } = setup({
  root: process.argv[3],
  html,
  targets: [process.argv[2]],
  globals: {
    t: (k) => k,
    escapeHtml: (s) => String(s),
    showToast: () => {},
    Api: {
      desktop: {
        devices: async () => { calls.get.push(1); return fixture; },
        revokeToken: async (id) => { calls.del.push(id); return { revoked: id }; },
      },
    },
  },
});
const owner = globalThis.DevicesModule || window.DevicesModule;
for (const name of ['HTMLElement', 'HTMLButtonElement',
                    'AbortController', 'AbortSignal']) {
  global[name] = window[name];
}
global._renderDeviceAgents = owner.renderDeviceAgents;
global._renderDeviceTokens = owner.renderDeviceTokens;
global._devicesRevokeToken = owner.devicesRevokeToken;

// ── 渲染:agents 两行 + tokens 一行 ──
_renderDeviceAgents(fixture.agents);
_renderDeviceTokens(fixture.tokens);
const rows = document.querySelectorAll('.devices-agent-row');
check('agents_two_rows', rows.length === 2);
check('agent_offline_marked', rows[1].classList.contains('devices-offline'));
check('agent_root_shown', rows[0].innerHTML.includes('myapp'));
check('token_row_present',
      document.querySelectorAll('.devices-token-row').length === 1);
check('token_secret_never_rendered', !document.body.innerHTML.includes('secret'));

check('no_manual_auth_controls',
      !document.getElementById('devicesMintBtn') &&
      !document.getElementById('devicesMintedToken'));

// ── revoke:DELETE 后刷新 ──
_devicesRevokeToken('k_1', null);
for (let _i = 0; _i < 6; _i++) await Promise.resolve();
check('revoke_deleted', calls.del[0] === 'k_1');

// ── 空态 ──
_renderDeviceAgents([]);
check('empty_state', document.getElementById('devicesAgentsList')
      .innerHTML.includes('devices.empty'));

report();
process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _compile_devices(tmp_path):
    built = tmp_path / 'devices.js'
    proc = subprocess.run(
        [str(ESBUILD), str(DEVICES_TS), '--bundle', '--format=iife',
         '--platform=browser', '--global-name=DevicesModule',
         '--footer:js=globalThis.DevicesModule = DevicesModule;',
         f'--outfile={built}'],
        cwd=PROJECT_ROOT, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return built


@pytest.mark.skipif(not ESBUILD.is_file(), reason='esbuild not installed')
def test_devices_tab_behaviour_jsdom(tmp_path):
    built = _compile_devices(tmp_path)
    run_harness(
        target_js=str(built),
        body_js=_DEVICES_BODY,
        extra_targets=[os.path.join(PROJECT_ROOT, 'static',
                                    'settings_panels', 'devices.html')],
        min_pass=8,
        label='devices tab',
    )


_NEUTER_HOOK_BODY = r"""
(async () => {
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>' +
    '<button class="settings-tab" data-tab="devices"></button>' +
    '<div class="settings-tab-panel" id="settingsTab_devices">' +
    '<div id="devicesAgentsList"></div><div id="devicesTokensList"></div>' +
    '</div></body>',
  targets: [process.argv[2]],
  globals: {
    t: (k) => k, escapeHtml: (s) => String(s),
    _fitMatrixPanelWidth: () => {},
    Api: { desktop: { devices: async () => ({ agents: [], tokens: [] }) } },
  },
});
// indirect eval 把被测文件的顶层函数挂到 node global(不挂 window)——
// core_panel 里的裸 typeof 查的是 global,所以桩必须两边都挂。
window._populateDevicesTab = global._populateDevicesTab = () => {
  document.getElementById('devicesAgentsList').innerHTML = 'POPULATED';
};
switchSettingsTab('devices');
for (let _i = 0; _i < 6; _i++) await Promise.resolve();
check('hook_fills_tab',
      document.getElementById('devicesAgentsList').innerHTML === 'POPULATED');
report();
process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_neuter_hook_means_blank_tab():
    """NEUTER:core_panel 摘掉 devices 钩子后,切页签不再填充 ——
    先证带钩子会填充,再证摘钩子(用未挂钩版本)不填充。"""
    # 正控制:真实 core_panel.js 带钩子 → POPULATED
    run_harness(
        target_js=os.path.join(JS_DIR, 'settings', 'core_panel.js'),
        body_js=_NEUTER_HOOK_BODY,
        min_pass=1,
        label='devices hook present',
    )
    # NEUTER:临时副本摘钩子 → 断言行不通(填充不再发生)
    import subprocess
    import tempfile
    src = _read(os.path.join(JS_DIR, 'settings', 'core_panel.js'))
    anchor = "if (tabId === 'devices' && typeof _populateDevicesTab === 'function') {"
    assert anchor in src, 'neuter 锚点不在 —— 钩子形态变了?'
    neutered = src.replace(anchor, "if (false) {", 1)
    tmp = []
    try:
        with tempfile.NamedTemporaryFile(
            'w', suffix='.js', dir=os.path.join(JS_DIR, 'settings'),
            delete=False, encoding='utf-8') as fh:
            npath = fh.name
            fh.write(neutered)
        tmp.append(npath)
        with tempfile.NamedTemporaryFile(
            'w', suffix='.js', dir=os.path.dirname(os.path.abspath(__file__)),
            delete=False, encoding='utf-8') as hf:
            harness = hf.name
            hf.write(_NEUTER_HOOK_BODY.replace(
                "check('hook_fills_tab',",
                "check('neuter_tab_stays_blank',").replace(
                "=== 'POPULATED'", "!== 'POPULATED'"))
        tmp.append(harness)
        _harness_js = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   '_jsdom_harness.js')
        proc = subprocess.run(
            ['node', harness, npath, ROOT],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, 'JSDOM_HARNESS': _harness_js})
        out = (proc.stdout or '').strip()
        assert 'PASS neuter_tab_stays_blank' in out, (
            f'NEUTER 未咬:摘掉钩子后页签仍被填充?\n{out}')
    finally:
        for p in tmp:
            try:
                os.remove(p)
            except OSError:
                pass
