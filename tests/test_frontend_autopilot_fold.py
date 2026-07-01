"""jsdom regression for the autopilot run-fold DOM grouping.

WHY
---
`_applyAutopilotRunFolds` (static/js/ui/chat_render.js) collapses a CONCLUDED
autopilot run's VU<->agent transcript into one `<details>`. Two paths:

  • clean TASK_DONE → a `data-ap-summary` element anchors the fold's end;
  • manual-stop (Stop / new user message) → NO summary anchor, so the fold
    walks the contiguous range of THIS run's stamped turns + interleaved
    un-stamped worker turns and MUST stop at the next real human turn.

That manual-stop range-walk (`nextIsRun || nextIsWorker`) is exactly the kind
of DOM heuristic that silently swallows the following human turn if the
boundary logic is wrong. This harness loads the REAL shipped chat_render.js
under jsdom and asserts the fold stops at the run boundary in both modes —
and, decisively, that it does NOT swallow the human message after a
manual-stop run, nor a back-to-back second run.

Skips cleanly when node + jsdom aren't installed.
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


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.setTimeout = win.setTimeout = (fn) => 0;
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => 0;

// ── Minimal globals chat_render.js touches at LOAD time / inside the fold fn ──
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
// t(): return the en-ish value by echoing the key (the fold only checks
// "t(k) !== k" to decide whether to use the translation; returning the key
// means it falls back to the hardcoded English label, which is fine).
win.t = global.t = (k) => k;
win.CSS = global.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&') };
win.activeStreams = global.activeStreams = new Map();
win.conversations = global.conversations = [];
// This suite validates the DOM boundary-WALK (which turns land in the fold),
// which only runs once a run is concluded. Fold gating is now
// BACKEND-AUTHORITATIVE (_apRunConcluded reads conv.autopilotSummaries[runId]
// .status), so provide a conv that marks every run id in these fixtures as
// concluded — R1/R3/R4/R5 as manual stops (reason=stopped, no report → the
// range-walk + Summarize affordance path), R2 as a clean close-out
// (reason=task_done, WITH a legacy data-ap-summary anchor element).
win._foldConv = {
  id: 'fold-conv', messages: [], activeTaskId: null,
  autopilotSummaries: {
    R1: { runId: 'R1', status: 'concluded', reason: 'stopped', ts: 1 },
    R2: { runId: 'R2', status: 'concluded', reason: 'task_done', content: 'SUMMARY REPORT', ts: 1 },
    R3: { runId: 'R3', status: 'concluded', reason: 'stopped', ts: 1 },
    R4: { runId: 'R4', status: 'concluded', reason: 'stopped', ts: 1 },
    R5: { runId: 'R5', status: 'concluded', reason: 'stopped', ts: 1 },
  },
};
win.getActiveConv = global.getActiveConv = () => win._foldConv;

// chat_render.js is large and references many helpers; stub the ones touched
// at module-load (top-level const/function bodies don't run, but a few module
// consts call out). Provide broad no-op stubs to be safe.
const _noop = () => '';
for (const name of [
  'renderMarkdown','safeHtml','raw','renderToolRoundsHTML','getToolRoundsFromMsg',
  'renderFinishInfo','renderMcpLoginHintHtml','renderTurnProvenanceHtml',
  'renderPreferenceLearnedHtml','_buildSwarmInboxChipsHTML','renderTurnCtxNote',
  '_injectAnchoredBranches','stripNoTranslateTags','buildTurnNav','_forceScrollToBottom',
  '_prefetchConvCosts','_prefetchConvFileChanges','_stampFreshness','scrollToBottom',
  'isNearBottom','showStreamingUIForConv','_ensureLazyObserver','_destroyLazyObserver',
  'ConvCache','saveConversations','_buildConvConfig','renderChat',
]) {
  if (typeof win[name] === 'undefined') { win[name] = global[name] = _noop; }
}
win.BASE_PATH = global.BASE_PATH = '';
win._INITIAL_RENDER = global._INITIAL_RENDER = 20;

eval(fs.readFileSync(process.argv[2], 'utf8'));  // ui/chat_render.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _applyAutopilotRunFolds !== 'function') {
  console.log('FAIL fn_exposed _applyAutopilotRunFolds missing');
  process.exit(0);
}
check('fn_exposed', true);

// Helper: build a .message element with optional run/summary attrs + class.
function mkMsg(id, opts) {
  opts = opts || {};
  const el = document.createElement('div');
  el.className = 'message' + (opts.user ? ' user-msg' : '');
  el.id = 'msg-' + id;
  el.setAttribute('data-msg-id', 'm' + id);
  if (opts.run) el.setAttribute('data-ap-run', opts.run);
  if (opts.summary) el.setAttribute('data-ap-summary', '1');
  el.textContent = opts.text || ('msg' + id);
  return el;
}

// ════════════════════════════════════════════════════════════════════
// Case 1 — MANUAL-STOP run (no summary): fold must stop at the next human
//          turn and NOT swallow it.
//   DOM order: human(0) | VU(1,run=R) worker(2) VU(3,run=R) | human(4)
// ════════════════════════════════════════════════════════════════════
{
  const inner = document.getElementById('chatInner');
  inner.innerHTML = '';
  inner.appendChild(mkMsg(0, { user: true, text: 'original objective' }));
  inner.appendChild(mkMsg(1, { run: 'R1', user: true, text: 'VU keep going' }));
  inner.appendChild(mkMsg(2, { text: 'worker reply A' }));        // un-stamped worker
  inner.appendChild(mkMsg(3, { run: 'R1', user: true, text: 'VU again' }));
  inner.appendChild(mkMsg(4, { user: true, text: 'NEW HUMAN TURN' }));

  _applyAutopilotRunFolds(inner);

  const fold = inner.querySelector('details.autopilot-run-fold[data-ap-run-fold="R1"]');
  check('c1_fold_created', !!fold);
  // The fold must contain exactly the 3 run turns (VU1, worker, VU3).
  const folded = fold ? fold.querySelectorAll('.message').length : -1;
  check('c1_fold_has_3_turns', folded === 3);
  // The NEW HUMAN TURN must NOT be inside the fold (the boundary bug).
  const humanInFold = fold ? !!fold.querySelector('#msg-4') : true;
  check('c1_human_not_swallowed', !humanInFold);
  // The human turn must still be a direct child of chatInner, AFTER the fold.
  const human4 = document.getElementById('msg-4');
  check('c1_human_is_sibling', human4 && human4.parentNode === inner);
  // The original objective (un-stamped, before the run) stays outside/above.
  const human0 = document.getElementById('msg-0');
  check('c1_objective_outside', human0 && human0.parentNode === inner
        && !human0.closest('.autopilot-run-fold'));
  // A manual-stop fold offers the "Summarize this run" affordance.
  check('c1_summarize_btn', fold && !!fold.querySelector('.apf-summarize-btn'));
}

// ════════════════════════════════════════════════════════════════════
// Case 2 — CLEAN TASK_DONE run (summary anchor present): fold stops before
//          the summary; summary stays the visible tail (outside the fold).
//   DOM order: human(0) | VU(1,run=R) worker(2) | summary(3,run=R,sum) | human(4)
// ════════════════════════════════════════════════════════════════════
{
  const inner = document.getElementById('chatInner');
  inner.innerHTML = '';
  inner.appendChild(mkMsg(0, { user: true, text: 'objective' }));
  inner.appendChild(mkMsg(1, { run: 'R2', user: true, text: 'VU' }));
  inner.appendChild(mkMsg(2, { text: 'worker' }));
  inner.appendChild(mkMsg(3, { run: 'R2', summary: true, text: 'SUMMARY REPORT' }));
  inner.appendChild(mkMsg(4, { user: true, text: 'follow up' }));

  _applyAutopilotRunFolds(inner);

  const fold = inner.querySelector('details.autopilot-run-fold[data-ap-run-fold="R2"]');
  check('c2_fold_created', !!fold);
  // Fold contains the 2 transcript turns (VU + worker), NOT the summary.
  check('c2_fold_has_2_turns', fold && fold.querySelectorAll('.message').length === 2);
  const summary3 = document.getElementById('msg-3');
  check('c2_summary_outside_fold', summary3 && !summary3.closest('.autopilot-run-fold'));
  check('c2_summary_is_sibling', summary3 && summary3.parentNode === inner);
  // A clean-done fold does NOT offer the summarize button (already summarized).
  check('c2_no_summarize_btn', fold && !fold.querySelector('.apf-summarize-btn'));
  // Follow-up human turn untouched.
  const human4 = document.getElementById('msg-4');
  check('c2_human_sibling', human4 && human4.parentNode === inner
        && !human4.closest('.autopilot-run-fold'));
}

// ════════════════════════════════════════════════════════════════════
// Case 3 — BACK-TO-BACK manual-stop runs: each folds independently, the
//          second run is NOT swallowed into the first.
//   DOM: human(0) | VU(1,R3) worker(2) | human(3) | VU(4,R4) worker(5) | human(6)
// ════════════════════════════════════════════════════════════════════
{
  const inner = document.getElementById('chatInner');
  inner.innerHTML = '';
  inner.appendChild(mkMsg(0, { user: true, text: 'obj' }));
  inner.appendChild(mkMsg(1, { run: 'R3', user: true, text: 'VU r3' }));
  inner.appendChild(mkMsg(2, { text: 'worker r3' }));
  inner.appendChild(mkMsg(3, { user: true, text: 'human between runs' }));
  inner.appendChild(mkMsg(4, { run: 'R4', user: true, text: 'VU r4' }));
  inner.appendChild(mkMsg(5, { text: 'worker r4' }));
  inner.appendChild(mkMsg(6, { user: true, text: 'final human' }));

  _applyAutopilotRunFolds(inner);

  const f3 = inner.querySelector('details[data-ap-run-fold="R3"]');
  const f4 = inner.querySelector('details[data-ap-run-fold="R4"]');
  check('c3_both_folds', !!f3 && !!f4);
  check('c3_r3_has_2', f3 && f3.querySelectorAll('.message').length === 2);
  check('c3_r4_has_2', f4 && f4.querySelectorAll('.message').length === 2);
  // The human turn between the two runs must NOT be in either fold.
  const human3 = document.getElementById('msg-3');
  check('c3_between_human_free', human3 && human3.parentNode === inner
        && !human3.closest('.autopilot-run-fold'));
  // R3's fold must not contain R4's turns.
  check('c3_no_cross_swallow', f3 && !f3.querySelector('[data-ap-run="R4"]'));
}

// ════════════════════════════════════════════════════════════════════
// Case 4 — idempotent: running the pass twice does NOT double-wrap.
// ════════════════════════════════════════════════════════════════════
{
  const inner = document.getElementById('chatInner');
  inner.innerHTML = '';
  inner.appendChild(mkMsg(0, { user: true, text: 'obj' }));
  inner.appendChild(mkMsg(1, { run: 'R5', user: true, text: 'VU' }));
  inner.appendChild(mkMsg(2, { text: 'worker' }));
  inner.appendChild(mkMsg(3, { user: true, text: 'human' }));

  _applyAutopilotRunFolds(inner);
  _applyAutopilotRunFolds(inner);  // second pass — must be a no-op

  const folds = inner.querySelectorAll('details[data-ap-run-fold="R5"]');
  check('c4_single_fold', folds.length === 1);
  check('c4_no_nested', !inner.querySelector('.autopilot-run-fold .autopilot-run-fold'));
}

console.log(out.join('\n'));
"""


def _run():
    harness = os.path.join(HERE, '_autopilot_fold_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'chat_render.js'),   # argv[2]
             ROOT,                                            # argv[3]
             ],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'autopilot-fold failures:\n' + output
    assert output.count('PASS') >= 18, f'expected >=18 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_autopilot_run_fold_boundaries():
    _run()
