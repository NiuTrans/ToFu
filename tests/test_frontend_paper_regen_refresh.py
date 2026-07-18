"""jsdom test for the "regenerate interrupted by refresh" paper-report bug.

Reproduces the user-reported flow: in Report viewing mode the user clicks
Regenerate, then hard-refreshes BEFORE the force ``/start`` round-trip lands on
the server. The old behaviour reverted to showing only the stale DB-cached
report while the old search task kept running orphaned. The fix (A+B):

  A. ``_regeneratePaperReport`` is atomic — it relies on the backend's
     ``force=true`` /start to abort+restart in one transaction (no separate
     /abort), and ``await``s the start.
  B. Before the await it persists a "regenerate in progress for (paperHash,
     lang)" intent to localStorage. On re-entry, ``_loadOrGenerateReport``
     checks that intent at step 1.5 — BEFORE the step-2 lookup-reconnect — so a
     pending intent ALWAYS forces a fresh regenerate (force /start = atomic
     abort+restart) instead of either re-attaching to the still-running orphan
     (step 2) or rendering the stale DB cache (step 3). Attaching the new
     task_id clears the intent (re-entrancy guard).

The harness loads the REAL shipped ``static/js/paper-reader.js`` under jsdom and
covers BOTH halves of the bug:
  • lookup MISS + intent → force /start, cache NOT consulted, old report NOT
    rendered, intent cleared, new task attached;
  • lookup HIT on a still-RUNNING orphan + intent → force /start, NOT reattached
    to the orphan (the decisive step-2 priority case);
  • controls: no intent + lookup MISS → falls through to cache; no intent +
    lookup HIT running → reconnects (normal chat-roundtrip resume preserved).

Negative control (proven at source level): moving the intent check back AFTER
the step-2 lookup makes ``orphan_not_reattached`` / ``orphan_forcestart_issued``
FAIL (it reconnects to the orphan); restoring the step-1.5 priority byte-
identically makes it pass again.

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
const dom = new JSDOM(
  '<!DOCTYPE html><body>' +
  '<div id="paperReportContent"></div>' +
  '</body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.localStorage = win.localStorage;
global.console = console;

// escapeHtml is referenced by render paths; renderMarkdown is intentionally
// LEFT UNDEFINED so _renderFinalReport falls back to a simple <pre> dump,
// which keeps the harness free of the heavy TOC/figure decorators while still
// letting us detect whether the stale cached report was rendered.
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
win.t = global.t = (k) => k;

// ── Spy-able Api surface ──
const calls = { start: [], cache: 0, lookup: 0, poll: 0 };
// `lookupResult` is mutable so a scenario can flip the lookup between a MISS
// (no running task — the orphan already went terminal / never registered) and
// a HIT on the OLD orphan task that is still running after a cooperative abort.
let lookupResult = { ok: false };
const OLD_REPORT = 'OLD-CACHED-REPORT-SHOULD-NOT-SHOW';
const OLD_ORPHAN_TASK = 'rpt_OLD_orphan';
global.Api = win.Api = { paper: {
  // Return the active paper so _loadPaperLibrary() (fired at eval-time via the
  // DOMContentLoaded hook) keeps _activePaperId rather than dropping it — the
  // cross-paper guard `_activePaperId !== startPaperId` would otherwise bail
  // out of _generatePaperReport after the start round-trip.
  libraryList: async () => ({ ok: true, papers: [{ id: 'paper-1', title: 'Test Paper', paperHash: 'phash-1' }] }),
  reportLookup: async () => { calls.lookup++; return lookupResult; },
  reportCache:  async () => { calls.cache++; return { ok: true, report: OLD_REPORT, paper_hash: 'phash-1' }; },
  reportStart:  async (body) => { calls.start.push(body); return { ok: true, task_id: 'rpt_new_1', paper_hash: 'phash-1' }; },
  // Poll returns DONE immediately so the loop never reschedules a timer.
  reportPoll:   async () => { calls.poll++; return { ok: true, status: 'done', report: 'NEW', next_cursor: 0, events: [] }; },
  reportAbort:  async () => { return { ok: true }; },
}};

// Seed the persisted active-paper pointer BEFORE eval, since paper-reader.js
// fires _loadPaperLibrary() at DOMContentLoaded (eval time) and reads this key.
localStorage.setItem('paper_active_id', 'paper-1');
localStorage.setItem('paper_library_migrated_v1', '1');

eval(fs.readFileSync(process.argv[2], 'utf8'));  // paper/report.js (report/review fns)
if (process.argv[4]) eval(fs.readFileSync(process.argv[4], 'utf8'));  // paper-reader.js core

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// ── Stub the few helpers that touch unrelated subsystems ──
// _getActivePaperEntry → null so _saveActivePaperState early-returns (no
// server PUT); _generatePaperReport then falls back to _paperFileName.
_getActivePaperEntry = () => null;
// _saveActivePaperState moved to paper/library.js (not eval'd here); before the
// Epic E split it lived in paper-reader.js, so this harness relied on the real
// one. Stub it (as the sibling paper harnesses already do).
_saveActivePaperState = () => {};
_renderReportSkeleton = (container) => { if (container) container.innerHTML = '<div class="skeleton"></div>'; };
_syncReportToolbar = () => {};
_populatePaperReportModelDropdown = () => {};

// ── Seed module state as it would be right after a hard refresh ──
_paperReportStream = null;          // local poll state is gone after refresh
_paperReportCache = '';             // not yet loaded
_paperHash = 'phash-1';             // restored from the library entry
_paperParsedText = 'x'.repeat(500); // recovered paper text (skip _ensurePaperText)
_paperFileName = 'Test Paper';
_paperReportModel = 'some-model';   // skip the dropdown populate path
_activePaperId = 'paper-1';
_i18nLang = 'en';

(async () => {
  // Let the eval-time _loadPaperLibrary() (async) settle so it doesn't reset
  // _activePaperId underneath us mid-test.
  for (let i = 0; i < 10; i++) { await new Promise(r => setTimeout(r, 0)); }
  _activePaperId = 'paper-1';

  // ── Simulate: user clicked Regenerate (intent persisted) then refreshed
  //    BEFORE force /start landed. So the intent is present but NO task exists.
  _setReportRegenIntent('phash-1', 'en');
  check('intent_seeded', !!_getReportRegenIntent());

  // ── Re-entry: opening the Report tab calls _loadOrGenerateReport ──
  await _loadOrGenerateReport();
  // Let the (unawaited) _generatePaperReport(true) → reportStart resolve.
  for (let i = 0; i < 20; i++) { await new Promise(r => setTimeout(r, 0)); }

  // DECISIVE: the regenerate was RESUMED via force /start, not reverted to cache.
  check('forcestart_issued', calls.start.length === 1);
  check('forcestart_force_true', calls.start.length === 1 && calls.start[0].force === true);
  // The stale DB cache must NOT have been consulted nor rendered.
  check('cache_not_consulted', calls.cache === 0);
  const html = document.getElementById('paperReportContent').innerHTML;
  check('old_report_not_rendered', html.indexOf(OLD_REPORT) === -1);
  // Re-entrancy guard: attaching a task cleared the intent.
  check('intent_cleared_after_attach', _getReportRegenIntent() === null);
  // The attached stream must be the NEW task, never a reconnect.
  check('attached_new_task', _paperReportStream && _paperReportStream.taskId === 'rpt_new_1');

  // ══════════════════════════════════════════════════════════════════
  //  DECISIVE: refresh interrupts force /start, but the OLD task is STILL
  //  RUNNING (cooperative abort) and the dedup index still points to it, so
  //  lookup HITS the orphan. A pending intent MUST take priority over the
  //  step-2 lookup-reconnect — otherwise we silently re-attach to exactly the
  //  task the user asked to replace. (This is the half of the bug that lands
  //  on step-2, not step-3.)
  // ══════════════════════════════════════════════════════════════════
  calls.start.length = 0; calls.cache = 0; calls.lookup = 0;
  _paperReportStream = null; _paperReportCache = '';
  // Lookup now HITS the still-running orphan.
  lookupResult = { ok: true, task_id: OLD_ORPHAN_TASK, status: 'running', paper_hash: 'phash-1' };
  _setReportRegenIntent('phash-1', 'en');
  await _loadOrGenerateReport();
  for (let i = 0; i < 20; i++) { await new Promise(r => setTimeout(r, 0)); }
  // The regenerate was resumed via force /start...
  check('orphan_forcestart_issued', calls.start.length === 1 && calls.start[0].force === true);
  // ...and we did NOT reconnect to the orphan task.
  check('orphan_not_reattached',
        _paperReportStream && _paperReportStream.taskId === 'rpt_new_1'
        && _paperReportStream.taskId !== OLD_ORPHAN_TASK);
  check('orphan_intent_cleared', _getReportRegenIntent() === null);

  // ── Sanity: WITHOUT an intent, a lookup HIT on a running task DOES reconnect
  //    (we didn't break the normal chat-roundtrip resume path). ──
  calls.start.length = 0; calls.lookup = 0;
  _paperReportStream = null;
  _clearReportRegenIntent();
  lookupResult = { ok: true, task_id: OLD_ORPHAN_TASK, status: 'running', paper_hash: 'phash-1' };
  await _loadOrGenerateReport();
  for (let i = 0; i < 20; i++) { await new Promise(r => setTimeout(r, 0)); }
  check('no_intent_reconnects_running',
        calls.start.length === 0
        && _paperReportStream && _paperReportStream.taskId === OLD_ORPHAN_TASK);

  // Reset lookup to MISS for the remaining control case.
  lookupResult = { ok: false };

  // ── Control: WITHOUT a pending intent, a lookup miss DOES fall through to
  //    the DB cache (normal behaviour preserved — we didn't break step 3). ──
  calls.start.length = 0; calls.cache = 0;
  _paperReportStream = null; _paperReportCache = '';
  _clearReportRegenIntent();
  await _loadOrGenerateReport();
  for (let i = 0; i < 20; i++) { await new Promise(r => setTimeout(r, 0)); }
  check('no_intent_uses_cache', calls.cache === 1 && calls.start.length === 0);
  const html2 = document.getElementById('paperReportContent').innerHTML;
  check('no_intent_renders_cache', html2.indexOf(OLD_REPORT) !== -1);

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


def _run():
    harness = os.path.join(HERE, '_paper_regen_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'paper', 'report.js'),  # argv[2]
             ROOT,                                        # argv[3]
             os.path.join(JS_DIR, 'paper-reader.js'),     # argv[4] (core helpers)
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
    assert not fails, 'paper regen-refresh failures:\n' + output
    assert output.count('PASS') >= 13, f'expected >=13 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_regenerate_interrupted_by_refresh_resumes_not_reverts():
    _run()
