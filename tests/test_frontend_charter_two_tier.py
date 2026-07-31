"""jsdom regression: the charter panel renders TWO-TIER decisions + a health
strip + a required summary input on proposal commits (owner 2026-07-28).

WHY
The kind-routed charter made every invariant carry a one-line `summary` (the
binding rule) with the full evidence text behind it. The panel must render
that structure — summary headline + kind badge, evidence behind the clamp —
plus a health strip driven by BACKEND-computed signals (a missing north star
is loud, not silent), and the human commit path must collect the summary the
REST route now requires (the kindless backflow gap).

Assertions (fake Api records calls; no network):
  • a structured decision renders its summary as the headline with a kind
    badge; the full evidence text stays in the clamp area, NOT the headline;
  • a legacy string decision renders without a head row (no crash);
  • health.contentSet=false → the no-goal warning strip is visible;
    health.contentSet=true → the stats strip shows the backend counts;
  • the proposal card carries a summary input pre-filled with the first line;
    clearing it disables Commit; typing re-enables; Commit sends the summary;
  • Commit with an EMPTY summary NEVER calls commitCharter.

DOUBLE-NEUTER (in COPIES; shipped files byte-identical after):
  • NC-1: drop the head-row branch → the summary headline assertion fails.
  • NC-2: drop the health-warn branch → the no-goal strip assertion fails.

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

_RULE = 'Credential redaction is a fail-closed whitelist'
_EVIDENCE = 'Three secret-carrying fields were leaked in a row (env -> headers -> url); each time the author just happened to remember.'


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
<div id="projectBrainInfluence"><div id="projectBrainInfluenceBody"></div><span id="projectBrainInfluenceConv"></span></div>
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
win._i18nLang = global._i18nLang = 'en';
// project-brain.js's _t(key, fallback) calls the GLOBAL t(key) with ONE arg —
// the fallback only fires when t is absent. Mirror production (where the real
// i18n table holds these keys) with a minimal table for the keys we assert on.
const I18N_TABLE = {
  'projectBrain.healthStats': '{n} decisions · {m} shown per turn',
  'projectBrain.healthNoGoal': 'No north-star goal is set — the decisions below are implementation-level intent only.',
};
win.t = global.t = (k) => (k in I18N_TABLE) ? I18N_TABLE[k] : k;
win.Icon = global.Icon = () => '<svg></svg>';
win.escapeHtml = global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
win.activeConvId = global.activeConvId = '';

const CALLS = [];
win.Api = global.Api = { project: {
  commitCharter: (p, b) => { CALLS.push(['commit', p, b]); return Promise.resolve({ version: 99 }); },
} };

eval(fs.readFileSync(BRAIN, 'utf8'));
eval(fs.readFileSync(I18N, 'utf8'));
const PB = win.ProjectBrain;
PB._state.path = '/proj/x';

function drain() {
  return new Promise((resolve) => { let n = 0;
    (function tick(){ if (n++ > 30) return resolve(); setTimeout(tick, 0); })(); });
}
function click(el) { el.dispatchEvent(new win.MouseEvent('click', { bubbles: true })); }

const RULE = RULE_PH;
const EVIDENCE = EVIDENCE_PH;
const FULL = RULE + '. Full evidence: ' + EVIDENCE;

(async () => {
  const out = {};
  const body = win.document.getElementById('projectBrainCharterBody');

  // ── Two-tier structured decision + health stats ──
  PB.renderCharter({
    content: 'Ship the platform.', version: 3, exists: true,
    health: { contentSet: true, decisionCount: 16, injectedCount: 16 },
    decisions: [
      { text: FULL, summary: RULE, kind: 'invariant', ts: 1, by_conv: 'cA' },
      'A legacy string decision without kind or summary.',
    ],
  }, []);
  await drain();

  const head = body.querySelector('li[data-decision-idx="0"] .pb-decision-head');
  out.headPresent = !!head;
  const badge = body.querySelector('li[data-decision-idx="0"] .pb-kind-badge');
  out.badgeText = badge ? badge.textContent : null;
  const sumEl = body.querySelector('li[data-decision-idx="0"] .pb-decision-summary');
  out.summaryHeadline = sumEl ? sumEl.textContent : null;
  out.evidenceNotInHead = head ? (head.textContent.indexOf(EVIDENCE) === -1) : null;
  const decText = body.querySelector('li[data-decision-idx="0"] .pb-decision-text');
  out.evidenceInClamp = decText ? (decText.textContent.indexOf(EVIDENCE) !== -1) : null;
  // Legacy string decision → NO head row.
  out.legacyHasNoHead = !body.querySelector('li[data-decision-idx="1"] .pb-decision-head');
  // Health stats strip shows the BACKEND numbers verbatim.
  const stats = body.querySelector('[data-pb-health="ok"]');
  out.healthStatsText = stats ? stats.textContent : null;

  // ── Missing north star → LOUD warning ──
  PB.renderCharter({
    content: '', version: 3, exists: true,
    health: { contentSet: false, decisionCount: 16, injectedCount: 16 },
    decisions: [{ text: FULL, summary: RULE, kind: 'invariant', ts: 1, by_conv: 'cA' }],
  }, []);
  await drain();
  const warn = body.querySelector('.pb-charter-health-warn[data-pb-health="no-goal"]');
  out.noGoalWarnPresent = !!warn;
  out.noGoalWarnText = warn ? warn.textContent : null;

  // ── Proposal commit: summary input required ──
  CALLS.length = 0;
  PB.renderCharter({ content: '', version: 7, exists: true,
    health: { contentSet: false, decisionCount: 0, injectedCount: 0 },
    decisions: [] },
    [{ event_id: 'ev1', proposalId: 'pid1',
       payload: { proposal: 'Adopt AST boundaries everywhere.\nLong rationale follows.', proposalId: 'pid1' } }]);
  await drain();
  const input = body.querySelector('.pb-proposal-summary');
  out.summaryInputPresent = !!input;
  out.summaryPrefilled = input ? input.value : null;
  const commitBtn = body.querySelector('.pb-proposal-commit');
  out.commitEnabledWithPrefill = commitBtn ? !commitBtn.disabled : null;

  // Clear the input → disabled; click → NO call.
  input.value = '';
  input.dispatchEvent(new win.Event('input', { bubbles: true }));
  out.commitDisabledWhenEmpty = commitBtn.disabled;
  click(commitBtn);
  await drain();
  out.commitCallsWhenEmpty = CALLS.length;

  // Type a summary → enabled; click → commitCharter carries the summary.
  input.value = 'Adopt AST boundaries';
  input.dispatchEvent(new win.Event('input', { bubbles: true }));
  out.commitEnabledAfterTyping = !commitBtn.disabled;
  click(commitBtn);
  await drain();
  const com = CALLS.find(c => c[0] === 'commit');
  out.commitSummary = com ? (com[2] || {}).summary : null;
  out.commitDecision = com ? (com[2] || {}).add_decision : null;
  out.commitResolves = com ? (com[2] || {}).resolves_proposal : null;

  // ── renderInfluence consumer (peer-flagged): structured decisions must
  // NOT render as [object Object] in the influence banner either. ──
  PB.renderInfluence({
    charter: { content: 'Ship the platform.', injected: true,
      decisions: [{ text: FULL, summary: RULE, kind: 'invariant', ts: 1, by_conv: 'cA' }] },
    board: { mine: [], avoid: [], open: [] },
    pendingDecisions: [],
  });
  await drain();
  const infBody = win.document.getElementById('projectBrainInfluenceBody');
  const infText = infBody ? infBody.textContent : '';
  out.infShowsRule = infText.indexOf(RULE) !== -1;
  out.infNoObjectObject = infText.indexOf('[object Object]') === -1;

  console.log('__RESULT__' + JSON.stringify(out));
})();
'''.replace('DOM_PLACEHOLDER', json.dumps(_DOM)) \
   .replace('RULE_PH', json.dumps(_RULE)) \
   .replace('EVIDENCE_PH', json.dumps(_EVIDENCE))


def _run(brain=_BRAIN_SRC, i18n=_I18N_SRC):
    # NOTE: on this project's FUSE mount a bare `require('jsdom')` measures
    # ~72s (I/O wait over thousands of small files) — the node timeout must
    # clear that comfortably or the harness dies before the first assertion.
    proc = subprocess.run(
        ['node', '-e', _harness(), brain, i18n, ROOT],
        capture_output=True, text=True, timeout=240)
    if proc.returncode != 0:
        raise AssertionError(f'harness failed: {proc.stderr or proc.stdout}')
    for line in proc.stdout.splitlines():
        if line.startswith('__RESULT__'):
            return json.loads(line[len('__RESULT__'):])
    raise AssertionError(f'no result line: {proc.stdout}\n{proc.stderr}')


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_two_tier_health_strip_and_summary_gate():
    out = _run()
    # Two-tier row: summary headline + kind badge; evidence stays in the clamp.
    assert out['headPresent'] is True, out
    assert out['badgeText'] == 'invariant', out
    assert out['summaryHeadline'] == _RULE, out
    assert out['evidenceNotInHead'] is True, out
    assert out['evidenceInClamp'] is True, out
    # Legacy entries render without a head row (no crash).
    assert out['legacyHasNoHead'] is True, out
    # Health strip: backend counts shown verbatim; missing goal is LOUD.
    assert out['healthStatsText'] == '16 decisions · 16 shown per turn', out
    assert out['noGoalWarnPresent'] is True, out
    assert 'No north-star goal' in (out['noGoalWarnText'] or ''), out
    # Proposal commit: summary input pre-filled with the first line.
    assert out['summaryInputPresent'] is True, out
    assert out['summaryPrefilled'] == 'Adopt AST boundaries everywhere.', out
    assert out['commitEnabledWithPrefill'] is True, out
    # Empty summary → disabled AND a click never reaches the route.
    assert out['commitDisabledWhenEmpty'] is True, out
    assert out['commitCallsWhenEmpty'] == 0, out
    # Typed summary → enabled; commit carries it + resolves the proposal.
    assert out['commitEnabledAfterTyping'] is True, out
    assert out['commitSummary'] == 'Adopt AST boundaries', out
    assert out['commitDecision'] == 'Adopt AST boundaries everywhere.\nLong rationale follows.', out
    assert out['commitResolves'] == 'pid1', out
    # renderInfluence consumer: structured decisions render the rule, never
    # '[object Object]' (peer-flagged miss from the structured-payload change).
    assert out['infShowsRule'] is True, out
    assert out['infNoObjectObject'] is True, out


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC3_stringifying_the_object_renders_object_Object(tmp_path):
    """NC-3: revert the influence consumer to String(decs[i]) → the banner
    shows '[object Object]' again and the assertion fails. Byte-identical."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = ("          var _dTxt = (_di && typeof _di === 'object')\n"
              "            ? (_di.summary || _di.text || '') : String(_di);")
    assert anchor in original, 'influence-decision anchor not found'
    patched = original.replace(
        anchor, "          var _dTxt = String(_di);  // NC-3", 1)
    assert patched != original
    src = os.path.join(tmp_path, 'brain-nc3.js')
    with open(src, 'w', encoding='utf-8') as f:
        f.write(patched)
    out = _run(brain=src)
    assert out['infNoObjectObject'] is False, \
        f'NC-3: stringifying the structured decision must bring back [object Object]: {out}'
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC1_dropping_the_head_row_breaks_the_summary_headline(tmp_path):
    """NC-1: neuter the head-row branch (no badge/summary headline) → the
    summary-headline assertion fails. Shipped file byte-identical after."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = "        if (dKind || dSummary) {"
    assert anchor in original, 'head-row anchor not found'
    patched = original.replace(anchor, "        if (false) {  // NC-1", 1)
    assert patched != original
    src = os.path.join(tmp_path, 'brain-nc1.js')
    with open(src, 'w', encoding='utf-8') as f:
        f.write(patched)
    out = _run(brain=src)
    assert out['headPresent'] is False, \
        f'NC-1: dropping the head-row branch must remove the summary headline: {out}'
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC2_dropping_the_warn_branch_breaks_the_no_goal_strip(tmp_path):
    """NC-2: neuter the health-warn branch → a charter with NO north star
    renders no warning → the no-goal strip assertion fails. Byte-identical."""
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        original = f.read()
    anchor = "    if (health && !health.contentSet) {"
    assert anchor in original, 'health-warn anchor not found'
    patched = original.replace(anchor, "    if (false) {  // NC-2", 1)
    assert patched != original
    src = os.path.join(tmp_path, 'brain-nc2.js')
    with open(src, 'w', encoding='utf-8') as f:
        f.write(patched)
    out = _run(brain=src)
    assert out['noGoalWarnPresent'] is False, \
        f'NC-2: dropping the warn branch must hide the no-goal strip: {out}'
    with open(_BRAIN_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped project-brain.js must be byte-identical'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
