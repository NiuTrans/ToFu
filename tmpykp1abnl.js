
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
global.pdfjsLib = {
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

let _viewerSrc = fs.readFileSync(path.join(ROOT, 'static', 'js', 'paper', 'pdf_viewer.js'), 'utf8');
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
const src = fs.readFileSync(path.join(ROOT, 'static', 'js', 'paper-reader.js'), 'utf8');
(0, eval)(src);

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
  out.current_url_is_b = (typeof _paperCurrentUrl === 'string' && _paperCurrentUrl.indexOf('B_ok') >= 0);

  console.log(JSON.stringify(out));
})().catch((e) => { console.log(JSON.stringify({ _threw: String(e && e.stack || e) })); });
