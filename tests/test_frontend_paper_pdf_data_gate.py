#!/usr/bin/env python3
"""Gated client-side ArrayBuffer loader for Paper Mode (range-bypass last resort).

WHY
---
The primary fix makes ``serve_paper_pdf`` range-capable so pdf.js range-loads a
large PDF. But if a cloud-IDE proxy STRIPS/IGNORES HTTP Range (the transport log
shows a single ``range=False -> 200`` full GET), ranged transport can't help —
the robust fallback is client-side: the app fetches the whole PDF as an
ArrayBuffer through its own proxy-correct URL and hands pdf.js
``getDocument({data})`` instead of ``getDocument({url})``, bypassing the
transport entirely.

Staged DORMANT behind ``localStorage['tofu_paper_pdf_data']==='1'`` (a no-build
console flip). This harness drives the REAL ``_loadPaperPdf`` from
``static/js/paper-reader.js`` and asserts:
  • flag OFF → pdf.js ``getDocument`` receives the URL string (default path).
  • flag ON  → pdf.js ``getDocument`` receives ``{data: <bytes>}`` fetched by
    the client, byte-matching what ``fetch`` returned.

Uses a self-contained harness (not the shared _jsdom_harness) because jsdom's
``window.localStorage`` is a getter-only property the shared harness can't
override; the source reads BARE ``localStorage`` (→ ``global.localStorage``),
which we stub directly. Skips cleanly when node/jsdom are absent.
"""

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


