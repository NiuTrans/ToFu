"""jsdom guard: Reading-Mode Report/Review must NOT auto-generate on tab open.

User preference (2026-07-02): opening the Report or Review tab of Reading Mode
must NOT immediately start a generation — otherwise the user cannot adjust the
model / generation language / target venue in the toolbar before the run
begins. Generation must be USER-INITIATED (a Generate button click), while the
automatic resume/reconnect/cache-hit paths (``_loadOrGenerateReport`` steps
1-3) stay automatic so a returning user still sees work already done instantly.

The regression risk is that a future refactor edits step 4 of
``_loadOrGenerateReport`` back to ``_generatePaperReport(false, view)`` — the
old auto-start — and nothing catches it. This guard pins the behaviour.

The harness loads the REAL shipped ``static/js/paper-reader.js`` under jsdom
with a spy-able ``Api.paper`` surface where ``reportStart`` IS the
``/api/paper/report/start`` interceptor the task calls. It mocks a clean cache
MISS (no cached report, no running task) and asserts, for BOTH the Report and
Review tabs:

  • opening the tab issues ZERO ``reportStart`` (no ``/report/start`` POST);
  • ``#paper{Report,Review}Content`` contains a ``.paper-report-generate-btn``;
  • that button is wired to ``_generatePaperReport()`` / ``_generatePaperReview()``;
  • clicking it (invoking that fn) DOES issue a ``reportStart`` — proving the
    manual path still works and the assertions can distinguish start-vs-no-start.

DB-free by construction: every endpoint is stubbed via the JS ``Api`` object;
no ``server.app``, no Postgres/SQLite bootstrap (respects the bare-CI rule).

Negative-control (automated, source-level): a second test patches a COPY of the
typed owner, reverting the terminal prompt to ``_generatePaperReport(false, view)``
auto-start, and asserts the harness then FAILS the no-autostart / button-present
checks. The shipped file is never modified.

Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._paper_vite import compiled_typescript
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root, shipped_source_text

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = _legacy_test_root()
JS_DIR = os.path.join(ROOT, 'static', 'js')
# The typed report owner owns reopen/manual-start policy; retained sections keep
# the _reportView adapter and report renderers.
REPORT_JS = os.path.join(JS_DIR, 'paper', 'report.js')
CORE_JS = os.path.join(JS_DIR, 'paper-reader.js')
PAPER_JS = REPORT_JS  # the file under test (holds the step-4 marker)
REPORT_RUNTIME_TS = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'paper', 'report-runtime.ts')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
// Both report + review containers must coexist (the view registry prefixes
// inner ids per view; the two panels persist together in the real DOM).
const dom = new JSDOM(
  '<!DOCTYPE html><body>' +
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
win.t = global.t = (k) => k;

// ── Spy-able Api surface. reportStart IS the /api/paper/report/start POST. ──
// Clean MISS: no running task (lookup ok:false), no DB cache (cache ok:false).
const calls = { start: [], cache: 0, lookup: 0, resolve: 0 };
global.Api = win.Api = { paper: {
  libraryList: async () => ({ ok: true, papers: [{ id: 'paper-1', title: 'Test Paper', paperHash: 'phash-1' }] }),
  reportLookup: async () => { calls.lookup++; return { ok: false }; },
  reportResolve: async () => { calls.resolve++; return { ok: false }; },
  reportCache:  async () => { calls.cache++;  return { ok: false }; },
  reportStart:  async (body) => { calls.start.push(body); return { ok: true, task_id: 'gen_1', paper_hash: 'phash-1' }; },
  reportPoll:   async () => ({ ok: true, status: 'done', report: 'GENERATED', next_cursor: 0, events: [] }),
  reportAbort:  async () => ({ ok: true }),
}};

localStorage.setItem('paper_active_id', 'paper-1');
localStorage.setItem('paper_library_migrated_v1', '1');
localStorage.setItem('tofu_ui_lang', 'en');

eval(fs.readFileSync(process.argv[2], 'utf8'));  // paper/report.js (report/review fns)
if (process.argv[4]) eval(fs.readFileSync(process.argv[4], 'utf8'));  // paper-reader.js core
eval(fs.readFileSync(process.argv[5], 'utf8'));  // native report task owner
Object.keys(win).forEach((name) => {
  if (name.startsWith('_') && typeof win[name] === 'function') global[name] = win[name];
});

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Stub the few helpers that touch unrelated subsystems (skeleton/toolbar/model
// dropdown/persist). We deliberately do NOT stub _renderReportStartPrompt —
// that is the function under test.
_saveActivePaperState = win._saveActivePaperState = () => {};
_getActivePaperEntry = win._getActivePaperEntry = () => null;
_renderReportSkeleton = (c) => { if (c) c.innerHTML = '<div class="skeleton"></div>'; };
_syncReportToolbar = () => {};
_populatePaperReportModelDropdown = () => {};
if (typeof toggleSidebar === 'undefined') { global.toggleSidebar = win.toggleSidebar = () => {}; }

function reportBtn(id) {
  return document.getElementById(id).querySelector('.paper-report-generate-btn');
}

(async () => {
  // Let the eval-time _loadPaperLibrary() settle so it doesn't reset _activePaperId.
  for (let i = 0; i < 10; i++) { await new Promise(r => setTimeout(r, 0)); }

  // ── Post-refresh state: paper loaded, no local stream, nothing cached. ──
  _paperReportStream = null;
  _paperReviewStream = null;
  _paperReportCache = '';
  _paperReviewCache = '';
  _paperHash = 'phash-1';
  _paperParsedText = 'x'.repeat(500);
  _paperFileName = 'Test Paper';
  _paperReportModel = 'some-model';
  _paperReviewModel = 'some-model';
  _paperReviewVenue = 'neurips';
  _activePaperId = 'paper-1';
  win._activePaperId = _activePaperId;
  _i18nLang = 'en';

  // ══════════════════ REPORT tab ══════════════════
  calls.start.length = 0; calls.cache = 0; calls.lookup = 0; calls.resolve = 0;
  await _loadOrGenerateReport();                       // == _reportView('report')
  for (let i = 0; i < 20; i++) { await new Promise(r => setTimeout(r, 0)); }

  // DECISIVE: opening the tab must NOT have issued /report/start.
  check('report_open_no_start', calls.start.length === 0);
  // The manual-start affordance is present + correctly wired.
  const rbtn = reportBtn('paperReportContent');
  check('report_button_present', !!rbtn);
  check('report_button_wired', !!rbtn
        && rbtn.getAttribute('data-tofu-action') === '_generatePaperReport()');
  // Live-task + preferred/fallback cache resolution is one fused request.
  check('report_open_uses_one_fused_resolve',
        calls.resolve === 1 && calls.lookup === 0 && calls.cache === 0);

  // Clicking Generate (invoking the wired fn) DOES start — proving the manual
  // path works AND that the no-start assertion above can distinguish states.
  await _generatePaperReport();
  for (let i = 0; i < 20; i++) { await new Promise(r => setTimeout(r, 0)); }
  check('report_button_click_starts',
        calls.start.length === 1 && calls.start[0].lang === 'en');

  // ══════════════════ REVIEW tab ══════════════════
  calls.start.length = 0; calls.cache = 0; calls.lookup = 0; calls.resolve = 0;
  _paperReviewStream = null; _paperReviewCache = '';
  await _loadOrGenerateReport(_reportView('review'));
  for (let i = 0; i < 20; i++) { await new Promise(r => setTimeout(r, 0)); }

  check('review_open_no_start', calls.start.length === 0);
  const vbtn = reportBtn('paperReviewContent');
  check('review_button_present', !!vbtn);
  check('review_button_wired', !!vbtn
        && vbtn.getAttribute('data-tofu-action') === '_generatePaperReview()');
  check('review_open_uses_one_fused_resolve',
        calls.resolve === 1 && calls.lookup === 0 && calls.cache === 0);

  await _generatePaperReview();
  for (let i = 0; i < 20; i++) { await new Promise(r => setTimeout(r, 0)); }
  // Review start carries the composite venue cache key.
  check('review_button_click_starts',
        calls.start.length === 1 && calls.start[0].lang === 'review:neurips:en');

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


def _run_harness(
    report_js: str,
    core_js: str = CORE_JS,
    runtime_contents: str | None = None,
) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_paper_no_autostart_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        with compiled_typescript(
            REPORT_RUNTIME_TS,
            contents=runtime_contents,
            expose_feature_registry_to_window=True,
        ) as runtime_js:
            return subprocess.run(
                ['node', harness, report_js, ROOT, core_js, runtime_js],
                capture_output=True, text=True, timeout=60,
            )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_report_and_review_tab_open_does_not_autostart():
    proc = _run_harness(PAPER_JS)
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{out}'
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'paper no-autostart failures:\n' + out
    assert out.count('PASS') >= 10, f'expected >=10 PASS lines, got:\n{out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_source_level_negative_control_step4_autostart_reintroduces_bug():
    """Revert step 4 to the old auto-start and prove the guard FAILS.

    We patch a COPY of the typed report owner so ``loadOrGenerateReport``
    calls ``_generatePaperReport(false, view)`` again (the pre-fix auto-start)
    instead of ``renderReportStartPrompt(view)``. The harness must then FAIL
    the no-autostart + button-present checks. The shipped file is untouched.
    """
    src = shipped_source_text('frontend/src/features/paper/report-runtime.ts')

    marker = "  renderReportStartPrompt(view);\n}"
    assert marker in src, 'fix marker not found — test is stale, update the marker'
    broken = src.replace(
        marker,
        "  void globals()._generatePaperReport?.(false, view);\n}",
        1,
    )
    assert broken != src, 'negative-control patch was a no-op'
    proc = _run_harness(PAPER_JS, runtime_contents=broken)
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node crashed: {proc.stderr}\n{out}'
    # Reverting to auto-start: the tab open fires /report/start and never
    # renders the Generate button → these two checks MUST flip to FAIL.
    assert 'FAIL report_open_no_start' in out, \
        'reverting step 4 did NOT reintroduce auto-start — guard is non-load-bearing:\n' + out
    assert 'FAIL report_button_present' in out, \
        'reverting step 4 still rendered the Generate button — guard is non-load-bearing:\n' + out

    assert shipped_source_text('frontend/src/features/paper/report-runtime.ts') == src, (
        'typed report runtime was modified!'
    )


if __name__ == '__main__':
    test_report_and_review_tab_open_does_not_autostart()
    print('positive: PASS')
    test_source_level_negative_control_step4_autostart_reintroduces_bug()
    print('negative-control: PASS')
    print('ALL PASSED')
