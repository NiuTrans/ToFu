"""jsdom guard: entering Paper Mode must paint the overlay INSTANTLY.

Regression (2026-07): after the two-bundle split, ``enterPaperMode()`` used to
``await _loadPaperLibrary()`` (a ``/api/paper/library`` round-trip + one-time
legacy migration) BEFORE swapping the view. On a slow connection the click felt
dead — the chat view stayed up until the fetch resolved, with zero feedback.

The fix reorders ``enterPaperMode`` into two phases:
  • Phase 1 (synchronous, no await): swap the view — show
    ``#paperModeContainer`` (display:flex), hide ``.chat-wrapper`` /
    ``.input-area``, render a bookshelf skeleton + landing placeholder.
  • Phase 2 (after ``await _loadPaperLibrary()``): resolve/restore the active
    paper and hydrate the bookshelf.

This guard loads the REAL shipped ``static/js/paper-reader.js`` under jsdom with
a DEFERRED ``Api.paper.libraryList`` (a promise we resolve manually). It asserts
that WHILE the library fetch is still pending:
  • ``#paperModeContainer`` is already ``display:flex`` (overlay painted);
  • ``.chat-wrapper`` / ``.input-area`` are already hidden;
  • the bookshelf shows the loading skeleton (``.paper-lib-loading``).
Then it resolves the fetch and asserts the bookshelf hydrates
(``.paper-lib-item`` rendered) — i.e. the paint is NOT gated on the fetch, yet
the library still populates once it lands.

DB-free by construction: every endpoint is stubbed via the JS ``Api`` object.

Negative-control (source-level): a COPY of paper-reader.js is patched to
``await _loadPaperLibrary()`` FIRST (the pre-fix order), and the harness must
then FAIL the paint-before-fetch check. The shipped file is never modified.

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
  '<div id="sidebar"></div>' +
  '<div class="chat-wrapper"></div>' +
  '<div class="input-area"></div>' +
  '<div id="paperModeContainer" style="display:none"></div>' +
  '<button id="paperModeBtn"></button>' +
  '<div id="paperLibraryList"></div>' +
  '<span id="paperLibCount"></span>' +
  '<div id="paperPdfViewer"></div>' +
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
win.Icon = global.Icon = () => '<svg></svg>';
win.debugLog = global.debugLog = () => {};

// ── Deferred library fetch: resolves only when we call resolveLibrary(). ──
let resolveLibrary;
const libraryPromise = new Promise((res) => { resolveLibrary = res; });
global.Api = win.Api = { paper: {
  libraryList: () => libraryPromise,
}};

// Skip the one-time legacy migration network path.
localStorage.setItem('paper_library_migrated_v1', '1');

eval(fs.readFileSync(process.argv[2], 'utf8'));  // paper-reader.js (real, shipped)

// Stub helpers that touch unrelated subsystems (kept out of scope for the
// paint-timing assertion). We deliberately do NOT stub _renderPaperLibrary /
// _showPaperLanding / the view swap — those are what we're testing.
_loadPaperPdf = () => {};
_switchPaperTab = () => {};
_setPaperMobileView = () => {};
_populatePaperReportModelDropdown = () => {};
_applyReaderPrefs = () => {};
_updatePaperTitles = () => {};
if (typeof toggleSidebar === 'undefined') { global.toggleSidebar = win.toggleSidebar = () => {}; }

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  const container = document.getElementById('paperModeContainer');
  const chatWrapper = document.querySelector('.chat-wrapper');
  const inputArea = document.querySelector('.input-area');
  const listEl = document.getElementById('paperLibraryList');

  // Fire enterPaperMode but DO NOT await it — phase 1 runs synchronously up to
  // the first `await _loadPaperLibrary()`, then the promise is pending on our
  // deferred libraryPromise.
  const entering = enterPaperMode();

  // Let any pre-await microtasks settle (there should be none before the await,
  // but be robust): a single macrotask turn. libraryPromise is STILL pending.
  await new Promise(r => setTimeout(r, 0));

  // ── DECISIVE: overlay is painted while the library fetch is still pending ──
  check('paint_container_flex_before_fetch', container.style.display === 'flex');
  check('paint_chat_hidden_before_fetch', chatWrapper.style.display === 'none');
  check('paint_input_hidden_before_fetch', inputArea.style.display === 'none');
  check('paint_skeleton_before_fetch',
        listEl.innerHTML.indexOf('paper-lib-loading') !== -1);
  check('paint_btn_active_before_fetch',
        document.getElementById('paperModeBtn').classList.contains('active'));

  // ── Now resolve the fetch and let phase 2 hydrate the bookshelf. ──
  resolveLibrary({ ok: true, papers: [
    { id: 'paper-1', title: 'Test Paper', createdAt: Date.now() },
  ]});
  await entering;
  for (let i = 0; i < 10; i++) { await new Promise(r => setTimeout(r, 0)); }

  check('hydrate_library_item_after_fetch',
        listEl.innerHTML.indexOf('paper-lib-item') !== -1);
  check('hydrate_skeleton_gone_after_fetch',
        listEl.innerHTML.indexOf('paper-lib-loading') === -1);
  check('hydrate_count_badge',
        document.getElementById('paperLibCount').textContent === '1');

  console.log(out.join('\n'));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _run_harness(paper_js: str) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_paper_instant_paint_harness.js')
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
def test_enter_paper_mode_paints_before_library_fetch():
    proc = _run_harness(PAPER_JS)
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{out}'
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'paper instant-paint failures:\n' + out
    assert out.count('PASS') >= 8, f'expected >=8 PASS lines, got:\n{out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_source_level_negative_control_await_first_reintroduces_lag():
    """Revert to awaiting the library BEFORE the view swap; prove the guard FAILS.

    We patch a COPY of paper-reader.js so ``enterPaperMode`` awaits
    ``_loadPaperLibrary()`` before painting (the pre-fix order). The harness
    must then FAIL the paint-before-fetch checks, proving the guard is
    load-bearing. The shipped file is never modified.
    """
    src = open(PAPER_JS, encoding='utf-8').read()

    # The fix marker: phase 1 begins right after we set paperMode = true, with
    # NO await before the view swap. Reintroduce the await there.
    marker = "  paperMode = true;\n\n  // ── Phase 1: paint the overlay IMMEDIATELY"
    assert marker in src, 'fix marker not found — test is stale, update the marker'
    broken = src.replace(
        marker,
        "  paperMode = true;\n"
        "  try { await _loadPaperLibrary(); } catch (e) {}\n\n"
        "  // ── Phase 1: paint the overlay IMMEDIATELY",
        1,
    )
    assert broken != src, 'negative-control patch was a no-op'

    tmp = os.path.join(HERE, '_paper_reader_await_first.js')
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(broken)
    try:
        chk = subprocess.run(['node', '--check', tmp], capture_output=True, text=True, timeout=30)
        assert chk.returncode == 0, f'patched JS invalid: {chk.stderr}'
        proc = _run_harness(tmp)
        out = proc.stdout.strip()
        assert proc.returncode == 0, f'node crashed: {proc.stderr}\n{out}'
        # Awaiting first: the view swap never happens before we resolve the
        # fetch → the container stays hidden → this check MUST flip to FAIL.
        assert 'FAIL paint_container_flex_before_fetch' in out, \
            'awaiting the library first did NOT delay the paint — guard is non-load-bearing:\n' + out
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    assert open(PAPER_JS, encoding='utf-8').read() == src, 'shipped file was modified!'


if __name__ == '__main__':
    test_enter_paper_mode_paints_before_library_fetch()
    print('positive: PASS')
    test_source_level_negative_control_await_first_reintroduces_lag()
    print('negative-control: PASS')
    print('ALL PASSED')
