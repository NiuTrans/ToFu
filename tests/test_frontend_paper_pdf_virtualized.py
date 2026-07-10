#!/usr/bin/env python3
"""Virtualized Paper-Mode PDF rendering (fixes "reading mode loads too slowly").

WHY
---
``_renderAllPages`` used to rasterize EVERY page at ``_paperScale ×
devicePixelRatio`` in a sequential ``await`` loop before the load was
considered complete. Time-to-first-page grew with the page count and the tab
stayed janky on multi-page papers. The fix virtualizes the render: build all
page WRAPPERS up front (cheap viewport math, no raster), rasterize page 1
immediately, then rasterize the rest lazily as they scroll near the viewport
via an IntersectionObserver — and release canvases that scroll far off-screen.

This harness drives the REAL ``_loadPaperPdf`` / ``_renderAllPages`` from
``static/js/paper-reader.js`` with a CONTROLLABLE ``IntersectionObserver`` stub
(jsdom has none) so it can assert:
  • Phase 1 builds one wrapper PER PAGE up front (full scroll height).
  • Only page 1 is rasterized on load — pages 2..N are NOT yet (the whole point).
  • Every wrapper is observed by the IntersectionObserver.
  • Feeding an "intersecting" entry for a later page rasterizes THAT page lazily.
  • Feeding a "not intersecting" entry for a rendered page RELEASES its canvas.

Neuter: forcing ``IntersectionObserver`` undefined makes the source fall back to
eager render (all pages rasterized on load) — which fails the "only page 1
rendered on load" assertion, proving that assertion actually bites.

Skips cleanly when node/jsdom are absent.
"""

import json
import os
import shutil
import subprocess
import tempfile

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _node_deps_available():
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


# ``__NEUTER__`` is replaced by the test body: '' for the real run, '1' to
# disable IntersectionObserver (forcing the eager fallback) for the neuter run.
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

global.apiUrl = (u) => u;
global.debugLog = () => {};
global.escapeHtml = (s) => String(s == null ? '' : s);
global._saveActivePaperState = () => {};
global._getActivePaperEntry = () => null;
global._persistPaperEntry = () => {};
global._renderPaperLibrary = () => {};
global._updatePaperTitles = () => {};
global.paperFitWidth = () => {};
global._autoRefitIfOverflowing = () => {};
global._paperViewerPadX = () => 0;
global._updateZoomLabel = () => {};

// Track raster work: getContext is called once per rasterized page.
let renderedPages = [];
dom.window.HTMLCanvasElement.prototype.getContext = function () { return {}; };

const N_PAGES = 8;
function _makePage(n) {
  return {
    getViewport: () => ({ width: 600, height: 800 }),
    render: () => { renderedPages.push(n); return { promise: Promise.resolve() }; },
    getTextContent: async () => ({ items: [] }),
  };
}
global.pdfjsLib = {
  getDocument: (param) => ({
    promise: Promise.resolve({
      numPages: N_PAGES,
      getPage: async (n) => _makePage(n),
      destroy: () => {},
    }),
  }),
};
global.Api = window.Api = { paper: { pdfArrayBuffer: async () => new Uint8Array([0x25]) } };

// Controllable IntersectionObserver: capture instances so the harness can feed
// entries manually (jsdom has none). Neuter run leaves it undefined.
let ioInstances = [];
if (!NEUTER) {
  global.IntersectionObserver = window.IntersectionObserver = class {
    constructor(cb, opts) { this.cb = cb; this.opts = opts; this.observed = []; ioInstances.push(this); }
    observe(el) { this.observed.push(el); }
    unobserve(el) { this.observed = this.observed.filter((x) => x !== el); }
    disconnect() { this.observed = []; }
  };
} else {
  try { delete global.IntersectionObserver; } catch (e) {}
  try { delete window.IntersectionObserver; } catch (e) {}
  global.IntersectionObserver = undefined;
}
// ResizeObserver stub (used by _observePageWrappers).
global.ResizeObserver = window.ResizeObserver = class {
  observe() {} unobserve() {} disconnect() {}
};

const src = fs.readFileSync(path.join(ROOT, 'static', 'js', 'paper-reader.js'), 'utf8');
(0, eval)(src);

const viewer = document.getElementById('paperPdfViewer');
const out = {};

function wrapperEls() { return viewer.querySelectorAll('.paper-page-wrapper'); }
function canvasCount() { return viewer.querySelectorAll('.paper-pdf-canvas').length; }

