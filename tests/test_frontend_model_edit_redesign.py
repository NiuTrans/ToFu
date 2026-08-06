#!/usr/bin/env python3
"""Frontend test — the model edit form redesign (2026-08-01).

WHY
---
The owner flagged four defects on the per-model edit form:
  1. The Model ID sat in a `1fr auto auto` grid cell and truncated long ids
     ("claude-opus…") — it is the identity of the whole form and was the
     LEAST readable thing on it.
  2. The price section (RPM / routing cost / input / output) inherited the
     same ragged grid, so every row was a different width.
  3. With the wire-face pin on 'automatic', the form never said WHICH face
     the family rule actually picked — the user had to save and read the
     card pill to learn the answer.
  4. The request-id pool was a single comma-separated text input: one
     missing comma silently MERGED two ids into an unroutable garbage name.

WHAT IS GUARDED (results, not implementation — charter 2026-07-27)
------------------------------------------------------------------
  * Model ID spans the full first row (stg-field-wide) in a uniform
    2-column grid; the form is stamped data-prov/data-model.
  * Tag editor: chips render from the pool, Enter/blur commits, pasted
    commas/semicolons/CJK commas SPLIT into chips, duplicates refused,
    ×/Backspace removes, save reads chips (never a comma string), a
    half-typed value is flushed on save, and the request_ids non-empty
    guard still fires. Legacy `aliases` entries may legitimately be empty.
  * Face auto-note: visible exactly when the pin is 'automatic'; renders
    the landed resolution (protocol + face + endpoint tooltip), shows a
    pending line on a cold cache, is patched in place when the resolution
    lands (the provider_faces.js hook), mirrors the pin warning on toggle,
    and surfaces the refusal with its reason.
  * Wire section (2026-08-06): ALWAYS rendered — a single-face provider
    gets an in-dialog provider-protocol select (openai/anthropic/responses,
    unknown stored values preserved, writes p.protocol + re-resolves);
    a multi-face provider gets the face pin whose options NAME the
    protocol ('face — protocol'); the form reads as labelled sections.
  * i18n: every new key exists in both languages.

NEUTERS (source-level, on mutated copies — shipped files untouched):
  * N1: drop the auto-note from the edit form   → no verdict line (red)
  * N2: make _poolTagValues return []           → save loses the pool (red)
  * N3: drop the provider_faces.js repaint hook → note stuck pending (red)
  * N5: drop the provider-protocol select        → single-face has no wire control (red)
"""

from __future__ import annotations

import json
import os
import re

import pytest

from tests._jsdom import JS_DIR, run_harness

pytestmark = pytest.mark.unit

MODEL_EDIT_JS = os.path.join(JS_DIR, 'settings', 'model_edit.js')
PROVIDER_FACES_JS = os.path.join(JS_DIR, 'settings', 'provider_faces.js')
ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
I18N_JS = os.path.join(ROOT, 'static', 'js', 'i18n.js')
STYLES_CSS = os.path.join(ROOT, 'static', 'styles.css')

_HTML = ('<!DOCTYPE html><body><div id="list">'
         '<div class="stg-mcard" data-prov="0" data-model="0"></div>'
         '<div class="stg-mcard" data-prov="0" data-model="1"></div>'
         '<div class="stg-mcard" data-prov="0" data-model="2"></div>'
         '</div></body>')

