"""jsdom test: reopening the Review tab restores the persisted READING language.

Loads the REAL shipped ``static/js/paper-reader.js`` under jsdom and reproduces
the reopen flow the owner hit:

  • A paper's review reading language is persisted as 'zh'
    (localStorage ``paper_review_lang_by_id``).
  • On reopen, ``_resetAllReportViews`` has wiped the in-memory translation
    state, so ``_loadOrGenerateReport(review)`` hits the server DB cache and
    renders the canonical ENGLISH review.
  • ``_restoreReviewReadingLang`` must then re-apply the persisted Chinese
    reading view — by TRANSLATING the English review (Babel translate task),
    NEVER by regenerating the English review (no report/start).

Assertions:
  • the rendered Review content is the TRANSLATED Chinese text;
  • ``translateStart`` was called exactly once;
  • ``reportStart`` was NOT called (the English review is never regenerated).

Negative control (source-level, byte-reverting): neutering
``_restoreReviewReadingLang`` to an immediate ``return`` (a no-op) MUST make the
restore assertions FAIL — the reopen stays English and no translate fires. The
harness runs the SAME real JS twice; the NC run patches only the function body
in-memory (the on-disk file is never modified).

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
const NC = process.argv[4] === 'nc';   // neuter _restoreReviewReadingLang?
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body>' +
  '<div id="paperReportContent"></div>' +
  '<div id="paperReviewContent"></div>' +
  '<div id="reviewLangToggle">' +
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

const calls = { reportStart: 0, translateStart: 0 };
global.Api = win.Api = { paper: {
  libraryList: async () => ({ ok: true, papers: [{ id: 'paper-1', title: 'P', paperHash: 'phash-1' }] }),
  // No running server task; the review is a warm DB-cache hit (English).
  reportLookup: async () => ({ ok: false }),
  reportCache:  async () => ({ ok: true, report: 'ENGLISH REVIEW BODY', paper_hash: 'phash-1' }),
  reportStart:  async (body) => { calls.reportStart++; return { ok: true, task_id: 'rpt_1', paper_hash: 'phash-1' }; },
  reportPoll:   async () => ({ ok: true, status: 'done', report: 'NEW', next_cursor: 0, events: [] }),
  reportAbort:  async () => ({ ok: true }),
  // Review translation path — cold client-cache → start + poll to done.
  translateCache: async () => ({ ok: false }),
  translateStart: async () => { calls.translateStart++; return { ok: true, task_id: 'tr_1', paper_hash: 'phash-1' }; },
  translatePoll:  async () => ({ ok: true, status: 'done', next_cursor: 1,
                                 json: async () => ({ ok: true, status: 'done', next_cursor: 1,
                                                      events: [{ type: 'done', text: '中文译文' }] }) }),
  translateAbort: async () => ({ ok: true }),
}};

// Persisted reading language for this paper = Chinese (the reopen state).
localStorage.setItem('paper_active_id', 'paper-1');
localStorage.setItem('paper_library_migrated_v1', '1');
localStorage.setItem('paper_review_lang_by_id', JSON.stringify({ 'paper-1': 'zh' }));

const PAPER_DIR = path.join(path.dirname(process.argv[2]), 'paper');
// _restoreReviewReadingLang/_activeReviewLang/_loadOrGenerateReport moved to
// paper/report.js; _loadPaperLibrary to paper/library.js — eval them first
// (window-scope concatenation, same as the shipped script tags).
let reportSrc = fs.readFileSync(path.join(PAPER_DIR, 'report.js'), 'utf8');
if (NC) {
  // Source-level negative control: turn the restore into a no-op. Byte-identical
  // on-disk file is untouched; only this in-memory copy is patched.
  const before = reportSrc;
  reportSrc = reportSrc.replace(
    'function _restoreReviewReadingLang(view) {',
    'function _restoreReviewReadingLang(view) {\n  return; // NC: neutered');
  if (reportSrc === before) { console.log('FAIL nc_patch_applied'); process.exit(0); }
}
eval(reportSrc);  // paper/report.js (real, shipped)
eval(fs.readFileSync(path.join(PAPER_DIR, 'library.js'), 'utf8'));  // paper/library.js
let src = fs.readFileSync(process.argv[2], 'utf8');
eval(src);  // paper-reader.js (real, shipped)

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Stub helpers touching unrelated subsystems; keep the real render path simple.
_getActivePaperEntry = () => null;
_renderReportSkeleton = (c) => { if (c) c.innerHTML = '<div class="skeleton"></div>'; };
_renderFinalReport = (c, text) => { if (c) c.innerHTML = '<pre>' + escapeHtml(text || '') + '</pre>'; };
_teardownReadingTracker = () => {};

_paperReportStream = null;
_paperReviewStream = null;
_paperReportCache = '';
_paperReviewCache = '';
// Reopen: in-memory translation state was wiped by _resetAllReportViews.
_paperReviewTranslatedText = '';
_paperReviewShowTranslation = false;
_paperReviewTranslating = false;
_paperHash = 'phash-1';
_paperParsedText = 'x'.repeat(500);
_paperFileName = 'P';
_paperReportModel = 'm';
_paperReviewModel = 'm';
_paperReviewVenue = 'neurips';
_activePaperId = 'paper-1';
_paperActiveTab = 'review';
_i18nLang = 'en';   // English UI — restore must still honour the per-paper 'zh'

(async () => {
  for (let i = 0; i < 5; i++) { await new Promise(r => setTimeout(r, 0)); }
  _activePaperId = 'paper-1';

  // Sanity: the persisted reading language is 'zh'.
  check('persisted_zh', _activeReviewLang() === 'zh');

  // Reopen the Review tab: DB-cache hit renders English, then the restore hook
  // should translate it into the persisted Chinese reading view.
  await _loadOrGenerateReport(_reportView('review'));
  for (let i = 0; i < 40; i++) { await new Promise(r => setTimeout(r, 0)); }

  const revHtml = document.getElementById('paperReviewContent').innerHTML;
  check('restored_chinese_view', revHtml.indexOf('中文译文') !== -1);
  check('translate_called_once', calls.translateStart === 1);
  check('never_regenerated_english', calls.reportStart === 0);

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


def _run(nc: bool):
    harness = os.path.join(HERE, '_review_lang_restore_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    argv = ['node', harness, os.path.join(JS_DIR, 'paper-reader.js'), ROOT]
    if nc:
        argv.append('nc')
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_review_reading_lang_restored_on_reopen():
    output = _run(nc=False)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'review-reading-lang restore failures:\n' + output
    for expected in ('PASS persisted_zh', 'PASS restored_chinese_view',
                     'PASS translate_called_once', 'PASS never_regenerated_english'):
        assert expected in output, f'missing {expected!r} in:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_nc_neutered_restore_fails():
    """Neutering _restoreReviewReadingLang MUST break the restore assertions:
    the reopen stays English (no translate), proving the restore hook is what
    re-applies the persisted Chinese reading view."""
    output = _run(nc=True)
    # The persisted-lang sanity check still passes (localStorage unchanged), but
    # the restore-dependent checks MUST fail with the hook neutered.
    assert 'FAIL restored_chinese_view' in output, \
        'expected restored_chinese_view to FAIL under NC:\n' + output
    assert 'FAIL translate_called_once' in output, \
        'expected translate_called_once to FAIL under NC:\n' + output


if __name__ == '__main__':
    if not _node_deps_available():
        print('SKIP: node + jsdom not available')
    else:
        print(_run(nc=False))
        print('--- NC ---')
        print(_run(nc=True))
