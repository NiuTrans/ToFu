"""jsdom guard: Report tab shows an EXISTING report in the OTHER language
instead of the manual Generate prompt.

User request (2026-07-10): "If a report has already been generated, just show
it. If the English version was generated, show English; same for Chinese. Only
offer the manual trigger when NOTHING has been generated."

The report is generated + cached per ``(paper_hash, lang)`` and the active
report language is a per-paper persisted choice. So a paper can have a report in
one language while the ACTIVE language (the one the toggle currently points at)
has none. The fused report resolver selects the requested or fallback language
in one owner-scoped query; the typed runtime adopts the resolved language and
paints it without auto-starting paid work.

This harness loads the REAL shipped ``static/js/paper-reader.js`` under jsdom
with an ``Api.paper.reportResolve`` that returns an English fallback for the
active Chinese request. It asserts:

  • opening the tab issues ZERO ``reportStart`` (never auto-generates);
  • the English report body is painted (not the Generate button);
  • the active report language is ADOPTED to 'en' (so the toggle / snapshot /
    export all resolve consistently);
  • ``.paper-report-generate-btn`` is ABSENT.

Negative-control (source-level): a COPY of the typed owner with cache adoption removed
must FALL BACK to the Generate prompt → the harness FAILS the "report painted"
and "generate button absent" checks. The shipped file is never modified.

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
# The retained sections provide renderers and view adapters; the typed owner
# owns fused lookup/cache resolution and fallback-language adoption.
REPORT_JS = os.path.join(JS_DIR, 'paper', 'report.js')
CORE_JS = os.path.join(JS_DIR, 'paper-reader.js')
PAPER_JS = REPORT_JS  # the file under test (holds the step-3.5 markers)
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
  '<div id="reportLangToggle">' +
  '  <button class="paper-report-lang-opt" data-lang="en"></button>' +
  '  <button class="paper-report-lang-opt" data-lang="zh"></button>' +
  '</div>' +
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

// One fused resolve returns the NON-active language (en) for a zh request.
const calls = { start: [], cacheByLang: [], resolveByLang: [] };
global.Api = win.Api = { paper: {
  libraryList: async () => ({ ok: true, papers: [{ id: 'paper-1', title: 'P', paperHash: 'phash-1' }] }),
  reportLookup: async () => ({ ok: false }),
  reportResolve: async (_hash, lang) => {
    calls.resolveByLang.push(lang);
    return {
      ok: true, cached: true, report: 'ENGLISH_REPORT_BODY',
      paper_hash: 'phash-1', lang: 'en', meta: null,
    };
  },
  reportCache:  async (body) => {
    calls.cacheByLang.push(body.lang);
    if (body.lang === 'en') return { ok: true, report: 'ENGLISH_REPORT_BODY', paper_hash: 'phash-1', meta: null };
    return { ok: false };
  },
  reportStart:  async (body) => { calls.start.push(body); return { ok: true, task_id: 'gen_1', paper_hash: 'phash-1' }; },
  reportPoll:   async () => ({ ok: true, status: 'done', report: 'X', next_cursor: 0, events: [] }),
  reportAbort:  async () => ({ ok: true }),
}};

localStorage.setItem('paper_active_id', 'paper-1');
localStorage.setItem('paper_library_migrated_v1', '1');
// Active report language for paper-1 = zh (the one with NO report).
localStorage.setItem('paper_report_lang_by_id', JSON.stringify({ 'paper-1': 'zh' }));

eval(fs.readFileSync(process.argv[2], 'utf8'));  // paper/report.js (report/review fns)
if (process.argv[4]) eval(fs.readFileSync(process.argv[4], 'utf8'));  // paper-reader.js core
eval(fs.readFileSync(process.argv[5], 'utf8'));
Object.keys(win).forEach((name) => {
  if (name.startsWith('_') && typeof win[name] === 'function') global[name] = win[name];
});

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

_saveActivePaperState = win._saveActivePaperState = () => {};
_getActivePaperEntry = win._getActivePaperEntry = () => null;
_renderReportSkeleton = (c) => { if (c) c.innerHTML = '<div class="skeleton"></div>'; };
win._renderFinalReport = _renderFinalReport = (c, text) => {
  if (c) c.innerHTML = '<pre>' + escapeHtml(text || '') + '</pre>';
};
_syncReportToolbar = () => {};
_populatePaperReportModelDropdown = () => {};
if (typeof toggleSidebar === 'undefined') { global.toggleSidebar = win.toggleSidebar = () => {}; }

(async () => {
  for (let i = 0; i < 10; i++) { await new Promise(r => setTimeout(r, 0)); }

  _paperReportStream = null;
  _paperReviewStream = null;
  _paperReportCache = '';
  _paperReviewCache = '';
  _paperHash = 'phash-1';
  _paperParsedText = 'x'.repeat(500);
  _paperFileName = 'P';
  _paperReportModel = 'm';
  _paperReviewModel = 'm';
  _paperReviewVenue = 'neurips';
  _activePaperId = 'paper-1';
  win._activePaperId = _activePaperId;
  _i18nLang = 'en';
  _paperActiveTab = 'report';

  // Active language is zh (persisted) and has no report.
  check('active_lang_is_zh', _activeReportLang() === 'zh');

  await _loadOrGenerateReport();  // report view
  for (let i = 0; i < 30; i++) { await new Promise(r => setTimeout(r, 0)); }

  // Must NOT auto-generate.
  check('no_autostart', calls.start.length === 0);
  check('one_fused_zh_resolve',
        calls.resolveByLang.length === 1 && calls.resolveByLang[0] === 'zh'
        && calls.cacheByLang.length === 0);
  // The English report body is painted (not the Generate button).
  const html = document.getElementById('paperReportContent').innerHTML;
  check('english_report_painted', html.indexOf('ENGLISH_REPORT_BODY') !== -1);
  check('generate_button_absent',
        document.getElementById('paperReportContent').querySelector('.paper-report-generate-btn') === null);
  // The active report language is adopted to the language that actually has a
  // report, so the toggle / snapshot key resolve consistently.
  check('active_lang_adopted_en', _activeReportLang() === 'en');

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


def _run_harness(
    report_js: str,
    core_js: str = CORE_JS,
    runtime_contents: str | None = None,
) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_paper_other_lang_fallback_harness.js')
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
def test_report_tab_shows_other_generated_language():
    proc = _run_harness(PAPER_JS)
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{out}'
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'other-language fallback failures:\n' + out
    assert out.count('PASS') >= 6, f'expected >=6 PASS lines, got:\n{out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_source_level_negative_control_without_cache_adoption_shows_prompt():
    """Remove typed cache adoption and prove fallback is hidden by the prompt.

    We patch a COPY of the typed owner so a fused cached response falls through
    to the manual Generate prompt. The harness must
    then FAIL the "english_report_painted" + "generate_button_absent" checks.
    The shipped file is untouched.
    """
    src = shipped_source_text('frontend/src/features/paper/report-runtime.ts')
    marker = "  if (resolved?.ok && resolved.report && container) {"
    assert marker in src, 'cache-adoption marker not found — test is stale'
    broken = src.replace(marker, "  if (false && resolved?.report && container) {", 1)
    assert broken != src, 'negative-control patch was a no-op'
    proc = _run_harness(PAPER_JS, runtime_contents=broken)
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node crashed: {proc.stderr}\n{out}'
    assert 'FAIL english_report_painted' in out, \
        'removing cache adoption still painted the report — guard is non-load-bearing:\n' + out
    assert 'FAIL generate_button_absent' in out, \
        'removing cache adoption still hid the Generate button — guard is non-load-bearing:\n' + out

    assert shipped_source_text('frontend/src/features/paper/report-runtime.ts') == src, (
        'typed report runtime was modified!'
    )


if __name__ == '__main__':
    if not _node_deps_available():
        print('SKIP: node + jsdom not available')
    else:
        test_report_tab_shows_other_generated_language()
        print('positive: PASS')
        test_source_level_negative_control_without_cache_adoption_shows_prompt()
        print('negative-control: PASS')
        print('ALL PASSED')
