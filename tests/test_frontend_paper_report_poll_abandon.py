"""jsdom regression test for the Reading-mode "Generate / Regenerate / rebuttal
button freezes after a few clicks" bug.

Reported flow: in the paper reader's Report/Review tab, clicking
Regenerate (or toggling the Review↔Rebuttal segments) a few times while a
generation is in flight eventually leaves the button dead.

Root cause: ``_pollReportTask`` captures ``s = view.stream`` at entry and, on
every tick, unconditionally reschedules ``setTimeout(_pollReportTask, …)`` while
``s.status === 'running'``. But a force-regenerate / paper switch / reset
REPLACES ``view.stream`` with a brand-new object (``_resetReportLocalState`` +
``_makeReportStreamState``). An in-flight poll captured on the OLD stream then
resumes and:
  • repaints against a dead stream, and
  • schedules ANOTHER poll chain — now reading the NEW ``view.stream`` — so the
    new task is driven by two (or more) timer chains that race each other's
    repaints. The compounding duplicate chains + stale-closure repaints are the
    mechanism behind the wedged toolbar (Stop disabled + Regenerate hidden,
    status stuck ``running``).

The fix adds a stream-identity ABANDON guard: ``_pollReportTask`` bails the
instant ``view.stream !== s`` (its stream was replaced), and only reschedules
while it still owns the active stream. On any terminal status it nulls
``pollTimer`` so the single-live-chain invariant (``!view.stream.pollTimer``,
relied on by ``_loadOrGenerateReport`` / ``_restoreRebuttalPanel``) reads true.

Harness: loads the REAL shipped ``static/js/paper/report.js`` under jsdom, stubs
a controllable ``Api.paper.reportPoll`` whose resolution the test gates by hand
(so a poll can be left "in flight" across a stream swap), and asserts an
orphaned poll neither repaints the new stream nor spawns a duplicate timer.

Negative control (source-level): the SAME harness against a COPY of report.js
with the abandon guard reverted must FAIL the decisive check. The shipped file
is never modified. Skips cleanly when node + jsdom aren't installed.
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
REPORT_JS = os.path.join(JS_DIR, 'paper', 'report.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body>' +
  '<div id="paperReportContent"></div>' +
  '<button id="paperReportStopBtn" style="display:none"><span>Stop</span></button>' +
  '<button id="paperReportRegenBtn"></button>' +
  '</body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
win.t = global.t = (k) => k;
global.escapeHtml = win.escapeHtml = (s) => String(s == null ? '' : s);
// Neutralise the heavy repaint path — we only care about poll-loop control flow.
global.renderMarkdown = win.renderMarkdown = (s) => String(s || '');
global._activePaperId = win._activePaperId = 'p1';

// Count how many times each stream's events get applied so we can detect a
// dead (orphaned) stream being polled again, and a controllable poll promise.
let pollCalls = [];
let pending = null;           // {resolve} for the currently in-flight poll
function makePollResponse(status) {
  return { ok: true, status: 200, json: async () => ({
    ok: true, events: [], next_cursor: 1, status: status,
  }) };
}
global.Api = win.Api = { paper: {
  reportPoll: (taskId, cursor) => {
    pollCalls.push(taskId);
    return new Promise((resolve) => { pending = { resolve, taskId }; });
  },
}};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // paper/report.js (real / patched)

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
function tick() { return new Promise((r) => setTimeout(r, 0)); }

const view = {
  kind: 'report',
  idPrefix: 'report',
  containerId: 'paperReportContent',
  stopBtnId: 'paperReportStopBtn',
  regenBtnId: 'paperReportRegenBtn',
  regenIntentKey: 'k',
  langKey: () => 'en',
  uiLang: () => 'en',
  stream: null,
  cache: '',
  meta: null,
};

(async () => {
  // ── 1. Start polling on stream A (task-A). One poll goes in flight.
  const streamA = _makeReportStreamState('p1', 'en', 'task-A', 'report');
  view.stream = streamA;
  _pollReportTask(view);
  await tick();
  check('pollA_in_flight', pending && pending.taskId === 'task-A');
  check('pollA_busy', streamA.pollBusy === true);

  // ── 2. Simulate a force-regenerate WHILE poll A is in flight: view.stream is
  //    replaced with a fresh stream B (this is exactly what
  //    _resetReportLocalState + _makeReportStreamState do on regenerate).
  const streamB = _makeReportStreamState('p1', 'en', 'task-B', 'report');
  view.stream = streamB;

  // ── 3. The in-flight poll A now resolves (server still says 'running').
  //    With the abandon guard, poll A must recognise it no longer owns
  //    view.stream and STOP — it must NOT schedule another poll (which would
  //    read streamB) and must NOT leave streamA driving anything.
  const resolveA = pending.resolve; pending = null;
  resolveA(makePollResponse('running'));
  await tick(); await tick();

  // ★ DECISIVE: stream A must not have scheduled a follow-up poll chain.
  //    A correctly-abandoned poll leaves streamA.pollTimer null (or, at worst,
  //    does not enqueue a new reportPoll). We assert NO new poll was fired for
  //    task-A after the swap, and that streamA is not left with a live timer.
  check('abandoned_A_no_new_timer', !streamA.pollTimer);
  const pollsForAAfterSwap = pollCalls.filter((t) => t === 'task-A').length;
  check('abandoned_A_not_repolled', pollsForAAfterSwap === 1);

  // ── 4. Stream B was never started by the orphan (no duplicate chain). The
  //    only way task-B should ever be polled is an explicit _pollReportTask(B).
  const pollsForBFromOrphan = pollCalls.filter((t) => t === 'task-B').length;
  check('no_duplicate_chain_on_B', pollsForBFromOrphan === 0);

  // ── 5. Explicitly drive stream B — the single legitimate chain. It goes in
  //    flight exactly once.
  _pollReportTask(view);
  await tick();
  check('B_polled_once', pollCalls.filter((t) => t === 'task-B').length === 1);
  check('B_in_flight', pending && pending.taskId === 'task-B');

  // ── 6. Resolve B as terminal (done) → its pollTimer must be nulled so the
  //    !pollTimer single-chain invariant reads true afterwards.
  const resolveB = pending.resolve; pending = null;
  resolveB(makePollResponse('done'));
  await tick(); await tick();
  check('B_terminal_no_timer', !streamB.pollTimer);
  check('B_status_done', streamB.status === 'done');

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


def _run(js_path: str):
    harness = os.path.join(HERE, '_paper_poll_abandon_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, js_path, ROOT],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return [ln for ln in output.splitlines() if ln.startswith(('PASS ', 'FAIL '))]


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_orphaned_poll_does_not_duplicate_or_wedge():
    lines = _run(REPORT_JS)
    fails = [ln for ln in lines if ln.startswith('FAIL')]
    assert not fails, 'paper report poll-abandon failures:\n' + '\n'.join(lines)
    assert len(lines) >= 9, 'expected >=9 result lines, got:\n' + '\n'.join(lines)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_negative_control_revert_reintroduces_duplicate_chain():
    """Prove the test catches the bug: revert the abandon guard in a COPY and the
    decisive checks must FAIL (orphan re-polls / spawns a duplicate chain). The
    shipped report.js is never modified."""
    src = open(REPORT_JS, encoding='utf-8').read()
    marker = 'if (view.stream !== s) return;'
    assert marker in src, 'abandon-guard marker not found — did _pollReportTask change?'
    # Revert the entry-point abandon guard.
    patched = src.replace(marker, 'if (false) return;', 1)
    # Also revert the reschedule identity checks so the orphan reschedules.
    patched = patched.replace(
        "if (s.status === 'running' && view.stream === s) {",
        "if (s.status === 'running') {")
    patched = patched.replace(
        "if (s && s.status === 'running' && view.stream === s) {",
        "if (s && s.status === 'running') {")
    assert patched != src

    tmp = os.path.join(HERE, '_report_poll_abandon_reverted.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(patched)
    try:
        lines = _run(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    joined = '\n'.join(lines)
    # Under the reverted (buggy) code the orphaned poll reschedules → a new poll
    # chain drives streamB, so task-B is polled by the orphan before we ever
    # explicitly drive it.
    assert ('FAIL no_duplicate_chain_on_B' in joined
            or 'FAIL abandoned_A_no_new_timer' in joined), \
        'negative control did not fail as expected:\n' + joined
