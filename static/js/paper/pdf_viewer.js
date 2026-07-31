/* ═════════════════════════════════════════════════
   paper/pdf_viewer.js — PDF loading + multi-page render + zoom (Reading Mode)

   Extracted verbatim from static/js/paper-reader.js (2026-07-11, Epic E
   cut #5). The pdf.js load/open pipeline (_resolvePaperPdfUrl / _shouldFetchPdfAsData
   / _fetchPdfArrayBuffer / _openPaperPdfDoc / _loadPaperPdf / _renderAllPages /
   _rasterizePage / _releasePage / _maybeReopenViaData / _observePageWrappers)
   + zoom controls (paperZoomIn/Out / paperSetScaleFromSlider/Input / paperFitWidth
   / _syncZoomUI / _updateZoomLabel). Owns _paperResizeObserver + _paperZoomDebounce.
   SHARED doc state (_paperPdfDoc / _paperScale / _paperTotalPages / _paperRenderToken)
   STAYS in the core State block. All cross-refs are window-scope var + RUNTIME
   (core callers at enterPaperMode/Init; pdf_responsive.js calls paperFitWidth via
   typeof-guard) — so this loads BEFORE paper-reader.js AND before pdf_responsive.js
   in _DEFERRED_FILES. Zoom fns are window-hoisted (bound by Init listeners).
   ═════════════════════════════════════════════════ */

// ══════════════════════════════════════════════════════
//  ★ PDF Loading & Rendering (always in #paperPdfViewer)
// ══════════════════════════════════════════════════════

/** Re-base a stored paper PDF/asset URL onto the CURRENT proxy base path.
 *
 *  A library row's ``pdfUrl`` is persisted by two writers with different
 *  shapes: the server ingest stores a root-relative ``/api/paper/pdf/<f>``
 *  (the backend can't know the proxy prefix), while a client PUT stores an
 *  ``apiUrl()``-prefixed value baked with THAT session's BASE_PATH. Either is
 *  wrong to hand to pdf.js verbatim under a cloud-IDE proxy (e.g.
 *  ``/proxy/15000/``): the root-relative one drops the prefix (→ gateway 404,
 *  "Missing PDF") and the baked one goes stale if the port/prefix changes.
 *  Strip back to the canonical ``/api/...`` segment and re-apply the live
 *  BASE_PATH so the URL is correct regardless of how it was stored. Non-API
 *  URLs (blob:, data:, absolute externals) are returned untouched. */
function _resolvePaperPdfUrl(url) {
  if (!url) return url;
  var i = url.indexOf('/api/');
  if (i < 0) return url;  // blob:/data:/external — leave as-is
  var canonical = url.slice(i);
  return (typeof apiUrl === 'function') ? apiUrl(canonical) : canonical;
}

/** Robust last-resort loader gate. When a cloud-IDE proxy strips/ignores HTTP
 *  Range (the transport log shows a single ``range=False -> 200`` full GET),
 *  ranged transport can't help pdf.js — the fix is to stop relying on the
 *  transport entirely: the CLIENT downloads the whole PDF once, on a URL it
 *  proxy-corrects itself, with a timeout it controls, then hands pdf.js the
 *  bytes via ``getDocument({data})`` instead of ``getDocument({url})``. Dormant
 *  by default; flip in the browser console with
 *  ``localStorage.setItem('tofu_paper_pdf_data','1')`` (no rebuild — no-build JS)
 *  the instant the log proves the proxy defeats ranging. */
function _shouldFetchPdfAsData() {
  try { return localStorage.getItem('tofu_paper_pdf_data') === '1'; }
  catch (_) { return false; }
}

