"""jsdom guard: Reading-Mode responsive fold/orientation crossing (tablets/foldables).

Requirement (2026-07-06): on a portrait tablet / unfolded foldable the paper
reader must show a single full-screen pane + bottom switcher, and — the crux —
a *fold / orientation crossing* must re-assert which pane is shown and refit the
PDF to the now-correct width. Layout is decided in CSS by the predicate
``(max-width:768px),(max-width:1024px) and (pointer:coarse)``; paper-reader.js
mirrors that predicate in ``matchMedia('(max-width:1024px) and (pointer:coarse)')``
and, on a crossing, runs ``_paperResponsiveOnCrossing()`` which:

  • when the single-pane predicate matches, guarantees a pane is shown —
    defaulting ``data-paper-view`` to ``'pdf'`` when the body has none yet
    (a body that only ever lived in the desktop split has no view attr, so
    entering single-pane would otherwise show NEITHER pane); and
  • rAF-defers ``paperFitWidth()`` because the pane just changed width and a PDF
    laid out at the old width now overflows / under-fills.

The harness loads the REAL shipped ``static/js/paper-reader.js`` under jsdom
with a controllable ``matchMedia`` MediaQueryList (so we can flip ``matches``
to simulate a fold-in / fold-out), a spy ``paperFitWidth``, and the REAL
``_setPaperMobileView``. rAF/setTimeout run synchronously so the coalesced
crossing + the deferred fit both fire in-test.

Triple-neuter (each on a COPY; shipped file byte-identical after):
  • NC-1 (load-bearing default-to-pdf): drop the ``cur = 'pdf'`` default so a
    crossing into single-pane with no view leaves the body with NEITHER pane
    shown → the view-assert check FAILS.
  • NC-2 (load-bearing refit): remove the ``paperFitWidth()`` call from the
    crossing handler so a fold leaves the PDF mis-sized → the refit check FAILS.
  • NC-3 (predicate mirror): break the matchMedia predicate to ``min-width``
    (never matches the tablet case) so a crossing into the tablet width does
    NOT enter single-pane → the view-assert check FAILS.

DB-free by construction; skips cleanly when node + jsdom aren't installed.
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
PAPER_TS = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'paper', 'pdf-responsive.ts')
ESBUILD = os.path.join(ROOT, 'scripts', 'vite_test_bundle.mjs')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));

// A desktop-origin body: the split view never needs [data-paper-view], so a
// fresh body entering single-pane has NONE — the exact case the default-to-pdf
// branch exists for. Include the switcher buttons + a PDF viewer container.
const dom = new JSDOM(
  '<!DOCTYPE html><body>' +
  '<div class="paper-mode-container">' +
    '<div class="paper-body">' +
      '<div class="paper-left"><div class="paper-pdf-container" id="paperPdfViewer">' +
        '<div class="paper-page-wrapper" style="width:900px"></div>' +
      '</div></div>' +
      '<div class="paper-divider" id="paperDivider"></div>' +
      '<div class="paper-right"></div>' +
    '</div>' +
    '<div class="paper-mobile-switch" id="paperMobileSwitch">' +
      '<button class="paper-mobile-switch-btn active" data-view="pdf"></button>' +
      '<button class="paper-mobile-switch-btn" data-view="reader"></button>' +
    '</div>' +
  '</div>' +
  '</body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.localStorage = win.localStorage;
global.console = console;
global.HTMLElement = win.HTMLElement;
global.MouseEvent = win.MouseEvent;
global.TouchEvent = win.TouchEvent;
global.AbortController = win.AbortController;

// Synchronous rAF/setTimeout so the coalescing frame AND the deferred fit both
// run inline. (The crossing handler nests a second rAF for the fit.)
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => { fn(); return 0; };
global.setTimeout = win.setTimeout = (fn) => { if (typeof fn === 'function') fn(); return 0; };

// ── Controllable MediaQueryList so we can simulate a fold in/out. ──
// The module captures matchMedia at eval time into _singlePaneMq; we hand it
// this object and later flip .matches + dispatch its 'change' listeners.
const mql = {
  matches: false,
  media: '',
  _listeners: [],
  addEventListener(_ev, fn) { this._listeners.push(fn); },
  removeEventListener(_ev, fn) { this._listeners = this._listeners.filter(f => f !== fn); },
  dispatch() { this._listeners.slice().forEach(fn => fn({ matches: this.matches })); },
};
win.matchMedia = global.matchMedia = (q) => { mql.media = q; return mql; };
win.__mql = mql;

// Spy fit-to-width. The REAL paperFitWidth needs a live pdf.js doc; we only
// care THAT it's called on a crossing, so replace it after eval.
let fitCalls = 0;
const setMobileView = (view) => {
  const selected = view === 'reader' ? 'reader' : 'pdf';
  document.querySelector('.paper-body').setAttribute('data-paper-view', selected);
  document.querySelectorAll('.paper-mobile-switch-btn').forEach((button) => {
    button.classList.toggle('active', button.dataset.view === selected);
  });
};
global._setPaperMobileView = win._setPaperMobileView = setMobileView;

const responsiveSource = fs.readFileSync(process.argv[2], 'utf8');
(0, eval)(responsiveSource);  // compiled TypeScript owner
eval(responsiveSource);  // mixed Vite/classic retry must remain single-owner

// Post-eval overrides. _setPaperMobileView stays REAL (it's under test).
paperFitWidth = win.paperFitWidth = () => { fitCalls++; };
_paperPdfDoc = win._paperPdfDoc = { numPages: 3 };  // truthy so refit is attempted

// jsdom evals with document.readyState==='loading', so the module deferred its
// init to DOMContentLoaded (exactly like a real browser page load). Fire it now
// so _initPaperResponsive() runs and wires the crossing listeners onto our mql.
win.document.dispatchEvent(new win.Event('DOMContentLoaded'));

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

function body() { return document.querySelector('.paper-body'); }

(async () => {
  // Confirm the module captured our predicate + the crossing fn is exposed.
  check('predicate_is_coarse_1024',
        String(mql.media) === '(max-width:1024px) and (pointer:coarse)');
  check('crossing_fn_exposed', typeof win._paperResponsiveOnCrossing === 'function');
  check('responsive_single_owner', mql._listeners.length === 1);

  // ── Baseline: desktop split, body has NO view attribute. ──
  check('baseline_no_view', body().getAttribute('data-paper-view') === null);

  // ── FOLD IN: predicate now matches (tablet portrait). Fire the crossing. ──
  fitCalls = 0;
  mql.matches = true;
  mql.dispatch();                 // → _scheduleCrossing → rAF → crossing handler
  // The body with no prior view must now show the PDF pane (default), and the
  // PDF must have been refit to the new pane width.
  check('foldin_view_defaults_pdf', body().getAttribute('data-paper-view') === 'pdf');
  check('foldin_refit_called', fitCalls >= 1);

  // ── User switches to Reader while in single-pane. ──
  _setPaperMobileView('reader');
  check('switch_to_reader', body().getAttribute('data-paper-view') === 'reader');

  // ── FOLD OUT then FOLD IN again: the chosen 'reader' view must be PRESERVED
  //    (default-to-pdf only kicks in when there is NO valid view). ──
  fitCalls = 0;
  mql.matches = false; mql.dispatch();   // fold out (desktop split)
  check('foldout_refit_called', fitCalls >= 1);
  mql.matches = true; mql.dispatch();    // fold back in
  check('foldin_preserves_reader', body().getAttribute('data-paper-view') === 'reader');

  // ── orientationchange also refits (pane resized even without a predicate flip). ──
  fitCalls = 0;
  win.dispatchEvent(new win.Event('orientationchange'));
  check('orientationchange_refits', fitCalls >= 1);

  console.log(out.join('\n'));
  process.exit(0);
})();
"""


