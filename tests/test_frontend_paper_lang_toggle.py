"""jsdom test for the per-paper EN/中 language toggle on Report + Review.

Loads the REAL shipped ``static/js/paper-reader.js`` under jsdom and drives the
toggle logic:

  REPORT (generation language, per-paper, persisted):
    • _activeReportLang defaults to the UI language on first open;
    • _setReportLang('zh') persists per paper id, resets local state, drops the
      in-memory cache, and (on the report tab) routes through
      _loadOrGenerateReport for the new (paper_hash, lang) cache key;
    • switching to a NEVER-SHOWN language does NOT auto-generate — it renders the
      manual-start prompt (Generate button) so the user can still tune the model
      before the run begins, per the no-autostart user preference (2026-07-02,
      guarded by test_frontend_paper_no_autostart.py). Zero /report/start fires
      on the switch itself; the button click is what starts generation;
    • the choice survives a "reload" (persisted in localStorage, re-derived);
    • REGRESSION: toggling BACK to a language already shown this session
      repaints from the in-memory snapshot and does NOT regenerate, even when
      the server DB cache misses (the reported bug: en→zh regenerated Chinese).

  REVIEW (reading language, bidirectional, ALWAYS available — not UI-gated):
    • _activeReviewLang defaults to 'en' (English is canonical);
    • _setReviewLang('zh') requests the translate task and shows the translated
      view WITHOUT regenerating the English review;
    • _setReviewLang('en') restores the canonical English render;
    • the segmented control is synced even when the app UI language is English
      (the old bug: the translate button was hidden unless UI=zh).

Negative controls (in-harness, source-toggle proven separately):
    • report: after _setReportLang('zh'), the persisted map MUST read 'zh' and
      the switch to a never-shown language MUST render the manual Generate prompt
      with ZERO auto-issued /report/start — flipping _persistReportLang to a
      no-op makes the reload-persistence check FAIL;
    • review: _setReviewLang('zh') MUST call translateStart and NOT reportStart
      (never regenerates the English review).

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
  '<div id="paperReviewContent"></div>' +
  // Segmented controls, mirroring index.html.
  '<div id="reportLangToggle">' +
  '  <button class="paper-report-lang-opt" data-lang="en"></button>' +
  '  <button class="paper-report-lang-opt" data-lang="zh"></button>' +
  '</div>' +
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

const calls = { report: [], translateStart: 0, translateCache: 0 };
global.Api = win.Api = { paper: {
  libraryList: async () => ({ ok: true, papers: [{ id: 'paper-1', title: 'P', paperHash: 'phash-1' }] }),
  reportLookup: async () => ({ ok: false }),
  reportCache:  async () => ({ ok: false }),
  reportStart:  async (body) => { calls.report.push(body); return { ok: true, task_id: 'rpt_1', paper_hash: 'phash-1' }; },
  reportPoll:   async () => ({ ok: true, status: 'done', report: 'NEW', next_cursor: 0, events: [] }),
  reportAbort:  async () => ({ ok: true }),
  // Review translation path.
  translateCache: async () => { calls.translateCache++; return { ok: false }; },
  translateStart: async () => { calls.translateStart++; return { ok: true, task_id: 'tr_1', paper_hash: 'phash-1' }; },
  translatePoll:  async () => ({ ok: true, status: 'done', next_cursor: 1,
                                 json: async () => ({ ok: true, status: 'done', next_cursor: 1,
                                                      events: [{ type: 'done', text: '中文译文' }] }) }),
  translateAbort: async () => ({ ok: true }),
}};

localStorage.setItem('paper_active_id', 'paper-1');
localStorage.setItem('paper_library_migrated_v1', '1');

eval(fs.readFileSync(process.argv[2], 'utf8'));  // paper-reader.js (real, shipped)

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Stub helpers touching unrelated subsystems.
_getActivePaperEntry = () => null;
_renderReportSkeleton = (c) => { if (c) c.innerHTML = '<div class="skeleton"></div>'; };
_renderFinalReport = (c, text) => { if (c) c.innerHTML = '<pre>' + escapeHtml(text || '') + '</pre>'; };
_populatePaperReportModelDropdown = () => {};
_teardownReadingTracker = () => {};

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
_i18nLang = 'en';   // deliberately English UI — the review toggle must STILL work

(async () => {
  for (let i = 0; i < 10; i++) { await new Promise(r => setTimeout(r, 0)); }
  _activePaperId = 'paper-1';

  // ── REPORT: default lang = UI (en) ──
  check('report_default_en', _activeReportLang() === 'en');

  // ── REPORT: switch to zh from the report tab → persist + generate in zh ──
  _paperActiveTab = 'report';
  const startsBeforeZh = calls.report.length;
  _setReportLang('zh', 'report');
  for (let i = 0; i < 20; i++) { await new Promise(r => setTimeout(r, 0)); }
  check('report_now_zh', _activeReportLang() === 'zh');
  check('report_persisted_zh',
        (JSON.parse(localStorage.getItem('paper_report_lang_by_id')) || {})['paper-1'] === 'zh');
  // Switching to a NEVER-SHOWN language must NOT auto-generate (no-autostart
  // preference); it renders the manual Generate prompt and fires zero
  // /report/start. Generation is user-initiated by the button.
  check('report_switch_zh_no_autostart', calls.report.length === startsBeforeZh);
  check('report_switch_zh_shows_generate_btn',
        document.getElementById('paperReportContent')
          .querySelector('.paper-report-generate-btn') !== null);

  // ── REPORT: persistence survives a "reload" (re-derive from localStorage) ──
  // Simulate reload: wipe the in-memory derivation path by clearing caches;
  // _activeReportLang reads localStorage fresh every call.
  check('report_reload_reads_zh', _activeReportLang() === 'zh');

  // ── REPORT: switching back to en (also never shown this session) again
  //    shows the manual Generate prompt, not an auto-generate. ──
  const beforeEn = calls.report.length;
  _setReportLang('en', 'report');
  for (let i = 0; i < 20; i++) { await new Promise(r => setTimeout(r, 0)); }
  check('report_back_en', _activeReportLang() === 'en');
  check('report_switch_en_no_autostart', calls.report.length === beforeEn);
  check('report_switch_en_shows_generate_btn',
        document.getElementById('paperReportContent')
          .querySelector('.paper-report-generate-btn') !== null);

  // ── REGRESSION (the reported bug): toggling BACK to a language already
  //    shown this session must repaint from the in-memory snapshot and NOT
  //    regenerate — EVEN when the server DB cache misses (reportCache stub
  //    returns {ok:false} here, exactly the log-observed failure). ──
  _setReportLang('zh', 'report');   // land on zh
  for (let i = 0; i < 20; i++) { await new Promise(r => setTimeout(r, 0)); }
  // Simulate zh having been rendered to screen this session.
  _rememberReportSnapshot(_reportView('report'), 'ZH_SNAPSHOT_BODY', null);
  _paperReportCache = 'ZH_SNAPSHOT_BODY';
  _setReportLang('en', 'report');   // regenerate in en (expected/fine)
  for (let i = 0; i < 20; i++) { await new Promise(r => setTimeout(r, 0)); }
  const startsAfterEn = calls.report.length;
  _setReportLang('zh', 'report');   // back to zh — MUST NOT regenerate
  for (let i = 0; i < 20; i++) { await new Promise(r => setTimeout(r, 0)); }
  check('report_toggle_back_no_regen', calls.report.length === startsAfterEn);
  check('report_toggle_back_repaints_snapshot',
        document.getElementById('paperReportContent').innerHTML.indexOf('ZH_SNAPSHOT_BODY') !== -1);

  // ── REVIEW: default reading lang = en (English canonical) ──
  check('review_default_en', _activeReviewLang() === 'en');

  // ── REVIEW: with a generated English review, switch to zh → translate,
  //    NOT regenerate. The toggle works even though _i18nLang === 'en'. ──
  const reportCallsBeforeReview = calls.report.length;
  _paperReviewCache = 'ENGLISH REVIEW BODY';   // a review exists
  _paperActiveTab = 'review';
  await _setReviewLang('zh');
  for (let i = 0; i < 30; i++) { await new Promise(r => setTimeout(r, 0)); }
  check('review_now_zh', _activeReviewLang() === 'zh');
  check('review_translate_called', calls.translateStart === 1);
  check('review_did_not_regenerate', calls.report.length === reportCallsBeforeReview);
  check('review_persisted_zh',
        (JSON.parse(localStorage.getItem('paper_review_lang_by_id')) || {})['paper-1'] === 'zh');
  const revHtml = document.getElementById('paperReviewContent').innerHTML;
  check('review_shows_translation', revHtml.indexOf('中文译文') !== -1);

  // ── REVIEW: back to en restores the canonical English render (no new
  //    translate call). ──
  const trBefore = calls.translateStart;
  await _setReviewLang('en');
  for (let i = 0; i < 10; i++) { await new Promise(r => setTimeout(r, 0)); }
  check('review_back_en', _activeReviewLang() === 'en');
  check('review_no_new_translate_on_en', calls.translateStart === trBefore);
  const revHtml2 = document.getElementById('paperReviewContent').innerHTML;
  check('review_restores_english', revHtml2.indexOf('ENGLISH REVIEW BODY') !== -1);

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


def _run():
    harness = os.path.join(HERE, '_paper_lang_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, os.path.join(JS_DIR, 'paper-reader.js'), ROOT],
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
    assert not fails, 'paper lang-toggle failures:\n' + output
    assert output.count('PASS') >= 16, f'expected >=16 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_paper_language_toggle_report_and_review():
    _run()


if __name__ == '__main__':
    if not _node_deps_available():
        print('SKIP: node + jsdom not available')
    else:
        _run()
        print('PASS: paper language toggle (report + review)')
