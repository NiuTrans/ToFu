"""jsdom regression: Project Brain Charter is EDITABLE + DELETABLE, and the
content-translation overlay is DEFAULT-ON.

WHY
The owner asked that the charter (north star + committed decisions) be
editable and deletable from the panel, and that its content auto-translate to
the UI language. This drives the REAL shipped `renderCharter` +
`_wireCharterEditControls` (project-brain.js) and `ProjectBrainI18n`
(project-brain-i18n.js) under jsdom over the real charter DOM fragment.

Assertions (all against a fake Api that records the calls — no network):
  • north-star EDIT opens an inline textarea seeded with the ORIGINAL text and
    Save → commitCharter({content, expected_version});
  • a committed decision EDIT → updateDecision(index, text, {expected_version});
    the editor is seeded from data-pb-src (the ORIGINAL), NOT the translation
    overlay — the load-bearing i18n invariant;
  • decision DELETE is TWO-STEP (first click arms, second click fires) →
    deleteDecision(index, {expected_version});
  • DELETE-CHARTER (two-step) → deleteCharter({expected_version});
  • the overlay isEnabled() defaults TRUE with NO stored preference (opt-out).

TRIPLE-NEUTER (all in COPIES; shipped files byte-identical after):
  • NC-1 (edit-source-of-truth): make the editor seed from textContent (the
    translated view) instead of data-pb-src → the "editor seeded with the
    ORIGINAL" assertion fails.
  • NC-2 (two-step delete): make _confirmInline fire on the FIRST click → the
    "one click does not delete" assertion fails.
  • NC-3 (default-on): revert isEnabled to `=== '1'` → the default-on assertion
    fails.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')
_BRAIN_SRC = os.path.join(JS_DIR, 'project-brain.js')
_I18N_SRC = os.path.join(JS_DIR, 'project-brain-i18n.js')

# A decision long enough to be clamped (so data-pb-src is stamped and the
# overlay would translate it) and clearly ENGLISH so a Chinese-UI overlay
# translates it (source ≠ target).
_EN_DECISION = ('Adopt Redis as the single externalization substrate for both '
                'push fan-out and lease-TTL counters across every replica of '
                'the deployment, gated behind an env flag defaulting to inproc.')


def _node_deps_available():
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_DOM = r'''<!DOCTYPE html><body>
<div class="project-brain-overlay" id="projectBrainOverlay">
  <div class="project-brain-head"><div class="project-brain-head-actions">
    <button type="button" class="pb-tr-toggle" id="projectBrainTranslateToggle" aria-pressed="false" role="switch">
      <span class="pb-tr-toggle-ico"></span><span class="pb-tr-toggle-label"></span>
    </button>
  </div></div>
  <div class="project-brain-columns">
    <div class="project-brain-col pb-tab-panel pb-tab-panel-active" data-pb-panel="charter"><div class="project-brain-col-body" id="projectBrainCharterBody"></div></div>
  </div>
</div>
</body>'''


def _harness():
    return r'''
const fs = require('fs');
const path = require('path');
const BRAIN = process.argv[1];
const I18N = process.argv[2];
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(DOM_PLACEHOLDER, { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;
global.localStorage = win.localStorage;
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => setTimeout(() => fn(Date.now()), 0);
win._i18nLang = global._i18nLang = 'zh';
win.t = global.t = (k, f) => (f || k);
win.Icon = global.Icon = () => '<svg></svg>';
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
win.activeConvId = global.activeConvId = '';

// Fake Api.project recording every charter mutation; refresh calls are no-ops.
const CALLS = [];
win.Api = global.Api = { project: {
  commitCharter:  (p, b) => { CALLS.push(['commit', p, b]); return Promise.resolve({ version: 99 }); },
  updateDecision: (p, i, t, b) => { CALLS.push(['update', p, i, t, b]); return Promise.resolve({ version: 99 }); },
  deleteDecision: (p, i, b) => { CALLS.push(['deleteDec', p, i, b]); return Promise.resolve({ version: 99 }); },
  deleteCharter:  (p, b) => { CALLS.push(['deleteAll', p, b]); return Promise.resolve({ deleted: true }); },
  // NOTE: `charter`/`charterPending` are deliberately OMITTED so refreshCharter
  // (called after each save/delete) early-returns instead of async re-rendering
  // the body — which would clobber the DOM the next phase drives. We assert the
  // mutation CALL was made; the server round-trip re-render is out of scope here.
}, translate: {
  run: (body) => Promise.resolve({ _ok: true, translated: '译文：' + body.text }),
} };

eval(fs.readFileSync(BRAIN, 'utf8'));
eval(fs.readFileSync(I18N, 'utf8'));
const PB = win.ProjectBrain;
const I = win.ProjectBrainI18n;
PB._state.path = '/proj/x';

function drain() {
  return new Promise((resolve) => { let n = 0;
    (function tick(){ if (n++ > 30) return resolve(); setTimeout(tick, 0); })(); });
}
function click(el) { el.dispatchEvent(new win.MouseEvent('click', { bubbles: true })); }

(async () => {
  const out = {};

  // Default-ON: NO stored preference → overlay is enabled (opt-out).
  out.overlayDefaultsOn = I.isEnabled();

  // Render a charter: a north star + one EN committed decision.
  PB.renderCharter({ content: 'Ship the thing.', version: 7,
    decisions: [{ text: EN_DECISION_PH }], exists: true }, []);
  await drain();  // let the translation overlay lay over the EN decision

  const body = win.document.getElementById('projectBrainCharterBody');

  // The decision text node now SHOWS the translation (overlay default-on)…
  const decText = body.querySelector('li[data-decision-idx="0"] .pb-decision-text .pb-clamp, li[data-decision-idx="0"] .pb-decision-text .pb-clamp-inner');
  out.decisionShowsTranslation = !!(decText && decText.textContent.indexOf('译文：') === 0);
  out.decisionSrcIsOriginal = !!(decText && decText.getAttribute('data-pb-src') === EN_DECISION_PH);

  // ── EDIT a decision: opens an inline editor seeded with the ORIGINAL ──
  const editBtn = body.querySelector('.pb-decision-edit');
  out.editBtnPresent = !!editBtn;
  click(editBtn);
  const editor = body.querySelector('li[data-decision-idx="0"] .pb-inline-editor-input');
  out.editorOpened = !!editor;
  // LOAD-BEARING: the editor is seeded from data-pb-src (original), NOT the
  // translated view laid over innerHTML.
  out.editorSeededWithOriginal = !!(editor && editor.value === EN_DECISION_PH);
  // Save an edit → updateDecision(index, newText, {expected_version:7}).
  if (editor) editor.value = 'Edited decision text.';
  const saveBtn = body.querySelector('.pb-inline-save');
  click(saveBtn);
  await drain();
  const upd = CALLS.find(c => c[0] === 'update');
  out.updateCalled = !!upd;
  out.updateArgs = upd ? { idx: upd[2], text: upd[3], ev: (upd[4] || {}).expected_version } : null;

  // ── EDIT the north star → commitCharter({content, expected_version:7}) ──
  PB.renderCharter({ content: 'Ship the thing.', version: 7,
    decisions: [{ text: EN_DECISION_PH }], exists: true }, []);
  await drain();
  const body2 = win.document.getElementById('projectBrainCharterBody');
  click(body2.querySelector('.pb-charter-edit-northstar'));
  const nsEditor = body2.querySelector('.pb-charter-northstar-row .pb-inline-editor-input');
  out.nsEditorSeeded = !!(nsEditor && nsEditor.value === 'Ship the thing.');
  if (nsEditor) nsEditor.value = 'New north star.';
  click(body2.querySelector('.pb-charter-northstar-row .pb-inline-save'));
  await drain();
  const com = CALLS.find(c => c[0] === 'commit');
  out.commitArgs = com ? { content: (com[2] || {}).content, ev: (com[2] || {}).expected_version } : null;

  // ── DELETE a decision: TWO-STEP confirm ──
  PB.renderCharter({ content: '', version: 7,
    decisions: [{ text: 'D0' }, { text: 'D1' }], exists: true }, []);
  await drain();
  const body3 = win.document.getElementById('projectBrainCharterBody');
  const delBtn = body3.querySelector('li[data-decision-idx="1"] .pb-decision-delete');
  click(delBtn);                       // first click = ARM, no call yet
  await drain();
  out.deleteAfterOneClick = CALLS.filter(c => c[0] === 'deleteDec').length;
  click(delBtn);                       // second click = FIRE
  await drain();
  const delc = CALLS.find(c => c[0] === 'deleteDec');
  out.deleteAfterTwoClicks = !!delc;
  out.deleteArgs = delc ? { idx: delc[2], ev: (delc[3] || {}).expected_version } : null;

  // ── DELETE the whole charter: TWO-STEP confirm ──
  const delAll = body3.querySelector('.pb-charter-delete-all');
  out.deleteAllPresent = !!delAll;
  click(delAll); await drain();
  out.deleteAllAfterOneClick = CALLS.filter(c => c[0] === 'deleteAll').length;
  click(delAll); await drain();
  const dac = CALLS.find(c => c[0] === 'deleteAll');
  out.deleteAllFired = !!dac;
  out.deleteAllEv = dac ? (dac[2] || {}).expected_version : null;

  console.log('__RESULT__' + JSON.stringify(out));
})();
'''.replace('DOM_PLACEHOLDER', json.dumps(_DOM)) \
   .replace('EN_DECISION_PH', json.dumps(_EN_DECISION))


def _run(brain=_BRAIN_SRC, i18n=_I18N_SRC):
    proc = subprocess.run(
        ['node', '-e', _harness(), brain, i18n, ROOT],
        capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f'harness failed: {proc.stderr or proc.stdout}')
    for line in proc.stdout.splitlines():
        if line.startswith('__RESULT__'):
            return json.loads(line[len('__RESULT__'):])
    raise AssertionError(f'no result line: {proc.stdout}\n{proc.stderr}')


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_charter_edit_delete_and_default_on_overlay():
    out = _run()
    # Overlay defaults ON with no stored pref (opt-out auto-translate).
    assert out['overlayDefaultsOn'] is True, out
    # The EN decision is shown translated (overlay ran by default)…
    assert out['decisionShowsTranslation'] is True, out
    assert out['decisionSrcIsOriginal'] is True, out
    # …yet EDIT seeds the textarea with the ORIGINAL, not the translation.
    assert out['editBtnPresent'] is True and out['editorOpened'] is True, out
    assert out['editorSeededWithOriginal'] is True, \
        f'the editor MUST be seeded with the original text, never the translation: {out}'
    assert out['updateCalled'] is True, out
    assert out['updateArgs'] == {'idx': 0, 'text': 'Edited decision text.', 'ev': 7}, out
    # North-star edit → commitCharter with content + expected_version.
    assert out['nsEditorSeeded'] is True, out
    assert out['commitArgs'] == {'content': 'New north star.', 'ev': 7}, out
    # Decision delete is TWO-STEP.
    assert out['deleteAfterOneClick'] == 0, \
        f'a single click must NOT delete (two-step confirm): {out}'
    assert out['deleteAfterTwoClicks'] is True, out
    assert out['deleteArgs'] == {'idx': 1, 'ev': 7}, out
    # Delete-charter is present + two-step.
    assert out['deleteAllPresent'] is True, out
    assert out['deleteAllAfterOneClick'] == 0, out
    assert out['deleteAllFired'] is True, out
    assert out['deleteAllEv'] == 7, out


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC1_editor_seeded_from_translation_is_load_bearing(tmp_path):
    """NC-1: make the editor seed from textContent (the translated view)
    instead of data-pb-src → the "seeded with the ORIGINAL" assertion fails.
    Shipped file byte-identical after."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = ("    return node.getAttribute('data-pb-src') != null\n"
              "      ? node.getAttribute('data-pb-src')\n"
              "      : (node.textContent || '');")
    assert anchor in original, 'charter-src anchor not found'
    patched = original.replace(
        anchor, "    return (node.textContent || '');  // NC-1", 1)
    assert patched != original
    src = os.path.join(tmp_path, 'brain-nc1.js')
    with open(src, 'w', encoding='utf-8') as f:
        f.write(patched)
    out = _run(brain=src)
    assert out['editorSeededWithOriginal'] is False, \
        f'NC-1: seeding from the translation must break the original-seed invariant: {out}'
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC2_two_step_confirm_is_load_bearing(tmp_path):
    """NC-2: make _confirmInline fire on the FIRST click → a single click
    deletes, breaking the two-step-confirm assertion. Byte-identical after."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = ("  function _confirmInline(btn, onConfirm) {\n"
              "    if (btn._pbConfirmArmed) {")
    assert anchor in original, 'confirm-inline anchor not found'
    patched = original.replace(
        anchor,
        ("  function _confirmInline(btn, onConfirm) {\n"
         "    onConfirm(); return;  // NC-2 (fire on first click)\n"
         "    if (btn._pbConfirmArmed) {"),
        1)
    assert patched != original
    src = os.path.join(tmp_path, 'brain-nc2.js')
    with open(src, 'w', encoding='utf-8') as f:
        f.write(patched)
    out = _run(brain=src)
    assert out['deleteAfterOneClick'] == 1, \
        f'NC-2: firing on the first click must break two-step confirm: {out}'
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC3_default_on_is_load_bearing(tmp_path):
    """NC-3: revert isEnabled to the old `=== '1'` default-OFF → the
    default-on assertion fails. Byte-identical after."""
    with open(_I18N_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = "    try { return localStorage.getItem(PREF_KEY) !== '0'; }"
    assert anchor in original, 'isEnabled anchor not found'
    patched = original.replace(
        anchor, "    try { return localStorage.getItem(PREF_KEY) === '1'; }  // NC-3", 1)
    assert patched != original
    src = os.path.join(tmp_path, 'i18n-nc3.js')
    with open(src, 'w', encoding='utf-8') as f:
        f.write(patched)
    out = _run(i18n=src)
    assert out['overlayDefaultsOn'] is False, \
        f'NC-3: reverting to default-off must break the default-on assertion: {out}'
    with open(_I18N_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain-i18n.js must be byte-identical'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
