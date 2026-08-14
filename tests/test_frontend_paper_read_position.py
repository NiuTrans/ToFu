"""jsdom guard: Reading-Mode last-read position + already-generated shortcut.

Two user-requested behaviours (2026-07-09), both in ``static/js/paper-reader.js``:

  1. REMEMBER THE LAST READ POSITION. Scrolling the report/review persists a
     reading anchor keyed per (paper, view-language) so re-opening the tab
     (switch OR hard refresh) restores where the reader left off. The logic:
     _persistReadingPosition / _loadReadingPosition (localStorage key
     ``paper_read_pos_by_key``), keyed by _reportSnapshotKey (paper::langKey),
     and _renderFinalReport falls back to _loadReadingPosition when the live
     DOM capture is empty (fresh render).

  2. ALREADY-GENERATED → SHOW DIRECTLY, NO "Generate" BUTTON. When a report is
     already in the in-memory cache, _loadOrGenerateReport paints it instantly
     (step 1.6) with ZERO round-trips and no Generate prompt. On a cache MISS
     it shows a neutral loading placeholder (not the baked-in Generate button)
     while the lookup/cache round-trips run, only rendering the real Generate
     prompt if every path misses.

The harness loads the REAL shipped paper-reader.js under jsdom with a spy-able
Api.paper surface. _renderFinalReport is stubbed to write a marker (jsdom has
no layout, so real render/scroll math is meaningless); the functions under test
(_persistReadingPosition / _loadReadingPosition / _loadOrGenerateReport /
_renderReportStartPrompt) run for real.

Neuters (each on a COPY; shipped file byte-identical after):
  • NC-persist: _persistReadingPosition no-ops → round-trip load returns null.
  • NC-cache-shortcut: remove the step-1.6 in-memory cache short-circuit →
    a cached report no longer paints instantly (falls through to round-trips /
    Generate prompt) → the "cached paints, no start" checks FAIL.
  • NC-placeholder: remove the loading-placeholder block → the cache-miss path
    no longer shows the spinner synchronously → that check FAILS.

DB-free; skips when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._paper_vite import compiled_typescript

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')
# _persistReadingPosition / _loadReadingPosition / _loadOrGenerateReport moved to
# paper/report.js (Epic E split, 2026-07-11); paper-reader.js keeps _reportView
# + shared helpers. Both must be eval'd; the NC markers live in report.js.
REPORT_JS = os.path.join(JS_DIR, 'paper', 'report.js')
CORE_JS = os.path.join(JS_DIR, 'paper-reader.js')
PAPER_JS = REPORT_JS  # the file under test (holds the NC markers)
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

const calls = { start: [], cache: 0, lookup: 0 };
global.Api = win.Api = { paper: {
  libraryList: async () => ({ ok: true, papers: [{ id: 'paper-1', title: 'P', paperHash: 'phash-1' }] }),
  reportLookup: async () => { calls.lookup++; return { ok: false }; },
  reportCache:  async () => { calls.cache++;  return { ok: false }; },
  reportStart:  async (body) => { calls.start.push(body); return { ok: true, task_id: 'g1', paper_hash: 'phash-1' }; },
  reportPoll:   async () => ({ ok: true, status: 'done', report: 'GEN', next_cursor: 0, events: [] }),
  reportAbort:  async () => ({ ok: true }),
}};

localStorage.setItem('paper_active_id', 'paper-1');
localStorage.setItem('paper_library_migrated_v1', '1');

eval(fs.readFileSync(process.argv[2], 'utf8'));  // paper/report.js (report/review fns)
if (process.argv[4]) eval(fs.readFileSync(process.argv[4], 'utf8'));  // paper-reader.js core
eval(fs.readFileSync(process.argv[5], 'utf8'));
Object.keys(win).forEach((name) => {
  if (name.startsWith('_') && typeof win[name] === 'function') global[name] = win[name];
});

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Stub helpers touching unrelated subsystems / layout. _renderFinalReport is
// stubbed to a marker write (jsdom has no layout); _renderReportStartPrompt
// and the two position helpers under test run for real.
_saveActivePaperState = () => {};
_getActivePaperEntry = () => null;
_renderReportSkeleton = (c) => { if (c) c.innerHTML = '<div class="skeleton"></div>'; };
_syncReportToolbar = () => {};
_populatePaperReportModelDropdown = () => {};
_restoreReviewReadingLang = () => {};
_renderFinalReport = (c, text) => { if (c) c.innerHTML = 'RENDERED:' + escapeHtml(text || ''); };
if (typeof toggleSidebar === 'undefined') { global.toggleSidebar = win.toggleSidebar = () => {}; }

function genBtn(id) { return document.getElementById(id).querySelector('.paper-report-generate-btn'); }

(async () => {
  for (let i = 0; i < 10; i++) { await new Promise(r => setTimeout(r, 0)); }

  _paperReportStream = null; _paperReviewStream = null;
  _paperReportCache = ''; _paperReviewCache = '';
  _paperHash = 'phash-1';
  _paperParsedText = 'x'.repeat(500);
  _paperFileName = 'P';
  _paperReportModel = 'm'; _paperReviewModel = 'm';
  _activePaperId = 'paper-1';
  _i18nLang = 'en';

  // ══════════ (1) Reading-position persistence ══════════
  const vReport = _reportView('report');
  check('pos_helpers_exposed',
        typeof _persistReadingPosition === 'function' &&
        typeof _loadReadingPosition === 'function');

  // Round-trip: persist an anchor for the report (lang en) → load it back.
  _persistReadingPosition(vReport, { index: 3, offset: 42 });
  const got = _loadReadingPosition(vReport);
  check('pos_roundtrip', !!got && got.index === 3 && got.offset === 42);

  // Keyed per view-language: switching the report language to zh gives a
  // DIFFERENT slot (empty) while the en slot is untouched.
  localStorage.setItem('paper_report_lang_by_id', JSON.stringify({ 'paper-1': 'zh' }));
  check('pos_lang_scoped_zh_empty', _loadReadingPosition(_reportView('report')) === null);
  // Persist a zh position, then confirm en + zh keep separate places.
  _persistReadingPosition(_reportView('report'), { frac: 0.5 });
  const zhPos = _loadReadingPosition(_reportView('report'));
  localStorage.setItem('paper_report_lang_by_id', JSON.stringify({ 'paper-1': 'en' }));
  const enPos = _loadReadingPosition(_reportView('report'));
  check('pos_per_lang_distinct',
        !!zhPos && zhPos.frac === 0.5 && !!enPos && enPos.index === 3);

  // Null anchor CLEARS the slot (reader scrolled back to the top).
  _persistReadingPosition(_reportView('report'), null);
  check('pos_null_clears', _loadReadingPosition(_reportView('report')) === null);

  // ══════════ (2a) Already-generated → paint directly, no Generate btn ══════════
  calls.start.length = 0; calls.cache = 0; calls.lookup = 0;
  _paperReportStream = null;
  _paperReportCache = 'CACHED_BODY';           // report already loaded this session
  await _loadOrGenerateReport();
  for (let i = 0; i < 10; i++) { await new Promise(r => setTimeout(r, 0)); }
  check('cached_no_start', calls.start.length === 0);
  check('cached_no_roundtrips', calls.lookup === 0 && calls.cache === 0);
  check('cached_no_generate_btn', genBtn('paperReportContent') === null);
  check('cached_paints_report',
        document.getElementById('paperReportContent').innerHTML.indexOf('RENDERED:CACHED_BODY') !== -1);

  // ══════════ (2b) Cache MISS → loading placeholder synchronously (not Generate) ══════════
  calls.start.length = 0; calls.cache = 0; calls.lookup = 0;
  _paperReportStream = null;
  _paperReportCache = '';                       // nothing cached
  document.getElementById('paperReportContent').innerHTML = '';
  const p = _loadOrGenerateReport();            // do NOT await — inspect the sync state
  const midHtml = document.getElementById('paperReportContent').innerHTML;
  check('miss_shows_loading_placeholder', midHtml.indexOf('paper-loading') !== -1);
  check('miss_no_generate_btn_midflight', genBtn('paperReportContent') === null);
  await p;
  for (let i = 0; i < 20; i++) { await new Promise(r => setTimeout(r, 0)); }
  // Every path missed → the real Generate prompt is the terminal state.
  check('miss_falls_through_to_generate_prompt', genBtn('paperReportContent') !== null);
  check('miss_still_no_autostart', calls.start.length === 0);

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


def _run(report_js: str, core_js: str = CORE_JS) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_paper_read_position_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        with compiled_typescript(REPORT_RUNTIME_TS) as runtime_js:
            return subprocess.run(
                ['node', harness, report_js, ROOT, core_js, runtime_js],
                capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_read_position_and_already_generated_shortcut():
    proc = _run(PAPER_JS)
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{out}'
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'paper read-position failures:\n' + out
    assert out.count('PASS') >= 12, f'expected >=12 PASS lines, got:\n{out}'


def _run_neuter(patched_src: str, tag: str) -> str:
    tmp = os.path.join(HERE, f'_paper_read_position_neuter_{tag}.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(patched_src)
    try:
        chk = subprocess.run(['node', '--check', tmp], capture_output=True, text=True, timeout=30)
        assert chk.returncode == 0, f'patched JS invalid ({tag}): {chk.stderr}'
        proc = _run(tmp)
        assert proc.returncode == 0, f'node crashed ({tag}): {proc.stderr}\n{proc.stdout}'
        return proc.stdout.strip()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_neuters_are_load_bearing():
    src = open(PAPER_JS, encoding='utf-8').read()

    # ── NC-persist: _persistReadingPosition no-ops → round-trip load fails. ──
    m1 = 'function _persistReadingPosition(view, anchor) {\n  view = view || _reportView(\'report\');'
    assert m1 in src, 'NC-persist marker not found — test stale'
    out1 = _run_neuter(
        src.replace(m1, m1 + '\n  return;', 1), 'persist')
    assert 'FAIL pos_roundtrip' in out1, \
        'NC-persist: no-op persist did NOT break the round-trip:\n' + out1

    # ── NC-cache-shortcut: drop step-1.6 → cached report no longer paints. ──
    m2 = (
        "  // (1.6) In-memory cache for this view → paint instantly, no round-trip. This\n"
        "  // is the fast path when the report was already loaded once this session.\n"
        "  if (view.cache) {\n"
        "    var cEl = document.getElementById(view.containerId);\n"
        "    if (cEl) _renderFinalReport(cEl, view.cache, undefined, view);\n"
        "    _restoreReviewReadingLang(view);\n"
        "    return;\n"
        "  }\n"
    )
    assert m2 in src, 'NC-cache-shortcut marker not found — test stale'
    out2 = _run_neuter(src.replace(m2, '', 1), 'cacheshortcut')
    assert ('FAIL cached_no_roundtrips' in out2 or 'FAIL cached_paints_report' in out2), \
        'NC-cache-shortcut: removing step 1.6 did NOT change the cached path:\n' + out2

    # ── NC-placeholder: drop the loading placeholder → miss path shows nothing. ──
    m3 = (
        "  var loadEl = document.getElementById(view.containerId);\n"
        "  if (loadEl) {\n"
        "    loadEl.innerHTML =\n"
    )
    assert m3 in src, 'NC-placeholder marker not found — test stale'
    out3 = _run_neuter(
        src.replace(m3, "  var loadEl = null;\n  if (loadEl) {\n    loadEl.innerHTML =\n", 1),
        'placeholder')
    assert 'FAIL miss_shows_loading_placeholder' in out3, \
        'NC-placeholder: removing the placeholder did NOT break the sync loading state:\n' + out3

    assert open(PAPER_JS, encoding='utf-8').read() == src, 'shipped file was modified!'


if __name__ == '__main__':
    test_read_position_and_already_generated_shortcut()
    print('positive: PASS')
    test_neuters_are_load_bearing()
    print('neuter: PASS')
    print('ALL PASSED')