/** Download a PDF to a Uint8Array for pdf.js ``getDocument({data})``. Routes
 *  through the unified API client (``Api.paper.pdfArrayBuffer``) rather than a
 *  raw fetch so base-path resolution stays single-sourced in api.js and the
 *  §3.2.0 isolation seam holds; we pass the CANONICAL ``/api/...`` path (Api
 *  re-applies the live BASE_PATH). Throws on non-2xx / abort so the caller
 *  surfaces a clear error.
 *
 *  NO deadline: this is a byte TRANSFER, not a liveness probe — a big paper
 *  over a slow link legitimately runs past any fixed ceiling, and the old 120s
 *  abort surfaced as "Failed to load PDF" for a download that was still
 *  progressing. Pinned by tests/test_frontend_no_client_timeouts.py. */
async function _fetchPdfArrayBuffer(url) {
  var i = (url || '').indexOf('/api/');
  var canonical = i >= 0 ? url.slice(i) : url;  // strip any baked prefix; Api re-resolves
  return await Api.paper.pdfArrayBuffer(canonical, { timeout: 0 });
}

/** Open a PDF with pdf.js, auto-falling back to a client-side byte download if
 *  the transport-based load fails.
 *
 *  Strategy:
 *   • Manual override — if ``_shouldFetchPdfAsData()`` is set, skip straight to
 *     the {data} download (a known-bad proxy avoids even the first failed try).
 *   • Otherwise attempt ``getDocument({url})`` and PROBE page 1: pdf.js resolves
 *     the doc from the first response's metadata, but a mangled-206 / truncated
 *     body only surfaces when a page is actually pulled — so getPage(1) is the
 *     real "did the transport deliver bytes" test.
 *   • On ANY failure of that path, retry EXACTLY ONCE via ``_fetchPdfArrayBuffer``
 *     → ``getDocument({data})`` (one plain full GET the client owns end-to-end,
 *     immune to Range mangling). If the {data} attempt also fails, throw — the
 *     caller surfaces the real error. The single-retry cap prevents any loop. */
async function _openPaperPdfDoc(url, forceData) {
  // forceData (or the manual flag) → skip the transport entirely and download.
  if (forceData || _shouldFetchPdfAsData()) {
    debugLog('[Paper] Loading PDF via client ArrayBuffer (range-bypass)…', 'info');
    var _bytesM = await _fetchPdfArrayBuffer(url);
    return { doc: await pdfjsLib.getDocument({ data: _bytesM }).promise, viaData: true };
  }
  var doc = null;
  try {
    doc = await pdfjsLib.getDocument(url).promise;
    // Probe page 1 — a stripped/truncated ranged body fails HERE, not at open.
    await doc.getPage(1);
    return { doc: doc, viaData: false };
  } catch (e) {
    if (doc) { try { doc.destroy(); } catch (_) {} }
    debugLog('[Paper] URL load failed (' + (e && e.message || e) +
             ') — auto-retrying via client ArrayBuffer (range-bypass)…', 'warning');
    var _bytes = await _fetchPdfArrayBuffer(url);
    return { doc: await pdfjsLib.getDocument({ data: _bytes }).promise, viaData: true };
  }
}

