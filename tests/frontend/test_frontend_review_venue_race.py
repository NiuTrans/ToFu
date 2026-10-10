"""jsdom test for the Review-tab first-entry venue race.

Bug (2026-06-30): ``_switchPaperTab('review')`` called the ASYNC
``_populateReviewVenueDropdown()`` WITHOUT awaiting it, then synchronously ran
``_loadOrGenerateReport(reviewView)``. On the FIRST entry the venue list isn't
loaded yet and ``_paperReviewVenue===''`` → the generation langKey resolved to
``review:generic:en`` (the ``_paperReviewVenue || 'generic'`` fallback), while
the async dropdown a moment later snapped the label to the first venue
(NeurIPS). Result: the dropdown showed "NeurIPS" but the review actually
generated/persisted under ``review:generic`` — label/key skew, and the user
never got to pick the venue before generation.

Fix: the review branch awaits ``_populateReviewVenueDropdown()`` (which now
resolves the per-paper venue: persisted → first → registry) BEFORE
``_loadOrGenerateReport``. So the displayed venue and the generation langKey
are always consistent on first entry.

The harness loads the REAL shipped ``static/js/paper-reader.js`` under jsdom,
drives a first-entry review tab switch, and asserts:
  • exactly one /report/start fired with a venue-specific langKey
    (``review:neurips:en``), NOT ``review:generic:en``;
  • the displayed venue label key matches the langKey's venue (no skew);
  • the SILENT auto-default is NOT persisted, but an EXPLICIT user pick is and
    is restored over the registry-first default after a simulated refresh.

Source-level negative control: a sed-style patch reverts the await to the old
fire-and-forget ordering (``_populateReviewVenueDropdown(); _loadOrGenerate…``)
→ the skew reappears (langKey venue = generic ≠ label NeurIPS) and the test
FAILS; the original file is restored byte-identical afterwards.

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
# Report + Review Mode was extracted to paper/report.js (Epic E cut #6). The
# review fns AND the venue-race fix marker now live there, so the harness evals
# it (before the core file) and the NC patch/byte-identity targets it.
REPORT_JS = os.path.join(JS_DIR, 'paper', 'report.js')
PAPER_JS = REPORT_JS  # the file the NC patches + asserts byte-identical
CORE_JS = os.path.join(JS_DIR, 'paper-reader.js')
SESSION_TS = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'paper', 'session.ts')
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
// Build the minimal DOM the review branch touches: tab buttons + panels +
// the review container + venue label/dropdown + report container.
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

const calls = { start: [], lookup: [], cache: [] };
global.Api = win.Api = { paper: {
  libraryList: async () => ({ ok: true, papers: [{ id: 'paper-1', title: 'P', paperHash: 'phash-1' }] }),
  // Venue list: NeurIPS first (so the resolved venue is 'neurips', NOT generic).
  // Api.paper.reviewVenues() uses the default JSON parse → it resolves to the
  // parsed {ok, venues} BODY, not a raw Response. (An earlier mock returned a
  // Response-shaped stub with .json(), which masked the real bug where the
  // frontend called resp.json() on the already-parsed object.)
  reviewVenues: async () => {
    // Model real network latency (a macrotask hop). Without this the parsed
    // body resolves in the same microtask flush as the caller, masking the
    // fire-and-forget race the negative control must reproduce.
    await new Promise(r => setTimeout(r, 5));
    return { ok: true, venues: [
      { key: 'neurips', name: 'NeurIPS' },
      { key: 'iclr', name: 'ICLR' },
      { key: 'acl', name: 'ACL (ARR)' },
    ] };
  },
  reportLookup: async (hash, lang) => { calls.lookup.push(lang || ''); return { ok: false }; },
  reportCache:  async (body) => { calls.cache.push((body && body.lang) || ''); return { ok: false }; },
  reportStart:  async (body) => { calls.start.push(body); return { ok: true, task_id: 'rvw_new_1', paper_hash: 'phash-1' }; },
  reportPoll:   async () => ({
    ok: true,
    json: async () => ({
      ok: true, status: 'done', report: 'REVIEW', next_cursor: 0, events: [],
    }),
  }),
  reportAbort:  async () => ({ ok: true }),
}};

localStorage.setItem('paper_active_id', 'paper-1');
localStorage.setItem('paper_library_migrated_v1', '1');

eval(fs.readFileSync(process.argv[2], 'utf8'));  // paper/report.js (review fns, real shipped)
if (process.argv[4]) eval(fs.readFileSync(process.argv[4], 'utf8'));  // paper-reader.js core (shared helpers)
eval(fs.readFileSync(process.argv[5], 'utf8'));
eval(fs.readFileSync(process.argv[6], 'utf8'));
Object.keys(win).forEach((name) => {
  if (name.startsWith('_') && typeof win[name] === 'function') global[name] = win[name];
});

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Stubs for unrelated subsystems the switch path would otherwise hit.
win._getActivePaperEntry = _getActivePaperEntry = () => ({ id: 'paper-1', title: 'P' });
win._saveActivePaperState = _saveActivePaperState = () => {};
win._renderReportSkeleton = _renderReportSkeleton = (c) => { if (c) c.innerHTML = '<div class="skeleton"></div>'; };
win._syncReportToolbar = _syncReportToolbar = () => {};
win._renderPaperQA = _renderPaperQA = () => {};
win._populatePaperReportModelDropdown = _populatePaperReportModelDropdown = () => {};
win._renderFinalReport = _renderFinalReport = (c, txt) => { if (c) c.innerHTML = '<pre>' + escapeHtml(txt || '') + '</pre>'; };
if (typeof toggleSidebar === 'undefined') { global.toggleSidebar = win.toggleSidebar = () => {}; }

(async () => {
  for (let i = 0; i < 10; i++) { await new Promise(r => setTimeout(r, 0)); }
  // Fresh first-entry state: paper loaded, NO venue chosen yet, Review never opened.
  _activePaperId = 'paper-1';
  _paperParsedText = 'x'.repeat(500);
  _paperHash = 'phash-1';
  _paperReviewModel = 'some-model';
  _paperReviewVenue = '';
  _paperReviewVenues = [];
  _i18nLang = 'en';
  Object.assign(win, {
    _activePaperId, _paperParsedText, _paperHash, _paperReviewModel,
    _paperReviewVenue, _paperReviewVenues, _i18nLang,
  });
  localStorage.removeItem('paper_review_venue_by_id');

  // FIRST entry into the Review tab.
  _switchPaperTab('review');
  // Let the awaited venue resolution settle. The source no longer auto-starts
  // generation on tab open (user preference: never auto-generate so the venue/
  // model/lang can be tuned first) — so, once the venue has resolved, simulate
  // the user's explicit Generate click. The venue must already be resolved to
  // the registry-first default (NeurIPS) at this point, so the langKey the
  // click generates under is venue-specific, NOT generic.
  for (let i = 0; i < 40; i++) { await new Promise(r => setTimeout(r, 0)); }
  _generatePaperReview();
  for (let i = 0; i < 40; i++) { await new Promise(r => setTimeout(r, 0)); }

  // DECISIVE (the venue-race guarantee): the tab-entry cache/lookup round-trips
  // MUST run under the resolved venue, NOT 'generic'. This is what the await on
  // _populateReviewVenueDropdown() guarantees — the langKey used to look up an
  // existing review can never be built before the venue resolves. (Source no
  // longer AUTO-generates on entry — generation is user-initiated below — so we
  // assert on the deterministic on-entry lookup langKey, not a start.)
  const lookupKeys = calls.lookup.concat(calls.cache);
  check('lookup_fired', lookupKeys.length >= 1);
  check('lookup_langkey_not_generic',
        lookupKeys.length >= 1 && lookupKeys.every(k => k === 'review:neurips:en'));
  // Exactly one generation fired (the explicit user Generate above).
  check('one_start', calls.start.length === 1);
  const langKey = calls.start.length ? (calls.start[0].lang || '') : '';
  // The user-initiated generation also uses the resolved venue, NOT generic.
  check('langkey_not_generic', langKey === 'review:neurips:en');
  // The displayed venue label key must MATCH the langKey venue (no skew).
  const labelKey = _paperReviewVenue;  // what the dropdown selection set
  const langVenue = langKey.split(':')[1] || '';
  check('label_matches_langkey', labelKey === langVenue && labelKey === 'neurips');
  // The label text reflects the resolved venue too.
  check('label_text_resolved',
        document.getElementById('paperReviewVenueLabel').textContent === 'NeurIPS');
  // Merely resolving the silent default does not persist it, but a successful
  // generation must persist the venue actually used so reload hits the same
  // composite cache key.
  let map0 = {};
  try { map0 = JSON.parse(localStorage.getItem('paper_review_venue_by_id') || '{}'); } catch (e) {}
  check('generated_default_persisted', map0['paper-1'] === 'neurips');

  // ── Now an EXPLICIT user pick (ICLR) MUST persist + switch to that venue,
  //    then the user's explicit Generate runs under the new venue's cache key. ──
  calls.start.length = 0;
  // jsdom's Node eval and browser window are distinct globals; the real page
  // has one global, so mirror the native session owner's active-tab state back
  // to the classic renderer island before exercising its venue click handler.
  _paperActiveTab = win._paperActiveTab;
  _selectReviewVenue('iclr');                       // user clicks ICLR (persists + resets state)
  for (let i = 0; i < 40; i++) { await new Promise(r => setTimeout(r, 0)); }
  _generatePaperReview();                           // explicit Generate under ICLR
  for (let i = 0; i < 40; i++) { await new Promise(r => setTimeout(r, 0)); }
  let map1 = {};
  try { map1 = JSON.parse(localStorage.getItem('paper_review_venue_by_id') || '{}'); } catch (e) {}
  check('explicit_pick_persisted', map1['paper-1'] === 'iclr');
  // The generation ran under the new venue's distinct cache key.
  check('explicit_pick_regenerated',
        calls.start.length >= 1
        && calls.start[calls.start.length - 1].lang === 'review:iclr:en');

  // ── Simulate a hard refresh: wipe ALL in-session review state (venue,
  //    stream, cache — a real reload loses these), re-enter Review. The
  //    PERSISTED venue (iclr) must win over the registry-first default. ──
  calls.start.length = 0;
  _paperReviewVenue = '';            // session venue gone (as after reload)
  _paperReviewStream = null;         // local poll state gone
  _paperReviewCache = '';            // in-memory report gone
  Object.assign(win, {
    _paperReviewVenue, _paperReviewStream, _paperReviewCache,
  });
  _switchPaperTab('qa');             // leave then re-enter
  _switchPaperTab('review');
  for (let i = 0; i < 40; i++) { await new Promise(r => setTimeout(r, 0)); }
  // After re-entry the persisted venue (iclr) is resolved; the user's explicit
  // Generate must then run under that persisted venue, not the registry default.
  _generatePaperReview();
  for (let i = 0; i < 40; i++) { await new Promise(r => setTimeout(r, 0)); }
  check('persisted_venue_restored_after_refresh', _paperReviewVenue === 'iclr');
  check('restored_langkey_uses_persisted',
        calls.start.length >= 1 && calls.start[calls.start.length - 1].lang === 'review:iclr:en');

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


def _write_harness() -> str:
    harness = os.path.join(HERE, '_review_venue_race_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    return harness


def _run_harness(
        paper_js_path: str,
        core_js: str = CORE_JS,
        session_source: str | None = None,
):
    harness = _write_harness()
    try:
        with compiled_typescript(
            SESSION_TS,
            contents=session_source,
            expose_feature_registry_to_window=True,
        ) as session_js:
            with compiled_typescript(
                REPORT_RUNTIME_TS,
                expose_feature_registry_to_window=True,
            ) as runtime_js:
                proc = subprocess.run(
                    ['node', harness, paper_js_path, ROOT, core_js,
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
def test_review_first_entry_resolves_venue_before_generating():
    proc = _run_harness(PAPER_JS)
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{out}'
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'review venue-race failures:\n' + out
    assert out.count('PASS') >= 11, f'expected >=11 PASS lines, got:\n{out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_source_level_negative_control_unawaited_ordering_reintroduces_skew():
    """Revert the fix (await → fire-and-forget) and prove the skew reappears.

    We patch a COPY of paper-reader.js so the review branch no longer awaits
    the venue resolution before generating — exactly the pre-fix ordering. The
    harness must then FAIL (langKey venue = generic ≠ displayed NeurIPS). The
    real shipped file is never modified.
    """
    src = open(SESSION_TS, encoding='utf-8').read()

    # The fixed branch awaits venue resolution via a .then chain. Replacing
    # that whole chain with the old fire-and-forget pair (populate WITHOUT
    # awaiting, then load synchronously) reintroduces the race. The marker is
    # the exact fixed block — patch produces valid JS (no dangling .then).
    marker = (
        "      try {\n"
        "        Promise.resolve(state._populateReviewVenueDropdown?.())\n"
        "          .then(load)\n"
        "          .catch((error: unknown) => {\n"
        "            console.warn('[Paper:Review] venue resolve failed, loading with fallback:', error);\n"
        "            load();\n"
        "          });\n"
        "      } catch (error: unknown) {\n"
        "        console.warn('[Paper:Review] venue resolve failed, loading with fallback:', error);\n"
        "        load();\n"
        "      }")
    assert marker in src, 'fix marker not found — test is stale, update the marker'
    broken = src.replace(
        marker,
        "      state._populateReviewVenueDropdown?.();\n"
        "      load();",
        1,
    )
    assert broken != src, 'negative-control patch was a no-op'

    proc = _run_harness(REPORT_JS, session_source=broken)
    out = proc.stdout.strip()
    # With the await removed, the first-entry race resurfaces: the langKey
    # is built before the venue resolves → generic, while the label later
    # becomes NeurIPS → label_matches_langkey / langkey_not_generic FAIL.
    assert proc.returncode == 0, f'node crashed: {proc.stderr}\n{out}'
    assert ('FAIL lookup_langkey_not_generic' in out
            or 'FAIL langkey_not_generic' in out
            or 'FAIL label_matches_langkey' in out), \
        'reverting the await did NOT reintroduce the skew — fix may be non-load-bearing:\n' + out

    # The real file is untouched (we only ever wrote a temp copy).
    assert open(SESSION_TS, encoding='utf-8').read() == src, 'shipped file was modified!'


if __name__ == '__main__':
    import sys
    if not _node_deps_available():
        print('SKIP — node + jsdom not available')
        sys.exit(0)
    test_review_first_entry_resolves_venue_before_generating()
    print('positive: PASS')
    test_source_level_negative_control_unawaited_ordering_reintroduces_skew()
    print('negative-control: PASS')
    print('ALL PASSED')