def _run_harness(paper_js: str) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_paper_mobile_responsive_harness.js')
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
def test_paper_responsive_crossing_reasserts_view_and_refits(
        paper_responsive_js):
    proc = _run_harness(paper_responsive_js)
    out = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{out}'
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'paper responsive crossing failures:\n' + out
    assert out.count('PASS') >= 9, f'expected >=9 PASS lines, got:\n{out}'


@pytest.fixture(scope='module')
def paper_responsive_js():
    if not os.path.isfile(ESBUILD):
        pytest.skip('esbuild dev dependency not installed')
    with compiled_typescript(
        PAPER_TS,
        expose_feature_registry_to_window=True,
    ) as built:
        yield built


def _run_neuter(patched_src: str, tag: str, tmp_path) -> str:
    """Compile a patched TypeScript COPY and run the real-owner harness."""
    del tmp_path
    with compiled_typescript(
        PAPER_TS,
        contents=patched_src,
        expose_feature_registry_to_window=True,
    ) as built:
        proc = _run_harness(built)
    assert proc.returncode == 0, f'node crashed ({tag}): {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_triple_neuter_crossing_is_load_bearing(tmp_path):
    """Three independent neuters, each on a COPY; shipped file untouched."""
    src = open(PAPER_TS, encoding='utf-8').read()

    # ── NC-1: neuter the whole single-pane view-assert branch → a no-view body
    #    entering single-pane is NEVER given a pane → foldin_view_defaults_pdf
    #    FAILS. (The explicit cur='pdf' default is deliberately redundant with
    #    _setPaperMobileView's own invalid→pdf coercion — a belt for the
    #    _setPaperMobileView-absent fallback path — so the load-bearing unit is
    #    the branch RUNNING on a crossing, which is what this neuters.) ──
    m1 = "    if (singlePaneQuery?.matches) {"
    assert m1 in src, 'NC-1 marker not found — test is stale'
    out1 = _run_neuter(
        src.replace(m1, "    if (false && singlePaneQuery?.matches) {", 1),
        'nc1', tmp_path)
    assert 'FAIL foldin_view_defaults_pdf' in out1, \
        'NC-1: skipping the single-pane view-assert did NOT break it — non-load-bearing:\n' + out1

    # ── NC-2: remove the refit call from the crossing handler → a fold leaves
    #    the PDF mis-sized → the refit checks FAIL. ──
    m2 = "    if (typeof legacyWindow().paperFitWidth === 'function') {"
    assert m2 in src, 'NC-2 marker not found — test is stale'
    out2 = _run_neuter(
        src.replace(
            m2,
            "    if (false && typeof legacyWindow().paperFitWidth === 'function') {",
            1),
        'nc2', tmp_path)
    # The fold-OUT and orientationchange paths do NOT run the single-pane
    # view-assert (which itself refits via _setPaperMobileView('pdf')), so the
    # handler's OWN refit is the only thing that fits there — neutering it must
    # break both.
    assert 'FAIL foldout_refit_called' in out2 and 'FAIL orientationchange_refits' in out2, \
        'NC-2: removing the handler refit did NOT break the fold-out / orientation refit — non-load-bearing:\n' + out2

    # ── NC-3: break the matchMedia predicate to min-width (never matches the
    #    tablet case) → a fold-in never enters single-pane → view-assert FAILS. ──
    m3 = "'(max-width:1024px) and (pointer:coarse)'"
    assert m3 in src, 'NC-3 marker not found — test is stale'
    out3 = _run_neuter(
        src.replace(m3, "'(min-width:99999px) and (pointer:coarse)'", 1),
        'nc3', tmp_path)
    # The predicate string check pins the mirror; and with our controllable mql
    # the handler still keys off mql.matches, so the discriminating signal is
    # the predicate assertion flipping to FAIL.
    assert 'FAIL predicate_is_coarse_1024' in out3, \
        'NC-3: changing the predicate did NOT flip the predicate mirror check — non-load-bearing:\n' + out3

    assert open(PAPER_TS, encoding='utf-8').read() == src, 'shipped file was modified!'
