#!/usr/bin/env python3
"""jsdom test for the Search settings tab redesign (settings/other_tabs.js +
settings/save_export.js, with the REAL core/safe_html.js beneath).

Pins the contracts the redesign introduced:

  * MB display: the backend stores max_bytes in BYTES, the UI shows MB
    (20971520 → "20"); saving converts MB back to bytes (20 → 20971520).
    The pre-redesign UI made the user edit raw bytes.
  * Pipeline preview: one sentence says what the backend will DO with the
    current knob values, and live-updates when the user edits the inputs
    (including the filter-off "raw text" phrasing).

(The read-only 搜索诊断 status strip was later removed from the settings
panel — owner call 2026-08-14 — so its populate/render contract went with it.)

Run: make test-frontend  (skips cleanly when node/jsdom aren't installed)
"""

import os

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit

_BODY = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>' +
    '<div id="searchPipelinePreview"></div>' +
    '<input type="checkbox" id="settingLlmContentFilter" checked>' +
    '<input id="settingFetchTopN" value="6">' +
    '<input id="settingFetchTimeout" value="15">' +
    '<input id="settingMaxCharsSearch" value="60000">' +
    '<input id="settingMaxCharsDirect" value="200000">' +
    '<input id="settingMaxCharsPdf" value="0">' +
    '<input id="settingMaxBytesMB">' +
    '<div id="settingSkipDomains"></div>' +
    '<input id="settingHttpProxy"><input id="settingHttpsProxy">' +
    '<div id="settingProxyBypass"></div>' +
    '</body>',
  // Order matters: safe_html defines safeHtml/raw used by other_tabs.js.
  targets: [process.argv[4], process.argv[2], process.argv[5]],
  globals: {
    _setVal: function (id, value, prop) {
      var el = document.getElementById(id);
      if (!el) return;
      if (prop === 'checked') el.checked = !!value; else el.value = value;
    },
    // Mini t(): real zh strings for the keys this tab renders; '' elsewhere
    // so the `|| 'fallback'` branches exercise their literal defaults.
    t: function (key) {
      var dict = {
        'settings.searchPipelineTpl': '搜索引擎返回结果 → 抓取前 {n} 个网页（每页 ≤{chars} 字符 · 超时 {timeout}s）→ {filter} → 注入对话',
        'settings.searchFilterOnTpl': 'LLM 过滤杂质',
        'settings.searchFilterOffTpl': '跳过过滤（原文直送）',
      };
      return dict[key] || '';
    },
    ChipInput: {
      init: function (id, vals) { this._vals = vals || []; },
      getValues: function (id) { return this._vals || []; },
    },
    _stgPresets: {},
    _stgProviders: [],
    _serverConfig: { hidden_models: [], hidden_ig_models: [] },
    _collectModelDefaults: function () { return {}; },
    Api: { serverConfig: { update: async function (payload) {
      window.__savedPayload = payload;
      return { json: async () => ({ ok: true }) };
    } } },
    _loadServerConfigAndPopulate: function () {},
    debugLog: function () {},
  },
});

const $ = (id) => document.getElementById(id);
const previewTxt = () => $('searchPipelinePreview').textContent;

(async () => {
  try {
    const cfg = {
      search: {
        llm_content_filter: true,
        fetch_top_n: 8, fetch_timeout: 20,
        max_chars_search: 50000, max_chars_direct: 150000, max_chars_pdf: 0,
        max_bytes: 20971520,
        skip_domains: ['youtube.com'],
      },
    };

    // ── 1. populate: MB display conversion + preview ──
    _populateSearchTab(cfg);
    check('mb_display_20', $('settingMaxBytesMB').value === '20');
    check('topn_populated', $('settingFetchTopN').value === '8');
    check('preview_values',
      previewTxt().includes('抓取前 8 个网页') && previewTxt().includes('超时 20s'));
    check('preview_chars_formatted', previewTxt().includes('50,000'));
    check('preview_filter_on', previewTxt().includes('LLM 过滤杂质'));

    // ── 2. live update: edit inputs → preview follows ──
    $('settingFetchTopN').value = '3';
    $('settingFetchTopN').dispatchEvent(new window.Event('input'));
    check('preview_live_topn', previewTxt().includes('抓取前 3 个网页'));

    $('settingLlmContentFilter').checked = false;
    $('settingLlmContentFilter').dispatchEvent(new window.Event('change'));
    check('preview_filter_off_text', previewTxt().includes('跳过过滤'));
    check('preview_filter_off_class',
      $('searchPipelinePreview').classList.contains('filter-off'));

    // ── 3. save path: MB → bytes ──
    $('settingMaxBytesMB').value = '20';
    $('settingLlmContentFilter').checked = true;
    await _saveServerConfig();
    check('save_bytes_20971520',
      window.__savedPayload && window.__savedPayload.search.max_bytes === 20971520);
    check('save_topn', window.__savedPayload.search.fetch_top_n === 3);

    // fractional MB is honoured (0.5 MB → 524288), not floored to default
    $('settingMaxBytesMB').value = '0.5';
    await _saveServerConfig();
    check('save_fractional_mb', window.__savedPayload.search.max_bytes === 524288);

    // junk input falls back to the 20MB default rather than saving garbage
    $('settingMaxBytesMB').value = 'abc';
    await _saveServerConfig();
    check('save_junk_default', window.__savedPayload.search.max_bytes === 20971520);

    // ── 6. _bytesToMB edge cases ──
    check('bytes_to_mb_int', _bytesToMB(20971520) === 20);
    check('bytes_to_mb_fraction', _bytesToMB(1572864) === 1.5);
  } catch (e) {
    check('harness_threw: ' + (e && e.message), false);
  } finally {
    report();
  }
})();
'''


def test_search_settings_tab_frontend():
    run_harness(
        target_js=os.path.join(JS_DIR, 'settings', 'other_tabs.js'),
        body_js=_BODY,
        extra_targets=[
            os.path.join(JS_DIR, 'core', 'safe_html.js'),
            os.path.join(JS_DIR, 'settings', 'save_export.js'),
        ],
        min_pass=14,
        label='search-settings-tab',
    )
