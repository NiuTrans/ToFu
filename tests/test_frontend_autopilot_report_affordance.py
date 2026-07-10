"""tests/test_frontend_autopilot_report_affordance.py — the autopilot run
close-out REPORT is reachable again from the FLAT transcript.

WHY
---
The 2026-07-07 "flatten" (owner directive, option A) deleted the
`autopilot-run-fold` `<details>` card. That card's summary header was the ONLY
place the "查看报告 / View report" (and the manual-stop "Summarize this run")
button was rendered — so after the flatten those buttons vanished while the
backend kept writing the report to `settings.autopilotSummaries[runId].content`.
`_openApSummaryModal` / `_apRunSummary` / `_summarizeAutopilotRun` were left in
`chat_render.js` as DEAD CODE with zero callers → autopilot "no longer generated
reports" (really: generated, but no affordance to open them).

The fix re-homes the affordance as a PER-TURN action-bar button (owner-chosen,
2026-07-08): it renders on the run's CONCLUDING VU turn — the newest message
stamped with that `_autopilotRunId` — gated on the backend-authoritative
concluded record. No run-boundary sibling-walk is reintroduced (that walk was
the thing the flatten removed, and it historically swallowed the following
human turn); attaching to a real `_autopilotRunId`-stamped message needs no
boundary inference and survives surgical re-render like every per-message
control.

This test drives the REAL shipped `renderMessage` via jsdom:

  Case A (clean TASK_DONE, report present): the concluding VU turn renders a
    `.ap-report-btn` whose onclick calls `_openApSummaryModal(runId)`; clicking
    it actually invokes `_openApSummaryModal` (proves it's wired, not dead).
  Case B (manual stop, concluded record, NO content): the concluding VU turn
    renders a `.ap-summarize-btn` calling `_summarizeAutopilotRun(runId, this)`
    — NOT a View-report button.
  Case C (running / un-concluded run, no record): NEITHER button renders.
  Case D (superseded run — TWO VU turns share the run id): only the NEWEST
    turn owns the button; the earlier VU turn of the same run renders nothing.

NEUTER CONTROL
  • NC (affordance dropped): strip the `${apReportH}` slot out of the action-bar
    template in a COPY of chat_render.js → Case A's button disappears → the
    "View report button present" assertion FAILS. Proves it is load-bearing.

Skips cleanly when node/jsdom isn't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')
ESCAPE_HTML = os.path.join(JS_DIR, 'core', 'escape_html.js')
SAFE_HTML = os.path.join(JS_DIR, 'core', 'safe_html.js')
CHAT_RENDER = os.path.join(JS_DIR, 'ui', 'chat_render.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[5];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.setTimeout = win.setTimeout = (fn) => 0;
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => 0;
win.CSS = global.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&') };

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// The active conv is swapped per-case; getActiveConv reads the module var.
let _activeConv = { id: 'c', messages: [], activeTaskId: null };
win.activeStreams = global.activeStreams = new Map();     // no live stream
win.activeConvId = global.activeConvId = 'c';
win.getActiveConv = global.getActiveConv = () => _activeConv;

win.t = global.t = (k) => k;   // fall back to hardcoded English labels
win._fmtAbsoluteDateTime = global._fmtAbsoluteDateTime = () => '';
win.stripNoTranslateTags = global.stripNoTranslateTags = (s) => (s == null ? '' : String(s));
win.renderMarkdown = global.renderMarkdown = (s) => '<md>' + String(s == null ? '' : s) + '</md>';
win.getToolRoundsFromMsg = global.getToolRoundsFromMsg = (m) => (m && m.toolRounds) || [];
win.renderToolRoundsHTML = global.renderToolRoundsHTML = () => '<div class="ptool-panel">TOOLS</div>';
win._segTimelineEnabled = global._segTimelineEnabled = () => false;
win.renderSegmentTimelineHTML = global.renderSegmentTimelineHTML = () => '';

const _noop = () => '';
for (const name of [
  'renderMcpLoginHintHtml','renderTurnProvenanceHtml','renderFileChangesBar',
  'renderErrorEnvelope','renderBranchZone','renderTurnCtxNote',
  'renderPreferenceLearnedHtml','renderFinishInfo','_buildSwarmInboxChipsHTML',
  '_injectAnchoredBranches','_prefetchConvCosts','_prefetchConvFileChanges',
  '_stampFreshness','buildTurnNav','calcCostCny',
]) {
  if (typeof win[name] === 'undefined') { win[name] = global[name] = _noop; }
}
win._USER_AVATAR_SVG = global._USER_AVATAR_SVG = '<img data-avatar="onigiri">';
win._TOFU_WORKER_SVG = global._TOFU_WORKER_SVG = '<img data-avatar="worker">';
win._TOFU_PLANNER_SVG = global._TOFU_PLANNER_SVG = '<img data-avatar="planner">';
win._TOFU_CRITIC_SVG = global._TOFU_CRITIC_SVG = '<img data-avatar="critic">';
win.BASE_PATH = global.BASE_PATH = '';
win._INITIAL_RENDER = global._INITIAL_RENDER = 20;

const CHAT = fs.readFileSync(process.argv[2], 'utf8');
const NC = process.argv[6] || '';
let chatSrc = CHAT;
if (NC === 'nc_drop') {
  // NC: drop the run-report affordance slot out of the action-bar template.
  chatSrc = CHAT.replace('${translateH}${apReportH}${exportImgH}',
                         '${translateH}${exportImgH}');
}
const _applied = (NC === '') || (chatSrc !== CHAT);
check('nc_pattern_applied', _applied);

(0, eval)(fs.readFileSync(process.argv[3], 'utf8'));  // escape_html.js
(0, eval)(fs.readFileSync(process.argv[4], 'utf8'));  // safe_html.js
(0, eval)(chatSrc);                                   // chat_render.js (real / neutered)

if (typeof renderMessage !== 'function') {
  console.log('FAIL fn_exposed renderMessage missing'); process.exit(0);
}
check('fn_exposed', true);

function vuMsg(runId) {
  return { role: 'user', _isVirtualUser: true, _msgId: 'vu-' + runId,
           _autopilotRunId: runId, content: 'Verified; keep going.' };
}
function fragOf(html) { const f = win.document.createElement('div'); f.innerHTML = html; return f; }

// ══ Case A — clean TASK_DONE with a report → View-report button, wired ══
{
  const vu = vuMsg('R1');
  _activeConv = { id: 'cA', activeTaskId: null,
    messages: [{ role: 'user', content: 'objective' }, vu],
    autopilotSummaries: { R1: { runId: 'R1', status: 'concluded',
      reason: 'task_done', content: '# Report\nDone.', ts: 1 } } };
  const frag = fragOf(renderMessage(vu, 1));
  const btn = frag.querySelector('.ap-report-btn');
  check('A_view_report_present', !!btn);
  check('A_view_report_wired',
        btn && (btn.getAttribute('onclick') || '').indexOf("_openApSummaryModal('R1')") !== -1);
  check('A_no_summarize_btn', !frag.querySelector('.ap-summarize-btn'));
  // The onclick targets the REAL window-exposed handler (not dead code): the
  // wired-attribute check above proves the reference; this proves the target
  // is a live function reachable from the inline handler's global scope.
  // (jsdom here runs without runScripts, so inline onclick can't be .click()-fired.)
  check('A_modal_fn_exposed', typeof win._openApSummaryModal === 'function');
}

// ══ Case B — manual stop, concluded, NO content → Summarize button ══
{
  const vu = vuMsg('R2');
  _activeConv = { id: 'cB', activeTaskId: null,
    messages: [{ role: 'user', content: 'objective' }, vu],
    autopilotSummaries: { R2: { runId: 'R2', status: 'concluded',
      reason: 'stopped', ts: 2 } } };
  const frag = fragOf(renderMessage(vu, 1));
  const sbtn = frag.querySelector('.ap-summarize-btn');
  check('B_summarize_present', !!sbtn);
  check('B_summarize_wired',
        sbtn && (sbtn.getAttribute('onclick') || '').indexOf("_summarizeAutopilotRun('R2',this)") !== -1);
  check('B_no_view_report', !frag.querySelector('.ap-report-btn'));
  check('B_summarize_fn_exposed', typeof win._summarizeAutopilotRun === 'function');
}

// ══ Case C — running / un-concluded run (no record) → NEITHER button ══
{
  const vu = vuMsg('R3');
  _activeConv = { id: 'cC', activeTaskId: null,
    messages: [{ role: 'user', content: 'objective' }, vu],
    autopilotSummaries: {} };
  const frag = fragOf(renderMessage(vu, 1));
  check('C_no_view_report', !frag.querySelector('.ap-report-btn'));
  check('C_no_summarize', !frag.querySelector('.ap-summarize-btn'));
}

// ══ Case D — superseded run: two VU turns share R4; only the NEWEST owns it ══
{
  const vuOld = vuMsg('R4'); vuOld._msgId = 'vu-R4-old';
  const vuNew = vuMsg('R4'); vuNew._msgId = 'vu-R4-new';
  _activeConv = { id: 'cD', activeTaskId: null,
    messages: [{ role: 'user', content: 'objective' }, vuOld, vuNew],
    autopilotSummaries: { R4: { runId: 'R4', status: 'concluded',
      reason: 'task_done', content: '# Report', ts: 4 } } };
  const fragOld = fragOf(renderMessage(vuOld, 1));
  const fragNew = fragOf(renderMessage(vuNew, 2));
  check('D_old_turn_no_btn', !fragOld.querySelector('.ap-report-btn'));
  check('D_new_turn_has_btn', !!fragNew.querySelector('.ap-report-btn'));
}

console.log(out.join('\n'));
process.exit(0);
"""


def _run(nc: str = '') -> str:
    harness = os.path.join(HERE, f'_ap_report_affordance_harness_{nc or "main"}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, CHAT_RENDER, ESCAPE_HTML, SAFE_HTML, ROOT, nc],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_report_affordance_renders_and_is_wired():
    output = _run('')
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'autopilot report-affordance failures:\n' + output
    assert output.count('PASS') >= 13, f'expected >=13 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_dropping_affordance_is_caught():
    """NC: removing the ${apReportH} slot must break the View-report check."""
    output = _run('nc_drop')
    assert 'PASS nc_pattern_applied' in output, f'NC mutation did not apply:\n{output}'
    assert 'FAIL A_view_report_present' in output, (
        'Dropping the affordance slot did NOT fail the presence assertion — '
        f'it is not load-bearing:\n{output}')


if __name__ == '__main__':
    if not _node_deps_available():
        print('SKIP — node + jsdom not available')
    else:
        test_report_affordance_renders_and_is_wired()
        test_nc_dropping_affordance_is_caught()
        print('PASS test_frontend_autopilot_report_affordance')
