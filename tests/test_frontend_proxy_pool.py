#!/usr/bin/env python3
"""jsdom contract for the low-cognition Network proxy settings editor.

The UI owns one concept per proxy: a complete URL. Credential storage stays
behind that field; secondary name/scope controls are collapsed. Bypass rules
are directly editable rows rather than immutable chips.
"""

import os

import pytest

from tests._jsdom import JS_DIR, run_harness
from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
HTML_SAFETY = native_module_path(
    '.native/proxy-html-safety.js',
    os.path.join(ROOT, 'frontend', 'src', 'html-safety.ts'),
)

_BODY = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>' +
    '<span id="proxyPoolCount"></span>' +
    '<div id="proxyPoolList"></div>' +
    '<button id="proxyPoolAddBtn"></button>' +
    '<div id="proxyEnvBanner" style="display:none"><span id="proxyEnvBannerText"></span></div>' +
    '<span id="proxyBypassCount"></span>' +
    '<div id="proxyBypassList"></div>' +
    '<button id="proxyBypassAddBtn"></button>' +
    '<div id="proxyEnvHint" style="display:none"><span id="proxyEnvHintText"></span></div>' +
    '</body>',
  targets: [process.argv[4], process.argv[2], process.argv[5]],
  globals: {
    _setVal: function (id, value) {
      var el = document.getElementById(id);
      if (el) el.value = value;
    },
    t: function (key) {
      var dict = {
        'settings.proxyPoolAdd': '添加代理',
        'settings.proxyBypassAdd': '添加直连地址',
        'settings.proxyCardTitle': '代理',
        'settings.proxyPhName': '例如：公司出口',
        'settings.proxyUrlEmpty': '尚未填写链接',
        'settings.proxyUrlProtected': '敏感信息已隐藏',
        'settings.proxyShowUrl': '显示完整链接',
        'settings.proxyHideUrl': '隐藏链接',
        'settings.proxyScopeSub': '仅订阅流量',
        'settings.proxyScopeGlobal': '全部出站流量',
        'settings.proxyPoolCount': '{count} 个代理',
        'settings.proxyBypassCount': '{count} 项',
        'settings.proxyEdit': '编辑代理',
        'settings.proxyCloseEditor': '收起编辑',
        'settings.proxyEnable': '启用',
        'settings.proxyTest': '测试连接',
        'settings.proxyTesting': '测试中…',
        'settings.proxyDel': '删除',
        'settings.proxyTestOkTpl': '{label} 可达（HTTP {code} · {ms}ms）',
        'settings.proxyTestBlockedTpl': '{label} 被拦截（HTTP {code}）',
        'settings.proxyTestFailTpl': '{label} 网络失败：{err}',
        'settings.proxyLegacyRow': '旧版单代理（迁移）',
      };
      return dict[key] || '';
    },
    _stgPresets: {},
    _stgProviders: [],
    _serverConfig: { hidden_models: [], hidden_ig_models: [] },
    _collectModelDefaults: function () { return {}; },
    Api: {
      serverConfig: { update: async function (payload) {
        window.__savedPayload = payload;
        return { json: async () => ({ ok: true }) };
      } },
      network: { proxyTest: async function (payload) {
        window.__testPayload = payload;
        if (payload.url.indexOf('broken') !== -1) throw new Error('boom');
        return { any_ok: true, results: [
          { label: 'OpenAI Auth', target: 'https://auth.openai.com/oauth/token',
            status: 400, latency_ms: 321, verdict: 'ok' },
          { label: 'Anthropic API', target: 'https://api.anthropic.com/v1/messages',
            status: 403, latency_ms: 100, verdict: 'geo_blocked' },
        ] };
      } },
      credentials: { reveal: async function (name) {
        window.__revealedVault = name;
        return { name: name, value: 'saved-user:saved:password' };
      } },
    },
    _loadServerConfigAndPopulate: function () {},
    debugLog: function () {},
  },
});

const $ = (id) => document.getElementById(id);
const rows = () => document.querySelectorAll('#proxyPoolList .proxy-pool-row');
const bypassRows = () => document.querySelectorAll('#proxyBypassList .proxy-bypass-row');