_HARNESS = r"""
const fs = require('fs'), path = require('path');
const ROOT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="paperPdfViewer"></div></body>',
                      { url: 'http://localhost/' });
global.window = dom.window;
global.document = dom.window.document;

// In-memory localStorage stub. Source reads BARE `localStorage` → global scope.
const _ls = {};
const _lsShim = {
  getItem: (k) => (k in _ls ? _ls[k] : null),
  setItem: (k, v) => { _ls[k] = String(v); },
  removeItem: (k) => { delete _ls[k]; },
};
global.localStorage = _lsShim;
try { Object.defineProperty(dom.window, 'localStorage', { value: _lsShim, configurable: true }); } catch (e) {}

// Minimal deps _loadPaperPdf touches on the pre-render path.
global.apiUrl = (u) => u;                 // identity → canonical /api/... path
global.debugLog = () => {};
global.escapeHtml = (s) => String(s == null ? '' : s);
global._saveActivePaperState = () => {};
global._getActivePaperEntry = () => null;
global._persistPaperEntry = () => {};
global._renderPaperLibrary = () => {};
global._updatePaperTitles = () => {};

// jsdom has no canvas backend → give getContext a dummy so the REAL
// _renderAllPages runs without a "not implemented" throw.
dom.window.HTMLCanvasElement.prototype.getContext = () => ({});

// Controllable pdf.js stub driving the REAL _openPaperPdfDoc + _renderAllPages.
//   urlLoadOk        — does the {url} OPEN + page-1 probe succeed
//   urlRenderFailPg  — if >0, page N RENDER fails on the {url} doc (a later-page
//                      truncation that passes the page-1 probe); the {data} doc
//                      always renders. Captures every getDocument param.
const PDF_BYTES = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x0a]); // %PDF\n
let captured = [];
let urlLoadOk = true;
let urlRenderFailPg = 0;
function _makePage(kind, n) {
  return {
    getViewport: () => ({ width: 600, height: 800 }),
    render: () => ({
      promise: (kind === 'url' && urlRenderFailPg === n)
        ? Promise.reject(new Error('mangled range: page ' + n + ' body truncated'))
        : Promise.resolve(),
    }),
    getTextContent: async () => ({ items: [] }),
  };
}
function _makeDoc(kind) {
  return {
    numPages: 2,
    _kind: kind,
    getPage: async (n) => {
      // Open-level failure (probe): only page-1 pull throws when urlLoadOk=false.
      if (kind === 'url' && !urlLoadOk && n === 1) throw new Error('mangled-206: page pull failed');
      return _makePage(kind, n);
    },
    destroy: () => {},
  };
}
global.pdfjsLib = {
  getDocument: (param) => {
    captured.push(param);
    const isData = (param && typeof param === 'object' && !!param.data);
    return { promise: Promise.resolve(_makeDoc(isData ? 'data' : 'url')) };
  },
  // renderTextLayer intentionally absent → source skips the text-layer branch.
};

// Unified-client stub: _fetchPdfArrayBuffer delegates to Api.paper.pdfArrayBuffer
// (isolation seam). Capture the path it passes; return the tiny PDF header.
let apiPaths = [];
global.Api = window.Api = {
  paper: {
    pdfArrayBuffer: async (path, opts) => { apiPaths.push(path); return PDF_BYTES; },
  },
};
// Post-open helpers _loadPaperPdf calls; stub the pure-DOM one, keep the real render.
global.paperFitWidth = () => {};
global._autoRefitIfOverflowing = () => {};
global._paperViewerPadX = () => 0;
global._updateZoomLabel = () => {};

const src = fs.readFileSync(path.join(ROOT, 'static', 'js', 'paper-reader.js'), 'utf8');
(0, eval)(src);  // indirect eval → defs land on globalThis (mirrors the bundle)

const URL_IN = '/api/paper/pdf/arxiv_x.pdf';
const out = {};
out.loadfn_exists = (typeof _loadPaperPdf === 'function');
out.fetch_helper_exists = (typeof _fetchPdfArrayBuffer === 'function');
out.gate_helper_exists = (typeof _shouldFetchPdfAsData === 'function');
out.openfn_exists = (typeof _openPaperPdfDoc === 'function');

(async () => {
  // 1) flag OFF, url loads + renders fine (Branch A) → only {url}, NO {data}.
  localStorage.removeItem('tofu_paper_pdf_data');
  captured = []; apiPaths = []; urlLoadOk = true; urlRenderFailPg = 0;
  await _loadPaperPdf(URL_IN);
  out.flagoff_ok_getdoc_is_url =
    (captured.length === 1 && typeof captured[0] === 'string' && captured[0].indexOf(URL_IN) >= 0);
  out.flagoff_ok_no_data_fallback = (apiPaths.length === 0);

  // 2) flag OFF, OPEN fails (mangled-206 at page-1 probe) → AUTO-retry via {data}.
  captured = []; apiPaths = []; urlLoadOk = false; urlRenderFailPg = 0;
  await _loadPaperPdf(URL_IN);
  const autop = captured[captured.length - 1];
  out.autoretry_first_was_url = (typeof captured[0] === 'string');
  out.autoretry_then_data = (!!autop && typeof autop === 'object' && autop.data instanceof Uint8Array);
  out.autoretry_bytes_match = (!!autop && !!autop.data && autop.data.length === 5 && autop.data[0] === 0x25);
  out.autoretry_used_api_client = (apiPaths.length === 1 && apiPaths[0].indexOf('/api/paper/pdf/') >= 0);
  out.autoretry_single_attempt = (apiPaths.length === 1);  // loop-guard: exactly one {data} try

  // 3) flag ON → skip straight to {data}, no {url} attempt at all.
  localStorage.setItem('tofu_paper_pdf_data', '1');
  captured = []; apiPaths = []; urlLoadOk = true; urlRenderFailPg = 0;
  await _loadPaperPdf(URL_IN);
  const p = captured[0];
  out.flagon_first_is_data = (!!p && typeof p === 'object' && p.data instanceof Uint8Array);
  out.flagon_no_url_attempt = !captured.some((c) => typeof c === 'string');
  out.flagon_used_api_client = (apiPaths.length === 1 && apiPaths[0].indexOf('/api/paper/pdf/') >= 0);

  // 4) flag OFF, url OPENS + page-1 renders, but a LATER page RENDER fails
  //    (truncated later range — the literal "Page N failed to render" symptom)
  //    → re-open ONCE via {data} and re-render, no flag.
  localStorage.removeItem('tofu_paper_pdf_data');
  captured = []; apiPaths = []; urlLoadOk = true; urlRenderFailPg = 2;
  await _loadPaperPdf(URL_IN);
  out.renderfail_first_was_url = (typeof captured[0] === 'string');
  const rlast = captured[captured.length - 1];
  out.renderfail_reopened_data = (!!rlast && typeof rlast === 'object' && rlast.data instanceof Uint8Array);
  out.renderfail_used_api_client = (apiPaths.length === 1 && apiPaths[0].indexOf('/api/paper/pdf/') >= 0);
  out.renderfail_single_attempt = (apiPaths.length === 1);  // one {data} re-open only
  // Final DOM: no per-page error left (the {data} re-render succeeded)
  const errEls = document.querySelectorAll('.paper-page-error');
  out.renderfail_no_error_left = (errEls.length === 0);

  console.log(JSON.stringify(out));
})().catch((e) => { console.log(JSON.stringify({ _threw: String(e && e.message) })); });
"""