_BODY = r'''
const fs = require('fs');
const { setup } = require(process.env.JSDOM_HARNESS);
const { document, window, check, report } = setup({
  root: process.argv[3],
  html: HTML_PLACEHOLDER,
  targets: [process.argv[2], process.argv[4]],   // model_edit.js, provider_faces.js
  globals: {
    debugLog: () => {},
    // Interpolating echo so chips carry their params ({protocol=anthropic}).
    t: (k, o) => {
      let s = k;
      if (o) for (const q of Object.keys(o)) s += '{' + q + '=' + o[q] + '}';
      return s;
    },
    _stgPresets: {},
    _serverConfig: {},
    _renderProvidersTab: () => {},
    _renderPresetsTab: () => {},
    showAlert: (m) => { (window._alertCalls = window._alertCalls || []).push(m); },
    Api: { providers: { resolveFaces: (p) => {
      window._resolveCalls = (window._resolveCalls || 0) + 1;
      return Promise.resolve({
        ok: true,
        faces: ['default', 'anthropic'],
        dual_face_host: true,
        resolutions: [{
          model_id: 'claude-opus-4.6', ok: true, face: 'anthropic',
          protocol: 'anthropic', base_url: 'https://gw/v1/anthropic',
          forced: false, error: '',
        }],
      });
    } } },
  },
});

const indirectEval = eval;
const ME_SRC = fs.readFileSync(process.argv[2], 'utf8');
const PF_SRC = fs.readFileSync(process.argv[4], 'utf8');

function seedProviders() {
  global._stgProviders = window._stgProviders = [
    { id: 'provA', brand: 'claude', enabled: true, api_keys: ['k1'],
      base_url: 'https://gw/v1/openai/native',
      faces: { anthropic: { base_url: 'https://gw/v1/anthropic',
                            protocol: 'anthropic' } },
      models: [
        { model_id: 'claude-opus-4.6',
          request_ids: ['aws.claude-opus-4.6', 'vertex.claude-opus-4.6'],
          capabilities: ['text'], rpm: 30, cost: 0.045,
          pricing: { input: 5, output: 25 } },
        { model_id: 'kimi-k3', aliases: [], capabilities: ['text'],
          rpm: 30, cost: 0.002 },
      ]},
  ];
}

function seedResolutions() {
  global._stgFaceResolutions = window._stgFaceResolutions = {
    0: {
      byModel: {
        'claude-opus-4.6': { model_id: 'claude-opus-4.6', ok: true,
          face: 'anthropic', protocol: 'anthropic',
          base_url: 'https://gw/v1/anthropic', forced: false, error: '' },
      },
      faces: ['default', 'anthropic'], dualFaceHost: true,
    },
  };
}

function openForm(modelIdx) {
  _editModel(0, modelIdx);
  return document.querySelector('.stg-edit-form');
}

(async () => {
try {
  // ══ 1. Layout: Model ID owns the first row, grid is uniform 2-col ══
  seedProviders();
  seedResolutions();
  let form = openForm(0);
  check('edit_form_opened', form !== null);
  check('form_stamped_prov_model',
    form.getAttribute('data-prov') === '0' &&
    form.getAttribute('data-model') === '0');
  const wide = form.querySelector('.stg-edit-grid .stg-field-wide .stg-edit-mid');
  check('model_id_spans_full_row', wide !== null);
  check('model_id_value', wide && wide.value === 'claude-opus-4.6');
  const gridFields = form.querySelectorAll('.stg-edit-grid > .stg-field');
  check('grid_has_five_fields', gridFields.length === 5);
  check('only_first_field_wide',
    gridFields[0].classList.contains('stg-field-wide') &&
    !gridFields[1].classList.contains('stg-field-wide') &&
    !gridFields[4].classList.contains('stg-field-wide'));

  // ══ 2. Tag editor: chips render from the pool, no comma input ══
  const editor = form.querySelector('.stg-tag-editor');
  check('tag_editor_present', editor !== null);
  check('no_comma_input_left',
    form.querySelector('.stg-edit-aliases') === null);
  check('pool_field_is_request_ids',
    editor && editor.dataset.poolField === 'request_ids');
  let chips = form.querySelectorAll('.stg-tag-chip');
  check('pool_chips_rendered', chips.length === 2);
  check('chip_values',
    chips.length === 2 &&
    chips[0].getAttribute('data-value') === 'aws.claude-opus-4.6' &&
    chips[1].getAttribute('data-value') === 'vertex.claude-opus-4.6');

  // Enter commits
  const input = editor.querySelector('.stg-tag-input');
  input.value = 'new.gateway-id';
  let prevented = false;
  _poolTagKey(input, { key: 'Enter', preventDefault: () => { prevented = true; } });
  check('enter_commits_chip',
    editor.querySelectorAll('.stg-tag-chip').length === 3 && prevented);
  check('enter_clears_box', input.value === '');

  // Pasted delimiters split (comma, CJK comma, semicolons)
  input.value = 'a.example,b.example；c.example，d.example';
  _poolTagSplit(input);
  check('paste_splits_into_chips',
    _poolTagValues(form).join('|').indexOf('a.example|b.example|c.example|d.example') >= 0);

  // Duplicates refused
  const before = editor.querySelectorAll('.stg-tag-chip').length;
  _poolTagAdd(editor, 'a.example');
  check('duplicate_refused',
    editor.querySelectorAll('.stg-tag-chip').length === before);

  // × removes
  const victim = editor.querySelector('.stg-tag-chip .stg-tag-x');
  _poolTagRemove(victim);
  check('x_removes_chip',
    editor.querySelectorAll('.stg-tag-chip').length === before - 1);

  // Backspace on an empty box pops the last chip
  const beforeBs = editor.querySelectorAll('.stg-tag-chip').length;
  _poolTagKey(input, { key: 'Backspace', preventDefault: () => {} });
  check('backspace_pops_last_chip',
    editor.querySelectorAll('.stg-tag-chip').length === beforeBs - 1);

  // ══ 3. Save reads chips (and flushes a half-typed value) ══
  window._alertCalls = [];
  input.value = 'half-typed.example';
  _saveModelEdit(0, 0);
  const saved0 = _stgProviders[0].models[0];
  check('save_reads_chips',
    saved0.request_ids.indexOf('vertex.claude-opus-4.6') >= 0 &&
    saved0.request_ids.indexOf('a.example') >= 0 &&
    saved0.request_ids.indexOf('new.gateway-id') >= 0);
  check('save_flushes_half_typed',
    saved0.request_ids.indexOf('half-typed.example') >= 0);
  check('save_no_alert', window._alertCalls.length === 0);

  // Emptying a request_ids pool is REFUSED (the routing guard survives)
  form = openForm(0);
  form.querySelectorAll('.stg-tag-chip .stg-tag-x').forEach((x) => _poolTagRemove(x));
  window._alertCalls = [];
  _saveModelEdit(0, 0);
  check('empty_pool_alerts', window._alertCalls.length === 1);
  check('empty_pool_not_saved',
    _stgProviders[0].models[0].request_ids.length > 0);

  // Legacy aliases may legitimately be empty
  form = openForm(1);
  const ed1 = form.querySelector('.stg-tag-editor');
  check('aliases_field_for_legacy_entry',
    ed1 && ed1.dataset.poolField === 'aliases');
  window._alertCalls = [];
  _saveModelEdit(0, 1);
  check('empty_aliases_allowed',
    window._alertCalls.length === 0 &&
    Array.isArray(_stgProviders[0].models[1].aliases) &&
    _stgProviders[0].models[1].aliases.length === 0);

  // ══ 4. Face auto-note: the automatic verdict is VISIBLE ══
  seedProviders();
  seedResolutions();
  form = openForm(0);
  const note = form.querySelector('.stg-face-auto-note');
  check('auto_note_present', note !== null);
  check('auto_note_visible', note && note.style.display !== 'none');
  check('auto_note_shows_verdict',
    note && note.textContent.indexOf('settings.meFaceAutoResolved') >= 0 &&
    note.textContent.indexOf('protocol=anthropic') >= 0);
  const pick = form.querySelector('.stg-face-auto-pick');
  check('auto_note_carries_endpoint_tooltip',
    pick && pick.getAttribute('title') === 'https://gw/v1/anthropic');
  check('pin_warn_hidden_on_auto',
    form.querySelector('.stg-face-warn').style.display === 'none');

  // Pinning flips note ↔ warning
  const sel = form.querySelector('.stg-edit-face');
  sel.value = 'anthropic';
  _onFacePinChange(sel);
  check('pin_hides_note', note.style.display === 'none');
  check('pin_shows_warn',
    form.querySelector('.stg-face-warn').style.display !== 'none');
  sel.value = '';
  _onFacePinChange(sel);
  check('unpin_restores_note', note.style.display !== 'none');
  check('unpin_hides_warn',
    form.querySelector('.stg-face-warn').style.display === 'none');

  // Cold cache: pending line + a resolution round-trip is kicked off…
  seedProviders();
  global._stgFaceResolutions = window._stgFaceResolutions = {};
  window._resolveCalls = 0;
  form = openForm(0);
  const coldNote = form.querySelector('.stg-face-auto-note');
  check('cold_cache_shows_pending',
    coldNote.textContent.indexOf('settings.meFaceAutoPending') >= 0);
  check('cold_cache_kicks_resolution', window._resolveCalls >= 1);

  // …and the note is patched IN PLACE when the resolution lands
  seedResolutions();
  _repaintFaceAutoNote(0);
  check('note_patched_on_landing',
    coldNote.textContent.indexOf('settings.meFaceAutoResolved') >= 0);

  // Refusal surfaces its reason
  global._stgFaceResolutions = window._stgFaceResolutions = { 0: {
    byModel: { 'claude-opus-4.6': { model_id: 'claude-opus-4.6', ok: false,
      face: '', protocol: '', base_url: '', forced: false,
      error: 'no anthropic face' } },
    faces: ['default', 'anthropic'], dualFaceHost: true } };
  _repaintFaceAutoNote(0);
  check('refusal_shown_with_reason',
    coldNote.textContent.indexOf('settings.meFaceAutoRefused') >= 0 &&
    coldNote.textContent.indexOf('no anthropic face') >= 0);

  // ══ 5. The provider_faces hook patches an open form after refresh ══
  seedProviders();
  global._stgFaceResolutions = window._stgFaceResolutions = {};
  form = openForm(0);   // cold → triggers _refreshFaceResolutions itself
  const hookNote = form.querySelector('.stg-face-auto-note');
  await _refreshFaceResolutions(0);
  check('refresh_patches_open_form',
    hookNote.textContent.indexOf('settings.meFaceAutoResolved') >= 0);

  // ══ 6. Draft-aware note: a renamed id must NEVER show the old verdict ══
  // (The cached resolution answers for the SAVED id. Showing it for a
  // draft that says something else is a wrong claim — worse than none.)
  seedProviders();
  seedResolutions();
  form = openForm(0);
  const midEl = form.querySelector('.stg-edit-mid');
  const draftNote = form.querySelector('.stg-face-auto-note');
  check('warm_verdict_before_rename',
    draftNote.textContent.indexOf('settings.meFaceAutoResolved') >= 0);
  window._resolveCalls = 0;
  midEl.value = 'claude-opus-5';
  _onModelIdDraftInput(midEl);
  check('rename_hides_stale_verdict',
    draftNote.textContent.indexOf('settings.meFaceAutoResolved') < 0);
  check('rename_shows_draft_pending',
    draftNote.textContent.indexOf('settings.meFaceAutoDraft') >= 0);
  check('draft_typing_fires_no_backend', window._resolveCalls === 0);
  midEl.value = 'claude-opus-4.6';
  _onModelIdDraftInput(midEl);
  check('restore_id_restores_verdict',
    draftNote.textContent.indexOf('settings.meFaceAutoResolved') >= 0);

  // default face is LOCALIZED (no raw 'default' jargon in the zh UI)
  global._stgFaceResolutions = window._stgFaceResolutions = { 0: {
    byModel: { 'claude-opus-4.6': { model_id: 'claude-opus-4.6', ok: true,
      face: 'default', protocol: 'openai',
      base_url: 'https://gw/v1/openai/native', forced: false, error: '' } },
    faces: ['default', 'anthropic'], dualFaceHost: true } };
  _repaintFaceAutoNote(0);
  check('default_face_localized',
    draftNote.textContent.indexOf('settings.meFaceDefaultFace') >= 0);

  // New model (empty saved id): typing an id shows the honest pending line
  _stgProviders[0].models.push({ model_id: '', aliases: [],
    capabilities: ['text'], rpm: 30, cost: 0.01 });
  seedResolutions();
  window._resolveCalls = 0;
  form = openForm(2);
  const newNote = form.querySelector('.stg-face-auto-note');
  check('new_model_no_verdict_yet',
    newNote.textContent.indexOf('settings.meFaceAutoResolved') < 0);
  check('new_model_open_fires_no_backend', window._resolveCalls === 0);
  const newMid = form.querySelector('.stg-edit-mid');
  newMid.value = 'claude-opus-5';
  _onModelIdDraftInput(newMid);
  check('new_model_typing_shows_pending',
    newNote.textContent.indexOf('settings.meFaceAutoDraft') >= 0);
  check('new_model_still_no_backend', window._resolveCalls === 0);

  // ══ 7. Sectioned layout + pin options NAME the protocol ══
  seedProviders();
  seedResolutions();
  form = openForm(0);
  check('five_sections_render',
    form.querySelectorAll('.stg-edit-sec').length === 5);
  const secLabels = Array.from(form.querySelectorAll('.stg-edit-sec-label'))
    .map((el) => el.textContent).join('|');
  check('section_labels_stamped',
    secLabels.indexOf('settings.meSecIdentity') >= 0 &&
    secLabels.indexOf('settings.meSecWire') >= 0 &&
    secLabels.indexOf('settings.meSecQuota') >= 0);
  const pinOpts = Array.from(form.querySelectorAll('.stg-edit-face option'))
    .map((o) => o.textContent);
  check('pin_options_carry_protocol',
    pinOpts.some((s) => s.indexOf('— anthropic') >= 0) &&
    pinOpts.some((s) => s.indexOf('— openai') >= 0));
  check('multi_face_has_no_provider_proto_select',
    form.querySelector('.stg-edit-proto') === null);
  check('cap_buttons_render_all_nine',
    form.querySelectorAll('.stg-cap-btn').length === 9);

  // ══ 8. Single-face provider: the wire protocol is editable in-dialog ══
  // (The owner's 2026-08-06 complaint: "nowhere to configure OpenAI /
  // Anthropic / Responses". Single-face = the common case; the pin select
  // would be a non-choice, so the section leads with the protocol itself.)
  global._stgProviders = window._stgProviders = [
    { id: 'provSolo', brand: 'custom', enabled: true, api_keys: ['k1'],
      base_url: 'https://solo.example.com/v1', protocol: 'openai',
      models: [ { model_id: 'solo-model', aliases: [],
        capabilities: ['text'], rpm: 30, cost: 0.01 } ] },
  ];
  global._stgFaceResolutions = window._stgFaceResolutions = {};
  form = openForm(0);
  const protoSel = form.querySelector('.stg-edit-proto');
  check('single_face_proto_select_present', protoSel !== null);
  check('single_face_proto_value', protoSel && protoSel.value === 'openai');
  check('single_face_has_no_pin_select',
    form.querySelector('.stg-edit-face') === null);
  /* NOTE: no `await` below — the refresh microtasks from the open-form
   * kick and the proto change land only at the NEXT harness await, so the
   * cleared single-face cache still holds for the reopen assertions. */
  window._resolveCalls = 0;
  protoSel.value = 'anthropic';
  _onModelProtoChange(0, protoSel);
  check('proto_change_writes_provider',
    _stgProviders[0].protocol === 'anthropic');
  check('proto_change_reresolves', window._resolveCalls >= 1);

  // An unknown stored protocol is PRESERVED as an option, never rewritten
  _stgProviders[0].protocol = 'wire-vNext';
  form = openForm(0);
  const protoSel2 = form.querySelector('.stg-edit-proto');
  const protoVals = protoSel2
    ? Array.from(protoSel2.options).map((o) => o.value) : [];
  check('unknown_protocol_preserved',
    protoVals.indexOf('wire-vNext') >= 0 && protoSel2.value === 'wire-vNext');

  /* Drain the async leftovers: section-8's proto change and the open-form
   * kicks queued refresh continuations that repaint via the INTACT hook.
   * Left pending, they land inside NEUTER 3's await window and resolve the
   * note that neuter expects to stay pending — flush them here instead.
   * Microtask ticks, NOT setTimeout: the shared harness NEUTERS setTimeout
   * to a no-op, so a timer-based drain suspends the whole IIFE forever
   * (measured: zero harness output, RC 0). Each await re-queues behind the
   * pending continuations; three ticks drains the whole chain. */
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();

  // ══ NEUTER 1: drop the auto-note from the edit form → no verdict ══
  {
    const n = ME_SRC.replace(
      "_faceAutoNoteHTML(provIdx, m) + '</div>'",
      "'' + '</div>'");
    check('N1_applied', n !== ME_SRC);
    indirectEval(n);
    seedProviders();
    seedResolutions();
    const f = openForm(0);
    check('N1_verdict_gone',
      !f.querySelector('.stg-face-auto-pick'));
    indirectEval(ME_SRC);   // restore
  }

  // ══ NEUTER 2: _poolTagValues blind → save loses the pool ══
  {
    const n = ME_SRC.replace(
      'function _poolTagValues(form) {',
      'function _poolTagValues(form) { return []; }\nfunction _poolTagValuesReal(form) {');
    check('N2_applied', n !== ME_SRC);
    indirectEval(n);
    seedProviders();
    seedResolutions();
    openForm(0);
    window._alertCalls = [];
    _saveModelEdit(0, 0);
    check('N2_pool_lost_or_guard_fires',
      window._alertCalls.length === 1 ||
      _stgProviders[0].models[0].request_ids.length === 0);
    indirectEval(ME_SRC);   // restore
  }

  // ══ NEUTER 3: drop the provider_faces repaint hook → note stuck ══
  {
    const n = PF_SRC.replace(
      "if (typeof _repaintFaceAutoNote === 'function') _repaintFaceAutoNote(provIdx);",
      '');
    check('N3_applied', n !== PF_SRC);
    indirectEval(n);            // provider_faces.js re-evaled WITHOUT the hook
    indirectEval(ME_SRC);       // model_edit.js intact (the hook is what is missing)
    seedProviders();
    global._stgFaceResolutions = window._stgFaceResolutions = {};
    const f = openForm(0);
    const nn = f.querySelector('.stg-face-auto-note');
    await _refreshFaceResolutions(0);
    check('N3_note_stuck_pending',
      nn.textContent.indexOf('settings.meFaceAutoResolved') < 0);
    indirectEval(PF_SRC);   // restore
    indirectEval(ME_SRC);
  }

  // ══ NEUTER 4: blind the draft check → the stale verdict SURVIVES a
  // rename (the exact wrong-claim failure the draft branch prevents) ══
  {
    const n = ME_SRC.replace(
      "if (_draft !== ((m && m.model_id) || '')) {",
      'if (false) {');
    check('N4_applied', n !== ME_SRC);
    indirectEval(n);
    seedProviders();
    seedResolutions();
    const f = openForm(0);
    const mEl = f.querySelector('.stg-edit-mid');
    mEl.value = 'claude-opus-5';
    _onModelIdDraftInput(mEl);
    const nn = f.querySelector('.stg-face-auto-note');
    check('N4_stale_verdict_survives',
      nn.textContent.indexOf('settings.meFaceAutoResolved') >= 0);
    indirectEval(ME_SRC);   // restore
  }

  // ══ NEUTER 5: drop the provider-protocol select → a single-face
  // provider's wire section has NO protocol control (the exact "nowhere
  // to configure the protocol" complaint this redesign answers) ══
  {
    const n = ME_SRC.replace('class="stg-edit-proto"',
                             'class="stg-edit-proto-gone"');
    check('N5_applied', n !== ME_SRC);
    indirectEval(n);
    global._stgProviders = window._stgProviders = [
      { id: 'provSolo', brand: 'custom', enabled: true, api_keys: ['k1'],
        base_url: 'https://solo.example.com/v1', protocol: 'openai',
        models: [ { model_id: 'solo-model', aliases: [],
          capabilities: ['text'], rpm: 30, cost: 0.01 } ] },
    ];
    global._stgFaceResolutions = window._stgFaceResolutions = {};
    const f = openForm(0);
    check('N5_proto_select_gone',
      f.querySelector('.stg-edit-proto') === null);
    indirectEval(ME_SRC);   // restore
  }
} catch (e) {
  check('harness_threw: ' + (e && e.message), false);
} finally {
  report();
}
})();
'''


