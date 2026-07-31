"""jsdom regression test for the paper-report "Stop button re-enabled by a poll
repaint" bug.

Reported flow: the user clicks Stop on an in-flight report generation.
``_stopPaperReport`` disables the button + shows "Stopping…", then waits for the
server's authoritative ``aborted`` event (which arrives on the NEXT poll, up to
~1.2s later). But the poll loop repaints on every tick via
``_paintReportFromState`` → ``_syncReportToolbar(status === 'running', …)``, and
until the ``aborted`` event lands the status is STILL ``running``. The old
``_syncReportToolbar`` unconditionally restored the resting state when
``running`` was true (``stopBtn.disabled = false`` + "Stop") — so the very next
poll repaint RESURRECTED a clickable Stop button, exactly the reported symptom
("the stop button can still be clicked again").

The fix threads a ``stopRequested`` flag through the stream state: set the
instant Stop is pressed, honoured by ``_syncReportToolbar`` so a ``running``
repaint keeps the button disabled + "Stopping…" instead of re-enabling it. A
fresh regenerate builds a NEW stream state, so the flag resets and Stop is
clickable again for the new run.

This file has a long history of toolbar-state regressions (the source comments
document prior "Stop had limited effect" / stuck-button bugs), so this test pins
the acceptance criterion: after Stop + a still-``running`` repaint, the button
stays disabled with the "Stopping…" label and abort is NOT re-fired.

Harness: loads the REAL shipped ``static/js/paper/report.js`` under jsdom and
drives ``_makeReportStreamState`` / ``_syncReportToolbar`` / ``_stopPaperReport``
directly with an explicit ``view`` stub (so the ``_reportView`` fallback, which
lives in paper-reader.js, is never reached) and a spy-able ``Api.paper.reportAbort``.

Negative control (automated, source-level): a second test runs the SAME harness
against a COPY of report.js with the fix reverted (``var stopping = false`` — the
old unconditional re-enable) and asserts the decisive repaint checks then FAIL.
The shipped file is never modified.

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
  '<button id="paperReportStopBtn" style="display:none"><span>Stop</span></button>' +
  '<button id="paperReportRegenBtn"></button>' +
  '</body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
// Identity t() → returns the i18n KEY, so we assert on the key the code picked
// ('paper.reportStop' vs 'paper.reportStopping') rather than translated text.
win.t = global.t = (k) => k;

// Spy-able abort endpoint. reportAbort is `.catch()`ed in the code, so it must
// return a promise.
let abortCalls = [];
global.Api = win.Api = { paper: {
  reportAbort: (taskId) => { abortCalls.push(taskId); return Promise.resolve({ ok: true }); },
}};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // paper/report.js (real / patched)

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Explicit view stub — passing it means the `view = view || _reportView(...)`
// fallback (defined in paper-reader.js, not loaded here) is never evaluated.
const view = {
  kind: 'report',
  stopBtnId: 'paperReportStopBtn',
  regenBtnId: 'paperReportRegenBtn',
  stream: null,
};
const stopBtn = document.getElementById('paperReportStopBtn');
const regenBtn = document.getElementById('paperReportRegenBtn');
function lbl() { return stopBtn.querySelector('span').textContent; }

(async () => {
  // ── 1. Fresh running task → toolbar shows an enabled, clickable "Stop".
  view.stream = _makeReportStreamState('p1', 'en', 'task-1', 'report');
  check('stream_running', view.stream.status === 'running');
  check('stream_stopRequested_false', view.stream.stopRequested === false);
  _syncReportToolbar(true, view);
  check('initial_stop_visible', stopBtn.style.display !== 'none');
  check('initial_stop_enabled', stopBtn.disabled === false);
  check('initial_stop_label', lbl() === 'paper.reportStop');

  // ── 2. User clicks Stop → abort fired once, button disabled + "Stopping…".
  _stopPaperReport(view);
  check('abort_fired_once', abortCalls.length === 1 && abortCalls[0] === 'task-1');
  check('stopRequested_set', view.stream.stopRequested === true);
  check('after_stop_disabled', stopBtn.disabled === true);
  check('after_stop_label', lbl() === 'paper.reportStopping');

  // ── 3. ★ DECISIVE: a poll repaint runs while the task is STILL `running`
  //    (the server's authoritative `aborted` event has NOT landed yet). The
  //    Stop button MUST stay disabled + "Stopping…" — it must NOT be
  //    resurrected into a second clickable Stop (the reported bug).
  _syncReportToolbar(true, view);
  check('poll_repaint_still_disabled', stopBtn.disabled === true);
  check('poll_repaint_still_stopping_label', lbl() === 'paper.reportStopping');
  check('abort_not_refired', abortCalls.length === 1);

  // A subsequent repaint (still running) keeps the state stable.
  _syncReportToolbar(true, view);
  check('second_repaint_stable', stopBtn.disabled === true && abortCalls.length === 1);

  // ── 4. Server's `aborted` lands → status flips terminal; a repaint hides
  //    Stop and reveals Regenerate.
  view.stream.status = 'aborted';
  _syncReportToolbar(false, view);
  check('terminal_stop_hidden', stopBtn.style.display === 'none');
  check('terminal_regen_shown', regenBtn.style.display !== 'none');

  // ── 5. Fresh regenerate builds a NEW stream state → stopRequested resets, so
  //    the Stop button is clickable again for the new run.
  view.stream = _makeReportStreamState('p1', 'en', 'task-2', 'report');
  check('fresh_stream_stopRequested_reset', view.stream.stopRequested === false);
  _syncReportToolbar(true, view);
  check('fresh_run_stop_enabled', stopBtn.disabled === false);
  check('fresh_run_stop_label', lbl() === 'paper.reportStop');

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


def _run(js_path: str):
    """Run the harness against ``js_path`` (report.js or a patched copy).

    Returns the list of ``PASS …`` / ``FAIL …`` result lines.
    """
    harness = os.path.join(HERE, '_paper_stop_btn_harness.js')
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
def test_stop_button_stays_disabled_across_poll_repaint():
    lines = _run(REPORT_JS)
    fails = [ln for ln in lines if ln.startswith('FAIL')]
    assert not fails, 'paper report stop-button failures:\n' + '\n'.join(lines)
    assert len(lines) >= 18, 'expected >=18 result lines, got:\n' + '\n'.join(lines)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_negative_control_revert_reenables_stop_button():
    """Prove the test actually catches the bug: revert the fix in a COPY and the
    decisive still-`running`-repaint checks must FAIL (the button is re-enabled).
    The shipped report.js is never modified."""
    src = open(REPORT_JS, encoding='utf-8').read()
    marker = 'var stopping = !!(view.stream && (view.stream.stopRequested || view.stream.pendingStop));'
    assert marker in src, 'fix marker not found — did _syncReportToolbar change?'
    # Old unconditional re-enable: `stopping` is always false when running.
    patched = src.replace(marker, 'var stopping = false;')
    assert patched != src

    tmp = os.path.join(HERE, '_report_stop_btn_reverted.js')
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
    # The decisive repaint checks must FAIL under the reverted (buggy) code.
    assert 'FAIL poll_repaint_still_disabled' in joined, \
        'negative control did not fail as expected:\n' + joined
    assert 'FAIL poll_repaint_still_stopping_label' in joined, \
        'negative control did not fail as expected:\n' + joined
