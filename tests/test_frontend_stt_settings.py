#!/usr/bin/env python3
"""jsdom test for static/js/settings/speech.js (Speech / STT settings tab).

Drives the REAL shipped speech.js under jsdom and asserts the WRITE PATH the
backend depends on:

  * enabled + a chosen card → _applySttToProviders() pushes ONE dedicated
    provider (id='stt') into _stgProviders whose model carries an EXPLICIT
    per-cell key_access[idx].capabilities override — the thing that defeats the
    DEFAULT_SLOT_CONFIGS trap (see settings/speech.js header + the Python
    integration test test_stt_settings_write_path.py).
  * the Omni card writes cap 'audio_chat'; endpoint cards write 'transcription'.
  * disabling the toggle removes the stt provider entirely (idempotent).
  * NEUTER: assert key_access is present AND non-empty on the written model —
    a shape without it is exactly the silently-broken pre-fix bug.

Run: make test-frontend  (skips cleanly when node/jsdom aren't installed)
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
MODULE_TS = ROOT / 'frontend/src/features/settings/speech.ts'
ESBUILD = ROOT / 'node_modules/.bin/esbuild'

_BODY = r'''
const { setup } = require(process.env.JSDOM_HARNESS);
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body>' +
    '<input type="checkbox" id="settingSttEnabled">' +
    '<div id="sttProviderFields"></div>' +
    '<select id="settingSttProvider">' +
      '<option value="openai">o</option><option value="groq">g</option>' +
      '<option value="omni">m</option><option value="doubao">d</option>' +
      '<option value="custom">c</option></select>' +
    '<div id="sttStatusBanner"></div><span id="sttStatusText"></span>' +
    // OpenAI card fields
    '<div id="sttCardOpenai"></div>' +
    '<select id="settingSttModelOpenai"><option value="gpt-4o-transcribe">x</option></select>' +
    '<input id="settingSttBaseOpenai"><input id="settingSttKeyOpenai">' +
    // Groq
    '<div id="sttCardGroq"></div>' +
    '<select id="settingSttModelGroq"><option value="whisper-large-v3-turbo">x</option></select>' +
    '<input id="settingSttBaseGroq"><input id="settingSttKeyGroq">' +
    // Omni
    '<div id="sttCardOmni"></div>' +
    '<input id="settingSttModelOmni"><input id="settingSttBaseOmni"><input id="settingSttKeyOmni">' +
    // Custom
    '<div id="sttCardCustom"></div>' +
    '<input id="settingSttModelCustom"><input id="settingSttBaseCustom"><input id="settingSttKeyCustom">' +
    '</body>',
  targets: [process.argv[2]],
  globals: {
    _stgProviders: [],
    // Minimal _setVal (real one lives in core_panel.js, not eval'd here).
    _setVal: function (id, value, prop) {
      var el = document.getElementById(id);
      if (!el) return;
      if (prop === 'checked') el.checked = !!value; else el.value = value;
    },
    Api: { audio: { capabilities: async () => ({ available: false, models: [] }) } },
  },
});

for (const name of ['HTMLElement', 'HTMLInputElement', 'HTMLSelectElement',
                    'AbortController', 'AbortSignal']) {
  global[name] = window[name];
}
// Classic scripts declare globals directly; the native module deliberately
// exports only its compatibility surface on window.
for (const name of ['_applySttToProviders', '_switchSttProvider']) {
  if (typeof window[name] === 'function') global[name] = window[name];
}

const $ = (id) => document.getElementById(id);
const sttProv = () => _stgProviders.filter((p) => p.id === 'stt')[0] || null;

(async () => {
  try {
    // ── 1. disabled → collect returns null, apply writes nothing ──
    $('settingSttEnabled').checked = false;
    _applySttToProviders();
    check('disabled_no_provider', sttProv() === null);

    // ── 2. enabled + OpenAI card → one stt provider with key_access override ──
    $('settingSttEnabled').checked = true;
    $('settingSttProvider').value = 'openai';
    $('settingSttModelOpenai').value = 'gpt-4o-transcribe';
    $('settingSttBaseOpenai').value = 'https://api.openai.com/v1';
    $('settingSttKeyOpenai').value = 'sk-abc';
    _applySttToProviders();
    let p = sttProv();
    check('openai_provider_written', !!p);
    check('openai_single_stt', _stgProviders.filter((x) => x.id === 'stt').length === 1);
    const m = p && p.models && p.models[0];
    check('openai_model', m && m.model_id === 'gpt-4o-transcribe');
    // THE load-bearing assertion: explicit per-cell key_access capability override.
    check('openai_key_access_present', !!(m && m.key_access && m.key_access['0']));
    check('openai_key_access_cap',
      m && m.key_access['0'].capabilities && m.key_access['0'].capabilities[0] === 'transcription');
    check('openai_key_index_matches_keys',
      p && Object.keys(m.key_access).length === (p.api_keys.length || 1));

    // ── 3. re-apply is idempotent (still exactly one stt provider) ──
    _applySttToProviders();
    check('reapply_idempotent', _stgProviders.filter((x) => x.id === 'stt').length === 1);

    // ── 4. Omni card writes audio_chat (NOT transcription) ──
    $('settingSttProvider').value = 'omni';
    $('settingSttModelOmni').value = 'gemini-3-flash-preview';
    $('settingSttBaseOmni').value = 'https://aigc.example/v1';
    $('settingSttKeyOmni').value = 'omni-key';
    _applySttToProviders();
    p = sttProv();
    check('omni_cap_audio_chat',
      p && p.models[0].key_access['0'].capabilities[0] === 'audio_chat');
    check('omni_model_cap_matches',
      p && p.models[0].capabilities[0] === 'audio_chat');
    // keyed omni → brand '' (normal cloud provider).
    check('omni_keyed_brand_blank', p && p.brand === '');

    // ── 4b. THE default-gateway path: blank-key Omni → brand:'local' ──
    // (else _build_slots_from_providers skips it for having no keys → dead).
    $('settingSttKeyOmni').value = '';
    _applySttToProviders();
    p = sttProv();
    check('omni_blank_key_written', !!p);
    check('omni_blank_key_no_api_keys', p && p.api_keys.length === 0);
    check('omni_blank_key_brand_local', p && p.brand === 'local');
    check('omni_blank_key_still_key_access',
      p && p.models[0].key_access && p.models[0].key_access['0'].capabilities[0] === 'audio_chat');

    // ── 4c. needsKey contract: blank-key OpenAI card → null (unconfigured) ──
    $('settingSttProvider').value = 'openai';
    $('settingSttBaseOpenai').value = 'https://api.openai.com/v1';
    $('settingSttKeyOpenai').value = '';   // public cloud endpoint needs a key
    _applySttToProviders();
    check('openai_blank_key_unconfigured', sttProv() === null);

    // ── 5. incomplete (no base URL) → treated as unconfigured (null) ──
    $('settingSttProvider').value = 'custom';
    $('settingSttModelCustom').value = 'whisper-1';
    $('settingSttBaseCustom').value = '';   // required, empty
    _applySttToProviders();
    // custom has a defaultBase of '' → base stays empty → null → removed.
    check('incomplete_custom_removed', sttProv() === null);

    // ── 6. disabling after a write REMOVES the provider ──
    $('settingSttProvider').value = 'openai';
    $('settingSttKeyOpenai').value = 'sk-abc';   // restore (cleared in 4c)
    $('settingSttEnabled').checked = true;
    _applySttToProviders();
    check('re_enabled_written', sttProv() !== null);
    $('settingSttEnabled').checked = false;
    _applySttToProviders();
    check('disable_removes', sttProv() === null);

    // ── 7. _switchSttProvider shows only the selected card ──
    _switchSttProvider('groq');
    check('switch_shows_groq', $('sttCardGroq').style.display === '');
    check('switch_hides_openai', $('sttCardOpenai').style.display === 'none');
  } catch (e) {
    check('harness_threw: ' + (e && e.message), false);
  } finally {
    report();
  }
})();
'''


def test_stt_settings_frontend():
    run_harness(
        target_js=os.path.join(JS_DIR, 'settings', 'speech.js'),
        body_js=_BODY,
        min_pass=21,
        label='stt-settings',
    )


@pytest.mark.skipif(not ESBUILD.is_file(), reason='esbuild not installed')
def test_vite_stt_settings_matches_classic_write_contract(tmp_path):
    built = tmp_path / 'speech.js'
    compiled = subprocess.run(
        [str(ESBUILD), str(MODULE_TS), '--bundle', '--format=iife',
         '--platform=browser', f'--outfile={built}'],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert compiled.returncode == 0, compiled.stderr
    run_harness(
        target_js=str(built),
        body_js=_BODY,
        min_pass=21,
        label='vite stt-settings',
    )


@pytest.mark.skipif(not ESBUILD.is_file(), reason='esbuild not installed')
def test_vite_stt_status_generation_and_panel_lifecycle(tmp_path):
    built = tmp_path / 'speech-lifecycle.js'
    compiled = subprocess.run(
        [str(ESBUILD), str(MODULE_TS), '--bundle', '--format=iife',
         '--platform=browser', f'--outfile={built}'],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert compiled.returncode == 0, compiled.stderr
    script = r'''
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!doctype html><body>' +
  '<input type="checkbox" id="settingSttEnabled">' +
  '<div id="sttProviderFields"></div>' +
  '<select id="settingSttProvider"><option value="openai">o</option>' +
    '<option value="custom">c</option></select>' +
  '<div id="sttCardOpenai"></div><div id="sttCardGroq"></div>' +
  '<div id="sttCardOmni"></div><div id="sttCardCustom"></div>' +
  '<div id="sttStatusBanner"></div><span id="sttStatusText"></span>' +
  '</body>');
global.window = dom.window;
global.document = dom.window.document;
for (const name of ['HTMLElement', 'HTMLInputElement', 'HTMLSelectElement',
                    'AbortController', 'AbortSignal']) global[name] = dom.window[name];
global._stgProviders = [];
window.t = (key) => key;
const resolvers = [];
window.Api = { audio: { capabilities: () => new Promise(
  (resolve) => resolvers.push(resolve)) } };
require(BUILT_PATH);
const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

(async () => {
  window._populateSpeechTab();       // probe generation 1
  const select = document.getElementById('settingSttProvider');
  select.value = 'custom';
  select.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
  const switchOwned = document.getElementById('sttCardCustom').style.display === ''
    && document.getElementById('sttCardOpenai').style.display === 'none';

  window._refreshSttStatus();        // probe generation 2
  resolvers[1]({ available: false, models: [] });
  await tick();
  resolvers[0]({ available: true, models: [{ model: 'STALE' }] });
  await tick();
  const staleWon = document.getElementById('sttStatusText').textContent.includes('STALE');

  const fields = document.getElementById('sttProviderFields');
  window._destroySpeechTab();
  document.getElementById('settingSttEnabled').checked = true;
  document.getElementById('settingSttEnabled').dispatchEvent(
    new dom.window.Event('change', { bubbles: true }));
  console.log(JSON.stringify({
    switchOwned,
    staleWon,
    listenerSurvivedDestroy: fields.style.display !== 'none',
  }));
})().catch((error) => { console.error(error); process.exitCode = 1; });
'''.replace('BUILT_PATH', json.dumps(str(built)))
    run = subprocess.run(
        ['node', '-e', script], cwd=ROOT,
        capture_output=True, text=True,
    )
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout.strip().splitlines()[-1])
    assert result == {
        'switchOwned': True,
        'staleWon': False,
        'listenerSurvivedDestroy': False,
    }
