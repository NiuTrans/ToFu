"""jsdom regression for the Review-tab venue PERSISTENCE skew.

Bug (2026-07): a user finishes generating a review, comes back later, and the
Review tab re-prompts "Generate" as if nothing was produced. Root cause: the
review's DB row is keyed by the composite ``review:<venue>:<uilang>``, but the
venue a review was ACTUALLY generated under was persisted per-paper ONLY when
the user clicked the venue dropdown explicitly (``_persistReviewVenue`` on an
explicit ``_selectReviewVenue``). A review generated under the silently
auto-resolved venue (or any in-session, non-explicit selection) left NOTHING in
``paper_review_venue_by_id``. On reload ``_resolveReviewVenue`` fell back to the
registry-FIRST default, which need not equal the venue the stored review used →
the lookup key (``review:<first>:en``) missed the stored row
(``review:<actual>:en``) → the "Generate" prompt reappeared even though the
review exists.

Fix: ``_persistGeneratedReviewVenue`` persists the venue parsed from the
composite langKey on EVERY terminal-success path (poll ``done`` / ``done``
event / ``/start`` cache-hit / lookup-cache reconnect), so a reload resolves the
same venue and its lookup key matches the stored row.

This harness loads the REAL shipped ``static/js/paper/report.js`` (+ the core
``paper-reader.js`` for shared helpers) under jsdom, models a tiny server DB
keyed by the composite langKey, and asserts:
  • a review generated under venue ``iclr`` (a NON-registry-first venue that is
    resolved in-session, NOT via an explicit dropdown click) is found on reload
    via a cache HIT under ``review:iclr:en`` and paints the report — the
    "Generate" prompt is NEVER shown;
  • the persisted per-paper venue after generation is ``iclr``.

Source-level negative control: neuter ``_persistGeneratedReviewVenue`` to a
no-op (the pre-fix silent-no-persist behaviour). Reload then re-resolves the
registry-first default (``neurips``), the lookup key ``review:neurips:en``
misses the stored ``review:iclr:en`` row, and the "Generate" prompt reappears →
the test FAILS. The shipped file is restored byte-identical afterwards.

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
JS_DIR = os.path.join(ROOT, 'static', 'js')
REPORT_JS = os.path.join(JS_DIR, 'paper', 'report.js')   # holds _persistGeneratedReviewVenue
CORE_JS = os.path.join(JS_DIR, 'paper-reader.js')
REPORT_RUNTIME_TS = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'paper', 'report-runtime.ts')
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
  '<button class="paper-tab-btn" data-tab="qa"></button>' +
  '<button class="paper-tab-btn" data-tab="report"></button>' +
  '<button class="paper-tab-btn" data-tab="review"></button>' +
  '<div class="paper-tab-panel" data-tab="qa"></div>' +
  '<div class="paper-tab-panel" data-tab="report"></div>' +
  '<div class="paper-tab-panel" data-tab="review">' +
  '  <span id="paperReviewVenueLabel"></span>' +
  '  <div id="paperReviewVenueDropdown"></div>' +
  '  <div id="paperReviewModelDropdown"></div>' +
  '  <span id="paperReviewModelLabel"></span>' +
  '  <div id="paperReviewContent"></div>' +
  '</div>' +
  '<div id="paperReportContent"></div>' +
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

// Tiny server DB keyed by the COMPOSITE langKey, exactly like paper_reports.
const db = {};                      // langKey -> report body
const calls = { start: [], cache: [], lookup: [] };
let lastStartLang = '';

global.Api = win.Api = { paper: {
  libraryList: async () => ({ ok: true, papers: [{ id: 'paper-1', title: 'P', paperHash: 'phash-1' }] }),
  reviewVenues: async () => {
    await new Promise(r => setTimeout(r, 5));
    // NeurIPS is registry-FIRST — so the silent auto-default is neurips, which
    // must DIFFER from the venue we generate under (iclr) to expose the skew.
    return { ok: true, venues: [
      { key: 'neurips', name: 'NeurIPS' },
      { key: 'iclr', name: 'ICLR' },
      { key: 'acl', name: 'ACL (ARR)' },
    ] };
  },
  reportLookup: async (hash, lang) => { calls.lookup.push(lang || ''); return { ok: false }; },
  reportCache:  async (body) => {
    const lang = (body && body.lang) || '';
    calls.cache.push(lang);
    if (db[lang]) return { ok: true, report: db[lang], paper_hash: 'phash-1', meta: null };
    return { ok: false };
  },
  reportStart:  async (body) => {
    calls.start.push(body);
    lastStartLang = body.lang || '';
    return { ok: true, task_id: 'rvw_new_1', paper_hash: 'phash-1' };
  },
  // Poll returns done + report AND (like the engine) persists under the
  // composite lang the task was started with. _pollReportTask does
  // `await resp.json()`, so the mock must be Response-shaped (ok + json()).
  reportPoll:   async () => {
    if (lastStartLang) db[lastStartLang] = 'REVIEW BODY';
    return { ok: true, status: 200,
             json: async () => ({ ok: true, status: 'done', report: 'REVIEW BODY',
                                  next_cursor: 0, events: [] }) };
  },
  reportAbort:  async () => ({ ok: true }),
}};

localStorage.setItem('paper_active_id', 'paper-1');
localStorage.setItem('paper_library_migrated_v1', '1');

eval(fs.readFileSync(process.argv[2], 'utf8'));  // paper/report.js
if (process.argv[4]) eval(fs.readFileSync(process.argv[4], 'utf8'));  // paper-reader.js core
eval(fs.readFileSync(process.argv[5], 'utf8'));
eval(fs.readFileSync(process.argv[6], 'utf8'));
Object.keys(win).forEach((name) => {
  if (name.startsWith('_') && typeof win[name] === 'function') global[name] = win[name];
});

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Stubs for unrelated subsystems.
win._getActivePaperEntry = _getActivePaperEntry = () => ({ id: 'paper-1', title: 'P' });
win._saveActivePaperState = _saveActivePaperState = () => {};
win._renderReportSkeleton = _renderReportSkeleton = (c) => { if (c) c.innerHTML = '<div class="skeleton"></div>'; };
win._syncReportToolbar = _syncReportToolbar = () => {};
win._renderPaperQA = _renderPaperQA = () => {};
win._populatePaperReportModelDropdown = _populatePaperReportModelDropdown = () => {};
let finalRenders = 0;
win._renderFinalReport = _renderFinalReport = (c, txt) => { finalRenders++; if (c) c.innerHTML = '<pre>' + escapeHtml(txt || '') + '</pre>'; };
// Track whether the manual Generate prompt was ever shown (the reported bug).
let startPromptShown = 0;
_renderReportStartPrompt = (view) => {
  startPromptShown++;
  var c = document.getElementById((view && view.containerId) || 'paperReviewContent');
  if (c) c.innerHTML = '<div class="paper-report-empty">GENERATE</div>';
};
if (typeof toggleSidebar === 'undefined') { global.toggleSidebar = win.toggleSidebar = () => {}; }

(async () => {
  for (let i = 0; i < 10; i++) { await new Promise(r => setTimeout(r, 0)); }

  // ── Session 1: paper loaded, venue list ready, and the venue resolved
  //    IN-SESSION to a NON-first venue (iclr) WITHOUT an explicit dropdown
  //    click — i.e. exactly the state _resolveReviewVenue's silent default (or
  //    any non-explicit selection) leaves, which the old code never persisted. ──
  _activePaperId = 'paper-1';
  _paperParsedText = 'x'.repeat(500);
  _paperHash = 'phash-1';
  _paperReviewModel = 'some-model';
  _i18nLang = 'en';
  Object.assign(win, {
    _activePaperId, _paperParsedText, _paperHash, _paperReviewModel,
    _paperReviewVenue, _paperReviewVenues, _i18nLang,
  });
  localStorage.removeItem('paper_review_venue_by_id');
  await _ensureReviewVenues();
  // Silent selection (no persist) — models the auto-resolved / carried venue.
  _selectReviewVenue('iclr', true);
  check('session1_venue_iclr', _paperReviewVenue === 'iclr');
  check('session1_not_persisted_by_silent_select',
        (JSON.parse(localStorage.getItem('paper_review_venue_by_id') || '{}'))['paper-1'] === undefined);

  // Generate the review (explicit user Generate). Runs under review:iclr:en.
  _generatePaperReview();
  for (let i = 0; i < 60; i++) { await new Promise(r => setTimeout(r, 0)); }
  check('generated_under_iclr', calls.start.length === 1 && calls.start[0].lang === 'review:iclr:en');
  check('db_has_iclr_row', db['review:iclr:en'] === 'REVIEW BODY');
  // THE FIX: the venue actually generated under is now persisted per-paper,
  // even though the selection was silent (not an explicit dropdown click).
  const persisted = JSON.parse(localStorage.getItem('paper_review_venue_by_id') || '{}');
  check('generated_venue_persisted', persisted['paper-1'] === 'iclr');

  // ── Session 2 (hard reload): wipe ALL in-session state (a real reload loses
  //    _paperReviewVenue / stream / cache / venue list), re-enter Review. The
  //    persisted venue (iclr) must be resolved so the lookup key matches the
  //    stored review:iclr:en row → cache HIT → report painted, NO Generate. ──
  calls.start.length = 0; calls.cache.length = 0; calls.lookup.length = 0;
  startPromptShown = 0; finalRenders = 0;
  _paperReviewVenue = '';
  _paperReviewVenues = [];
  _paperReviewStream = null;
  _paperReviewCache = '';
  Object.assign(win, {
    _paperReviewVenue, _paperReviewVenues, _paperReviewStream,
    _paperReviewCache,
  });
  _switchPaperTab('qa');
  _switchPaperTab('review');
  for (let i = 0; i < 60; i++) { await new Promise(r => setTimeout(r, 0)); }

  check('reload_resolved_iclr', _paperReviewVenue === 'iclr');
  check('reload_cache_lookup_used_iclr',
        calls.cache.length >= 1 && calls.cache.every(k => k === 'review:iclr:en'));
  check('reload_cache_hit_rendered', finalRenders >= 1);
  // The decisive regression assertion: the finished review must NOT re-prompt.
  check('reload_no_generate_prompt', startPromptShown === 0);
  check('reload_no_new_generation', calls.start.length === 0);

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


def _write_harness() -> str:
    harness = os.path.join(HERE, '_review_venue_persist_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    return harness


def _run_harness(report_js_path: str, core_js: str = CORE_JS):
    harness = _write_harness()
    try:
        with compiled_typescript(
            REPORT_RUNTIME_TS,
            expose_feature_registry_to_window=True,
        ) as runtime_js:
            with compiled_typescript(
                SESSION_TS,
                expose_feature_registry_to_window=True,
            ) as session_js:
                proc = subprocess.run(
                    ['node', harness, report_js_path, ROOT, core_js,
                     runtime_js, session_js],
                    capture_output=True, text=True, timeout=60,
                )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    return proc


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_generated_review_venue_persisted_so_reload_hits_cache():
    proc = _run_harness(REPORT_JS)
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{out}'
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'review venue-persistence failures:\n' + out
    assert out.count('PASS') >= 9, f'expected >=9 PASS lines, got:\n{out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_source_level_negative_control_no_persist_reintroduces_skew():
    """Neuter _persistGeneratedReviewVenue to a no-op (pre-fix silent-no-persist)
    and prove the reload skew reappears. The real shipped file is never modified.
    """
    src = open(REPORT_JS, encoding='utf-8').read()

    # The fix's body persists the venue parsed from the composite langKey.
    # Replace that body with an immediate return → the venue a silently-resolved
    # review was generated under is no longer remembered (the old behaviour).
    marker = (
        "function _persistGeneratedReviewVenue(view, langKey, paperId) {\n"
        "  if (!view || view.kind !== 'review') return;")
    assert marker in src, 'fix marker not found — test is stale, update the marker'
    broken = src.replace(
        marker,
        "function _persistGeneratedReviewVenue(view, langKey, paperId) {\n"
        "  if (true) return;  // NC: silent-no-persist (pre-fix behaviour)\n"
        "  if (!view || view.kind !== 'review') return;",
        1,
    )
    assert broken != src, 'negative-control patch was a no-op'

    tmp = os.path.join(HERE, '_report_no_persist.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(broken)
    try:
        chk = subprocess.run(['node', '--check', tmp], capture_output=True, text=True, timeout=30)
        assert chk.returncode == 0, f'patched JS invalid: {chk.stderr}'
        proc = _run_harness(tmp)
        out = proc.stdout.strip()
        assert proc.returncode == 0, f'node crashed: {proc.stderr}\n{out}'
        # Without persistence, reload re-resolves the registry-first default
        # (neurips), whose lookup key misses the stored review:iclr:en row →
        # the Generate prompt reappears / a fresh generation fires.
        assert ('FAIL generated_venue_persisted' in out
                or 'FAIL reload_resolved_iclr' in out
                or 'FAIL reload_cache_lookup_used_iclr' in out
                or 'FAIL reload_no_generate_prompt' in out
                or 'FAIL reload_no_new_generation' in out), \
            'neutering the persist did NOT reintroduce the skew — fix may be non-load-bearing:\n' + out
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    assert open(REPORT_JS, encoding='utf-8').read() == src, 'shipped file was modified!'


if __name__ == '__main__':
    import sys
    if not _node_deps_available():
        print('SKIP — node + jsdom not available')
        sys.exit(0)
    test_generated_review_venue_persisted_so_reload_hits_cache()
    print('positive: PASS')
    test_source_level_negative_control_no_persist_reintroduces_skew()
    print('negative-control: PASS')
    print('ALL PASSED')