def _run_harness():
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False, dir=ROOT) as f:
        harness = f.name
        f.write(_HARNESS)
    try:
        proc = subprocess.run(['node', harness, ROOT],
                              capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    if proc.returncode != 0:
        raise AssertionError(
            f'harness failed (rc={proc.returncode}):\n{proc.stderr}\n{proc.stdout}')
    import json
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_paper_pdf_data_gate():
    out = _run_harness()
    assert '_threw' not in out, f'harness threw: {out.get("_threw")}'
    assert out['loadfn_exists'], '_loadPaperPdf not defined'
    assert out['fetch_helper_exists'], '_fetchPdfArrayBuffer not defined'
    assert out['gate_helper_exists'], '_shouldFetchPdfAsData not defined'
    assert out['openfn_exists'], '_openPaperPdfDoc not defined'
    # 1) Branch A: url loads fine → {url} only, no {data} fallback (efficient path preserved)
    assert out['flagoff_ok_getdoc_is_url'], \
        'flag OFF + url OK: should load via {url} (single getDocument with the URL)'
    assert out['flagoff_ok_no_data_fallback'], \
        'flag OFF + url OK: must NOT trigger the {data} download when ranging works'
    # 2) Auto-retry: url load fails → transparently retries once via {data}, no flag needed
    assert out['autoretry_first_was_url'], \
        'auto-retry: first attempt should still be the {url} path'
    assert out['autoretry_then_data'], \
        'auto-retry: a failed {url} load MUST auto-fall-through to getDocument({data})'
    assert out['autoretry_bytes_match'], \
        'auto-retry: {data} bytes should match the client download'
    assert out['autoretry_used_api_client'], \
        'auto-retry: download must route through Api.paper.pdfArrayBuffer (no raw fetch)'
    assert out['autoretry_single_attempt'], \
        'auto-retry: exactly ONE {data} attempt (loop-guard)'
    # 3) Manual flag: skip straight to {data}, never attempt {url}
    assert out['flagon_first_is_data'], \
        'flag ON: first getDocument should be {data} (skip the failing transport)'
    assert out['flagon_no_url_attempt'], \
        'flag ON: must NOT attempt the {url} path at all'
    assert out['flagon_used_api_client'], \
        'flag ON: download must route through Api.paper.pdfArrayBuffer with the canonical /api/ path'
    # 4) Render-retry: {url} opens + page-1 renders, but a LATER page render fails
    #    (the literal "Page N failed to render" symptom) → re-open once via {data}
    assert out['renderfail_first_was_url'], \
        'render-retry: first attempt should still be the {url} path'
    assert out['renderfail_reopened_data'], \
        'render-retry: a per-page render failure MUST re-open via getDocument({data})'
    assert out['renderfail_used_api_client'], \
        'render-retry: re-open download must route through Api.paper.pdfArrayBuffer (no raw fetch)'
    assert out['renderfail_single_attempt'], \
        'render-retry: exactly ONE {data} re-open (loop-guard)'
    assert out['renderfail_no_error_left'], \
        'render-retry: after the {data} re-render, no per-page error should remain in the DOM'


if __name__ == '__main__':
    if _node_deps_available():
        test_paper_pdf_data_gate()
        print('\033[32m✓ paper pdf data gate\033[0m')
    else:
        print('\033[33m• jsdom not installed — skipped\033[0m')