async function _loadPaperPdf(url) {
  // Load-generation guard: each call bumps _paperLoadGen and captures its own
  // ``myGen``. A user who clicks paper A then quickly clicks paper B starts two
  // concurrent, un-awaited _loadPaperPdf runs that SHARE the single viewer +
  // global doc state (_paperPdfDoc / _paperCurrentUrl). Without this guard the
  // SLOWER/older load (e.g. A's 120s {data} download, or A's promise rejecting
  // after B has painted) writes its result — including "Failed to load PDF" —
  // straight into the viewer, clobbering B even though B is the row still
  // selected in the sidebar. That is the reported "file clearly selected but
  // PDF failed to load". ``_isStaleLoad()`` makes every viewer write and every
  // shared-state mutation bail the instant a newer load supersedes this one.
  var myGen = ++_paperLoadGen;
  function _isStaleLoad() { return myGen !== _paperLoadGen; }

  url = _resolvePaperPdfUrl(url);
  _paperCurrentUrl = url;
  var viewer = document.getElementById('paperPdfViewer');
  if (!viewer) return;
  viewer.innerHTML = '<div class="paper-loading"><div class="paper-loading-spinner"></div><div>Loading PDF…</div></div>';

  try {
    if (typeof pdfjsLib === 'undefined') {
      if (typeof _ensurePdfJs === 'function') await _ensurePdfJs();
      else { if (!_isStaleLoad()) viewer.innerHTML = '<div class="paper-error">PDF.js not available. Refresh the page.</div>'; return; }
    }
    if (typeof pdfjsLib === 'undefined') {
      if (!_isStaleLoad()) viewer.innerHTML = '<div class="paper-error">PDF.js failed to load.</div>';
      return;
    }
    if (_isStaleLoad()) return;  // a newer selection superseded us before open

    if (_paperPdfDoc) { try { _paperPdfDoc.destroy(); } catch (_) {} _paperPdfDoc = null; }

    // Load with automatic range-bypass fallback (see _openPaperPdfDoc). Default
    // path hands pdf.js the URL and lets it range-load (the primary fix); if
    // that load fails — the mangled-206 / range-stripping proxy case — it
    // transparently retries ONCE by downloading the whole file as bytes and
    // passing {data}, so the viewer recovers automatically for EVERY user with
    // no console flag. The manual flag short-circuits straight to {data}.
    // ── Measurement-first instrumentation: time the doc-open (network + parse)
    // phase separately from render, so the on-device debugLog proves where the
    // seconds go (transport vs render). _renderAllPages logs time-to-first-page
    // and layout-ready on its own.
    var _tOpen = _paperNow();
    var _opened = await _openPaperPdfDoc(url);
    // A newer selection started while we were opening — discard this doc and
    // bail WITHOUT touching the shared state / viewer the newer load now owns.
    if (_isStaleLoad()) { try { _opened.doc.destroy(); } catch (_) {} return; }
    _paperPdfDoc = _opened.doc;
    _paperViaData = _opened.viaData;
    _paperTotalPages = _paperPdfDoc.numPages;
    debugLog('[Paper] doc opened in ' + Math.round(_paperNow() - _tOpen) + 'ms (viaData=' +
             _paperViaData + ', pages=' + _paperTotalPages + ')', 'info');
    _updatePaperTitles();
    // Auto fit-to-width on initial load so the PDF sizes to the current panel
    // regardless of the current _paperScale value (matches Chrome/Acrobat default).
    try {
      var _firstPage = await _paperPdfDoc.getPage(1);
      var _baseVp = _firstPage.getViewport({ scale: 1.0 });
      var _container = document.getElementById('paperPdfViewer');
      var _containerW = _container ? (_container.clientWidth - _paperViewerPadX(_container)) : 0;
      if (_containerW > 0) {
        _paperScale = Math.max(0.25, Math.min(4.0, _containerW / _baseVp.width));
      }
    } catch (err) {
      console.warn('[Paper] Initial fit-width failed:', err);
    }
    _updateZoomLabel();
    // Virtualized render: builds all page shells (cheap viewport math, no
    // raster), rasterizes page 1 immediately so time-to-first-page is roughly
    // constant regardless of page count, then lazy-rasterizes the rest on
    // scroll via IntersectionObserver. A LATER-page render failure (mangled /
    // truncated Range on a buffering proxy — passes the page-1 probe but fails
    // when the page rasterizes) is recovered inside _renderAllPages →
    // _maybeReopenViaData (single-flight {data} re-open), which generalizes the
    // old initial-load-only fallback to ANY page at ANY time.
    await _renderAllPages();

    // Update library entry
    var entry = _getActivePaperEntry();
    if (entry) { entry.pageCount = _paperTotalPages; _persistPaperEntry(entry); }
    _renderPaperLibrary();
  } catch (e) {
    console.error('[Paper] Failed to load PDF:', e);
    // Only surface the error if THIS load is still the current one. An older
    // load failing after the user has already selected another paper must not
    // paint "Failed to load PDF" over the newer, correctly-loading document.
    if (!_isStaleLoad()) {
      viewer.innerHTML = '<div class="paper-error">Failed to load PDF: ' + escapeHtml(e.message) + '</div>';
    }
  }
}