def test_model_edit_redesign():
    body = _BODY.replace('HTML_PLACEHOLDER', json.dumps(_HTML))
    run_harness(
        target_js=MODEL_EDIT_JS,
        body_js=body,
        extra_targets=[PROVIDER_FACES_JS],
        # 70 check() call sites, one is the catch-block harness_threw that
        # only fires on a crash — a green run reports exactly 69 PASS. The
        # floor is the FULL green count so any dropped assertion goes red.
        min_pass=69,
        label='model-edit-redesign',
    )


# ═══════════════════════════════════════════════════════════════════
# Source-level guards (cheap, no DOM): the redesign's load-bearing facts
# ═══════════════════════════════════════════════════════════════════

def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def test_no_comma_input_remains_and_tag_editor_is_wired():
    """The comma input is GONE from the form; the tag editor is what the
    save path reads. A future revert to a comma string goes red here."""
    src = _read(MODEL_EDIT_JS)
    assert 'stg-edit-aliases' not in src, (
        'the comma-separated pool input is back — the tag editor replaced it')
    assert 'stg-tag-editor' in src and 'data-pool-field' in src
    save = src[src.index('function _saveModelEdit'):]
    assert '_poolTagValues(' in save, (
        'the save path must read the chips, not a comma string')


def test_edit_grid_is_uniform_two_column():
    """The misalignment the owner flagged was the 1fr/auto/auto template.
    It must not come back; the Model ID row spans the full width."""
    css = _read(STYLES_CSS)
    m = re.search(r'\.stg-edit-grid\s*\{[^}]*grid-template-columns:\s*([^;]+);',
                  css)
    assert m, '.stg-edit-grid template not found'
    assert m.group(1).strip() == '1fr 1fr', (
        'the edit grid must be a uniform two columns, got: %s' % m.group(1))
    assert '.stg-field-wide' in css, 'Model ID full-row class missing'
    assert '.stg-tag-editor' in css, 'tag editor styles missing'
    assert '.stg-face-auto-note' in css, 'auto-note styles missing'


