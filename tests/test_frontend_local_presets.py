#!/usr/bin/env python3
"""Frontend tests for the local-deployment rework (2026-07-25).

Drives the REAL shipped static/js/settings/local_endpoints.js (+ branding.js
+ provider_render.js) under jsdom and asserts the behaviour the owner
ratified:

  * 本地部署模型 button opens a preset chooser — vLLM / SGLang / Ollama /
    Custom, Custom LAST; picking an engine creates ONE card for that engine
    (re-picking focuses instead of duplicating).
  * Endpoint row blur AUTO-probes just that row (no manual 探测全部 click),
    writes provider.endpoint_models[url] with the served ROOT ids, merges the
    model entries, and renders inline model chips. A bare-origin URL that the
    backend rescues via /v1 gets its stored endpoint rewritten.
  * Binding lifecycle: editing a URL drops the old key, deleting a row drops
    its key, 清空 wipes the whole map, bulk 探测全部 rebuilds it (prune +
    overlay).
  * The jarring prose hint block is GONE — the facts moved into an ⓘ
    tooltip on the section label.
  * Model cards show a "via <endpoint>" placement chip when binding exists.

NEUTER (proven by construction): every DOM/model assertion reads state the
new code writes — _autoProbeEndpoint, the binding writes/prunes, the ⓘ
swap. Removing any of them flips the corresponding check red.
"""

import os
import json
import re

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit

_BODY = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="stgProviderList"></div></body>',
  targets: [process.argv[2]],
  globals: {
    t: function (k, vars) {
      if (k === 'settings.localEngineProviderName') return (vars && vars.name) + ' 本地部署';
      if (k === 'settings.localPresetCustomName') return '自定义';
      return k;
    },
    escapeHtml: function (s) {
      return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    },
    Icon: function () { return ''; },
    _renderPresetsTab: function () {},
    _renderProviderKeyStats: function () {},
    _findMatchingTemplate: function () { return null; },
    _coldSortModels: function () {},
    showAlert: function () {},
    showConfirm: async function () { return true; },
    Api: {
      providers: {
        probe: async function (url) { return window.__probeStub(url); },
        probeBulk: async function (urls) { return window.__bulkStub(urls); },
      },
      dispatch: { endpointMetrics: async function () { return { endpoints: {} }; } },
    },
  },
});

window.HTMLElement.prototype.scrollIntoView = function () {};

// local_endpoints.js declares `let _stgProviders` — trapped in the target
// file's eval scope (V8 does not leak `let` out of ANY eval). The python
// side concatenates the three sources + a same-scope accessor into ONE
// target file, so window.__providers() closes over the live array.
var _stgProviders = window.__providers();

const $ = (id) => document.getElementById(id);
// setTimeout is NEUTERED by the shared harness (fn never runs) — flush the
// microtask queue instead so the async probe stubs can settle.
const flush = async () => { for (let i = 0; i < 10; i++) await Promise.resolve(); };
const tileNames = () =>
  Array.from(document.querySelectorAll('#stgLocalPresetModal .stg-preset-tile .stg-preset-name'))
    .map((el) => el.textContent);

