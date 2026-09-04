"""jsdom guard: switching to the Report/Review tab must reach the text-recovery
path when a restored paper has empty parsedText + empty hash but a PDF on disk.

Bug (reported 2026-07, after a restart): a paper opened from the library shows
"No paper text available. Load a PDF first." even though its PDF is intact on
disk. Root cause: ``_switchPaperTab`` gated the load path on
``if (_paperParsedText || _paperHash)``. A library entry restored after a
restart can carry empty ``parsedText`` AND empty ``paperHash`` (saved before
server-side parsing, or a scanned/failed parse) while its PDF still lives under
PAPER_DIR. That guard sent those papers straight to the "load a PDF" message and
NEVER called ``_loadOrGenerateReport`` → never reached ``_ensurePaperText()``
(POST /api/paper/reparse), the documented recovery. So recovery was unreachable
from a fresh tab open.

Fix: widen the guard to also proceed when a PDF is available
(``_paperPdfUrl`` / ``_paperPdfFilename``), delegating recovery to
the typed report owner (which runs the retained ``_ensurePaperText`` port on
empty text before a user-initiated start).

This harness loads the REAL shipped ``static/js/paper-reader.js`` under jsdom,
stubs ``_loadOrGenerateReport`` as a spy, and asserts that with empty
text+hash but a PDF URL present, switching to the Report tab CALLS
``_loadOrGenerateReport`` and does NOT paint the "No paper text" message — while
a paper with NO PDF at all still shows the message (the branch stays reachable).

Source-level negative control: revert the guard to ``_paperParsedText ||
_paperHash`` and prove the PDF-only paper dead-ends on the message again. The
shipped file is never modified.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._paper_vite import compiled_typescript
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = _legacy_test_root()
PAPER_JS = os.path.join(ROOT, 'static', 'js', 'paper-reader.js')
SESSION_TS = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'paper', 'session.ts')


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
  '<div id="sidebar" class="collapsed"></div>' +
  '<button class="paper-tab-btn" data-tab="report"></button>' +
  '<button class="paper-tab-btn" data-tab="review"></button>' +
  '<div class="paper-tab-panel" data-tab="report"></div>' +
  '<div class="paper-tab-panel" data-tab="review"></div>' +
  '<div id="paperReportContent"></div>' +
  '<div id="paperReviewContent"></div>' +
  '</body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.localStorage = win.localStorage;
global.console = console;
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
win.t = global.t = (k) => (k === 'paper.reportNoText' ? 'No paper text available. Load a PDF first.' : k);
win.debugLog = global.debugLog = () => {};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // paper-reader.js (real, shipped)
eval(fs.readFileSync(process.argv[4], 'utf8'));
Object.keys(win).forEach((name) => {
  if (name.startsWith('_') && typeof win[name] === 'function') global[name] = win[name];
});

// Spy on the load path; stub subsystems the guard shouldn't need.
let loadCalls = 0;
win._loadOrGenerateReport = _loadOrGenerateReport = () => { loadCalls++; };
win._renderPaperQA = _renderPaperQA = () => {};
win._initBabelPdfTab = _initBabelPdfTab = () => {};
win._teardownReadingTracker = _teardownReadingTracker = () => {};
// Review path awaits venue resolution before loading — make it resolve fast.
win._populateReviewVenueDropdown = _populateReviewVenueDropdown = () => Promise.resolve();
if (typeof toggleSidebar === 'undefined') { global.toggleSidebar = win.toggleSidebar = () => {}; }
// _reportView must exist (real one from the file); if the decomposition moved
// it, fall back to a minimal shim keyed by tab.
if (typeof _reportView !== 'function') {
  _reportView = (tab) => ({ kind: tab,
    containerId: tab === 'review' ? 'paperReviewContent' : 'paperReportContent' });
}
win._reportView = _reportView;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

function reportEmptyShown(id) {
  return document.getElementById(id).innerHTML.indexOf('No paper text available') !== -1;
}

(async () => {
  // ── Case A: restored paper — empty text + empty hash, but a PDF on disk. ──
  _paperParsedText = '';
  _paperHash = '';
  _paperPdfUrl = '/api/paper/pdf/restored-paper.pdf';
  _paperPdfFilename = 'restored-paper.pdf';
  Object.assign(win, {
    _paperParsedText, _paperHash, _paperPdfUrl, _paperPdfFilename,
  });
  loadCalls = 0;
  document.getElementById('paperReportContent').innerHTML = '';
  _switchPaperTab('report');
  await new Promise(r => setTimeout(r, 0));
  check('pdf_only_enters_load_path', loadCalls === 1);
  check('pdf_only_no_dead_end_message', !reportEmptyShown('paperReportContent'));

  // Review tab (async venue resolve) — same expectation.
  loadCalls = 0;
  document.getElementById('paperReviewContent').innerHTML = '';
  _switchPaperTab('review');
  for (let i = 0; i < 10; i++) { await new Promise(r => setTimeout(r, 0)); }
  check('pdf_only_review_enters_load_path', loadCalls === 1);
  check('pdf_only_review_no_dead_end', !reportEmptyShown('paperReviewContent'));

  // ── Case B: truly no PDF (nothing to recover) — message MUST still show. ──
  _paperParsedText = '';
  _paperHash = '';
  _paperPdfUrl = '';
  _paperPdfFilename = '';
  Object.assign(win, {
    _paperParsedText, _paperHash, _paperPdfUrl, _paperPdfFilename,
  });
  loadCalls = 0;
  document.getElementById('paperReportContent').innerHTML = '';
  _switchPaperTab('report');
  await new Promise(r => setTimeout(r, 0));
  check('no_pdf_shows_message', reportEmptyShown('paperReportContent'));
  check('no_pdf_does_not_load', loadCalls === 0);

  // ── Case C: normal paper with parsed text — unchanged (loads). ──
  _paperParsedText = 'lots of text';
  _paperHash = '';
  _paperPdfUrl = '';
  _paperPdfFilename = '';
  Object.assign(win, {
    _paperParsedText, _paperHash, _paperPdfUrl, _paperPdfFilename,
  });
  loadCalls = 0;
  _switchPaperTab('report');
  await new Promise(r => setTimeout(r, 0));
  check('parsed_text_still_loads', loadCalls === 1);

  console.log(out.join('\n'));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _run_harness(
        paper_js: str, session_source: str | None = None,
) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_paper_switchtab_recovery_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        with compiled_typescript(
            SESSION_TS,
            contents=session_source,
            expose_feature_registry_to_window=True,
        ) as session_js:
            return subprocess.run(
                ['node', harness, paper_js, ROOT, session_js],
                capture_output=True, text=True, timeout=60,
            )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_switchtab_enters_recovery_path_for_pdf_only_paper():
    proc = _run_harness(PAPER_JS)
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{out}'
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'switchtab recovery failures:\n' + out
    assert out.count('PASS') >= 7, f'expected >=7 PASS lines, got:\n{out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_source_level_negative_control_narrow_guard_dead_ends():
    """Revert the guard to text||hash only and prove the PDF-only paper
    dead-ends on the message again. The shipped file is never modified."""
    src = open(SESSION_TS, encoding='utf-8').read()

    marker = (
        "    const recoverable = Boolean(\n"
        "      state._paperParsedText || state._paperHash\n"
        "      || state._paperPdfUrl || state._paperPdfFilename,\n"
        "    );"
    )
    assert marker in src, 'fix marker not found — test is stale, update the marker'
    broken = src.replace(
        marker,
        "    const recoverable = Boolean(\n"
        "      state._paperParsedText || state._paperHash,\n"
        "    );",
        1,
    )
    assert broken != src, 'negative-control patch was a no-op'
    proc = _run_harness(PAPER_JS, session_source=broken)
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node crashed: {proc.stderr}\n{out}'
    # With the narrow guard, the PDF-only paper no longer enters the load
    # path and paints the dead-end message → these checks flip to FAIL.
    assert ('FAIL pdf_only_enters_load_path' in out
            or 'FAIL pdf_only_no_dead_end_message' in out), \
        'narrowing the guard did NOT reintroduce the dead-end — fix non-load-bearing:\n' + out

    assert open(SESSION_TS, encoding='utf-8').read() == src, 'shipped file was modified!'


if __name__ == '__main__':
    import sys
    if not _node_deps_available():
        print('SKIP — node + jsdom not available')
        sys.exit(0)
    test_switchtab_enters_recovery_path_for_pdf_only_paper()
    print('positive: PASS')
    test_source_level_negative_control_narrow_guard_dead_ends()
    print('negative-control: PASS')
    print('ALL PASSED')
