"""jsdom guard: Report tab shows an EXISTING report in the OTHER language
instead of the manual Generate prompt.

User request (2026-07-10): "If a report has already been generated, just show
it. If the English version was generated, show English; same for Chinese. Only
offer the manual trigger when NOTHING has been generated."

The report is generated + cached per ``(paper_hash, lang)`` and the active
report language is a per-paper persisted choice. So a paper can have a report in
one language while the ACTIVE language (the one the toggle currently points at)
has none. Before this fix, ``_loadOrGenerateReport`` step 4 rendered the manual
Generate prompt in that case — hiding a report the user already paid to
generate. The fix adds step 3.5: on a clean miss for the active language, probe
the OTHER report language and, if it has a persisted report, adopt that language
and paint it (no auto-start).

This harness loads the REAL shipped ``static/js/paper-reader.js`` under jsdom
with an ``Api.paper.reportCache`` that returns a report ONLY for ``lang==='en'``
(the non-active language) and a MISS for the active ``'zh'``. It asserts:

  • opening the tab issues ZERO ``reportStart`` (never auto-generates);
  • the English report body is painted (not the Generate button);
  • the active report language is ADOPTED to 'en' (so the toggle / snapshot /
    export all resolve consistently);
  • ``.paper-report-generate-btn`` is ABSENT.

Negative-control (source-level): a COPY of paper-reader.js with step 3.5 removed
must FALL BACK to the Generate prompt → the harness FAILS the "report painted"
and "generate button absent" checks. The shipped file is never modified.

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
PAPER_JS = os.path.join(JS_DIR, 'paper-reader.js')


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

// reportCache returns a report ONLY for the NON-active language (en). The
// active language (zh) misses. lookup always misses (no running task).
const calls = { start: [], cacheByLang: [] };
global.Api = win.Api = { paper: {
  libraryList: async () => ({ ok: true, papers: [{ id: 'paper-1', title: 'P', paperHash: 'phash-1' }] }),
  reportLookup: async () => ({ ok: false }),
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

eval(fs.readFileSync(process.argv[2], 'utf8'));  // paper-reader.js (real, shipped)

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

_saveActivePaperState = () => {};
_getActivePaperEntry = () => null;
_renderReportSkeleton = (c) => { if (c) c.innerHTML = '<div class="skeleton"></div>'; };
_renderFinalReport = (c, text) => { if (c) c.innerHTML = '<pre>' + escapeHtml(text || '') + '</pre>'; };
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
  _i18nLang = 'en';
  _paperActiveTab = 'report';

  // Active language is zh (persisted) and has no report.
  check('active_lang_is_zh', _activeReportLang() === 'zh');

  await _loadOrGenerateReport();  // report view
  for (let i = 0; i < 30; i++) { await new Promise(r => setTimeout(r, 0)); }

  // Must NOT auto-generate.
  check('no_autostart', calls.start.length === 0);
  // The active-language (zh) cache was probed, then the other (en) was probed.
  check('probed_zh_then_en',
        calls.cacheByLang.indexOf('zh') !== -1 && calls.cacheByLang.indexOf('en') !== -1);
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


def _run_harness(paper_js: str) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_paper_other_lang_fallback_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        return subprocess.run(
            ['node', harness, paper_js, ROOT],
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
def test_source_level_negative_control_without_step35_falls_to_generate_prompt():
    """Remove step 3.5 and prove the report is hidden behind the Generate prompt.

    We patch a COPY of paper-reader.js deleting the ``view.kind === 'report'``
    other-language probe block, so ``_loadOrGenerateReport`` falls straight to
    the manual Generate prompt on a clean active-language miss. The harness must
    then FAIL the "english_report_painted" + "generate_button_absent" checks.
    The shipped file is untouched.
    """
    src = open(PAPER_JS, encoding='utf-8').read()

    start = src.index("  if (view.kind === 'report' && _paperHash) {\n    var otherLang")
    end = src.index("  // (4) No cache in EITHER language")
    assert start != -1 and end != -1 and end > start, \
        'step 3.5 markers not found — test is stale, update the markers'
    broken = src[:start] + src[end:]
    assert broken != src, 'negative-control patch was a no-op'

    tmp = os.path.join(HERE, '_paper_reader_no_step35.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(broken)
    try:
        chk = subprocess.run(['node', '--check', tmp], capture_output=True, text=True, timeout=30)
        assert chk.returncode == 0, f'patched JS invalid: {chk.stderr}'
        proc = _run_harness(tmp)
        out = proc.stdout.strip()
        assert proc.returncode == 0, f'node crashed: {proc.stderr}\n{out}'
        assert 'FAIL english_report_painted' in out, \
            'removing step 3.5 still painted the report — guard is non-load-bearing:\n' + out
        assert 'FAIL generate_button_absent' in out, \
            'removing step 3.5 still hid the Generate button — guard is non-load-bearing:\n' + out
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    assert open(PAPER_JS, encoding='utf-8').read() == src, 'shipped file was modified!'


if __name__ == '__main__':
    if not _node_deps_available():
        print('SKIP: node + jsdom not available')
    else:
        test_report_tab_shows_other_generated_language()
        print('positive: PASS')
        test_source_level_negative_control_without_step35_falls_to_generate_prompt()
        print('negative-control: PASS')
        print('ALL PASSED')
