#!/usr/bin/env python3
"""tests/test_frontend_devices_tab.py — RWA P4b-1:Settings → Devices 页.

钉住的事(每一处都是「页面静默消失」类事故的高发点):
  * 装配链:tab 按钮(index.html data-tab="devices")→ SETTINGS_PANEL:devices
    标记 → static/settings_panels/devices.html(id=settingsTab_devices)→
    settings/devices.js 在 _BUNDLE_FILES → core_panel 的 switchSettingsTab
    钩子 → Api.desktop 域命中三端点 → i18n 键存在;
  * 行为(jsdom):agents/tokens 渲染、mint 后原文上屏+列表刷新、revoke
    后刷新;NEUTER:摘掉 core_panel 钩子 → 切页签不填充 = 钩子承重。

Run isolated: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_frontend_devices_tab.py
"""

from __future__ import annotations

import os
import re

import pytest

from tests._jsdom import JS_DIR, ROOT, run_harness

pytestmark = pytest.mark.unit

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
    for el in ('devicesAgentsList', 'devicesTokensList', 'devicesMintBtn',
               'devicesMintedBox', 'devicesMintedToken'):
        assert el in frag, f'面板缺关键元素 #{el}'


def test_devices_js_in_bundle_list():
    src = _read(os.path.join(PROJECT_ROOT, 'lib', 'js_bundler.py'))
    assert "'settings/devices.js'" in src, (
        'settings/devices.js 不在 _BUNDLE_FILES —— 生产环境静默不加载 '
        '(§3.2.1 打包纪律)')


def test_switch_hook_delegates():
    src = _read(os.path.join(JS_DIR, 'settings', 'core_panel.js'))
    assert "tabId === 'devices'" in src and '_populateDevicesTab()' in src, (
        'core_panel.switchSettingsTab 缺 devices 填充钩子 —— 切到页签白屏')


def test_api_domain_hits_the_three_endpoints():
    src = _read(os.path.join(JS_DIR, 'api.js'))
    assert "get('/api/v1/desktop/devices'" in src
    assert "post('/api/v1/desktop/token'" in src
    assert '/api/v1/desktop/token/${encodeURIComponent(keyId)}' in src, (
        'Api.desktop 三端点缺一(§3.2.0 统一客户端纪律)')


def test_i18n_keys_present_both_langs():
    src = _read(os.path.join(JS_DIR, 'i18n.js'))
    for key in ("'settings.tabDevices'", "'devices.mint'", "'devices.revoke'",
                "'devices.empty'"):
        assert key in src, f'i18n 缺键 {key}'
    m = re.search(r"'settings\.tabDevices':\s*\{\s*zh:\s*'[^']+',\s*en:\s*'[^']+'\s*\}", src)
    assert m, 'settings.tabDevices 必须双语'


# ══════════════════════════════════════════════════════════════════════
#  2. 行为(jsdom 真驱)
# ══════════════════════════════════════════════════════════════════════

_DEVICES_BODY = r"""
(async () => {
const { setup } = require(process.env.JSDOM_HARNESS);

const FRAG = process.argv[4];
const fs = require('fs');
const html = '<!DOCTYPE html><body>' + fs.readFileSync(FRAG, 'utf8') + '</body>';

const calls = { get: [], post: [], del: [] };
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
        mintToken: async (name) => { calls.post.push(name);
          return { id: 'k_2', name: name || 'desktop-bridge', token: 'secret-xyz' }; },
        revokeToken: async (id) => { calls.del.push(id); return { revoked: id }; },
      },
    },
  },
});

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

// ── mint:POST 后原文上屏(唯一一次) ──
document.getElementById('devicesMintName').value = 'my-mac';
_devicesMintToken();
for (let _i = 0; _i < 6; _i++) await Promise.resolve();
check('mint_posted_with_name', calls.post[0] === 'my-mac');
check('minted_token_shown_once',
      document.getElementById('devicesMintedToken').textContent === 'secret-xyz');
check('minted_box_visible',
      document.getElementById('devicesMintedBox').style.display !== 'none');
check('list_refreshed_after_mint', calls.get.length >= 1);

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


def test_devices_tab_behaviour_jsdom():
    run_harness(
        target_js=os.path.join(JS_DIR, 'settings', 'devices.js'),
        body_js=_DEVICES_BODY,
        extra_targets=[os.path.join(PROJECT_ROOT, 'static',
                                    'settings_panels', 'devices.html')],
        min_pass=11,
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
