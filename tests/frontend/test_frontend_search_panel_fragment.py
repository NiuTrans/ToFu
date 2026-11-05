#!/usr/bin/env python3
"""Search settings populate/save behavior on the actual served markup.

Search limits (results per search, page sizes, timeouts, download size) are
fixed backend defaults, so the Search tab edits only access boundaries and the
optional search gateway. This suite feeds static/settings_panels/search.html
itself to jsdom, so a markup edit that orphans a control the JS reads fails
here instead of as a dead control in the browser.

  * controls passed to the real populate/save paths exist exactly once;
    missing and duplicate blocklist fixtures verify that this check works;
  * populate seeds the blocked-domain chips, including from a saved section
    written by an older client that still carries retired tuning keys;
  * save sends the blocked domains and no tuning key; the gateway is omitted
    while its card is not in use, so the server keeps the saved gateway.

Run: make test-frontend  (skips cleanly when node/jsdom aren't installed)
"""

import json
import os

import pytest

from tests._jsdom import JS_DIR, run_harness
from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
FRAGMENT = os.path.join(ROOT, 'static', 'settings_panels', 'search.html')
HTML_SAFETY = native_module_path(
    '.native/search-panel-html-safety.js',
    os.path.join(ROOT, 'frontend', 'src', 'html-safety.ts'),
)
SAVE_EXPORT = native_module_path(
    '.native/settings-save-export.js',
    os.path.join(ROOT, 'frontend', 'src', 'features', 'settings',
                 'save-export.ts'),
)


def _read_fragment():
    with open(FRAGMENT, encoding='utf-8') as fh:
        return fh.read()


_BODY = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const populatedControls = new Set(), readControls = new Set();
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>' + FRAGMENT + '</body>',
  // Order: safe_html defines safeHtml/raw used by other_tabs.js; the typed
  // save-export owner carries saveSettings' search payload.
  targets: [process.argv[4], process.argv[2], process.argv[5]],
  globals: {
    _setVal: function (id, value, prop) {
      populatedControls.add(id);
      var el = document.getElementById(id);
      if (!el) return;
      if (prop === 'checked') el.checked = !!value; else el.value = value;
    },
    ChipInput: {
      _vals: {},
      init: function (id, vals) {
        populatedControls.add(id);
        this._vals[id] = (vals || []).slice();
      },
      getValues: function (id) {
        readControls.add(id);
        return (this._vals[id] || []).slice();
      },
    },
    config: {},
    _stgPresets: {},
    _stgModelRouting: { contract_version: 'tofu.model-routing/v2' },
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

// The typed save owner type-guards elements with instanceof; the shared
// harness copies HTMLInputElement/HTMLTextAreaElement but not this one.
global.HTMLSelectElement = window.HTMLSelectElement;
// Match the production typed coordinator's ports; editor behavior remains real.
Object.assign(featureRegistry, {
  config: {}, _featureFlags: {}, _registeredModels: [],
  _stgModelRouting: {contract_version: 'tofu.model-routing/v2'},
  _stgModelRoutingSavedJson: JSON.stringify({contract_version: 'tofu.model-routing/v2'}),
  _collectProxyPool, _collectProxyBypassDomains,
});

const chips = () => JSON.stringify(ChipInput._vals.settingSkipDomains);

(async () => {
  try {
    // ── 1. a section saved by an older client still populates cleanly ──
    _populateSearchTab({ search: {
      profile: 'deep', fetch_top_n: 11, fetch_timeout: 25,
      max_chars_search: 88000, max_bytes: 20971520,
      llm_content_filter: true, deepen_enabled: true,
      overrides: { fetch_top_n: 11 },
      skip_domains: ['spam.example', 'youtube.com'],
    } });
    await Promise.resolve();  // let _renderSearchBrowserAccess microtasks settle
    check('populate_seeds_blocklist', chips() === '["spam.example","youtube.com"]');

    // ── 2. save carries the blocklist and no tuning key ──
    await saveSettings();
    const search = window.__savedPayload.search;
    check('save_sends_only_blocklist',
      JSON.stringify(Object.keys(search).sort()) === '["skip_domains"]');
    check('save_blocklist_values',
      JSON.stringify(search.skip_domains) === '["spam.example","youtube.com"]');

    // ── 3. no saved search section → empty blocklist, still no tuning key ──
    _populateSearchTab({});
    check('populate_without_section', chips() === '[]');
    await saveSettings();
    check('save_empty_blocklist',
      JSON.stringify(window.__savedPayload.search) === '{"skip_domains":[]}');

    // Observe real calls through the editor ports. The harness's own reads do
    // not count, and literal DOM identities catch both missing and duplicate IDs.
    const controls = new Set([...populatedControls, ...readControls]);
    const elements = Array.from(document.querySelectorAll('[id]'));
    check('search_control_bindings',
      populatedControls.has('settingSkipDomains') && readControls.has('settingSkipDomains') &&
      [...controls].every(id => elements.filter(element => element.id === id).length === 1));
  } catch (e) {
    check('harness_threw: ' + (e && e.message), false);
  } finally {
    report();
  }
})();
'''


def _run_fragment(frag):
    body_js = 'const FRAGMENT = ' + json.dumps(frag, ensure_ascii=False) + ';\n' + _BODY
    run_harness(
        target_js=os.path.join(JS_DIR, 'settings', 'other_tabs.js'),
        body_js=body_js,
        extra_targets=[
            HTML_SAFETY,
            SAVE_EXPORT,
        ],
        expect_pass=6,
        label='search-panel-fragment',
    )


def test_search_panel_fragment_dom():
    _run_fragment(_read_fragment())


@pytest.mark.parametrize('fault', ['missing', 'duplicate'])
def test_search_panel_harness_rejects_broken_control_binding(fault):
    fragment = _read_fragment()
    if fault == 'missing':
        fragment = fragment.replace('id="settingSkipDomains"', 'id="absentBlocklist"')
    else:
        fragment += '<div id="settingSkipDomains"></div>'
    with pytest.raises(AssertionError, match='FAIL search_control_bindings'):
        _run_fragment(fragment)
