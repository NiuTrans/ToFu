#!/usr/bin/env python3
"""Load-generation guard for Paper Mode PDF loading (fixes "file selected but PDF failed to load").

WHY
---
``_loadPaperPdf`` is called UN-AWAITED from the sidebar-selection paths
(``_openPaperEntry`` / ``enterPaperMode``). A user who clicks paper A and then
quickly clicks paper B starts two concurrent runs that SHARE the single
``#paperPdfViewer`` element and the global doc state (``_paperPdfDoc`` /
``_paperCurrentUrl``). Only ``_renderAllPages`` had a token; ``_loadPaperPdf``
itself had no generation guard, so the SLOWER/older load — e.g. paper A's
promise REJECTING after B has already painted, or A's slow ``{data}`` download
resolving late — wrote its result (including the ``Failed to load PDF`` error
box) straight into the viewer, clobbering paper B even though B is the row
still selected in the sidebar. That is the exact reported symptom.

The fix: each ``_loadPaperPdf`` call bumps a monotonic ``_paperLoadGen`` and
captures its own ``myGen``; every viewer write and shared-state mutation bails
via ``_isStaleLoad()`` the instant a newer load supersedes it.

This harness drives the compiled native ``_loadPaperPdf`` owner
with a pdf.js stub whose per-URL open latency + success is controllable, so it
can start load A (slow + failing), start load B (fast + OK) before A settles,
let both drain, and assert the viewer shows B's pages — NOT A's error.

Skips cleanly when node/jsdom are absent.
"""

import json
import os
import shutil
import subprocess
import tempfile

import pytest

from tests._esm_feature_harness import compile_feature_owner

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIEWER_TS = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'paper', 'pdf-viewer.ts')
ESBUILD = os.path.join(ROOT, 'scripts', 'vite_test_bundle.mjs')


def _node_deps_available():
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


