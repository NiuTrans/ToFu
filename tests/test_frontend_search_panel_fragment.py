#!/usr/bin/env python3
"""jsdom + fragment ratchet for the search settings panel on its REAL markup.

The sibling suite (test_frontend_search_settings_tab.py) pins behaviour on a
hand-built mini DOM — it never loads the shipped fragment, so a markup edit
that moves an input out of the override-gated group (exactly the
settingMaxCharsSearch hole fixed in a5117b0e: editable while the save path
silently dropped its value) would pass green there. This suite closes that
gap by feeding static/settings_panels/search.html itself to jsdom.

Contract under test — the profile select is the SINGLE source of truth
(快速/平衡/深入/自定义); the separate 自定义覆盖 toggle is gone:

  * populate with no saved overrides → select shows the preset and the four
    档位参数 knobs (.search-override-control) render disabled; the four
    profile-independent limit knobs stay live;
  * choosing 自定义 re-enables the four knobs WITHOUT rewriting their values;
    choosing a preset folds the preset values in and re-disables them;
  * populate WITH saved overrides → select shows 自定义 and knobs are live;
  * save: 自定义 sends the last real preset as profile plus the four concrete
    overrides (profile='custom' never hits the wire); a preset sends empty
    overrides and omits the preset-owned keys (legacy no-select surfaces keep
    the old concrete wire shape);
  * (pure-Python ratchet) every element ID the populate/save JS reads exists
    EXACTLY ONCE in the fragment, and the gated/free split is structural —
    a future fragment edit that orphans a wire ID fails here, not as a
    mysteriously dead knob in the browser.

Run: make test-frontend  (skips cleanly when node/jsdom aren't installed)
"""

import json
import os
import re

import pytest

from tests._jsdom import JS_DIR, run_harness
from tests._runtime_sections import native_module_path, runtime_section

pytestmark = pytest.mark.unit

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
FRAGMENT = os.path.join(ROOT, 'static', 'settings_panels', 'search.html')
HTML_SAFETY = native_module_path(
    '.native/search-panel-html-safety.js',
    os.path.join(ROOT, 'frontend', 'src', 'html-safety.ts'),
)

# The four preset-owned knobs the 自定义 option must gate as a SET.
_GATED_IDS = (
    'settingFetchTopN', 'settingMaxCharsSearch',
    'settingLlmContentFilter', 'settingSearchDeepen',
)
# Profile-independent caps that must never be disabled by the gate.
_FREE_IDS = (
    'settingFetchTimeout', 'settingMaxBytesMB',
    'settingMaxCharsDirect', 'settingMaxCharsPdf',
)


def _read_fragment():
    with open(FRAGMENT, encoding='utf-8') as fh:
        return fh.read()


def _extract_wire_ids():
    """IDs the populate/save JS actually reads, extracted from the migrated
    runtime sections — so a NEW getElementById in the search save path is
    policed automatically instead of relying on this file's list."""
    save = runtime_section('settings/save_export.js', scope_prelude=False)
    seg = save[save.find('// Search tab'):save.find('// Network')]
    ids = set(re.findall(r"getElementById\('([A-Za-z0-9_]+)'\)", seg))

    pop = runtime_section('settings/other_tabs.js', scope_prelude=False)
    a = pop.find('function _populateSearchTab')
    b = pop.find('\nfunction ', a + 10)
    seg2 = pop[a:b]
    ids |= set(re.findall(r"_setVal\('([A-Za-z0-9_]+)'", seg2))
    ids |= set(re.findall(r"getElementById\('([A-Za-z0-9_]+)'\)", seg2))
    ids |= set(re.findall(r"ChipInput\.init\('([A-Za-z0-9_]+)'", seg2))
    return ids


# ── Pure-Python structural ratchets (no jsdom needed) ──────────────────────

def test_every_search_wire_id_exists_exactly_once_in_fragment():
    frag = _read_fragment()
    ids = _extract_wire_ids()
    assert len(ids) >= 10, (
        'wire-ID extraction came up short — the // Search tab / populate '
        'slice markers in the runtime sections drifted?')
    problems = [f'{i}: x{frag.count("id=" + chr(34) + i + chr(34))}'
                for i in sorted(ids)
                if frag.count(f'id="{i}"') != 1]
    assert not problems, (
        'Search wire IDs not present EXACTLY once in the real fragment '
        f'(missing → dead knob in the browser; duplicated → ambiguous '
        f'getElementById): {problems}')


def test_override_gate_is_structural_in_fragment():
    """The four gated IDs must sit inside the override-gated group div, and
    the free IDs must sit outside it — the a5117b0e fix's markup contract."""
    frag = _read_fragment()
    start = frag.index('class="search-knob-group search-override-control"')
    end = frag.index('class="search-knob-group"', start + 10)
    span = frag[start:end]
    misplaced = [i for i in _GATED_IDS if f'id="{i}"' not in span]
    assert not misplaced, (
        f'Override-gated inputs moved OUTSIDE .search-override-control in '
        f'the fragment: {misplaced} — they would stay editable while the '
        'save path silently drops their values (regression of a5117b0e).')
    leaked = [i for i in _FREE_IDS if f'id="{i}"' in span]
    assert not leaked, (
        f'Profile-independent inputs ended up INSIDE the override-gated '
        f'group: {leaked} — they would be wrongly disabled off-Custom.')


# ── jsdom: real fragment + real section JS ─────────────────────────────────