/** Render pages vertically for scroll-based reading — VIRTUALIZED.
 *
 *  The old implementation rasterized EVERY page at ``_paperScale ×
 *  devicePixelRatio`` in a sequential ``await`` loop before the load was
 *  considered complete. For a 40-page paper that is 40 full canvas
 *  rasterizations + 40 text layers up front — time-to-first-page grew with
 *  the page count and the tab stayed janky ("loads too slowly").
 *
 *  Now the work is split:
 *  1. Build every page's WRAPPER up front with correct dimensions (cheap —
 *     pure viewport math via ``getPage``, NO raster). This gives the scroll
 *     container its full height immediately, so the scrollbar is correct.
 *  2. Rasterize page 1 (and any page already in/near the viewport) NOW, so
 *     time-to-first-page is roughly constant regardless of page count.
 *  3. Rasterize the remaining pages lazily as they scroll near the viewport,
 *     via an IntersectionObserver, and RELEASE canvases that scroll far away
 *     to cap memory (the wrapper keeps its size, so layout is stable).
 *
 *  Sharp-render + selectable-text invariants are preserved exactly per page
 *  (see _rasterizePage): CSS viewport for layout, hi-res buffer (× dpr) for
 *  sharpness CSS-sized down, the ``--scale-factor`` text-layer variable, and
 *  the transparent absolutely-positioned text layer for selection.
 *
 *  A per-run token (``_paperRenderToken``) makes a stale render loop (e.g. a
 *  zoom fired mid-render) self-cancel. Returns false always now — a
 *  later-page raster failure is recovered in-band by _maybeReopenViaData
 *  ({data} range-bypass re-open), which the initial-load path used to do
 *  only for the whole document.
 */
async function _renderAllPages() {
  if (!_paperPdfDoc) return false;
  var viewer = document.getElementById('paperPdfViewer');
  if (!viewer) return false;

  var token = ++_paperRenderToken;
  viewer.innerHTML = '';
  if (_paperIntersectionObserver) { _paperIntersectionObserver.disconnect(); _paperIntersectionObserver = null; }

  var tStart = _paperNow();

  // ── Phase 1: build all wrappers with correct dimensions (no raster) ──
  for (var i = 1; i <= _paperTotalPages; i++) {
    if (token !== _paperRenderToken) return false;  // superseded (e.g. zoom / reload)
    var cssW, cssH;
    try {
      var page = await _paperPdfDoc.getPage(i);
      var vp = page.getViewport({ scale: _paperScale });
      cssW = vp.width; cssH = vp.height;
    } catch (e) {
      console.warn('[Paper] Failed to size page', i, ':', e);
      // Best-effort fallback size (US-letter aspect) so the shell still exists.
      cssW = 612 * _paperScale; cssH = 792 * _paperScale;
    }
    var wrapper = document.createElement('div');
    wrapper.className = 'paper-page-wrapper';
    wrapper.dataset.page = String(i);
    wrapper.dataset.rendered = '0';
    wrapper.style.width = cssW + 'px';
    wrapper.style.aspectRatio = (cssW / cssH).toFixed(6);

    // Placeholder so an unrendered page isn't blank while scrolling fast.
    var ph = document.createElement('div');
    ph.className = 'paper-page-placeholder';
    wrapper.appendChild(ph);

    var pageLabel = document.createElement('div');
    pageLabel.className = 'paper-page-label';
    pageLabel.textContent = i + ' / ' + _paperTotalPages;
    wrapper.appendChild(pageLabel);

    viewer.appendChild(wrapper);
  }
  if (token !== _paperRenderToken) return false;
  debugLog('[Paper] page shells laid out in ' + Math.round(_paperNow() - tStart) +
           'ms (' + _paperTotalPages + ' pages, virtualized)', 'info');

  // ── Phase 2: lazy rasterization on scroll ──
  var wrappers = viewer.querySelectorAll('.paper-page-wrapper');
  if (typeof IntersectionObserver !== 'undefined') {
    _paperIntersectionObserver = new IntersectionObserver(function(entries) {
      for (var k = 0; k < entries.length; k++) {
        var w = entries[k].target;
        if (entries[k].isIntersecting) {
          // Lazy path: fire-and-forget; on a raster failure recover via {data}.
          _rasterizePage(w, token).then(function(needsReopen) {
            if (needsReopen) _maybeReopenViaData();
          });
        } else {
          _releasePage(w);  // scrolled far off — free the canvas, keep the shell
        }
      }
    }, {
      root: viewer,
      // Pre-render a screenful above/below so scrolling reveals ready pages.
      rootMargin: '150% 0px 150% 0px',
      threshold: 0.01,
    });
    for (var j = 0; j < wrappers.length; j++) _paperIntersectionObserver.observe(wrappers[j]);
  } else {
    // No IntersectionObserver (jsdom / very old engine): render all eagerly so
    // behaviour degrades to the old correctness, just without virtualization.
    for (var e = 0; e < wrappers.length; e++) {
      var _needReopen = await _rasterizePage(wrappers[e], token);
      if (_needReopen) { await _maybeReopenViaData(); return false; }
    }
  }

  // ── Phase 3: force page 1 NOW so time-to-first-page is immediate ──
  if (wrappers.length) {
    var _p1Reopen = await _rasterizePage(wrappers[0], token);
    if (_p1Reopen) { await _maybeReopenViaData(); return false; }
    if (token === _paperRenderToken) {
      debugLog('[Paper] first page painted in ' + Math.round(_paperNow() - tStart) + 'ms', 'info');
    }
  }

  // Observe wrappers to scale text layers when container shrinks
  _observePageWrappers(viewer);
  return false;
}