(async () => {
  try {
    const longName = '公司香港出口节点名称很长但必须始终可以看见和编辑';
    const cfg = {
      network: {
        http_proxy: '', https_proxy: '', proxy_configured: false,
        proxy_bypass_domains: ['.corp.example'],
        proxy_pool: [
          { id: 'hk', name: longName, url: 'http://g-hk.example.com:8080',
            scope: 'subscription', enabled: true,
            has_credential: true, credential_vault: 'proxy_hk_auth' },
          { id: 'plain', name: '', url: 'http://plain.example.com:3128',
            scope: 'global', enabled: false,
            has_credential: false, credential_vault: '' },
        ],
      },
    };

    // Primary surface: compact rows with exactly one inline editor at a time.
    _populateNetworkTab(cfg);
    check('two_proxy_rows_rendered', rows().length === 2);
    check('one_bypass_row_rendered', bypassRows().length === 1);
    check('proxy_count_matches_rows', $('proxyPoolCount').textContent === '2 个代理');
    check('bypass_count_matches_values', $('proxyBypassCount').textContent === '1 项');
    check('saved_sensitive_url_defaults_hidden',
      rows()[0].querySelector('.pp-url').type === 'password' &&
      rows()[0].querySelector('.pp-url').value === 'http://g-hk.example.com:8080');
    check('plain_url_defaults_visible', rows()[1].querySelector('.pp-url').type === 'text');
    check('no_separate_auth_form_exists',
      !rows()[0].querySelector('.pp-auth') &&
      !rows()[0].querySelector('.pp-username') &&
      !rows()[0].querySelector('.pp-password'));
    check('enable_switch_has_own_header_lane',
      !!rows()[0].querySelector('.proxy-pool-head > .pp-enabled-control') &&
      rows()[0].querySelector('.pp-enabled-text').textContent === '启用');
    check('reorder_and_actions_live_in_footer',
      !!rows()[0].querySelector('.proxy-pool-card-footer .pp-reorder') &&
      !!rows()[0].querySelector('.proxy-pool-card-footer .pp-test-btn') &&
      !rows()[0].querySelector('.proxy-pool-head .pp-move'));
    check('long_name_has_ellipsis_target_and_full_title',
      rows()[0].querySelector('.pp-display-name').textContent === longName &&
      rows()[0].querySelector('.pp-display-name').title === longName);
    check('all_editors_start_collapsed',
      Array.from(rows()).every(function (row) {
        return row.querySelector('.proxy-pool-detail').hidden &&
          !row.classList.contains('is-editing') &&
          row.querySelector('.pp-edit-toggle').getAttribute('aria-expanded') === 'false';
      }));
    _proxyPoolToggleEditor(rows()[0].querySelector('.pp-edit-toggle'));
    check('edit_opens_complete_configuration_for_one_row',
      rows()[0].classList.contains('is-editing') &&
      !rows()[0].querySelector('.proxy-pool-detail').hidden &&
      !!rows()[0].querySelector('.pp-url') &&
      !!rows()[0].querySelector('.pp-name') &&
      !!rows()[0].querySelector('.pp-scope'));
    _proxyPoolToggleEditor(rows()[1].querySelector('.pp-edit-toggle'));
    check('opening_another_editor_closes_the_previous_one',
      !rows()[0].classList.contains('is-editing') &&
      rows()[0].querySelector('.proxy-pool-detail').hidden &&
      rows()[1].classList.contains('is-editing') &&
      document.querySelectorAll('.proxy-pool-row.is-editing').length === 1);

    // Eye reveals the complete URL, not a second credential form.
    _proxyPoolToggleUrlVisibility(rows()[0].querySelector('.pp-url-visibility'));
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    check('eye_reveals_complete_url_from_requested_vault',
      window.__revealedVault === 'proxy_hk_auth' &&
      rows()[0].querySelector('.pp-url').value ===
        'http://saved-user:saved%3Apassword@g-hk.example.com:8080' &&
      rows()[0].querySelector('.pp-url').type === 'text');
    _proxyPoolToggleUrlVisibility(rows()[0].querySelector('.pp-url-visibility'));
    check('eye_hides_complete_url_again', rows()[0].querySelector('.pp-url').type === 'password');

    // Existing global row means no legacy synthetic row.
    check('no_legacy_row_when_global_exists',
      !document.querySelector('.proxy-pool-row[data-id="legacy"]'));

    // Legacy single proxy still migrates through the same one-URL card.
    _populateNetworkTab({ network: {
      http_proxy: 'http://old.example.com:3128', proxy_configured: true,
      proxy_bypass_domains: ['.corp.example'],
      proxy_pool: [{ id: 'hk', name: '', url: 'http://g-hk.example.com:8080',
                     scope: 'subscription', enabled: true,
                     has_credential: true, credential_vault: 'proxy_hk_auth' }],
    } });
    const legacyRow = document.querySelector('.proxy-pool-row[data-id="legacy"]');
    check('legacy_row_synthesized', !!legacyRow);
    check('legacy_row_is_global', legacyRow && legacyRow.querySelector('.pp-scope').value === 'global');

    // Add accepts the provider-issued URL unchanged and hides it immediately.
    const before = rows().length;
    $('proxyPoolAddBtn').click();
    check('add_appends_proxy_row', rows().length === before + 1);
    const newRow = rows()[rows().length - 1];
    check('new_proxy_opens_its_editor_and_updates_count',
      newRow.classList.contains('is-editing') &&
      document.querySelectorAll('.proxy-pool-row.is-editing').length === 1 &&
      $('proxyPoolCount').textContent === String(before + 1) + ' 个代理');
    const providerUrl = 'http://new-user:new%3Apass@new.example.com:8080';
    newRow.querySelector('.pp-url').value = providerUrl;
    _proxyPoolUrlChanged(newRow.querySelector('.pp-url'));
    newRow.querySelector('.pp-name').value = '新代理';
    _proxyPoolSyncMeta(newRow.querySelector('.pp-name'));
    check('provider_url_remains_one_opaque_value',
      newRow.querySelector('.pp-url').value === providerUrl &&
      newRow.querySelector('.pp-url').type === 'password');
    const newPayload = _proxyPoolRowPayload(newRow);
    check('payload_has_url_and_no_auth_fields',
      newPayload.url === providerUrl &&
      !('username' in newPayload) && !('password' in newPayload));
    _proxyPoolDelete(newRow.querySelector('.pp-delete-btn'));
    check('delete_removes_proxy_row_and_updates_count',
      rows().length === before && $('proxyPoolCount').textContent === String(before) + ' 个代理');

    // Collect and credential-removal inference stay compatible with vault storage.
    $('proxyPoolAddBtn').click();
    let collected = _collectProxyPool();
    check('collect_drops_blank_proxy_rows', collected.length === 2);
    check('collect_keeps_saved_vault_without_exposing_secret',
      collected[0].credential_vault === 'proxy_hk_auth' &&
      collected[0].clear_credential === false);
    const savedRow = rows()[0];
    savedRow.querySelector('.pp-url').value = 'http://replacement.example.com:8080';
    _proxyPoolUrlChanged(savedRow.querySelector('.pp-url'));
    check('replacing_with_plain_url_clears_old_embedded_credential',
      _proxyPoolRowPayload(savedRow).clear_credential === true);
    savedRow.querySelector('.pp-url').value = 'http://g-hk.example.com:8080';
    savedRow.querySelector('.pp-url').type = 'password';
    savedRow.setAttribute('data-url-dirty', '0');

    // Reordering changes both row order and visible priority.
    const firstBeforeMove = rows()[0].getAttribute('data-id');
    _proxyPoolMove(rows()[0].querySelector('.pp-move-down'), 1);
    check('move_reorders_proxy_rows', rows()[1].getAttribute('data-id') === firstBeforeMove);
    check('move_refreshes_priority_labels',
      rows()[0].querySelector('.pp-order').textContent === '01' &&
      rows()[1].querySelector('.pp-order').textContent === '02');
    _proxyPoolMove(rows()[1].querySelector('.pp-move-up'), -1);

    // Bypass values are ordinary inputs: edit, add, collect, delete.
    check('bypass_value_is_directly_editable_input',
      bypassRows()[0].querySelector('.pb-value').value === '.corp.example');
    bypassRows()[0].querySelector('.pb-value').value =
      'https://api.internal.example.com:8443/v1';
    bypassRows()[0].querySelector('.pb-value').dispatchEvent(new window.Event('input'));
    $('proxyBypassAddBtn').click();
    bypassRows()[1].querySelector('.pb-value').value = '*.lab.example.com';
    _proxyBypassRefreshCount();
    check('bypass_add_creates_editable_row',
      bypassRows().length === 2 && bypassRows()[1].querySelector('.pb-value').tagName === 'INPUT');
    check('bypass_count_tracks_nonempty_editable_values',
      $('proxyBypassCount').textContent === '2 项');
    check('bypass_collect_carries_edited_values',
      JSON.stringify(_collectProxyBypassDomains()) === JSON.stringify([
        'https://api.internal.example.com:8443/v1', '*.lab.example.com']));
    _proxyBypassDelete(bypassRows()[1].querySelector('.pb-delete-btn'));
    check('bypass_delete_removes_only_requested_row',
      bypassRows().length === 1 && $('proxyBypassCount').textContent === '1 项');

    // Save carries both editors and never resurrects legacy proxy_config.
    await _saveServerConfig();
    check('save_payload_has_proxy_pool',
      Array.isArray(window.__savedPayload.proxy_pool) &&
      window.__savedPayload.proxy_pool.length === 2);
    check('save_payload_has_edited_bypass_value',
      JSON.stringify(window.__savedPayload.proxy_bypass_domains) ===
      JSON.stringify(['https://api.internal.example.com:8443/v1']));
    check('save_payload_has_no_legacy_proxy_config',
      !('proxy_config' in window.__savedPayload));

    // Test sends the complete URL only; backend handles extraction/storage.
    const row = rows()[0];
    row.querySelector('.pp-url').value = 'http://fresh:pw%3Acolon@g-hk.example.com:8080';
    _proxyPoolUrlChanged(row.querySelector('.pp-url'));
    _proxyPoolTest(row.querySelector('.pp-test-btn'));
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    const resultEl = row.querySelector('.pp-result');
    check('test_payload_uses_complete_url_only',
      window.__testPayload.url === 'http://fresh:pw%3Acolon@g-hk.example.com:8080' &&
      !('username' in window.__testPayload) && !('password' in window.__testPayload));
    check('test_result_reachable_line',
      resultEl.textContent.indexOf('OpenAI Auth 可达（HTTP 400 · 321ms）') !== -1);
    check('test_result_blocked_line',
      resultEl.textContent.indexOf('Anthropic API 被拦截（HTTP 403）') !== -1);
    check('test_result_ok_class', resultEl.className.indexOf('ok') !== -1);

    row.querySelector('.pp-url').value = 'http://broken.example.com:8080';
    _proxyPoolUrlChanged(row.querySelector('.pp-url'));
    _proxyPoolTest(row.querySelector('.pp-test-btn'));
    await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
    check('test_error_path',
      resultEl.className.indexOf('err') !== -1 &&
      resultEl.textContent.indexOf('boom') !== -1);

    $('proxyPoolList').remove();
    $('proxyBypassList').remove();
    check('collect_pool_null_without_container', _collectProxyPool() === null);
    check('collect_bypass_null_without_container', _collectProxyBypassDomains() === null);
  } catch (e) {
    check('harness_threw: ' + (e && e.stack || e), false);
  } finally {
    report();
  }
})();
'''


def test_proxy_pool_editor_frontend():
    run_harness(
        target_js=os.path.join(JS_DIR, 'settings', 'other_tabs.js'),
        body_js=_BODY,
        extra_targets=[
            HTML_SAFETY,
            os.path.join(JS_DIR, 'settings', 'save_export.js'),
        ],
        expect_pass=43,
        timeout=300,
        label='proxy-pool-editor',
    )