_BODY = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>' + FRAGMENT + '</body>',
  // Order: safe_html defines safeHtml/raw used by other_tabs.js; save_export.js
  // carries _saveServerConfig's search payload. escapeHtml/t come from the
  // shared harness stubs (this t() override applies after them).
  targets: [process.argv[4], process.argv[2], process.argv[5]],
  globals: {
    _setVal: function (id, value, prop) {
      var el = document.getElementById(id);
      if (!el) return;
      if (prop === 'checked') el.checked = !!value; else el.value = value;
    },
    t: function (key) {
      var dict = {
        'settings.searchPipelineTpl': '搜索引擎返回结果 → 抓取前 {n} 个网页（每页 ≤{chars} 字符 · 超时 {timeout}s）→ {filter} → 注入对话',
        'settings.searchFilterOnTpl': 'LLM 过滤杂质',
        'settings.searchFilterOffTpl': '跳过过滤（原文直送）',
      };
      return dict[key] || '';
    },
    ChipInput: {
      init: function () {},
      getValues: function () { return []; },
    },
    _stgPresets: {},
    _stgProviders: [],
    _serverConfig: { hidden_models: [], hidden_ig_models: [] },
    _collectModelDefaults: function () { return {}; },
    _loadServerConfigAndPopulate: function () {},
    debugLog: function () {},
    Api: {
      serverConfig: { update: async function (payload) {
        window.__savedPayload = payload;
        return { json: async () => ({ ok: true }) };
      } },
      browser: {
        status: async function () { return { connected: false }; },
        adapters: async function () { return { adapters: [] }; },
        access: async function () { return {}; },
      },
    },
  },
});

const $ = (id) => document.getElementById(id);
const GATED = ['settingFetchTopN', 'settingMaxCharsSearch',
               'settingLlmContentFilter', 'settingSearchDeepen'];
const FREE = ['settingFetchTimeout', 'settingMaxBytesMB',
              'settingMaxCharsDirect', 'settingMaxCharsPdf'];
const setProfile = (v) => { $('settingSearchProfile').value = v; _searchProfileChanged(); };

(async () => {
  try {
    // ── 1. populate with NO saved overrides → preset owns the gated four ──
    _populateSearchTab({ search: {
      profile: 'balanced', fetch_top_n: 6, fetch_timeout: 15,
      max_chars_search: 60000, max_chars_direct: 200000, max_chars_pdf: 0,
      max_bytes: 20971520, llm_content_filter: true, deepen_enabled: false,
      skip_domains: [],
    } });
    await Promise.resolve();  // let _renderSearchBrowserAccess microtasks settle

    check('profile_select_balanced', $('settingSearchProfile').value === 'balanced');
    GATED.forEach((id) => check('gated_disabled_' + id, $(id).disabled === true));
    FREE.forEach((id) => check('limits_live_' + id, $(id).disabled === false));

    // ── 2. 自定义 unlocks the gated four without rewriting their values ──
    setProfile('custom');
    GATED.forEach((id) => check('custom_enabled_' + id, $(id).disabled === false));
    check('custom_keeps_topn', $('settingFetchTopN').value === '6');

    // ── 3. back to a preset → values fold + gate re-engages ──
    setProfile('fast');
    check('fast_sets_topn_3', $('settingFetchTopN').value === '3');
    check('fast_sets_chars_30000', $('settingMaxCharsSearch').value === '30000');
    check('fast_sets_filter_off', $('settingLlmContentFilter').checked === false);
    GATED.forEach((id) => check('fast_redisables_' + id, $(id).disabled === true));
    check('preview_tracks_fast',
      $('searchPipelinePreview').textContent.indexOf('抓取前 3 个网页') !== -1);

    // ── 4. populate WITH saved overrides → select shows 自定义, knobs live ──
    _populateSearchTab({ search: {
      profile: 'deep', fetch_top_n: 11, fetch_timeout: 25,
      max_chars_search: 88000, max_chars_direct: 200000, max_chars_pdf: 0,
      max_bytes: 20971520, llm_content_filter: false, deepen_enabled: true,
      skip_domains: [],
      overrides: { fetch_top_n: 11, max_chars_search: 88000,
                   llm_content_filter: false, deepen_enabled: true },
    } });
    check('profile_select_custom', $('settingSearchProfile').value === 'custom');
    GATED.forEach((id) => check('override_enabled_' + id, $(id).disabled === false));
    check('override_topn_11', $('settingFetchTopN').value === '11');

    // ── 5. save on 自定义: wire carries last preset + concrete overrides ──
    await _saveServerConfig();
    check('save_custom_overrides_topn',
      window.__savedPayload.search.overrides.fetch_top_n === 11);
    check('save_custom_profile_is_preset',
      window.__savedPayload.search.profile === 'deep');
    check('save_custom_keeps_topn_key',
      window.__savedPayload.search.fetch_top_n === 11);

    // ── 6. save on a preset: empty overrides + preset keys omitted ──
    setProfile('deep');
    await _saveServerConfig();
    check('save_preset_overrides_empty',
      Object.keys(window.__savedPayload.search.overrides).length === 0);
    check('save_preset_omits_topn',
      !('fetch_top_n' in window.__savedPayload.search));
    check('save_preset_profile_deep',
      window.__savedPayload.search.profile === 'deep');
  } catch (e) {
    check('harness_threw: ' + (e && e.message), false);
  } finally {
    report();
  }
})();
'''


def test_search_panel_fragment_dom():
    frag = _read_fragment()
    body_js = 'const FRAGMENT = ' + json.dumps(frag, ensure_ascii=False) + ';\n' + _BODY
    run_harness(
        target_js=os.path.join(JS_DIR, 'settings', 'other_tabs.js'),
        body_js=body_js,
        extra_targets=[
            HTML_SAFETY,
            os.path.join(JS_DIR, 'settings', 'save_export.js'),
        ],
        expect_pass=34,
        label='search-panel-fragment',
    )