/** Rasterize ONE page into its wrapper: hi-res canvas buffer + text layer.
 *  Idempotent — a wrapper already rendered (or mid-render) is skipped, so the
 *  IntersectionObserver re-firing on the same page is cheap. Honours the run
 *  token so a page whose render started before a zoom/reload is discarded. */
async function _rasterizePage(wrapper, token) {
  if (!wrapper || !_paperPdfDoc) return;
  if (wrapper.dataset.rendered === '1' || wrapper.dataset.rendering === '1') return;
  if (token != null && token !== _paperRenderToken) return;
  var pageNum = parseInt(wrapper.dataset.page, 10);
  if (!pageNum) return;
  wrapper.dataset.rendering = '1';
  var dpr = window.devicePixelRatio || 1;
  try {
    var page = await _paperPdfDoc.getPage(pageNum);
    if (token != null && token !== _paperRenderToken) { wrapper.dataset.rendering = '0'; return; }

    var cssViewport = page.getViewport({ scale: _paperScale });
    var cssW = cssViewport.width;
    var cssH = cssViewport.height;
    var hiresViewport = page.getViewport({ scale: _paperScale * dpr });

    // Keep wrapper dimensions in sync with the just-measured viewport (a zoom
    // between shell-layout and raster would otherwise leave the old size).
    wrapper.style.width = cssW + 'px';
    wrapper.style.aspectRatio = (cssW / cssH).toFixed(6);

    var canvas = document.createElement('canvas');
    canvas.className = 'paper-pdf-canvas';
    canvas.width = hiresViewport.width;
    canvas.height = hiresViewport.height;
    canvas.style.width = cssW + 'px';

    var textDiv = document.createElement('div');
    textDiv.className = 'paper-text-layer';
    textDiv.style.width = cssW + 'px';
    textDiv.style.height = cssH + 'px';
    textDiv.style.setProperty('--scale-factor', _paperScale.toString());

    var ctx = canvas.getContext('2d');
    await page.render({ canvasContext: ctx, viewport: hiresViewport }).promise;
    if (token != null && token !== _paperRenderToken) { wrapper.dataset.rendering = '0'; return; }

    // Swap placeholder → real content atomically.
    var ph = wrapper.querySelector('.paper-page-placeholder');
    if (ph) ph.remove();
    var label = wrapper.querySelector('.paper-page-label');
    wrapper.insertBefore(canvas, label || null);
    wrapper.insertBefore(textDiv, label || null);

    var textContent = await page.getTextContent();
    if (typeof pdfjsLib.renderTextLayer === 'function') {
      pdfjsLib.renderTextLayer({
        textContentSource: textContent,
        container: textDiv,
        viewport: cssViewport,
        textDivs: [],
      });
    }
    wrapper.dataset.rendered = '1';
    wrapper.dataset.rendering = '0';
    return false;
  } catch (e) {
    wrapper.dataset.rendering = '0';
    console.warn('[Paper] Failed to render page', pageNum, ':', e);
    // A later-page raster failure = the mangled/truncated Range case (passes
    // the page-1 probe, fails on real pull). Signal the caller to recover ONCE
    // via a full {data} re-open (immune to Range mangling) — but only if we are
    // not ALREADY on the {data} path. Returning true keeps the recovery
    // decision with the caller so the lazy (observer) path can fire-and-forget
    // while the eager/initial path can await it.
    if (!_paperViaData) return true;
    var errDiv = document.createElement('div');
    errDiv.className = 'paper-page-error';
    errDiv.textContent = 'Page ' + pageNum + ' failed to render';
    var lbl = wrapper.querySelector('.paper-page-label');
    wrapper.insertBefore(errDiv, lbl || null);
    return false;
  }
}