(async () => {
  try {
    // ── 1. Preset chooser: 4 tiles, engine order, custom LAST ──
    addLocalProvider();
    check('preset_modal_opens', !!$('stgLocalPresetModal'));
    const names = tileNames();
    check('preset_four_tiles', names.length === 4);
    check('preset_order', names.join('|') === 'vLLM|SGLang|Ollama|自定义');
    check('preset_custom_last',
      _LOCAL_ENGINE_PRESETS[_LOCAL_ENGINE_PRESETS.length - 1].custom === true);
    check('preset_ollama_placeholder',
      _LOCAL_ENGINE_PRESETS[2].placeholder === 'http://localhost:11434/v1');

    // ── 2. Pick vLLM → one engine card, named, with empty binding map ──
    _pickLocalPreset(0);
    check('preset_modal_closed', !$('stgLocalPresetModal'));
    check('vllm_card_created', _stgProviders.length === 1);
    check('vllm_engine_recorded', _stgProviders[0].engine === 'vllm');
    check('vllm_card_named', _stgProviders[0].name.indexOf('vLLM') >= 0);
    check('vllm_binding_map_init',
      _stgProviders[0].endpoint_models
      && Object.keys(_stgProviders[0].endpoint_models).length === 0);
    check('vllm_engine_badge',
      document.querySelector('.stg-provider-badges').textContent.indexOf('vLLM') >= 0);

    // ── 3. Re-pick vLLM → focus, NOT a duplicate card ──
    _pickLocalPreset(0);
    check('repick_no_duplicate', _stgProviders.length === 1);

    // ── 4. Pick Ollama → second card (mixed engines coexist) ──
    _pickLocalPreset(2);
    check('ollama_second_card', _stgProviders.length === 2);
    check('ollama_engine_recorded', _stgProviders[0].engine === 'ollama');

    // ── 5. Blur auto-probe writes binding + merges model + chips ──
    window.__probeStub = async (url) => ({
      ok: true, base_url: url,
      models: [{ model_id: 'qwen3-32b', aliases: [], capabilities: ['text'], rpm: 30, cost: 0 }],
    });
    // vllm card is now index 1 (ollama was unshifted to 0)
    _onLocalEndpointEdit(1, 0, 'http://10.0.0.5:8000/v1');
    await flush();
    const vllmP = _stgProviders[1];
    check('autoprobe_binding_written',
      (vllmP.endpoint_models['http://10.0.0.5:8000/v1'] || []).join() === 'qwen3-32b');
    check('autoprobe_model_merged',
      vllmP.models.some((m) => m.model_id === 'qwen3-32b'));
    const rowHtml = _renderLocalEndpointsSection(1, vllmP.endpoints);
    check('autoprobe_chips_rendered', rowHtml.indexOf('stg-ep-model-chip') >= 0
      && rowHtml.indexOf('qwen3-32b') >= 0);

    // ── 6. Bare-origin URL → backend /v1 rescue rewrites stored endpoint ──
    window.__probeStub = async (url) => ({
      ok: true, base_url: url.replace(/\/$/, '') + '/v1',
      models: [{ model_id: 'llama3.1', aliases: [], capabilities: ['text'], rpm: 30, cost: 0 }],
    });
    _onLocalEndpointEdit(0, 0, 'http://10.0.0.9:11434');
    await flush();
    const ollamaP = _stgProviders[0];
    check('bare_origin_rewritten', ollamaP.endpoints[0] === 'http://10.0.0.9:11434/v1');
    check('bare_origin_binding_keyed_effective',
      (ollamaP.endpoint_models['http://10.0.0.9:11434/v1'] || []).join() === 'llama3.1');

    // ── 7. Binding lifecycle: edit → old key gone; delete → key gone ──
    window.__probeStub = async (url) => ({
      ok: true, base_url: url,
      models: [{ model_id: 'qwen3-32b', aliases: [], capabilities: ['text'], rpm: 30, cost: 0 }],
    });
    _onLocalEndpointEdit(1, 0, 'http://10.0.0.5:8001/v1');
    await flush();
    check('edit_prunes_old_binding',
      !('http://10.0.0.5:8000/v1' in _stgProviders[1].endpoint_models));
    _deleteLocalEndpoint(0, 0);
    check('delete_prunes_binding',
      Object.keys(_stgProviders[0].endpoint_models).length === 0);
    _clearLocalEndpoints(1);
    await flush();
    check('clear_wipes_binding',
      Object.keys(_stgProviders[1].endpoint_models).length === 0);

    // ── 8. Bulk 探测全部 rebuilds binding (prune + overlay) ──
    _stgProviders[1].endpoints = ['http://10.0.0.5:8000/v1', 'http://10.0.0.6:8000/v1'];
    _stgProviders[1].endpoint_models = { 'http://stale.example/v1': ['ghost'] };
    window.__bulkStub = async (urls) => ({
      ok: true,
      results: urls.map((u, i) => ({
        ok: true, base_url: u,
        models: [{ model_id: i === 0 ? 'qwen3-32b' : 'llama-70b',
                   aliases: [], capabilities: ['text'], rpm: 30, cost: 0 }],
      })),
    });
    _discoverLocalModels(1);
    await flush();
    const bp = _stgProviders[1];
    check('bulk_binding_per_endpoint',
      (bp.endpoint_models['http://10.0.0.5:8000/v1'] || []).join() === 'qwen3-32b'
      && (bp.endpoint_models['http://10.0.0.6:8000/v1'] || []).join() === 'llama-70b');
    check('bulk_binding_prunes_stale', !('http://stale.example/v1' in bp.endpoint_models));
    check('bulk_models_union',
      bp.models.some((m) => m.model_id === 'qwen3-32b')
      && bp.models.some((m) => m.model_id === 'llama-70b'));

    // ── 9. Prose hint GONE, facts in ⓘ tooltip ──
    const secHtml = _renderLocalEndpointsSection(1, bp.endpoints);
    check('prose_hint_removed', secHtml.indexOf('stg-hint') === -1);
    check('info_tooltip_present', secHtml.indexOf('stg-keys-info') >= 0);

    // ── 10. Model card via-chip reflects placement ──
    const cardHtml = _renderModelCard(1, 0, bp.models.find((m) => m.model_id === 'qwen3-32b'));
    check('via_chip_rendered', cardHtml.indexOf('via 10.0.0.5:8000') >= 0);
    const noBindHtml = _renderModelCard(1, 0, { model_id: 'unbound', capabilities: ['text'], aliases: [] });
    check('via_chip_absent_without_binding', noBindHtml.indexOf('stg-mcard-via') === -1);

    // ── 11. Engine placeholders per card ──
    check('vllm_placeholder', _localEndpointPlaceholder(1).indexOf(':8000') >= 0);
    check('ollama_placeholder', _localEndpointPlaceholder(0).indexOf('11434') >= 0);
  } catch (e) {
    check('harness_threw: ' + (e && e.message), false);
  } finally {
    report();
  }
})();
'''


def test_local_presets_frontend(tmp_path):
    # Concatenate the three real sources + a same-scope accessor into ONE
    # target: V8 keeps `let _stgProviders` inside the file's eval scope, so
    # the accessor must be declared in the SAME scope to close over it.
    combined = tmp_path / 'combined_local_settings.js'
    sources = [
        os.path.join(JS_DIR, 'settings', 'local_endpoints.js'),
        os.path.join(JS_DIR, 'settings', 'branding.js'),
        os.path.join(JS_DIR, 'settings', 'provider_render.js'),
    ]
    accessor = (
        '\n;window.__providers = function () { return _stgProviders; };'
        '\n;window.__prov = function (i) { return _stgProviders[i]; };\n'
    )
    with open(combined, 'w', encoding='utf-8') as out:
        for src in sources:
            with open(src, encoding='utf-8') as fh:
                out.write(fh.read())
            out.write('\n;\n')
        out.write(accessor)
    run_harness(
        target_js=str(combined),
        body_js=_BODY,
        min_pass=31,
        label='local-presets',
    )


# ══════════════════════════════════════════════════════
#  Static guards (no jsdom needed)
# ══════════════════════════════════════════════════════

def _read(rel):
    if rel.startswith('js/'):
        rel = rel[3:]
    with open(os.path.join(JS_DIR, rel), encoding='utf-8') as f:
        return f.read()


def test_static_preset_order_and_icons():
    le = _read('js/settings/local_endpoints.js')
    order = [m for m in re.finditer(r"engine: '(\w*)'", le)]
    engines = [m.group(1) for m in order][:4]
    assert engines == ['vllm', 'sglang', 'ollama', ''], \
        'preset order must be vllm/sglang/ollama/custom-LAST, got %s' % engines

    branding = _read('js/settings/branding.js')
    for key in ('vllm', 'sglang', 'ollama'):
        assert re.search(r"^\s+%s: '<svg" % key, branding, re.M), \
            '%s official SVG missing from _BRAND_ICONS' % key
        assert re.search(r"%s: '#[0-9A-Fa-f]{6}'" % key, branding), \
            '%s brand color missing' % key


def test_static_prose_hint_removed_everywhere():
    le = _read('js/settings/local_endpoints.js')
    assert "stg-hint'>" not in le or 'localEndpointsHint' not in le.split('stg-hint')[0][-200:], \
        'the jarring prose block must not render localEndpointsHint as a bare hint span'
    # The i18n key now feeds the ⓘ tooltip instead.
    assert 'stg-keys-info' in le and 'localEndpointsHint' in le


def test_static_i18n_keys_bilingual():
    locale_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'frontend', 'src', 'i18n', 'locales')
    with open(os.path.join(locale_dir, 'zh.json'), encoding='utf-8') as f:
        zh = json.load(f)
    with open(os.path.join(locale_dir, 'en.json'), encoding='utf-8') as f:
        en = json.load(f)
    for key in ('settings.localPresetTitle', 'settings.localPresetDesc',
                'settings.localPresetVllmDesc', 'settings.localPresetSglangDesc',
                'settings.localPresetOllamaDesc', 'settings.localPresetCustomName',
                'settings.localPresetCustomDesc', 'settings.localEngineProviderName',
                'settings.epServedModelsTitle'):
        assert key in zh and key in en, 'i18n key %s missing or not bilingual' % key
    # The rewritten hint carries the binding semantics, not the old prose.
    assert '独立路由' in zh['settings.localEndpointsHint']


def main():
    raise SystemExit(pytest.main([__file__, '-v']))


if __name__ == '__main__':
    main()