# ``__NEUTER__`` is replaced per run: '' for the real guarded source, '1' to
# defeat the guard (force _isStaleLoad → always false) proving the assertion bites.
_HARNESS = r"""
const fs = require('fs'), path = require('path');
const ROOT = process.argv[2];
const NEUTER = process.argv[3] === '1';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="paperPdfViewer"></div></body>',
                      { url: 'http://localhost/' });
global.window = dom.window;
global.document = dom.window.document;

const _ls = {};
global.localStorage = {
  getItem: (k) => (k in _ls ? _ls[k] : null),
  setItem: (k, v) => { _ls[k] = String(v); },
  removeItem: (k) => { delete _ls[k]; },
};

global.apiUrl = window.apiUrl = (u) => u;
global.debugLog = window.debugLog = () => {};
global.escapeHtml = window.escapeHtml = (s) => String(s == null ? '' : s);
global._saveActivePaperState = () => {};
global._getActivePaperEntry = () => null;
global._persistPaperEntry = () => {};
global._renderPaperLibrary = () => {};
global._updatePaperTitles = () => {};
global.paperFitWidth = () => {};
global._autoRefitIfOverflowing = () => {};
global._paperViewerPadX = () => 0;
global._updateZoomLabel = () => {};

dom.window.HTMLCanvasElement.prototype.getContext = () => ({});
// Eager render fallback (no IntersectionObserver) so pages rasterize inline and
// the viewer DOM settles deterministically within the awaited load.
global.ResizeObserver = window.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} };

function _delay(ms) { return new Promise((r) => setTimeout(r, ms)); }

// Per-URL controllable pdf.js stub. A url ending in FAIL rejects its page-1
// probe after `openDelay`ms (a slow, failing load). Others resolve fast.
function _makePage() {
  return {
    getViewport: () => ({ width: 600, height: 800 }),
    render: () => ({ promise: Promise.resolve() }),
    getTextContent: async () => ({ items: [] }),
  };
}
function _makeDoc(shouldFail) {
  return {
    numPages: 2,
    getPage: async (n) => {
      if (shouldFail && n === 1) throw new Error('mangled-206: page pull failed');
      return _makePage();
    },
    destroy: () => {},
  };
}
// getDocument(url) resolves the doc after a per-url delay. The FAIL doc's
// page-1 probe (inside _openPaperPdfDoc) then throws.
global.pdfjsLib = window.pdfjsLib = {
  getDocument: (param) => {
    const u = (typeof param === 'string') ? param : '(data)';
    const shouldFail = u.indexOf('FAIL') >= 0;
    const openDelay = shouldFail ? 40 : 2;
    return { promise: _delay(openDelay).then(() => _makeDoc(shouldFail)) };
  },
};
// {data} fallback for the FAIL url also fails (so A cannot recover) — its
// download resolves slowly, well after B has painted.
global.Api = window.Api = {
  paper: { pdfArrayBuffer: async () => { await _delay(60); throw new Error('data download failed too'); } },
};

let _viewerSrc = fs.readFileSync(process.argv[4], 'utf8');
if (NEUTER) {
  // Defeat the guard: rewrite _isStaleLoad to always report "current", so an
  // old load's late error/paint clobbers the newer one (the pre-fix behaviour).
  const before = _viewerSrc;
  _viewerSrc = _viewerSrc.replace(
    'function _isStaleLoad() { return myGen !== _paperLoadGen; }',
    'function _isStaleLoad() { return false; }');
  if (_viewerSrc === before) { console.log(JSON.stringify({ _threw: 'NEUTER anchor not found — guard source changed' })); process.exit(0); }
}
(0, eval)(_viewerSrc);
if (typeof window._loadPaperPdf === 'function') {
  global._loadPaperPdf = window._loadPaperPdf;
}

const viewer = document.getElementById('paperPdfViewer');
const out = {};
out.loadfn_exists = (typeof _loadPaperPdf === 'function');

(async () => {
  // Start the SLOW, FAILING load for paper A, then immediately start the FAST,
  // OK load for paper B before A settles — the real double-click race.
  const pA = _loadPaperPdf('/api/paper/pdf/A_FAIL.pdf');
  const pB = _loadPaperPdf('/api/paper/pdf/B_ok.pdf');
  await Promise.all([pA, pB]);
  // Let A's slow {data} download + late rejection fully drain.
  await _delay(120);

  const errEls = viewer.querySelectorAll('.paper-page-error, .paper-error');
  const pageEls = viewer.querySelectorAll('.paper-page-wrapper');
  const html = viewer.innerHTML;
  out.no_error_box = (errEls.length === 0);
  out.has_error_text = (html.indexOf('Failed to load PDF') >= 0);
  out.b_pages_present = (pageEls.length === 2);
  const currentUrl = window._paperCurrentUrl || global._paperCurrentUrl || '';
  out.current_url_is_b = (typeof currentUrl === 'string' && currentUrl.indexOf('B_ok') >= 0);

  console.log(JSON.stringify(out));
})().catch((e) => { console.log(JSON.stringify({ _threw: String(e && e.stack || e) })); });
"""


def _run(viewer_js: str, neuter: bool = False):
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, dir=ROOT) as f:
        harness = f.name
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, ROOT, '1' if neuter else '', viewer_js],
                              capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    if proc.returncode != 0:
        raise AssertionError(
            f'harness failed (rc={proc.returncode}):\n{proc.stderr}\n{proc.stdout}')
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _assert_load_race_guard(out):
    assert '_threw' not in out, f'harness threw: {out.get("_threw")}'
    assert out['loadfn_exists'], '_loadPaperPdf not defined'
    # Paper B was selected last → its pages must be showing.
    assert out['b_pages_present'], "paper B's pages should be rendered in the viewer"
    assert out['current_url_is_b'], "_paperCurrentUrl should reflect the last selection (B)"
    # Paper A failed AFTER B painted → its error must NOT clobber B.
    assert out['no_error_box'], \
        "a stale/failed load (A) must not paint an error box over the newer paper (B)"
    assert not out['has_error_text'], \
        "'Failed to load PDF' from the stale load must not appear over the current paper"


@pytest.mark.skipif(not _node_deps_available() or not os.path.isfile(ESBUILD),
                    reason='node + jsdom + vite test bundler dev-deps not installed')
def test_vite_pdf_viewer_preserves_load_race_guard(tmp_path):
    built = tmp_path / 'paper-pdf-viewer.js'
    compiled = compile_feature_owner(ESBUILD, VIEWER_TS, built, tmp_path)
    assert compiled.returncode == 0, compiled.stderr
    out = _run(str(built))
    _assert_load_race_guard(out)