/** Release a page that has scrolled far off-screen: drop its canvas + text
 *  layer (the heavy memory), restore a lightweight placeholder, and mark it
 *  un-rendered so scrolling back re-rasterizes it. The wrapper keeps its
 *  width/aspect-ratio, so scroll position and layout are unaffected. */
function _releasePage(wrapper) {
  if (!wrapper || wrapper.dataset.rendered !== '1') return;
  var canvas = wrapper.querySelector('.paper-pdf-canvas');
  var textLayer = wrapper.querySelector('.paper-text-layer');
  if (canvas) canvas.remove();
  if (textLayer) textLayer.remove();
  if (!wrapper.querySelector('.paper-page-placeholder')) {
    var ph = document.createElement('div');
    ph.className = 'paper-page-placeholder';
    wrapper.insertBefore(ph, wrapper.firstChild);
  }
  wrapper.dataset.rendered = '0';
}

/** Single-flight {data} range-bypass re-open triggered when a page fails to
 *  rasterize (mangled/truncated Range on a buffering proxy). Downloads the
 *  whole PDF once (client-owned, immune to Range mangling) and re-renders. */
async function _maybeReopenViaData() {
  if (_paperReopenInFlight || _paperViaData || !_paperCurrentUrl) return;
  _paperReopenInFlight = true;
  var myGen = _paperLoadGen;  // the load this recovery belongs to
  try {
    debugLog('[Paper] A page failed to rasterize — re-opening via client ArrayBuffer (range-bypass) and re-rendering…', 'warning');
    if (_paperPdfDoc) { try { _paperPdfDoc.destroy(); } catch (_) {} _paperPdfDoc = null; }
    var reopened = await _openPaperPdfDoc(_paperCurrentUrl, true);
    // A newer paper was selected during the (slow) full download — discard.
    if (myGen !== _paperLoadGen) { try { reopened.doc.destroy(); } catch (_) {} return; }
    _paperPdfDoc = reopened.doc;
    _paperViaData = reopened.viaData;
    _paperTotalPages = _paperPdfDoc.numPages;
    await _renderAllPages();
  } catch (e) {
    console.error('[Paper] {data} re-open failed:', e);
  } finally {
    _paperReopenInFlight = false;
  }
}

/** ResizeObserver: scale text layers proportionally when page wrappers
 *  are constrained below their natural width (e.g. panel shrunk by drag). */
