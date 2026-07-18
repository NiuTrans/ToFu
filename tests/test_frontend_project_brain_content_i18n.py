"""jsdom regression: Project Brain CONTENT translation as a DISPLAY OVERLAY.

WHY
The Project Brain chrome (tab labels, buttons) already tracks the UI language
via t('projectBrain.*'). The agent/human-AUTHORED free-text CONTENT (charter
north-star + committed decisions, board epic titles, activity + peer summaries)
rendered verbatim. `project-brain-i18n.js` lays a translation OVER that content
in the UI language — as a VIEW, never a mutation:

  • the ORIGINAL stays the source of truth (kept in data-pb-src; the overlay
    writes into innerHTML but never touches the attribute), and — the
    load-bearing invariant — the commit/reject buttons act on their OWN
    data-text (the original proposal), which the overlay must NEVER clobber;
  • already-in-target text does ZERO work (an EN charter on an EN UI, a ZH line
    on a ZH UI) via a local CJK ratio gate;
  • toggling the panel OFF reverts every node to its original byte-for-byte.

This drives the REAL shipped `renderCharter` (project-brain.js) + the REAL
`ProjectBrainI18n` overlay under jsdom over the real DOM fragment.

TRIPLE-NEUTER (all in COPIES; shipped files byte-identical after):
  • NC-1 (source-of-truth for commit — THE load-bearing one): make the overlay
    _applySwap ALSO overwrite the sibling commit button's data-text with the
    translation → the "commit uses the ORIGINAL proposal" assertion fails.
  • NC-2 (already-target gate): make _alreadyTarget always return false → an
    already-Chinese line on a Chinese UI is needlessly sent to translate
    (the "zero work" assertion — no data-pb-tr on already-target — fails).
  • NC-3 (compare-before-swap): drop the `el._pbShown === translated` short
    circuit → a re-apply rewrites innerHTML even when unchanged (the
    "no redundant DOM write on re-apply" assertion fails).

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

# A committed/pending proposal in ENGLISH; UI target = Chinese → it SHOULD be
# translated (source is not already-target).
_EN_PROPOSAL = ('Adopt Redis as the single externalization substrate for both '
                'push fan-out and lease-TTL counters across all replicas.')
# An already-CHINESE decision; UI target = Chinese → it must be SKIPPED.
_ZH_DECISION = '所有横向扩展的运行时状态都必须通过共享租约存储，默认关闭以保持单机字节一致。'


def _node_deps_available():
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_DOM = r'''<!DOCTYPE html><body>
<div class="project-brain-overlay" id="projectBrainOverlay">
  <div class="project-brain-head">
    <div class="project-brain-head-actions">
      <button type="button" class="pb-tr-toggle" id="projectBrainTranslateToggle" aria-pressed="false" role="switch">
        <span class="pb-tr-toggle-ico"></span><span class="pb-tr-toggle-label"></span>
      </button>
    </div>
  </div>
  <div class="project-brain-columns">
    <div class="project-brain-col pb-tab-panel pb-tab-panel-active" data-pb-panel="charter"><div class="project-brain-col-body" id="projectBrainCharterBody"></div></div>
    <div class="project-brain-col pb-tab-panel" data-pb-panel="board"><div class="project-brain-col-body" id="projectBrainBoardBody"></div></div>
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
global.localStorage = win.localStorage;   // bare localStorage in the module → jsdom store
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => setTimeout(() => fn(Date.now()), 0);
// UI language = Chinese (target = 'Chinese').
win._i18nLang = global._i18nLang = 'zh';
win.t = global.t = (k, f) => (f || k);
win.Icon = global.Icon = () => '<svg></svg>';
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
win.activeConvId = global.activeConvId = '';

// Deterministic fake translate engine: a Chinese-looking prefix so the output
// is "predominantly CJK" and never mistaken for still-English by any guard.
let TR_CALLS = [];
win.Api = global.Api = { project: {}, translate: {
  run: (body) => { TR_CALLS.push(body.text); return Promise.resolve({ _ok: true, translated: '译文：' + body.text }); },
} };

eval(fs.readFileSync(BRAIN, 'utf8'));
eval(fs.readFileSync(I18N, 'utf8'));
const PB = win.ProjectBrain;
const I = win.ProjectBrainI18n;

// Drain the microtask + rAF queues (translate is async; swaps are rAF-coalesced).
function drain() {
  return new Promise((resolve) => {
    let n = 0;
    (function tick(){ if (n++ > 30) return resolve(); setTimeout(tick, 0); })();
  });
}

(async () => {
  const out = {};

  // Enable the overlay (default is OFF).
  try { win.localStorage.setItem('tofu_pb_translate', '1'); } catch (_e) {}
  out.enabledAfterSet = I.isEnabled();

  // Render the charter: an EN pending proposal (translate) + a ZH committed
  // decision (skip, already-target). renderCharter calls _applyContentI18n.
  PB.renderCharter(
    { content: '', decisions: [ZH_DECISION_PH], version: 3 },
    [ { event_id: 'ev1', proposalId: 'p1', summary: EN_PROPOSAL_PH,
        payload: { proposal: EN_PROPOSAL_PH, proposalId: 'p1' } } ]
  );
  await drain();

  const charterBody = win.document.getElementById('projectBrainCharterBody');

  // ── The EN proposal text node: overlaid with a translation ──
  const proposalNode = charterBody.querySelector('.pb-proposal-text .pb-clamp-inner, .pb-proposal-text .pb-clamp');
  out.proposalHasSrc = !!(proposalNode && proposalNode.getAttribute('data-pb-src') === EN_PROPOSAL_PH);
  out.proposalShowsTranslation = !!(proposalNode && proposalNode.textContent.indexOf('译文：') === 0);
  out.proposalTitleIsOriginal = !!(proposalNode && proposalNode.title === EN_PROPOSAL_PH);
  out.proposalSrcUnmutated = !!(proposalNode && proposalNode.getAttribute('data-pb-src') === EN_PROPOSAL_PH);

  // ── LOAD-BEARING: the commit button's data-text is the ORIGINAL proposal ──
  const commitBtn = charterBody.querySelector('.pb-proposal-commit');
  out.commitDataTextIsOriginal = !!(commitBtn && commitBtn.getAttribute('data-text') === EN_PROPOSAL_PH);

  // ── The already-Chinese committed decision: NOT translated (zero work) ──
  const decItems = charterBody.querySelectorAll('.pb-charter-decisions li .pb-clamp-inner, .pb-charter-decisions li .pb-clamp');
  let decNode = null;
  decItems.forEach(n => { if (n.getAttribute('data-pb-src') === ZH_DECISION_PH) decNode = n; });
  out.decisionFound = !!decNode;
  out.decisionNotTranslated = !!(decNode && !decNode.getAttribute('data-pb-tr') &&
    decNode.textContent.indexOf('译文：') === -1 && decNode.textContent.indexOf(ZH_DECISION_PH) !== -1);
  out.zhDecisionNeverSentToTranslate = TR_CALLS.indexOf(ZH_DECISION_PH) === -1;
  out.enProposalSentToTranslate = TR_CALLS.indexOf(EN_PROPOSAL_PH) !== -1;

  // ── compare-before-swap: re-apply does NOT rewrite an unchanged node ──
  // Tag the current node, re-run apply, assert innerHTML identity is stable
  // (we detect a rewrite by mutating a sentinel and checking it survives).
  if (proposalNode) {
    // Mark the current child text node. A compare-before-swap re-apply must
    // NOT rewrite innerHTML, so the SAME child node (with the mark) survives;
    // an unconditional rewrite recreates the child → the mark is gone.
    const childBefore = proposalNode.firstChild;
    if (childBefore) childBefore._mark = 'keep';
    I.apply(charterBody);
    await drain();
    out.reapplyNoRewrite = !!(proposalNode.firstChild &&
      proposalNode.firstChild === childBefore && proposalNode.firstChild._mark === 'keep');
  }

  // ── Toggle OFF → every node reverts to its ORIGINAL byte-for-byte ──
  const callsBeforeOff = TR_CALLS.length;
  I.toggle();  // now OFF
  await drain();
  out.disabledAfterToggle = !I.isEnabled();
  out.proposalRevertedToOriginal = !!(proposalNode &&
    proposalNode.textContent === EN_PROPOSAL_PH && !proposalNode.getAttribute('data-pb-tr'));
  out.noNewCallsWhenOff = TR_CALLS.length === callsBeforeOff;

  console.log('__RESULT__' + JSON.stringify(out));
})();
'''.replace('DOM_PLACEHOLDER', json.dumps(_DOM)) \
   .replace('EN_PROPOSAL_PH', json.dumps(_EN_PROPOSAL)) \
   .replace('ZH_DECISION_PH', json.dumps(_ZH_DECISION))


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
def test_content_overlay_translates_without_mutating_source():
    out = _run()
    assert out['enabledAfterSet'] is True, out
    # EN proposal → translated view laid over the original.
    assert out['proposalHasSrc'] is True, out
    assert out['proposalShowsTranslation'] is True, \
        f'the EN proposal must show a translated view: {out}'
    assert out['proposalTitleIsOriginal'] is True, \
        f'hover title must reveal the ORIGINAL: {out}'
    assert out['proposalSrcUnmutated'] is True, \
        f'data-pb-src (the source of truth) must be untouched: {out}'
    # LOAD-BEARING: commit acts on the ORIGINAL proposal text, never the translation.
    assert out['commitDataTextIsOriginal'] is True, \
        f'the commit button data-text MUST be the original proposal, never the translation: {out}'
    assert out['enProposalSentToTranslate'] is True, out
    # Already-Chinese decision on a Chinese UI → zero work.
    assert out['decisionFound'] is True, out
    assert out['decisionNotTranslated'] is True, \
        f'an already-target line must NOT be translated: {out}'
    assert out['zhDecisionNeverSentToTranslate'] is True, \
        f'an already-target line must never be sent to the engine (zero cost): {out}'
    # Anti-flicker: compare-before-swap.
    assert out['reapplyNoRewrite'] is True, \
        f're-apply must not rewrite an unchanged node: {out}'
    # Toggle off → byte-for-byte revert to originals.
    assert out['disabledAfterToggle'] is True, out
    assert out['proposalRevertedToOriginal'] is True, \
        f'toggling off must restore the original text: {out}'
    assert out['noNewCallsWhenOff'] is True, out


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC1_swap_clobbering_commit_source_is_load_bearing(tmp_path):
    """NC-1 (THE load-bearing neuter): make _applySwap ALSO overwrite the
    sibling commit button's data-text with the translation → the "commit uses
    the ORIGINAL proposal" assertion fails. Shipped file byte-identical after."""
    with open(_I18N_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = ("    el.innerHTML = _esc(item.translated);\n"
              "    el.title = item.src;                                   // hover reveals original")
    assert anchor in original, 'apply-swap anchor not found'
    patched = original.replace(
        anchor,
        ("    el.innerHTML = _esc(item.translated);\n"
         "    // NC-1: clobber the commit source with the translation (BUG).\n"
         "    var _wrap = el.closest ? el.closest('.pb-proposal') : null;\n"
         "    var _cb = _wrap ? _wrap.querySelector('.pb-proposal-commit') : null;\n"
         "    if (_cb) _cb.setAttribute('data-text', item.translated);\n"
         "    el.title = item.src;                                   // hover reveals original"),
        1)
    assert patched != original, 'NC-1 patch did not apply'
    src = os.path.join(tmp_path, 'i18n-nc1.js')
    with open(src, 'w', encoding='utf-8') as f:
        f.write(patched)
    out = _run(i18n=src)
    assert out['commitDataTextIsOriginal'] is False, \
        f'NC-1: clobbering the commit source must break the original-commit invariant: {out}'
    with open(_I18N_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain-i18n.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC2_already_target_gate_is_load_bearing(tmp_path):
    """NC-2: make _alreadyTarget always return false → an already-Chinese line
    on a Chinese UI is needlessly sent to translate. Byte-identical after."""
    with open(_I18N_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = ("  function _alreadyTarget(text, target) {\n"
              "    var r = _cjkRatio(text);")
    assert anchor in original, 'already-target anchor not found'
    patched = original.replace(
        anchor,
        ("  function _alreadyTarget(text, target) {\n"
         "    return false;  // NC-2 (never skip)\n"
         "    var r = _cjkRatio(text);"),
        1)
    assert patched != original, 'NC-2 patch did not apply'
    src = os.path.join(tmp_path, 'i18n-nc2.js')
    with open(src, 'w', encoding='utf-8') as f:
        f.write(patched)
    out = _run(i18n=src)
    assert out['zhDecisionNeverSentToTranslate'] is False, \
        f'NC-2: without the gate an already-target line IS sent to translate: {out}'
    with open(_I18N_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain-i18n.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC3_compare_before_swap_is_load_bearing(tmp_path):
    """NC-3: drop the `el._pbShown === translated` short circuit → a re-apply
    rewrites innerHTML even when unchanged (loses the sentinel). Byte-identical
    after."""
    with open(_I18N_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = "    if (el._pbShown === item.translated) return;           // already shown"
    assert anchor in original, 'compare-before-swap anchor not found'
    patched = original.replace(
        anchor,
        "    // NC-3: compare-before-swap removed (always rewrite)",
        1)
    assert patched != original, 'NC-3 patch did not apply'
    src = os.path.join(tmp_path, 'i18n-nc3.js')
    with open(src, 'w', encoding='utf-8') as f:
        f.write(patched)
    out = _run(i18n=src)
    assert out['reapplyNoRewrite'] is False, \
        f'NC-3: without compare-before-swap a re-apply rewrites the node: {out}'
    with open(_I18N_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain-i18n.js must be byte-identical'


# ── Truncated-translation fallback: keep the COMPLETE original ─────
# A standalone harness that drives ProjectBrainI18n.apply over a single
# [data-pb-src] node, with a fake engine that reports `truncated:true`. The
# overlay must NOT replace the complete original with the incomplete
# translation (the reported "displayed incompletely" bug), and must NOT cache
# it (a later pass can re-translate).
_TRUNC_HARNESS = r'''
const fs = require('fs');
const path = require('path');
const I18N = process.argv[2];
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div id="projectBrainOverlay">' +
  '<div class="pb-tab-panel pb-tab-panel-active">' +
  '<div id="node" data-pb-src="SRC_PH">SRC_PH</div>' +
  '</div></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win; global.document = win.document; global.console = console;
global.localStorage = win.localStorage;
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => setTimeout(() => fn(Date.now()), 0);
win._i18nLang = global._i18nLang = 'zh';
win.t = global.t = (k, f) => (f || k);
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s);
// jsdom has no indexedDB → the overlay's IDB layer fails open (no cache).
let TR_CALLS = [];
win.Api = global.Api = { project: {}, translate: {
  run: (body) => { TR_CALLS.push(body.text);
    // Report a TRUNCATED translation — the overlay must reject it.
    return Promise.resolve({ _ok: true, translated: '译文前半段', truncated: true }); },
} };

eval(fs.readFileSync(I18N, 'utf8'));
const I = win.ProjectBrainI18n;
function drain(){ return new Promise((res)=>{ let n=0; (function t(){ if(n++>30) return res(); setTimeout(t,0);})(); }); }

(async () => {
  const out = {};
  try { win.localStorage.setItem('tofu_pb_translate', '1'); } catch(_e){}
  const node = win.document.getElementById('node');
  I.apply(win.document.getElementById('projectBrainOverlay'));
  await drain();
  out.wasCalled = TR_CALLS.length > 0;               // engine WAS called
  out.stillOriginal = node.textContent === 'SRC_PH'; // but original kept
  out.noTrMarker = !node.getAttribute('data-pb-tr'); // never marked translated
  out.memCacheEmpty = Object.keys(I._memCache).length === 0;  // not cached
  console.log('__RESULT__' + JSON.stringify(out));
})();
'''.replace('SRC_PH', 'This is a long English peer message that must never be shown half-translated.')


def _run_trunc(i18n=_I18N_SRC):
    proc = subprocess.run(
        ['node', '-e', _TRUNC_HARNESS, _BRAIN_SRC, i18n, ROOT],
        capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise AssertionError(f'harness failed: {proc.stderr or proc.stdout}')
    for line in proc.stdout.splitlines():
        if line.startswith('__RESULT__'):
            return json.loads(line[len('__RESULT__'):])
    raise AssertionError(f'no result line: {proc.stdout}\n{proc.stderr}')


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_truncated_translation_keeps_complete_original():
    """A translation the engine flagged `truncated:true` must NOT replace the
    complete original — the overlay keeps the source visible (the reported
    'displayed incompletely' bug) and does not cache the partial."""
    out = _run_trunc()
    assert out['wasCalled'] is True, out
    assert out['stillOriginal'] is True, \
        f'a truncated translation must not replace the complete original: {out}'
    assert out['noTrMarker'] is True, out
    assert out['memCacheEmpty'] is True, \
        f'a truncated translation must not be cached: {out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC4_truncated_guard_is_load_bearing(tmp_path):
    """NC-4: drop the `!d.truncated` guard in _translateOne → a truncated
    translation IS applied over the original (the bug returns). Byte-identical
    after."""
    with open(_I18N_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = 'if (d && d._ok && d.translated && !d.truncated) return d.translated;'
    assert anchor in original, 'truncated-guard anchor not found'
    patched = original.replace(
        anchor,
        'if (d && d._ok && d.translated) return d.translated;  // NC-4 (guard removed)',
        1)
    assert patched != original, 'NC-4 patch did not apply'
    src = os.path.join(tmp_path, 'i18n-nc4.js')
    with open(src, 'w', encoding='utf-8') as f:
        f.write(patched)
    out = _run_trunc(i18n=src)
    assert out['stillOriginal'] is False, \
        f'NC-4: without the guard a truncated translation replaces the original: {out}'
    with open(_I18N_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain-i18n.js must be byte-identical'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