def test_new_i18n_keys_have_both_languages():
    """A missing key renders as its literal dotted name."""
    i18n = _read(I18N_JS)
    keys = [
        'settings.meFaceAutoResolved', 'settings.meFaceAutoPending',
        'settings.meFaceAutoRefused', 'settings.meFaceAutoSkipped',
        'settings.meFaceAutoDraft', 'settings.meFaceDefaultFace',
    ]
    missing = []
    for k in keys:
        m = re.search(r"^\s*'%s':\s*\{.*$" % re.escape(k), i18n, re.M)
        if not m:
            missing.append('%s (absent)' % k)
        elif 'zh:' not in m.group(0) or 'en:' not in m.group(0):
            missing.append('%s (missing zh or en)' % k)
    assert not missing, 'i18n keys incomplete: %s' % missing
    # The hints must no longer tell the user to type commas.
    for k in ('settings.meAliasesHint', 'settings.meRequestIdsHint'):
        m = re.search(r"^\s*'%s':\s*\{.*$" % re.escape(k), i18n, re.M)
        assert m, '%s missing' % k
        assert '逗号' not in m.group(0) and 'comma-separated' not in m.group(0), (
            '%s still instructs comma separation' % k)


def test_used_auto_note_keys_all_defined():
    """Every settings.* key model_edit.js asks for must exist in i18n.js
    (a typo'd key renders as raw dotted text)."""
    i18n = _read(I18N_JS)
    src = _read(MODEL_EDIT_JS)
    used = set(re.findall(r"t\(\s*'(settings\.[A-Za-z0-9_]+)'", src))
    assert used, 'no i18n keys found — scan is vacuous'
    missing = [k for k in sorted(used) if ("'%s':" % k) not in i18n]
    assert not missing, 'model_edit.js uses undefined i18n keys: %s' % missing


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