var _paperResizeObserver = null;
function _observePageWrappers(viewer) {
  if (_paperResizeObserver) { _paperResizeObserver.disconnect(); _paperResizeObserver = null; }
  if (typeof ResizeObserver === 'undefined') return;

  _paperResizeObserver = new ResizeObserver(function(entries) {
    for (var i = 0; i < entries.length; i++) {
      var wrapper = entries[i].target;
      var textLayer = wrapper.querySelector('.paper-text-layer');
      if (!textLayer) continue;
      var origW = parseFloat(textLayer.style.width);
      if (!origW) continue;
      var actualW = entries[i].contentBoxSize
        ? /** @type {any} */ (entries[i].contentBoxSize[0] || entries[i].contentBoxSize).inlineSize
        : wrapper.clientWidth;
      var scale = actualW / origW;
      if (Math.abs(scale - 1) < 0.001) {
        textLayer.style.transform = '';
      } else {
        textLayer.style.transform = 'scale(' + scale.toFixed(6) + ')';
      }
    }
  });

  var wrappers = viewer.querySelectorAll('.paper-page-wrapper');
  for (var j = 0; j < wrappers.length; j++) {
    _paperResizeObserver.observe(wrappers[j]);
  }
}

// ── Zoom ──

var _paperZoomDebounce = null;

function paperZoomIn() {
  _paperScale = Math.min(_paperScale + 0.25, 4.0);
  _syncZoomUI();
  _renderAllPages();
}

function paperZoomOut() {
  _paperScale = Math.max(_paperScale - 0.25, 0.25);
  _syncZoomUI();
  _renderAllPages();
}

/** Set scale from slider input (value = percentage integer) */
function paperSetScaleFromSlider(val) {
  _paperScale = Math.max(0.25, Math.min(4.0, parseInt(val, 10) / 100));
  _syncZoomUI();
  // Debounce re-render during slider drag
  clearTimeout(_paperZoomDebounce);
  _paperZoomDebounce = setTimeout(function() { _renderAllPages(); }, 120);
}

/** Set scale from text input (value like "150%" or "150") */
function paperSetScaleFromInput(val) {
  var num = parseInt(val.replace('%', ''), 10);
  if (isNaN(num) || num < 25) num = 25;
  if (num > 400) num = 400;
  _paperScale = num / 100;
  _syncZoomUI();
  _renderAllPages();
}

/** Measured horizontal padding (left+right) of the PDF viewer container.
 *
 * The fit-to-width math must subtract the container's actual padding to size a
 * page to the usable width. This was hardcoded to 32 (assuming 16px each side),
 * but the theme + responsive rules diverge: base .paper-pdf-container is 16px,
 * light/tofu are 20/24px, and the portrait-tablet band overrides it again — so
 * a fixed 32 renders the PDF several px too wide on those. Read the real value
 * from computed style so every theme/breakpoint fits exactly. */
function _paperViewerPadX(container) {
  try {
    var cs = getComputedStyle(container);
    var l = parseFloat(cs.paddingLeft) || 0;
    var r = parseFloat(cs.paddingRight) || 0;
    var px = l + r;
    return px > 0 ? px : 32;
  } catch (err) {
    console.warn('[Paper] padding measure failed, using 32:', err);
    return 32;
  }
}

/** Fit PDF width to container width */
function paperFitWidth() {
  if (!_paperPdfDoc) return;
  var container = document.getElementById('paperPdfViewer');
  if (!container) return;
  // Get first page to calculate ratio
  _paperPdfDoc.getPage(1).then(function(page) {
    var baseViewport = page.getViewport({ scale: 1.0 });
    var containerWidth = container.clientWidth - _paperViewerPadX(container);
    var fitScale = containerWidth / baseViewport.width;
    _paperScale = Math.max(0.25, Math.min(4.0, fitScale));
    _syncZoomUI();
    _renderAllPages();
  });
}

/** Sync slider + text input to current _paperScale */
function _syncZoomUI() {
  var pct = Math.round(_paperScale * 100);
  var input = document.getElementById('paperZoomLevel');
  if (input) input.value = pct + '%';
  var slider = document.getElementById('paperZoomSlider');
  if (slider) slider.value = pct;
}

// Legacy alias
function _updateZoomLabel() { _syncZoomUI(); }
