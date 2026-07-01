"""jsdom regression: autopilot run-fold is gated ONLY on the BACKEND-authoritative
concluded record + human-only summary PANEL (sidecar, not a chat message).

WHY
---
The fold decision is now CENTRALIZED in the backend. The frontend
(`_apRunConcluded` in static/js/ui/chat_render.js) folds a run iff:
  (a) the backend wrote a concluded record for it
      (`conv.autopilotSummaries[runId].status === 'concluded'`) — covering BOTH
      a clean [VU: TASK_DONE] (reason=task_done, with a report) AND a manual
      Stop / toggle-off / supersede (reason=stopped, NO report); OR
  (b) a NEWER run exists after it (structurally superseded).

Crucially, the frontend NO LONGER reads `activeStreams` / `conv.activeTaskId` /
the pending follow-up carrier — that inference WAS the inter-turn-gap mis-fold
bug (between turns the stream is briefly gone AND activeTaskId briefly null, so
"nothing in flight" was ALSO true mid-run → premature fold). Now the gap simply
has no concluded record, so the last run stays expanded until the backend says
the whole run ended.

The run summary/record is a HUMAN-ONLY sidecar (`conv.autopilotSummaries[runId]`),
rendered as the fold's read-only report PANEL (`_buildApSummaryPanel`) — it must
NOT be a chat message in `conv.messages`. A manual-stop record folds the run but
has NO report content, so no panel appears (the "Summarize this run" affordance
does instead).

This harness loads the REAL shipped chat_render.js under jsdom.
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

win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
win.t = global.t = (k) => k;
win.CSS = global.CSS = { escape: (s) => String(s).replace(/[^a-zA-Z0-9_-]/g, '\\$&') };
win.activeStreams = global.activeStreams = new Map();
win.conversations = global.conversations = [];
win.getActiveConv = global.getActiveConv = () => null;
// renderMarkdown for the summary panel body.
win.renderMarkdown = global.renderMarkdown = (s) => '<p class="md">' + String(s||'') + '</p>';
// convAutoTranslate gate (default: OFF — show original content).
win._convAutoTranslateOn = false;
win.convAutoTranslate = global.convAutoTranslate = (conv) => win._convAutoTranslateOn;
// Controllable pending-carrier signal (the inter-turn gap marker).
win._pendingCarrier = null;
win._findAutopilotPendingCarrier = global._findAutopilotPendingCarrier =
  (conv) => win._pendingCarrier;

const _noop = () => '';
for (const name of [
  'safeHtml','raw','renderToolRoundsHTML','getToolRoundsFromMsg',
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
// Build a DOM for ONE in-progress run: human(0) | VU(1,R) worker(2)
function buildRunDom() {
  const inner = document.getElementById('chatInner');
  inner.innerHTML = '';
  inner.appendChild(mkMsg(0, { user: true, text: 'objective' }));
  inner.appendChild(mkMsg(1, { run: 'RX', user: true, text: 'VU keep going' }));
  inner.appendChild(mkMsg(2, { text: 'worker reply' }));
  return inner;
}

// ════════════════════════════════════════════════════════════════════
// DECISIVE 1 — INTER-TURN GAP: NO backend concluded record yet → the run
//   must NOT fold. To PROVE the frontend no longer infers run-end from
//   stream/task/carrier state, we set the MOST "concluded-looking" client
//   state possible (no stream, no activeTaskId, no pending carrier) — the
//   run STILL must not fold, because the backend never said it concluded.
// ════════════════════════════════════════════════════════════════════
{
  const inner = buildRunDom();
  const conv = { id: 'c1', messages: [], activeTaskId: null, autopilotSummaries: {} };
  win.activeStreams = global.activeStreams = new Map();   // no live stream
  win._pendingCarrier = null;                             // no pending carrier

  _applyAutopilotRunFolds(inner, conv);

  const fold = inner.querySelector('details.autopilot-run-fold[data-ap-run-fold="RX"]');
  check('gap_no_fold', !fold);
  // The run turns remain direct children (fully expanded).
  check('gap_turns_unfolded',
        document.getElementById('msg-1').parentNode === inner &&
        document.getElementById('msg-2').parentNode === inner);
}

// ════════════════════════════════════════════════════════════════════
// DECISIVE 1b — GAP is INDEPENDENT of client stream/task state: even with
//   a live stream AND activeTaskId AND a pending carrier set, a run with a
//   concluded record folds; and (the mirror) a run with NO record never
//   folds no matter the client state. Here: record present + "busy" client
//   state → folds (the fold does NOT wait on client quiescence).
// ════════════════════════════════════════════════════════════════════
{
  const inner = buildRunDom();
  const conv = {
    id: 'c1b', messages: [], activeTaskId: 'task-next',
    autopilotSummaries: { RX: { runId: 'RX', status: 'concluded', reason: 'stopped', ts: 1 } },
  };
  win.activeStreams = global.activeStreams = new Map([['c1b', {}]]);  // "live"
  win._pendingCarrier = { msg: {}, idx: 0 };                          // "pending"

  _applyAutopilotRunFolds(inner, conv);

  check('record_folds_regardless_of_client_state',
        !!inner.querySelector('details.autopilot-run-fold[data-ap-run-fold="RX"]'));
}

// ════════════════════════════════════════════════════════════════════
// DECISIVE 2 — CLEAN CLOSE-OUT: a concluded record WITH a report arrived
//   (reason=task_done) → fold ONCE, the read-only report PANEL appears,
//   and it is NOT a chat message.
// ════════════════════════════════════════════════════════════════════
{
  const inner = buildRunDom();
  const conv = {
    id: 'c2', messages: [], activeTaskId: null,
    autopilotSummaries: { RX: { runId: 'RX', status: 'concluded', reason: 'task_done',
                                content: 'Outcome: shipped the exporter.', ts: 1 } },
  };
  win.activeStreams = global.activeStreams = new Map();
  win._pendingCarrier = null;

  _applyAutopilotRunFolds(inner, conv);

  const folds = inner.querySelectorAll('details.autopilot-run-fold[data-ap-run-fold="RX"]');
  check('done_fold_once', folds.length === 1);
  // The report PANEL exists, keyed by runId.
  const panel = inner.querySelector('.autopilot-summary-panel[data-ap-summary-run="RX"]');
  check('done_panel_present', !!panel);
  // Panel renders the summary content + the "for you only" framing.
  check('done_panel_content', panel && panel.textContent.indexOf('shipped the exporter') !== -1);
  check('done_panel_human_only',
        panel && !!panel.querySelector('.aps-private'));
  // The panel sits AFTER the fold (visible tail), and is NOT a .message.
  check('done_panel_after_fold',
        panel && folds[0].nextElementSibling === panel);
  check('done_panel_not_a_message',
        panel && !panel.classList.contains('message'));
  // A clean-done fold does NOT offer the summarize button.
  check('done_no_summarize_btn', folds[0] && !folds[0].querySelector('.apf-summarize-btn'));
  // ★ The summary is NOT in conv.messages (human-only sidecar).
  check('done_summary_not_in_messages',
        !conv.messages.some(m => m && (m._isAutopilotSummary || (m.content||'').indexOf('shipped the exporter') !== -1)));
}

// ════════════════════════════════════════════════════════════════════
// DECISIVE 2b — re-render is idempotent: a second pass does not stack a
//   second report panel for the same run.
// ════════════════════════════════════════════════════════════════════
{
  const inner = document.getElementById('chatInner');  // reuse c2's DOM
  const conv = {
    id: 'c2', messages: [], activeTaskId: null,
    autopilotSummaries: { RX: { runId: 'RX', status: 'concluded', reason: 'task_done',
                                content: 'Outcome: shipped the exporter.', ts: 1 } },
  };
  _applyAutopilotRunFolds(inner, conv);  // second pass (fold already there)
  check('done_single_panel',
        inner.querySelectorAll('.autopilot-summary-panel[data-ap-summary-run="RX"]').length === 1);
}

// ════════════════════════════════════════════════════════════════════
// DECISIVE 3a — MANUAL STOP with NO backend record yet (e.g. the disarm
//   response hasn't landed) → the run must NOT fold, even though the
//   client is fully idle. This is the crux: idleness alone is NOT a fold
//   signal anymore — only the backend record is.
// ════════════════════════════════════════════════════════════════════
{
  const inner = buildRunDom();
  const conv = { id: 'c3a', messages: [], activeTaskId: null, autopilotSummaries: {} };
  win.activeStreams = global.activeStreams = new Map();
  win._pendingCarrier = null;

  _applyAutopilotRunFolds(inner, conv);

  check('manual_idle_no_record_no_fold',
        !inner.querySelector('details.autopilot-run-fold[data-ap-run-fold="RX"]'));
}

// ════════════════════════════════════════════════════════════════════
// DECISIVE 3b — MANUAL-STOP CONCLUDED: the backend wrote a concluded
//   record with reason=stopped and NO report content → fold ONCE with a
//   "Summarize this run" affordance and NO report panel.
// ════════════════════════════════════════════════════════════════════
{
  const inner = buildRunDom();
  const conv = {
    id: 'c3', messages: [], activeTaskId: null,
    autopilotSummaries: { RX: { runId: 'RX', status: 'concluded', reason: 'stopped', ts: 1 } },
  };
  win.activeStreams = global.activeStreams = new Map();
  win._pendingCarrier = null;

  _applyAutopilotRunFolds(inner, conv);

  const fold = inner.querySelector('details.autopilot-run-fold[data-ap-run-fold="RX"]');
  check('manual_fold_once', !!fold);
  check('manual_summarize_btn', fold && !!fold.querySelector('.apf-summarize-btn'));
  check('manual_no_panel',
        !inner.querySelector('.autopilot-summary-panel[data-ap-summary-run="RX"]'));
  // ★ The concluded record is NOT a chat message (human-only sidecar).
  check('manual_record_not_in_messages',
        !conv.messages.some(m => m && m._isAutopilotSummary));
}

// ════════════════════════════════════════════════════════════════════
// DECISIVE 4 — SUPERSEDED run: an earlier run with a newer run after it
//   folds even while the LAST run is still in flight (pending carrier).
//   DOM: human(0) | VU(1,RA) worker(2) | human(3) | VU(4,RB) worker(5)
// ════════════════════════════════════════════════════════════════════
{
  const inner = document.getElementById('chatInner');
  inner.innerHTML = '';
  inner.appendChild(mkMsg(0, { user: true, text: 'obj' }));
  inner.appendChild(mkMsg(1, { run: 'RA', user: true, text: 'VU a' }));
  inner.appendChild(mkMsg(2, { text: 'worker a' }));
  inner.appendChild(mkMsg(3, { user: true, text: 'human between' }));
  inner.appendChild(mkMsg(4, { run: 'RB', user: true, text: 'VU b' }));
  inner.appendChild(mkMsg(5, { text: 'worker b' }));
  const conv = { id: 'c4', messages: [], activeTaskId: null, autopilotSummaries: {} };
  win.activeStreams = global.activeStreams = new Map();
  win._pendingCarrier = { msg: {}, idx: 0 };   // last run (RB) still pending

  _applyAutopilotRunFolds(inner, conv);

  // RA is superseded → folds. RB is the last run + pending → must NOT fold.
  check('super_RA_folded', !!inner.querySelector('details[data-ap-run-fold="RA"]'));
  check('super_RB_not_folded', !inner.querySelector('details[data-ap-run-fold="RB"]'));
}

console.log(out.join('\n'));
"""


def _run():
    harness = os.path.join(HERE, '_autopilot_collapse_gating_harness.js')
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
    assert not fails, 'autopilot-collapse-gating failures:\n' + output
    assert output.count('PASS') >= 18, f'expected >=18 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_autopilot_collapse_gating():
    _run()