(async () => {
  renderedPages = []; ioInstances = [];
  await _loadPaperPdf('/api/paper/pdf/x.pdf');

  // Phase 1: one wrapper per page laid out up front (full scroll height).
  out.wrappers_built = (wrapperEls().length === N_PAGES);

  if (NEUTER) {
    // Eager fallback: ALL pages rasterized on load (the old slow behaviour).
    out.rendered_on_load = renderedPages.slice().sort((a, b) => a - b);
    console.log(JSON.stringify(out));
    return;
  }

  // Only page 1 rasterized on load — 2..N deferred (the virtualization win).
  out.rendered_on_load = renderedPages.slice().sort((a, b) => a - b);
  out.only_page1_on_load = (renderedPages.length === 1 && renderedPages[0] === 1);
  out.canvas_count_on_load = canvasCount();  // expect 1

  // Every wrapper is observed for lazy rasterization.
  const io = ioInstances[ioInstances.length - 1];
  out.observer_created = !!io;
  out.all_wrappers_observed = (!!io && io.observed.length === N_PAGES);

  // Lazy raster: feed an "intersecting" entry for page 5 → it rasterizes now.
  renderedPages = [];
  const w5 = viewer.querySelector('.paper-page-wrapper[data-page="5"]');
  io.cb([{ target: w5, isIntersecting: true }]);
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  out.lazy_rendered_page5 = (renderedPages.indexOf(5) >= 0);
  out.page5_marked_rendered = (w5.dataset.rendered === '1');
  out.page5_has_canvas = !!w5.querySelector('.paper-pdf-canvas');

  // Release: feed a "not intersecting" entry for page 5 → canvas freed, shell kept.
  io.cb([{ target: w5, isIntersecting: false }]);
  out.page5_released_canvas = !w5.querySelector('.paper-pdf-canvas');
  out.page5_shell_kept = !!viewer.querySelector('.paper-page-wrapper[data-page="5"]');
  out.page5_has_placeholder = !!w5.querySelector('.paper-page-placeholder');
  out.page5_remarked_unrendered = (w5.dataset.rendered === '0');

  console.log(JSON.stringify(out));
})().catch((e) => { console.log(JSON.stringify({ _threw: String(e && e.stack || e) })); });
"""


def _run(neuter):
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, dir=ROOT) as f:
        harness = f.name
        f.write(_HARNESS)
    try:
        proc = subprocess.run(['node', harness, ROOT, '1' if neuter else ''],
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


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_paper_pdf_virtualized_render():
    out = _run(neuter=False)
    assert '_threw' not in out, f'harness threw: {out.get("_threw")}'
    assert out['wrappers_built'], 'phase 1 must build one wrapper per page up front'
    assert out['only_page1_on_load'], \
        f'only page 1 should rasterize on load, got {out.get("rendered_on_load")}'
    assert out['canvas_count_on_load'] == 1, \
        f'exactly one canvas should exist on load, got {out.get("canvas_count_on_load")}'
    assert out['observer_created'], 'an IntersectionObserver must be created'
    assert out['all_wrappers_observed'], 'every page wrapper must be observed'
    assert out['lazy_rendered_page5'], 'scrolling page 5 into view must rasterize it lazily'
    assert out['page5_marked_rendered'], 'lazily-rendered page must be marked rendered'
    assert out['page5_has_canvas'], 'lazily-rendered page must have a canvas'
    assert out['page5_released_canvas'], 'page scrolled far off-screen must free its canvas'
    assert out['page5_shell_kept'], 'released page must keep its wrapper shell (stable layout)'
    assert out['page5_has_placeholder'], 'released page must restore a placeholder'
    assert out['page5_remarked_unrendered'], 'released page must be re-marked unrendered'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_neuter_eager_render_bites():
    """Without IntersectionObserver the source falls back to eager render — ALL
    pages rasterize on load, which is exactly what the virtualization assertion
    forbids. Proves ``only_page1_on_load`` genuinely bites."""
    out = _run(neuter=True)
    assert '_threw' not in out, f'harness threw: {out.get("_threw")}'
    assert out['wrappers_built'], 'wrappers still built in eager fallback'
    rendered = out.get('rendered_on_load') or []
    assert len(rendered) == 8, \
        f'eager fallback must rasterize ALL pages on load (neuter proof), got {rendered}'


if __name__ == '__main__':
    if _node_deps_available():
        test_paper_pdf_virtualized_render()
        test_neuter_eager_render_bites()
        print('\033[32m✓ paper pdf virtualized render\033[0m')
    else:
        print('\033[33m• jsdom not installed — skipped\033[0m')
