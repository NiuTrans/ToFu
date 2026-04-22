/* ═══════════════════════════════════════════
   paper-reader.js — Paper Reading Mode v3

   Layout:  Sidebar  = paper library (persistent)
            Main L   = PDF (vertical scroll, largest)
            Main R   = Q&A / Report / Babel PDF
   ═══════════════════════════════════════════ */

// ── State ──
var paperMode = false;
var _paperPdfUrl = '';
var _paperFileName = '';
var _paperParsedText = '';
var _paperArxivId = '';
var _paperPdfDoc = null;
var _paperTotalPages = 0;
var _paperScale = 1.5;
var _paperActiveTab = 'qa';
var _paperReportCache = '';
var _paperHash = '';  // server-side hash for DB report cache lookup

var _paperQAHistory = [];
var _paperLoading = false;
var _paperQAStreaming = false;
var _paperQAAbort = null;
var _paperReportModel = '';  // user-selected model for report generation
var _paperImages = [];  // [{url, caption, page, source, width, height}] — for embedding in report
var _paperPdfFilename = '';  // server-side PDF filename (for /api/paper/extract-images lookup)

// ── Report streaming state (2026-04-18 rewrite) ──
// Server owns the report task; the frontend only polls.
// _paperReportStream mirrors the in-flight task's accumulated state for
// the currently-active paper. See the Tab 2 Report section below for
// the full lifecycle (start → poll → apply events → paint).
var _paperReportStream = null;

// ── Paper Library ──
//
// The bookshelf is persisted **server-side** in the paper_library SQL table
// via /api/paper/library. Each browser is just a cache; the server is the
// source of truth so you see the same bookshelf on any machine.
//
// We keep _activePaperId in localStorage so the last-viewed paper re-opens
// on reload, and do a one-time migration of any old localStorage entries
// (from before this feature existed) to the server.

var _paperLibrary = [];          // Array of paper objects (cached from server)
var _activePaperId = '';         // Currently viewed paper ID
var _PAPER_ACTIVE_KEY = 'paper_active_id';
var _PAPER_LEGACY_LIB_KEY = 'paper_library';  // pre-migration localStorage
var _PAPER_MIGRATED_FLAG = 'paper_library_migrated_v1';

/** Upsert this entry to the server. Per-paper PUT so one save can't
 *  clobber a concurrent save of another paper. Best-effort — failures
 *  are logged but don't block the UI. */
function _persistPaperEntry(entry) {
  if (!entry || !entry.id) return Promise.resolve();
  var body = {
    title: entry.title || '',
    pdfUrl: entry.pdfUrl || '',
    pdfFilename: entry.pdfFilename || '',
    arxivId: entry.arxivId || '',
    paperHash: entry.paperHash || '',
    parsedText: (entry.parsedText || '').slice(0, 200000),
    qaHistory: (entry.qaHistory || []).slice(-50),
    images: Array.isArray(entry.images) ? entry.images.slice(0, 60) : [],
    babelCache: entry.babelCache || {},
    pageCount: entry.pageCount || 0,
    createdAt: entry.createdAt || Date.now(),
  };
  return fetch(apiUrl('/api/paper/library/' + encodeURIComponent(entry.id)), {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }).then(function(r) { return r.json(); })
    .then(function(data) {
      if (!data || !data.ok) {
        console.warn('[Paper:Library] Upsert rejected:', data && data.error);
      }
      return data;
    })
    .catch(function(e) {
      console.warn('[Paper:Library] Upsert failed:', e);
    });
}

/** One-time migration: push any old localStorage bookshelf entries to the
 *  server, then clear the legacy key. Runs at most once per browser. */
async function _migrateLegacyLibrary() {
  if (localStorage.getItem(_PAPER_MIGRATED_FLAG)) return;
  var raw = localStorage.getItem(_PAPER_LEGACY_LIB_KEY);
  if (!raw) {
    localStorage.setItem(_PAPER_MIGRATED_FLAG, '1');
    return;
  }
  var legacy;
  try { legacy = JSON.parse(raw); } catch (e) {
    console.warn('[Paper:Library] Legacy bookshelf parse failed, discarding:', e);
    localStorage.removeItem(_PAPER_LEGACY_LIB_KEY);
    localStorage.setItem(_PAPER_MIGRATED_FLAG, '1');
    return;
  }
  if (!Array.isArray(legacy) || legacy.length === 0) {
    localStorage.removeItem(_PAPER_LEGACY_LIB_KEY);
    localStorage.setItem(_PAPER_MIGRATED_FLAG, '1');
    return;
  }
  debugLog('[Paper] Migrating ' + legacy.length + ' bookshelf entries to server…', 'info');
  for (var i = 0; i < legacy.length; i++) {
    try { await _persistPaperEntry(legacy[i]); }
    catch (e) { console.warn('[Paper:Library] Migrate entry failed:', e); }
  }
  localStorage.removeItem(_PAPER_LEGACY_LIB_KEY);
  localStorage.setItem(_PAPER_MIGRATED_FLAG, '1');
  debugLog('[Paper] Migration complete.', 'success');
}

/** Load the bookshelf from the server into _paperLibrary. */
async function _loadPaperLibrary() {
  _activePaperId = localStorage.getItem(_PAPER_ACTIVE_KEY) || '';
  try {
    await _migrateLegacyLibrary();
    var resp = await fetch(apiUrl('/api/paper/library'));
    var data = await resp.json();
    if (data && data.ok && Array.isArray(data.papers)) {
      _paperLibrary = data.papers;
    } else {
      _paperLibrary = [];
      console.warn('[Paper:Library] Unexpected server response:', data);
    }
  } catch (e) {
    console.warn('[Paper:Library] Load failed, falling back to empty:', e);
    _paperLibrary = [];
  }
  // Drop active pointer if it no longer exists on the server
  if (_activePaperId && !_paperLibrary.some(function(p) { return p.id === _activePaperId; })) {
    _activePaperId = '';
    localStorage.removeItem(_PAPER_ACTIVE_KEY);
  }
}

function _setActivePaperId(id) {
  _activePaperId = id || '';
  if (_activePaperId) localStorage.setItem(_PAPER_ACTIVE_KEY, _activePaperId);
  else localStorage.removeItem(_PAPER_ACTIVE_KEY);
}

function _createPaperEntry(title, pdfUrl, parsedText, arxivId) {
  var entry = {
    id: 'paper_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8),
    title: title || 'Untitled Paper',
    pdfUrl: pdfUrl || '',
    pdfFilename: '',
    arxivId: arxivId || '',
    parsedText: parsedText || '',
    qaHistory: [],
    paperHash: '',
    images: [],
    babelCache: {},
    createdAt: Date.now(),
    pageCount: 0,
  };
  _paperLibrary.unshift(entry);
  _setActivePaperId(entry.id);
  _persistPaperEntry(entry);
  return entry;
}

function _getActivePaperEntry() {
  if (!_activePaperId) return null;
  for (var i = 0; i < _paperLibrary.length; i++) {
    if (_paperLibrary[i].id === _activePaperId) return _paperLibrary[i];
  }
  return null;
}

function _saveActivePaperState() {
  var entry = _getActivePaperEntry();
  if (!entry) return;
  entry.pdfUrl = _paperPdfUrl;
  entry.pdfFilename = _paperPdfFilename || entry.pdfFilename || '';
  entry.title = _paperFileName || entry.title;
  entry.parsedText = _paperParsedText;
  entry.arxivId = _paperArxivId;
  entry.qaHistory = _paperQAHistory;
  entry.paperHash = _paperHash || '';
  entry.images = Array.isArray(_paperImages) ? _paperImages : [];
  entry.babelCache = _babelTranslatedPages || {};
  entry.pageCount = _paperTotalPages;
  _persistPaperEntry(entry);
}

function _deletePaperEntry(id) {
  _paperLibrary = _paperLibrary.filter(function(p) { return p.id !== id; });
  if (_activePaperId === id) {
    _setActivePaperId(_paperLibrary.length > 0 ? _paperLibrary[0].id : '');
  }
  fetch(apiUrl('/api/paper/library/' + encodeURIComponent(id)), { method: 'DELETE' })
    .catch(function(e) { console.warn('[Paper:Library] Delete failed:', e); });
  _renderPaperLibrary();

  // If we deleted the active paper, load the next one or show landing
  if (paperMode) {
    var next = _getActivePaperEntry();
    if (next) {
      _openPaperEntry(next);
    } else {
      _resetReportLocalState();
      _paperPdfUrl = '';
      _paperPdfFilename = '';
      _paperFileName = '';
      _paperParsedText = '';
      _paperQAHistory = [];
      _paperReportCache = '';
      _paperHash = '';
      _paperImages = [];
      _babelTranslatedPages = {};
      _showPaperLanding();
      _updatePaperTitles();
    }
  }
}

function _openPaperEntry(entry) {
  // Save current paper's QA + state before switching
  _saveActivePaperState();

  // Abort any in-flight QA stream from the previous paper (report is
  // server-owned and keeps running; we just detach our local poll state).
  if (_paperQAAbort) { try { _paperQAAbort.abort(); } catch (_) {} _paperQAAbort = null; }
  // Drop local report poll state — any running server task remains alive
  // and will be re-attached via /api/paper/report/lookup when the user
  // opens the Report tab on the new (or original) paper.
  _resetReportLocalState();

  _setActivePaperId(entry.id);
  _paperPdfUrl = entry.pdfUrl || '';
  _paperPdfFilename = entry.pdfFilename || '';
  _paperFileName = entry.title || 'Untitled';
  _paperParsedText = entry.parsedText || '';
  _paperArxivId = entry.arxivId || '';
  _paperQAHistory = entry.qaHistory || [];
  _paperReportCache = '';  // Report is loaded from server DB on demand
  _paperHash = entry.paperHash || '';
  _paperImages = Array.isArray(entry.images) ? entry.images : [];
  _babelTranslatedPages = entry.babelCache || {};
  _paperTotalPages = entry.pageCount || 0;

  // Blank the right-hand panels IMMEDIATELY so the previous paper's report /
  // QA / babel output can't linger while the new content loads asynchronously.
  var _rcEl = document.getElementById('paperReportContent');
  if (_rcEl) {
    _rcEl.innerHTML = '<div class="paper-loading"><div class="paper-loading-spinner"></div><div>Loading…</div></div>';
  }
  var _qaEl = document.getElementById('paperQAMessages');
  if (_qaEl) _qaEl.innerHTML = '';

  _updatePaperTitles();
  _renderPaperLibrary();

  if (_paperPdfUrl) {
    _loadPaperPdf(_paperPdfUrl);
  } else {
    _showPaperLanding();
  }

  _switchPaperTab(_paperActiveTab || 'qa');
}

function _renderPaperLibrary() {
  var listEl = document.getElementById('paperLibraryList');
  if (!listEl) return;

  // Update count badge
  var countEl = document.getElementById('paperLibCount');
  if (countEl) countEl.textContent = _paperLibrary.length || '';

  if (_paperLibrary.length === 0) {
    listEl.innerHTML =
      '<div class="paper-lib-empty">' +
        '<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.3"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>' +
        '<span>No papers yet</span>' +
        '<span class="paper-lib-empty-hint">Upload a PDF or fetch from arXiv</span>' +
      '</div>';
    return;
  }

  var html = '';
  for (var i = 0; i < _paperLibrary.length; i++) {
    var p = _paperLibrary[i];
    var isActive = p.id === _activePaperId;
    var dateStr = _formatPaperDate(p.createdAt);
    var pageStr = p.pageCount ? p.pageCount + 'p' : '';
    var hasReport = p.hasReport ? ' · report' : '';

    html +=
      '<div class="paper-lib-item' + (isActive ? ' active' : '') + '" data-id="' + p.id + '" onclick="_onPaperLibClick(\'' + p.id + '\')">' +
        '<div class="paper-lib-item-icon">' +
          '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>' +
        '</div>' +
        '<div class="paper-lib-item-info">' +
          '<span class="paper-lib-item-title" title="' + escapeHtml(p.title) + '">' + escapeHtml(p.title) + '</span>' +
          '<span class="paper-lib-item-meta">' + dateStr + (pageStr ? ' · ' + pageStr : '') + hasReport + '</span>' +
        '</div>' +
        '<button class="paper-lib-item-del" onclick="event.stopPropagation();_deletePaperEntry(\'' + p.id + '\')" title="Delete">' +
          '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>' +
        '</button>' +
      '</div>';
  }
  listEl.innerHTML = html;
}

function _onPaperLibClick(id) {
  for (var i = 0; i < _paperLibrary.length; i++) {
    if (_paperLibrary[i].id === id) {
      _openPaperEntry(_paperLibrary[i]);
      return;
    }
  }
}

function _formatPaperDate(ts) {
  if (!ts) return '';
  var d = new Date(ts);
  var now = new Date();
  var diff = now - d;
  if (diff < 86400000) {
    var h = d.getHours();
    var m = d.getMinutes();
    return (h < 10 ? '0' : '') + h + ':' + (m < 10 ? '0' : '') + m;
  }
  if (diff < 86400000 * 7) {
    return Math.floor(diff / 86400000) + 'd ago';
  }
  return (d.getMonth() + 1) + '/' + d.getDate();
}

// ══════════════════════════════════════════════════════
//  ★ Enter / Exit Paper Mode
// ══════════════════════════════════════════════════════

async function enterPaperMode(pdfUrl, fileName, parsedText, arxivId) {
  if (typeof imageGenMode !== 'undefined' && imageGenMode) exitImageGenMode();

  // Load bookshelf from the server so we see the same papers on every machine.
  // On fresh page-loads _paperLibrary is empty, so we must await before touching it.
  try { await _loadPaperLibrary(); }
  catch (e) { console.warn('[Paper] loadPaperLibrary failed:', e); }
  paperMode = true;

  // If called with a new PDF (not from library), create an entry
  if (pdfUrl && !_activePaperId) {
    _createPaperEntry(fileName, pdfUrl, parsedText, arxivId);
  } else if (pdfUrl) {
    // Update current entry if called with new data
    _paperPdfUrl = pdfUrl;
    _paperFileName = fileName || '';
    _paperParsedText = parsedText || '';
    _paperArxivId = arxivId || '';
  } else {
    // Entering paper mode without a specific PDF — restore last active
    var active = _getActivePaperEntry();
    if (active) {
      _paperPdfUrl = active.pdfUrl || '';
      _paperPdfFilename = active.pdfFilename || '';
      _paperFileName = active.title || '';
      _paperParsedText = active.parsedText || '';
      _paperArxivId = active.arxivId || '';
      _paperQAHistory = active.qaHistory || [];
      _paperReportCache = '';  // loaded from server DB on demand
      _paperHash = active.paperHash || '';
      _paperImages = Array.isArray(active.images) ? active.images : [];
      _babelTranslatedPages = active.babelCache || {};
      _paperTotalPages = active.pageCount || 0;
    } else {
      _paperPdfUrl = '';
      _paperPdfFilename = '';
      _paperFileName = '';
      _paperParsedText = '';
      _paperArxivId = '';
      _paperQAHistory = [];
      _paperReportCache = '';
      _paperHash = '';
      _paperImages = [];
      _babelTranslatedPages = {};
    }
  }

  _paperActiveTab = 'qa';
  if (!_paperQAHistory) _paperQAHistory = [];
  if (!_paperReportCache) _paperReportCache = '';

  // Sidebar → show paper library, hide conversations
  var sidebar = document.getElementById('sidebar');
  if (sidebar) {
    sidebar.classList.add('paper-active');
    if (sidebar.classList.contains('collapsed') && typeof toggleSidebar === 'function') toggleSidebar();
  }

  _updatePaperTitles();
  _renderPaperLibrary();

  // Show paper container, hide chat
  var container = document.getElementById('paperModeContainer');
  var chatWrapper = document.querySelector('.chat-wrapper');
  var inputArea = document.querySelector('.input-area');
  if (container) container.style.display = 'flex';
  if (chatWrapper) chatWrapper.style.display = 'none';
  if (inputArea) inputArea.style.display = 'none';

  var pmBtn = document.getElementById('paperModeBtn');
  if (pmBtn) {
    pmBtn.classList.add('active');
    // Swap icon to back-arrow
    pmBtn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>';
    pmBtn.title = 'Back to Chat';
  }

  if (_paperPdfUrl) {
    _loadPaperPdf(_paperPdfUrl);
  } else {
    _showPaperLanding();
  }

  _switchPaperTab('qa');

  // Seed the report model selection so the button label reflects the actual
  // model from the start (no more stale "Default" placeholder).
  try { _populatePaperReportModelDropdown(); } catch (e) {
    console.warn('[Paper] populate report model dropdown failed:', e);
  }

  debugLog('Paper Mode: ENTER', 'success');
}

function exitPaperMode() {
  _saveActivePaperState();
  paperMode = false;

  // ★ Restore topbar title to the active conversation (or 'New Chat' if none)
  try {
    var topbar = document.getElementById('topbarTitle');
    if (topbar) {
      var conv = (typeof activeConvId !== 'undefined' && activeConvId && typeof conversations !== 'undefined')
        ? (conversations || []).find(function (c) { return c && c.id === activeConvId; })
        : null;
      topbar.textContent = conv && conv.title ? conv.title : 'New Chat';
      topbar.title = '';
    }
  } catch (e) { console.warn('[Paper] restore topbar title failed:', e); }

  var sidebar = document.getElementById('sidebar');
  if (sidebar) sidebar.classList.remove('paper-active');

  var container = document.getElementById('paperModeContainer');
  var chatWrapper = document.querySelector('.chat-wrapper');
  var inputArea = document.querySelector('.input-area');
  if (container) container.style.display = 'none';
  if (chatWrapper) chatWrapper.style.display = '';
  if (inputArea) inputArea.style.display = '';

  var pmBtn = document.getElementById('paperModeBtn');
  if (pmBtn) {
    pmBtn.classList.remove('active');
    // Restore book icon
    pmBtn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/><line x1="8" y1="7" x2="16" y2="7"/><line x1="8" y1="11" x2="14" y2="11"/></svg>';
    pmBtn.title = 'Paper Reader';
  }

  if (_paperResizeObserver) { _paperResizeObserver.disconnect(); _paperResizeObserver = null; }
  if (_paperPdfDoc) { _paperPdfDoc.destroy(); _paperPdfDoc = null; }
  if (_paperQAAbort) { _paperQAAbort.abort(); _paperQAAbort = null; }

  // Stop the report poll timer — the server task keeps running and
  // will be re-attached on next entry via /api/paper/report/lookup.
  if (_paperReportStream && _paperReportStream.pollTimer) {
    clearTimeout(_paperReportStream.pollTimer);
    _paperReportStream.pollTimer = null;
  }

  var viewer = document.getElementById('paperPdfViewer');
  if (viewer) viewer.innerHTML = '';

  debugLog('Paper Mode: EXIT', 'info');
}

function togglePaperMode() {
  paperMode ? exitPaperMode() : enterPaperMode();
}

function _updatePaperTitles() {
  var name = _paperFileName || 'Paper Reader';
  var stitle = document.getElementById('paperSidebarTitle');
  if (stitle) { stitle.textContent = name; stitle.title = name; }
  var pageCount = document.getElementById('paperPageCount');
  if (pageCount && _paperTotalPages) {
    pageCount.textContent = _paperTotalPages + (_paperTotalPages === 1 ? ' page' : ' pages');
  } else if (pageCount) {
    pageCount.textContent = '';
  }
  // ★ Topbar title reflects Paper Mode, not the previous conversation
  if (paperMode) {
    var topbar = document.getElementById('topbarTitle');
    if (topbar) {
      var label = _paperFileName ? _paperFileName : 'Paper Reader';
      topbar.textContent = label;
      topbar.title = label;
    }
  }
}

// ══════════════════════════════════════════════════════
//  ★ PDF Loading & Rendering (always in #paperPdfViewer)
// ══════════════════════════════════════════════════════

async function _loadPaperPdf(url) {
  var viewer = document.getElementById('paperPdfViewer');
  if (!viewer) return;
  viewer.innerHTML = '<div class="paper-loading"><div class="paper-loading-spinner"></div><div>Loading PDF…</div></div>';

  try {
    if (typeof pdfjsLib === 'undefined') {
      if (typeof _ensurePdfJs === 'function') await _ensurePdfJs();
      else { viewer.innerHTML = '<div class="paper-error">PDF.js not available. Refresh the page.</div>'; return; }
    }
    if (typeof pdfjsLib === 'undefined') {
      viewer.innerHTML = '<div class="paper-error">PDF.js failed to load.</div>';
      return;
    }

    if (_paperPdfDoc) { try { _paperPdfDoc.destroy(); } catch (_) {} _paperPdfDoc = null; }

    _paperPdfDoc = await pdfjsLib.getDocument(url).promise;
    _paperTotalPages = _paperPdfDoc.numPages;
    _updatePaperTitles();
    // Auto fit-to-width on initial load so the PDF sizes to the current panel
    // regardless of the current _paperScale value (matches Chrome/Acrobat default).
    try {
      var _firstPage = await _paperPdfDoc.getPage(1);
      var _baseVp = _firstPage.getViewport({ scale: 1.0 });
      var _container = document.getElementById('paperPdfViewer');
      var _containerW = _container ? (_container.clientWidth - 32) : 0;
      if (_containerW > 0) {
        _paperScale = Math.max(0.25, Math.min(4.0, _containerW / _baseVp.width));
      }
    } catch (err) {
      console.warn('[Paper] Initial fit-width failed:', err);
    }
    _updateZoomLabel();
    await _renderAllPages();

    // Update library entry
    var entry = _getActivePaperEntry();
    if (entry) { entry.pageCount = _paperTotalPages; _persistPaperEntry(entry); }
    _renderPaperLibrary();
  } catch (e) {
    console.error('[Paper] Failed to load PDF:', e);
    viewer.innerHTML = '<div class="paper-error">Failed to load PDF: ' + escapeHtml(e.message) + '</div>';
  }
}

/** Render all pages vertically for scroll-based reading.
 *
 *  Strategy for sharp rendering + selectable text:
 *  1. Use a "CSS viewport" at _paperScale for layout dimensions.
 *  2. Render canvas pixel buffer at cssScale × devicePixelRatio for sharpness
 *     on HiDPI screens, but CSS-size the canvas to the CSS viewport dims.
 *  3. The wrapper div uses explicit CSS width/height (no aspect-ratio hack)
 *     so it works in all browsers.
 *  4. Text layer is positioned at CSS viewport size, absolutely covering
 *     the canvas, with transparent text + pointer-events for selection.
 */
async function _renderAllPages() {
  if (!_paperPdfDoc) return;
  var viewer = document.getElementById('paperPdfViewer');
  if (!viewer) return;
  viewer.innerHTML = '';

  var dpr = window.devicePixelRatio || 1;

  for (var i = 1; i <= _paperTotalPages; i++) {
    try {
      var page = await _paperPdfDoc.getPage(i);

      // CSS viewport — determines the on-screen layout size
      var cssViewport = page.getViewport({ scale: _paperScale });
      var cssW = cssViewport.width;
      var cssH = cssViewport.height;

      // Hi-res viewport — for sharp canvas pixel buffer
      var hiresViewport = page.getViewport({ scale: _paperScale * dpr });

      // ── Wrapper: explicit CSS dimensions, aspect-ratio for proportional scaling ──
      var wrapper = document.createElement('div');
      wrapper.className = 'paper-page-wrapper';
      wrapper.dataset.page = i;
      wrapper.style.width = cssW + 'px';
      wrapper.style.aspectRatio = (cssW / cssH).toFixed(6);

      // ── Canvas: hi-res buffer, CSS-sized to layout viewport ──
      // Only set width; height auto-scales via CSS aspect ratio
      var canvas = document.createElement('canvas');
      canvas.className = 'paper-pdf-canvas';
      canvas.width = hiresViewport.width;
      canvas.height = hiresViewport.height;
      canvas.style.width = cssW + 'px';
      wrapper.appendChild(canvas);

      // ── Text layer: original CSS dimensions, scaled via transform when wrapper shrinks ──
      var textDiv = document.createElement('div');
      textDiv.className = 'paper-text-layer';
      textDiv.style.width = cssW + 'px';
      textDiv.style.height = cssH + 'px';
      // pdf.js v3.x requires --scale-factor for correct text span positioning
      textDiv.style.setProperty('--scale-factor', _paperScale.toString());
      wrapper.appendChild(textDiv);

      // ── Page number label ──
      var pageLabel = document.createElement('div');
      pageLabel.className = 'paper-page-label';
      pageLabel.textContent = i + ' / ' + _paperTotalPages;
      wrapper.appendChild(pageLabel);

      viewer.appendChild(wrapper);

      // ── Render canvas at hi-res ──
      var ctx = canvas.getContext('2d');
      await page.render({ canvasContext: ctx, viewport: hiresViewport }).promise;

      // ── Render text layer at CSS viewport scale ──
      var textContent = await page.getTextContent();
      if (typeof pdfjsLib.renderTextLayer === 'function') {
        pdfjsLib.renderTextLayer({
          textContentSource: textContent,
          container: textDiv,
          viewport: cssViewport,
          textDivs: [],
        });
      }
    } catch (e) {
      console.warn('[Paper] Failed to render page', i, ':', e);
      var errDiv = document.createElement('div');
      errDiv.className = 'paper-page-error';
      errDiv.textContent = 'Page ' + i + ' failed to render';
      viewer.appendChild(errDiv);
    }
  }

  // Observe wrappers to scale text layers when container shrinks
  _observePageWrappers(viewer);
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
        ? (entries[i].contentBoxSize[0] || entries[i].contentBoxSize).inlineSize
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

/** Fit PDF width to container width */
function paperFitWidth() {
  if (!_paperPdfDoc) return;
  var container = document.getElementById('paperPdfViewer');
  if (!container) return;
  // Get first page to calculate ratio
  _paperPdfDoc.getPage(1).then(function(page) {
    var baseViewport = page.getViewport({ scale: 1.0 });
    var containerWidth = container.clientWidth - 32; // subtract padding
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

// ── Draggable Divider ──

(function() {
  var _dragging = false;
  var _startX = 0;
  var _startLeftW = 0;
  var _startRightW = 0;
  var _divider, _left, _right, _body;

  function _initDivider() {
    _divider = document.getElementById('paperDivider');
    if (!_divider) return;
    _divider.addEventListener('mousedown', _onMouseDown);
    // Touch support for tablets
    _divider.addEventListener('touchstart', _onTouchStart, { passive: false });
  }

  function _getElements() {
    _left = _divider ? _divider.previousElementSibling : null;
    _right = _divider ? _divider.nextElementSibling : null;
    _body = _divider ? _divider.parentElement : null;
  }

  function _onMouseDown(e) {
    e.preventDefault();
    _getElements();
    if (!_left || !_right || !_body) return;
    _dragging = true;
    _startX = e.clientX;
    _startLeftW = _left.getBoundingClientRect().width;
    _startRightW = _right.getBoundingClientRect().width;
    // Only set left to explicit width; right stays flex:1 to fill remaining space (prevents blank gap)
    _left.style.flex = 'none';
    _left.style.width = _startLeftW + 'px';
    _right.style.flex = '1';
    _right.style.width = '';
    _right.style.minWidth = '250px';
    _divider.classList.add('dragging');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    document.addEventListener('mousemove', _onMouseMove);
    document.addEventListener('mouseup', _onMouseUp);
  }

  function _onMouseMove(e) {
    if (!_dragging) return;
    var dx = e.clientX - _startX;
    var bodyW = _body.getBoundingClientRect().width;
    var dividerW = _divider.getBoundingClientRect().width;
    var available = bodyW - dividerW;
    var newLeftW = Math.max(250, Math.min(available - 250, _startLeftW + dx));
    _left.style.width = newLeftW + 'px';
    // Right panel auto-fills via flex:1
  }

  function _onMouseUp() {
    _dragging = false;
    _divider.classList.remove('dragging');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    document.removeEventListener('mousemove', _onMouseMove);
    document.removeEventListener('mouseup', _onMouseUp);
    _autoRefitIfOverflowing();
  }

  // Touch support
  function _onTouchStart(e) {
    if (e.touches.length !== 1) return;
    e.preventDefault();
    _getElements();
    if (!_left || !_right || !_body) return;
    _dragging = true;
    _startX = e.touches[0].clientX;
    _startLeftW = _left.getBoundingClientRect().width;
    _startRightW = _right.getBoundingClientRect().width;
    _left.style.flex = 'none';
    _left.style.width = _startLeftW + 'px';
    _right.style.flex = '1';
    _right.style.width = '';
    _right.style.minWidth = '250px';
    _divider.classList.add('dragging');
    document.addEventListener('touchmove', _onTouchMove, { passive: false });
    document.addEventListener('touchend', _onTouchEnd);
  }

  function _onTouchMove(e) {
    if (!_dragging || e.touches.length !== 1) return;
    e.preventDefault();
    var dx = e.touches[0].clientX - _startX;
    var bodyW = _body.getBoundingClientRect().width;
    var dividerW = _divider.getBoundingClientRect().width;
    var available = bodyW - dividerW;
    var newLeftW = Math.max(250, Math.min(available - 250, _startLeftW + dx));
    _left.style.width = newLeftW + 'px';
    // Right panel auto-fills via flex:1
  }

  function _onTouchEnd() {
    _dragging = false;
    _divider.classList.remove('dragging');
    document.removeEventListener('touchmove', _onTouchMove);
    document.removeEventListener('touchend', _onTouchEnd);
    _autoRefitIfOverflowing();
  }

  /** If divider drag shrank the panel enough that PDF pages now overflow
   *  horizontally, auto fit-to-width. Widening the panel preserves the
   *  user's current zoom (they get more whitespace, not a surprise re-render). */
  function _autoRefitIfOverflowing() {
    try {
      if (typeof _paperPdfDoc === 'undefined' || !_paperPdfDoc) return;
      var viewer = document.getElementById('paperPdfViewer');
      if (!viewer) return;
      var firstWrapper = viewer.querySelector('.paper-page-wrapper');
      if (!firstWrapper) return;
      var pageW = parseFloat(firstWrapper.style.width) || firstWrapper.clientWidth;
      var availW = viewer.clientWidth - 32;
      if (availW > 0 && pageW > availW + 1 && typeof paperFitWidth === 'function') {
        paperFitWidth();
      }
    } catch (err) {
      console.warn('[Paper] Auto-refit check failed:', err);
    }
  }

  // Double-click to reset to 50/50
  function _onDblClick() {
    _getElements();
    if (!_left || !_right) return;
    _left.style.flex = '1';
    _left.style.width = '';
    _right.style.flex = '1';
    _right.style.width = '';
    _right.style.minWidth = '';
  }

  // Init when DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function() {
      _initDivider();
      var d = document.getElementById('paperDivider');
      if (d) d.addEventListener('dblclick', _onDblClick);
    });
  } else {
    _initDivider();
    var d = document.getElementById('paperDivider');
    if (d) d.addEventListener('dblclick', _onDblClick);
  }
})();

// ══════════════════════════════════════════════════════
//  ★ Landing / Upload Screen
// ══════════════════════════════════════════════════════

function _showPaperLanding() {
  var viewer = document.getElementById('paperPdfViewer');
  if (!viewer) return;
  viewer.innerHTML =
    '<div class="paper-landing">' +
      '<div class="paper-landing-icon">📄</div>' +
      '<h3>Paper Reader</h3>' +
      '<p>Upload a PDF or paste an arXiv URL to get started</p>' +
      '<div class="paper-landing-actions">' +
        '<label class="paper-upload-btn">' +
          '<input type="file" accept=".pdf,application/pdf" onchange="_handlePaperFileUpload(event)" style="display:none">' +
          '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>' +
          ' Upload PDF' +
        '</label>' +
        '<div class="paper-arxiv-input">' +
          '<input type="text" id="paperArxivUrl" placeholder="arXiv URL or ID (e.g. 2301.12345)"' +
                 ' onkeydown="if(event.key===\'Enter\')_fetchArxivPaper()">' +
          '<button onclick="_fetchArxivPaper()" class="paper-arxiv-btn">Fetch</button>' +
        '</div>' +
      '</div>' +
    '</div>';
}

function _showPaperLandingForNew() {
  // Clear in-memory "which paper am I looking at" state and show the landing.
  // No new DB row is created until the user actually uploads or fetches a PDF.
  _setActivePaperId('');
  _paperPdfUrl = '';
  _paperPdfFilename = '';
  _paperFileName = '';
  _paperParsedText = '';
  _paperArxivId = '';
  _paperQAHistory = [];
  _paperReportCache = '';
  _paperHash = '';
  _paperImages = [];
  _babelTranslatedPages = {};
  _paperTotalPages = 0;
  _updatePaperTitles();
  _renderPaperLibrary();
  _showPaperLanding();
}

async function _handlePaperFileDrop(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith('.pdf') && file.type !== 'application/pdf') return;
  if (!paperMode) enterPaperMode();
  await _paperUploadFile(file);
}

async function _handlePaperFileUpload(event) {
  var file = event.target.files[0];
  if (!file || !file.name.toLowerCase().endsWith('.pdf')) return;
  await _paperUploadFile(file);
}

async function _paperUploadFile(file) {
  _paperLoading = true;

  // Create a new library entry for this paper (_createPaperEntry sets _activePaperId)
  _createPaperEntry(file.name);
  _paperFileName = file.name;
  _paperParsedText = '';
  _paperQAHistory = [];
  _paperReportCache = '';
  _paperHash = '';
  _paperPdfFilename = '';
  _paperImages = [];
  _babelTranslatedPages = {};
  _updatePaperTitles();
  _renderPaperLibrary();

  var viewer = document.getElementById('paperPdfViewer');
  if (viewer) viewer.innerHTML = '<div class="paper-loading"><div class="paper-loading-spinner"></div><div>Uploading PDF…</div></div>';

  try {
    var formData = new FormData();
    formData.append('file', file);
    var uploadResp = await fetch(apiUrl('/api/paper/upload'), { method: 'POST', body: formData });
    var uploadData = await uploadResp.json();
    if (!uploadData.ok) throw new Error(uploadData.error || 'Upload failed');

    _paperPdfUrl = apiUrl(uploadData.pdf_url);
    _paperPdfFilename = uploadData.filename || '';
    _updatePaperTitles();

    // Parse text for QA/report
    var parseForm = new FormData();
    parseForm.append('file', file);
    parseForm.append('maxTextChars', '0');
    parseForm.append('maxImages', '0');
    var parseResp = await fetch(apiUrl('/api/pdf/parse'), { method: 'POST', body: parseForm });
    var parseData = await parseResp.json();
    if (parseData.success) {
      _paperParsedText = parseData.text || '';
      debugLog('Paper parsed: ' + parseData.totalPages + ' pages, ' + parseData.textLength + ' chars', 'success');
    }

    await _loadPaperPdf(_paperPdfUrl);
    _saveActivePaperState();

    // Kick off image extraction in the background so the Report can embed figures/tables
    _extractPaperImages();

  } catch (e) {
    console.error('[Paper] Upload failed:', e);
    if (viewer) viewer.innerHTML = '<div class="paper-error">Upload failed: ' + escapeHtml(e.message) + '</div>';
  } finally {
    _paperLoading = false;
  }
}

/** Extract figures/tables from the current paper PDF (server-side).
 * Populates _paperImages with [{url, caption, page, source, width, height}]
 * and persists them on the active library entry. Silent / best-effort. */
async function _extractPaperImages() {
  if (!_paperPdfFilename) {
    // Try to recover from the public URL: "/api/paper/pdf/<filename>"
    var m = /\/api\/paper\/pdf\/([^?#]+)/.exec(_paperPdfUrl || '');
    if (m) _paperPdfFilename = decodeURIComponent(m[1]);
  }
  if (!_paperPdfFilename) {
    console.warn('[Paper] _extractPaperImages: no filename, skipping');
    return;
  }
  try {
    var body = { filename: _paperPdfFilename };
    if (_paperHash) body.paper_hash = _paperHash;
    var resp = await fetch(apiUrl('/api/paper/extract-images'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    var data = await resp.json();
    if (!data.ok) {
      console.warn('[Paper] Image extraction failed:', data.error);
      return;
    }
    _paperImages = Array.isArray(data.images) ? data.images : [];
    if (data.paper_hash && !_paperHash) _paperHash = data.paper_hash;
    _saveActivePaperState();
    if (_paperImages.length) debugLog('Extracted ' + _paperImages.length + ' figures/tables', 'success');
  } catch (e) {
    console.warn('[Paper] Image extraction error:', e);
  }
}

/** Format bytes as a human-friendly string (KB / MB). */
function _formatPaperBytes(n) {
  if (!n || n < 0) return '0 B';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / (1024 * 1024)).toFixed(2) + ' MB';
}

/** Render the arXiv fetch progress UI into the PDF viewer. */
function _renderArxivFetchProgress(state) {
  var viewer = document.getElementById('paperPdfViewer');
  if (!viewer) return;
  var isZh = (typeof _i18nLang !== 'undefined' && _i18nLang === 'zh');
  var labels = isZh
    ? { resolving: '解析 arXiv 链接…', downloading: '下载 PDF…',
        parsing: '解析 PDF 文本…', parsingImages: '提取图表…',
        pageOf: '第 {done} / {total} 页',
        cached: '已从缓存加载', pages: '页', chars: '字符' }
    : { resolving: 'Resolving arXiv link…', downloading: 'Downloading PDF…',
        parsing: 'Extracting PDF text…', parsingImages: 'Extracting figures…',
        pageOf: 'page {done} / {total}',
        cached: 'Loaded from cache', pages: 'pages', chars: 'chars' };

  var title;
  if (state.stage === 'resolve') title = labels.resolving;
  else if (state.stage === 'download') title = labels.downloading;
  else if (state.stage === 'download_done') title = state.cached ? labels.cached : labels.downloading;
  else if (state.stage === 'parse_start' || state.stage === 'parse_done') title = labels.parsing;
  else if (state.stage === 'parse_progress') {
    title = (state.parse_stage === 'images') ? labels.parsingImages : labels.parsing;
  }
  else title = labels.resolving;

  var pct = 0;
  var detail = '';
  if (state.stage === 'download') {
    if (state.total > 0) {
      pct = Math.min(100, Math.round(state.downloaded * 100 / state.total));
      detail = _formatPaperBytes(state.downloaded) + ' / ' + _formatPaperBytes(state.total);
    } else {
      detail = _formatPaperBytes(state.downloaded);
      pct = -1;  // indeterminate
    }
  } else if (state.stage === 'download_done') {
    pct = 100;
    detail = _formatPaperBytes(state.file_size || 0);
  } else if (state.stage === 'parse_start') {
    pct = -1;
    detail = '';
  } else if (state.stage === 'parse_progress') {
    var done = state.page || 0;
    var total = state.total_pages || 0;
    if (total > 0) {
      pct = Math.min(100, Math.round(done * 100 / total));
      detail = labels.pageOf.replace('{done}', done).replace('{total}', total);
    } else {
      pct = -1;
      detail = '';
    }
  } else if (state.stage === 'parse_done') {
    pct = 100;
    detail = (state.total_pages || 0) + ' ' + labels.pages +
             ' · ' + (state.text_length || 0).toLocaleString() + ' ' + labels.chars;
  }

  var barStyle = (pct < 0)
    ? 'width:40%;animation:paperProgressIndet 1.2s ease-in-out infinite'
    : 'width:' + pct + '%';

  viewer.innerHTML =
    '<div class="paper-loading paper-fetch-progress">' +
      '<div class="paper-loading-spinner"></div>' +
      '<div class="paper-fetch-title">' + escapeHtml(title) +
        (state.arxiv_id ? ' <span class="paper-fetch-id">arXiv:' + escapeHtml(state.arxiv_id) + '</span>' : '') +
      '</div>' +
      '<div class="paper-fetch-bar-wrap"><div class="paper-fetch-bar" style="' + barStyle + '"></div></div>' +
      (detail ? '<div class="paper-fetch-detail">' + escapeHtml(detail) + '</div>' : '') +
    '</div>';
}

async function _fetchArxivPaper() {
  var input = document.getElementById('paperArxivUrl');
  var url = input?.value?.trim();
  if (!url) { debugLog('Please enter an arXiv URL or ID', 'warning'); return; }

  _paperLoading = true;
  _renderArxivFetchProgress({ stage: 'resolve' });

  try {
    var resp = await fetch(apiUrl('/api/paper/fetch-arxiv-stream'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: url }),
    });
    if (!resp.ok || !resp.body) {
      var errText = '';
      try { var j = await resp.json(); errText = j.error || ''; } catch (_) {}
      throw new Error(errText || ('HTTP ' + resp.status));
    }

    var reader = resp.body.getReader();
    var decoder = new TextDecoder();
    var buffer = '';
    var doneData = null;
    var streamErr = '';
    var curArxivId = '';

    while (true) {
      var r = await reader.read();
      if (r.done) break;
      buffer += decoder.decode(r.value, { stream: true });
      var lines = buffer.split('\n');
      buffer = lines.pop();
      for (var li = 0; li < lines.length; li++) {
        var line = lines[li];
        if (!line.startsWith('data: ')) continue;
        var payload = line.slice(6).trim();
        if (!payload) continue;
        var ev;
        try { ev = JSON.parse(payload); }
        catch (pe) { console.warn('[Paper:arXiv] Bad SSE payload:', pe, payload); continue; }

        if (ev.arxiv_id) curArxivId = ev.arxiv_id;
        ev.arxiv_id = ev.arxiv_id || curArxivId;

        if (ev.stage === 'error') { streamErr = ev.error || 'Fetch failed'; break; }
        _renderArxivFetchProgress(ev);

        if (ev.stage === 'done') { doneData = ev; }
      }
      if (streamErr) break;
    }

    if (streamErr) throw new Error(streamErr);
    if (!doneData) throw new Error('Fetch ended without completion');

    _paperPdfUrl = apiUrl(doneData.pdf_url);
    // Extract filename from pdf_url (e.g. "/api/paper/pdf/arxiv_2301.12345.pdf") for image extraction
    var _pdfMatch = /\/api\/paper\/pdf\/([^?#]+)/.exec(doneData.pdf_url || '');
    _paperPdfFilename = _pdfMatch ? decodeURIComponent(_pdfMatch[1]) : '';
    _paperArxivId = doneData.arxiv_id || curArxivId || '';
    _paperFileName = 'arXiv:' + _paperArxivId;
    _paperParsedText = doneData.parsed_text || '';
    _paperTotalPages = doneData.total_pages || 0;

    // Create library entry now that we have everything (sets _activePaperId)
    _createPaperEntry(_paperFileName, _paperPdfUrl, _paperParsedText, _paperArxivId);
    _paperQAHistory = [];
    _paperReportCache = '';
    _paperHash = '';
    _paperImages = [];
    _babelTranslatedPages = {};
    _updatePaperTitles();
    _renderPaperLibrary();

    if (doneData.parse_error) {
      debugLog('[Paper] PDF text extraction failed: ' + doneData.parse_error, 'warning');
    } else if (_paperParsedText) {
      debugLog('arXiv parsed: ' + _paperTotalPages + ' pages, ' + (doneData.text_length || _paperParsedText.length) + ' chars', 'success');
    } else {
      debugLog('[Paper] arXiv PDF loaded but no text extracted — Q&A and Report unavailable', 'warning');
    }

    await _loadPaperPdf(_paperPdfUrl);
    _saveActivePaperState();

    // Kick off image extraction in the background so the Report can embed figures/tables
    _extractPaperImages();

    debugLog('Fetched arXiv:' + _paperArxivId + (doneData.cached ? ' (cached)' : ''), 'success');
  } catch (e) {
    console.error('[Paper] arXiv fetch failed:', e);
    var viewer = document.getElementById('paperPdfViewer');
    if (viewer) viewer.innerHTML = '<div class="paper-error">Failed: ' + escapeHtml(e.message || String(e)) + '<br><button onclick="_showPaperLanding()" class="paper-retry-btn">Try Again</button></div>';
  } finally {
    _paperLoading = false;
  }
}

// ══════════════════════════════════════════════════════
//  ★ Tab Switching
// ══════════════════════════════════════════════════════

function _switchPaperTab(tab) {
  _paperActiveTab = tab;
  document.querySelectorAll('.paper-tab-btn').forEach(function(btn) {
    btn.classList.toggle('active', btn.dataset.tab === tab);
  });
  document.querySelectorAll('.paper-tab-panel').forEach(function(panel) {
    panel.style.display = panel.dataset.tab === tab ? '' : 'none';
  });
  if (tab === 'report') {
    // Server owns the report task. The frontend always asks
    // _loadOrGenerateReport() which (a) resumes local poll if any, (b)
    // looks up a running server task, (c) hits DB cache, or (d) starts.
    if (_paperParsedText || _paperHash) {
      _loadOrGenerateReport();
    } else {
      var _empty = document.getElementById('paperReportContent');
      if (_empty) {
        _empty.innerHTML = '<div class="paper-report-empty"><p>No paper text available. Load a PDF first.</p></div>';
      }
    }
  }
  if (tab === 'qa') _renderPaperQA();
  if (tab === 'translate') _initBabelPdfTab();
}

// ══════════════════════════════════════════════════════
//  ★ Tab 1: Q&A
// ══════════════════════════════════════════════════════

function _renderPaperQA() {
  var container = document.getElementById('paperQAMessages');
  if (!container) return;
  if (!_paperQAHistory || _paperQAHistory.length === 0) {
    container.innerHTML =
      '<div class="paper-qa-empty"><div class="paper-qa-empty-icon">💬</div>' +
      '<p>Ask questions about this paper</p>' +
      '<p class="paper-qa-hint">Select text in the PDF to quote it, or type a question below</p></div>';
    return;
  }
  var html = '';
  for (var j = 0; j < _paperQAHistory.length; j++) {
    var msg = _paperQAHistory[j];
    var isUser = msg.role === 'user';
    var ch = isUser ? escapeHtml(msg.content) : (typeof renderMarkdown === 'function' ? renderMarkdown(msg.content) : escapeHtml(msg.content));
    html += '<div class="paper-qa-msg ' + (isUser ? 'paper-qa-user' : 'paper-qa-assistant') + '"><div class="paper-qa-msg-content">' + ch + '</div></div>';
  }
  container.innerHTML = html;
  container.scrollTop = container.scrollHeight;
}

/** Recover paper text by asking the server to re-parse the already-stored PDF.
 * Used when a library entry was saved before server-side parsing (or parsing failed).
 * Returns true on success, false otherwise. */
async function _ensurePaperText() {
  if (_paperParsedText) return true;
  // Figure out the server filename — prefer the stored one, fall back to URL match
  var fname = _paperPdfFilename;
  if (!fname && _paperPdfUrl) {
    var m = /\/api\/paper\/pdf\/([^?#]+)/.exec(_paperPdfUrl);
    if (m) fname = decodeURIComponent(m[1]);
  }
  if (!fname) return false;
  try {
    debugLog('[Paper] Re-parsing PDF to recover text…', 'info');
    var resp = await fetch(apiUrl('/api/paper/reparse'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: fname }),
    });
    var data = await resp.json();
    if (!data.ok || !data.text) {
      debugLog('[Paper] Re-parse failed: ' + (data.error || 'empty text'), 'warning');
      return false;
    }
    _paperParsedText = data.text;
    if (data.total_pages) _paperTotalPages = data.total_pages;
    _saveActivePaperState();
    debugLog('[Paper] Recovered ' + (data.text_length || data.text.length) + ' chars from PDF', 'success');
    return true;
  } catch (e) {
    console.warn('[Paper] Re-parse request failed:', e);
    debugLog('[Paper] Re-parse request failed: ' + (e.message || e), 'warning');
    return false;
  }
}

async function _sendPaperQuestion() {
  var input = document.getElementById('paperQAInput');
  var question = input?.value?.trim();
  if (!question || _paperQAStreaming) return;

  if (!_paperParsedText) {
    var ok = await _ensurePaperText();
    if (!ok) {
      debugLog('No paper text available — PDF may be scanned or parsing failed', 'warning');
      return;
    }
  }

  _paperQAHistory.push({ role: 'user', content: question, timestamp: Date.now() });
  input.value = '';
  _renderPaperQA();

  var systemMsg = 'You are a helpful research assistant. The user is reading an academic paper. Answer based on the paper content. Be specific and cite sections.\n\nPaper text:\n' + _paperParsedText.slice(0, 100000);
  var messages = [{ role: 'system', content: systemMsg }];
  var recent = _paperQAHistory.slice(-10);
  for (var k = 0; k < recent.length; k++) messages.push({ role: recent[k].role, content: recent[k].content });

  _paperQAHistory.push({ role: 'assistant', content: '', timestamp: Date.now() });
  _paperQAStreaming = true;

  try {
    _paperQAAbort = new AbortController();
    var resp = await fetch(apiUrl('/api/paper/chat'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      signal: _paperQAAbort.signal,
      body: JSON.stringify({ messages: messages, stream: true }),
    });
    var reader = resp.body.getReader();
    var decoder = new TextDecoder();
    var buffer = '';
    while (true) {
      var r = await reader.read();
      if (r.done) break;
      buffer += decoder.decode(r.value, { stream: true });
      var lines = buffer.split('\n'); buffer = lines.pop();
      for (var li = 0; li < lines.length; li++) {
        if (!lines[li].startsWith('data: ')) continue;
        var d = lines[li].slice(6).trim();
        if (d === '[DONE]') continue;
        try {
          var delta = JSON.parse(d).choices?.[0]?.delta?.content || '';
          if (delta) { _paperQAHistory[_paperQAHistory.length - 1].content += delta; _renderPaperQA(); }
        } catch (_) {}
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') {
      _paperQAHistory[_paperQAHistory.length - 1].content += '\n\n⚠️ Error: ' + e.message;
      _renderPaperQA();
    }
  } finally {
    _paperQAStreaming = false; _paperQAAbort = null; _saveActivePaperState();
  }
}

function _quotePaperSelection() {
  var sel = window.getSelection();
  var text = sel?.toString()?.trim();
  if (!text) return;
  var input = document.getElementById('paperQAInput');
  if (!input) return;
  if (_paperActiveTab !== 'qa') _switchPaperTab('qa');
  input.value = '> ' + text.replace(/\n/g, '\n> ') + '\n\n' + input.value;
  input.focus();
  sel.removeAllRanges();
  _hidePaperQuoteBar();
}

/** Ask about selected text — quote it and auto-send a question */
function _askAboutPaperSelection() {
  var sel = window.getSelection();
  var text = sel?.toString()?.trim();
  if (!text) return;
  var input = document.getElementById('paperQAInput');
  if (!input) return;
  if (_paperActiveTab !== 'qa') _switchPaperTab('qa');
  input.value = '> ' + text.replace(/\n/g, '\n> ') + '\n\nExplain this part of the paper.';
  sel.removeAllRanges();
  _hidePaperQuoteBar();
  // Auto-send after a brief delay for tab switch to settle
  setTimeout(function() { _sendPaperQuestion(); }, 100);
}

function _hidePaperQuoteBar() {
  var q = document.getElementById('paperQuoteBtn');
  if (q) q.style.display = 'none';
}

function _handlePaperTextSelection() {
  var sel = window.getSelection();
  var text = sel?.toString()?.trim();
  var q = document.getElementById('paperQuoteBtn');
  if (!q) return;
  if (!text || text.length < 3) { q.style.display = 'none'; return; }

  var viewer = document.getElementById('paperPdfViewer');
  if (!viewer || !viewer.contains(sel.anchorNode)) { q.style.display = 'none'; return; }

  var range = sel.getRangeAt(0);
  var rect = range.getBoundingClientRect();
  var leftEl = document.querySelector('.paper-left');
  if (!leftEl) { q.style.display = 'none'; return; }
  var lr = leftEl.getBoundingClientRect();
  q.style.display = 'flex';
  q.style.top = (rect.top - lr.top - 40) + 'px';
  q.style.left = Math.max(4, rect.left - lr.left + rect.width / 2 - 80) + 'px';
}

// ══════════════════════════════════════════════════════
//  ★ Tab 2: Report — server-owned background task + polling
// ══════════════════════════════════════════════════════
//
// ARCHITECTURE (2026-04-18 rewrite)
//   • Reports are generated EXACTLY ONCE on the server per (paper_hash, lang).
//     On completion the enriched report is persisted to `paper_reports`.
//   • The frontend is purely a progress renderer. It never owns report state.
//   • Flow:
//       POST /api/paper/report/start  → {task_id} (or {cached, report} if DB hit)
//       GET  /api/paper/report/poll?task_id=X&cursor=N → {events, next_cursor, …}
//   • On tab/mode switch, we simply pause the poll timer. On return, we
//     lookup the task by paper_hash via /api/paper/report/lookup and resume
//     polling from cursor=0, replaying all events → UI rebuilt from events.
//   • Tool-round events (tool_start / tool_done) use the same schema as
//     chat tool events, so `renderToolRoundsHTML(toolRounds)` from ui.js
//     renders them identically to how they look in the chat bubble.

/** Reset report state (called when switching paper / force regen). */
function _resetReportLocalState() {
  if (_paperReportStream && _paperReportStream.pollTimer) {
    clearTimeout(_paperReportStream.pollTimer);
  }
  _paperReportStream = null;
}

function _makeReportStreamState(paperId, lang, taskId) {
  return {
    paperId: paperId || '',
    lang: lang || 'en',
    taskId: taskId || '',
    cursor: 0,
    status: 'running',
    fullText: '',
    thinkingText: '',
    toolRounds: [],      // chat-compatible: [{roundNum, toolName, query, toolCallId, toolArgs, status, toolContent, _elapsed}]
    contentStarted: false,
    error: '',
    pollTimer: null,
    pollBusy: false,
  };
}

/** Skeleton DOM that gets populated by event application. */
function _renderReportSkeleton(container, lang) {
  container.innerHTML =
    '<div class="paper-report-tools" id="reportToolZone"></div>' +
    '<details class="paper-report-thinking" id="reportThinkingBlock" open style="display:none">' +
      '<summary><span class="thinking-dot"></span>' +
        (lang === 'zh' ? '思考中…' : 'Thinking…') +
      '</summary>' +
      '<div class="paper-report-thinking-body" id="reportThinkingBody"></div>' +
    '</details>' +
    '<div class="paper-report-body" id="reportBodyContent">' +
      '<div class="paper-loading"><div class="paper-loading-spinner"></div><div>' +
        (lang === 'zh' ? '正在生成报告…' : 'Generating report…') +
      '</div></div>' +
    '</div>';
}

/** Apply a single event to the in-memory stream state. Returns dirty flag. */
function _applyReportEvent(s, ev) {
  switch (ev.type) {
    case 'status':
      s.status = ev.status || s.status;
      return true;

    case 'thinking':
      s.thinkingText += (ev.delta || '');
      return true;

    case 'tool_start': {
      // Chat-compatible round entry
      s.toolRounds.push({
        roundNum: ev.roundNum,
        toolName: ev.toolName,
        query: ev.query || ev.toolName,
        toolCallId: ev.toolCallId || '',
        toolArgs: ev.toolArgs || '',
        status: 'searching',
        results: null,
      });
      return true;
    }

    case 'tool_done': {
      var r = null;
      for (var i = 0; i < s.toolRounds.length; i++) {
        if (s.toolRounds[i].roundNum === ev.roundNum) { r = s.toolRounds[i]; break; }
      }
      if (r) {
        r.status = 'done';
        if (typeof ev.elapsed === 'number') r._elapsed = ev.elapsed.toFixed(1) + 's';
        if (ev.toolContent) r.toolContent = ev.toolContent;
      }
      return true;
    }

    case 'delta':
      s.fullText += (ev.delta || '');
      s.contentStarted = true;
      return true;

    case 'enriched':
      s.fullText = ev.text || s.fullText;
      if (ev.paperHash) _paperHash = ev.paperHash;
      return true;

    case 'done':
      s.status = 'done';
      if (ev.report) {
        s.fullText = ev.report;
        _paperReportCache = ev.report;
      }
      if (ev.paperHash) _paperHash = ev.paperHash;
      return true;

    case 'error':
      s.status = 'error';
      s.error = ev.error || 'Unknown error';
      return true;
  }
  return false;
}

/** Paint the Report tab DOM from the current stream state. */
function _paintReportFromState() {
  var container = document.getElementById('paperReportContent');
  if (!container || !_paperReportStream) return;
  var s = _paperReportStream;

  // Terminal: done → just render final text.
  if (s.status === 'done' && s.fullText && !s.toolRounds.some(r => r.status === 'searching')) {
    container.innerHTML = typeof renderMarkdown === 'function' ? renderMarkdown(s.fullText) : '<pre>' + escapeHtml(s.fullText) + '</pre>';
    return;
  }

  // Ensure skeleton exists
  if (!document.getElementById('reportToolZone')) {
    _renderReportSkeleton(container, s.lang);
  }

  // Tool rounds — reuse chat's unified renderer for identical look & feel
  var toolZone = document.getElementById('reportToolZone');
  if (toolZone) {
    if (s.toolRounds.length > 0 && typeof renderToolRoundsHTML === 'function') {
      toolZone.innerHTML = renderToolRoundsHTML(s.toolRounds, s.status === 'running');
    } else {
      toolZone.innerHTML = '';
    }
  }

  // Thinking
  if (s.thinkingText) {
    var thBlock = document.getElementById('reportThinkingBlock');
    var thBody = document.getElementById('reportThinkingBody');
    if (thBlock) {
      thBlock.style.display = '';
      if (s.contentStarted) thBlock.open = false;
    }
    if (thBody) {
      thBody.textContent = s.thinkingText;
      thBody.scrollTop = thBody.scrollHeight;
    }
  }

  // Report body
  var bodyEl = document.getElementById('reportBodyContent');
  if (bodyEl) {
    if (s.contentStarted) {
      bodyEl.innerHTML = typeof renderMarkdown === 'function' ? renderMarkdown(s.fullText) : '<pre>' + escapeHtml(s.fullText) + '</pre>';
    } else if (s.status === 'error' && !s.fullText) {
      bodyEl.innerHTML = '<div class="paper-error">' + escapeHtml(s.error || 'Failed') +
        '<br><button onclick="_generatePaperReport()" class="paper-retry-btn">Retry</button></div>';
    }
    // Otherwise keep the loading spinner from the skeleton
  }
}

/** Poll /api/paper/report/poll once; schedule next if still running. */
async function _pollReportTask() {
  var s = _paperReportStream;
  if (!s || !s.taskId) return;
  if (s.pollBusy) return;
  s.pollBusy = true;
  try {
    var resp = await fetch(apiUrl('/api/paper/report/poll?task_id=' +
      encodeURIComponent(s.taskId) + '&cursor=' + s.cursor));
    if (!resp.ok) {
      if (resp.status === 404) {
        // Task expired or server restarted
        s.status = 'error';
        s.error = 'Task no longer available on server. Please regenerate.';
        _paintReportFromState();
        return;
      }
      throw new Error('HTTP ' + resp.status);
    }
    var data = await resp.json();
    if (!data.ok) {
      s.status = 'error';
      s.error = data.error || 'Poll failed';
      _paintReportFromState();
      return;
    }

    // Apply new events
    var events = data.events || [];
    for (var i = 0; i < events.length; i++) {
      _applyReportEvent(s, events[i]);
    }
    s.cursor = data.next_cursor;

    // Update status from server authoritative status
    if (data.status === 'done') {
      s.status = 'done';
      if (data.report) {
        s.fullText = data.report;
        _paperReportCache = data.report;
      }
      _saveActivePaperState();
    } else if (data.status === 'error') {
      s.status = 'error';
      s.error = data.error || s.error;
    }

    // Only repaint DOM when the user is actually on this paper (and Report tab)
    if (s.paperId === _activePaperId) {
      _paintReportFromState();
    }

    // Schedule next poll if still running
    if (s.status === 'running') {
      s.pollTimer = setTimeout(_pollReportTask, 1200);
    }
  } catch (e) {
    console.warn('[Paper:Report] Poll failed:', e);
    // Transient network error — retry with backoff
    if (s && s.status === 'running') {
      s.pollTimer = setTimeout(_pollReportTask, 3000);
    }
  } finally {
    s.pollBusy = false;
  }
}

/** Start (or join) a server-side report task, begin polling. */
async function _generatePaperReport(force) {
  var container = document.getElementById('paperReportContent');
  if (!container) return;

  // Already polling a live task for this paper and not forcing → just paint
  if (!force && _paperReportStream
      && _paperReportStream.paperId === _activePaperId
      && _paperReportStream.status === 'running') {
    _paintReportFromState();
    return;
  }

  // In-memory cache — instant path
  if (_paperReportCache && !force) {
    container.innerHTML = typeof renderMarkdown === 'function' ? renderMarkdown(_paperReportCache) : '<pre>' + escapeHtml(_paperReportCache) + '</pre>';
    return;
  }

  if (!_paperParsedText) {
    container.innerHTML =
      '<div class="paper-loading"><div class="paper-loading-spinner"></div>' +
      '<div>Recovering paper text…</div></div>';
    var ok = await _ensurePaperText();
    if (!ok) {
      container.innerHTML =
        '<div class="paper-report-empty"><p>No paper text available.</p>' +
        '<p style="opacity:0.6;font-size:12px;margin-top:6px">The PDF may be scanned/image-only, or parsing failed. Try re-uploading.</p></div>';
      return;
    }
  }

  var reportLang = (typeof _i18nLang !== 'undefined' && _i18nLang === 'zh') ? 'zh' : 'en';
  if (!_paperReportModel) _populatePaperReportModelDropdown();
  var reportModel = _paperReportModel || null;

  // Discard any prior stream state (force path or new paper path)
  if (force || (_paperReportStream && _paperReportStream.paperId !== _activePaperId)) {
    _resetReportLocalState();
  }

  // Make sure image metadata is fresh
  if ((!_paperImages || _paperImages.length === 0) && _paperPdfFilename) {
    try { await _extractPaperImages(); } catch (_) {}
  }

  _renderReportSkeleton(container, reportLang);

  try {
    var resp = await fetch(apiUrl('/api/paper/report/start'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        paper_text: _paperParsedText,
        lang: reportLang,
        model: reportModel,
        force: !!force,
        images: _paperImages || [],
      }),
    });
    var data = await resp.json();
    if (!data.ok) throw new Error(data.error || 'Start failed');

    // DB cache hit — done in one round-trip
    if (data.cached && data.report) {
      _paperReportCache = data.report;
      if (data.paper_hash) _paperHash = data.paper_hash;
      _saveActivePaperState();
      container.innerHTML = typeof renderMarkdown === 'function' ? renderMarkdown(data.report) : '<pre>' + escapeHtml(data.report) + '</pre>';
      return;
    }

    // Task started (or joined) — begin polling from cursor 0 so we replay all
    if (data.paper_hash) _paperHash = data.paper_hash;
    _paperReportStream = _makeReportStreamState(_activePaperId, reportLang, data.task_id);
    _pollReportTask();

  } catch (e) {
    console.warn('[Paper:Report] start failed:', e);
    container.innerHTML = '<div class="paper-error">Failed: ' + escapeHtml(e.message) +
      '<br><button onclick="_generatePaperReport()" class="paper-retry-btn">Retry</button></div>';
  }
}

/** Called when the user opens the Report tab. Priority:
 *   1. Have stream state for active paper → paint + resume poll if running.
 *   2. Look up server-side running task by paper_hash → attach + poll.
 *   3. Try DB cache lookup.
 *   4. Start a new task.
 */
async function _loadOrGenerateReport() {
  var reportLang = (typeof _i18nLang !== 'undefined' && _i18nLang === 'zh') ? 'zh' : 'en';

  // (1) Existing local stream state for this paper
  if (_paperReportStream && _paperReportStream.paperId === _activePaperId) {
    _paintReportFromState();
    if (_paperReportStream.status === 'running' && !_paperReportStream.pollTimer) {
      _pollReportTask();
    }
    return;
  }

  // (2) Server-side task lookup (survives chat-mode round-trips)
  if (_paperHash) {
    try {
      var lookupResp = await fetch(apiUrl('/api/paper/report/lookup'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paper_hash: _paperHash, lang: reportLang }),
      });
      var lookupData = await lookupResp.json();
      if (lookupData.ok && lookupData.task_id
          && (lookupData.status === 'running' || lookupData.status === 'pending')) {
        // Attach to the running server-side task
        var container = document.getElementById('paperReportContent');
        if (container) _renderReportSkeleton(container, reportLang);
        _paperReportStream = _makeReportStreamState(_activePaperId, reportLang, lookupData.task_id);
        _pollReportTask();
        return;
      }
    } catch (e) {
      console.warn('[Paper:Report] lookup failed (non-fatal):', e);
    }
  }

  // (3) Try server DB cache by hash (avoids re-sending text)
  try {
    var cacheBody = { lang: reportLang, images: _paperImages || [] };
    if (_paperHash) cacheBody.paper_hash = _paperHash;
    else cacheBody.paper_text = _paperParsedText;
    var cacheResp = await fetch(apiUrl('/api/paper/report/cache'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cacheBody),
    });
    var cacheData = await cacheResp.json();
    if (cacheData.ok && cacheData.report) {
      _paperReportCache = cacheData.report;
      if (cacheData.paper_hash) _paperHash = cacheData.paper_hash;
      _saveActivePaperState();
      var c2 = document.getElementById('paperReportContent');
      if (c2) {
        c2.innerHTML = typeof renderMarkdown === 'function' ? renderMarkdown(cacheData.report) : '<pre>' + escapeHtml(cacheData.report) + '</pre>';
      }
      return;
    }
  } catch (e) {
    console.warn('[Paper:Report] Cache lookup failed:', e);
  }

  // (4) No cache, no running task — start a new one
  _generatePaperReport();
}


// ── Report Model Picker ──

/** Populate the report model dropdown from _registeredModels (populated by main.js) */
function _populatePaperReportModelDropdown() {
  var dropdown = document.getElementById('paperReportModelDropdown');
  if (!dropdown) return;
  var models = (typeof _registeredModels !== 'undefined') ? _registeredModels : [];
  var hiddenSet = (typeof _hiddenModels !== 'undefined') ? _hiddenModels : new Set();

  dropdown.innerHTML = '';

  // Filter to chat-capable visible models
  var chatModels = models.filter(function(m) {
    if (hiddenSet.has(m.model_id)) return false;
    var caps = m.capabilities || [];
    for (var i = 0; i < caps.length; i++) {
      if (caps[i] === 'image_gen' || caps[i] === 'embedding') return false;
    }
    return true;
  });

  // No "Default (auto)" option — the report should always use a specific,
  // user-visible model. Auto-select the first visible chat model if nothing
  // has been chosen yet, so the "Default" label is never ambiguous.
  if (!_paperReportModel && chatModels.length > 0) {
    _selectPaperReportModel(chatModels[0].model_id);
  }

  // Group by provider
  var grouped = {};
  for (var i = 0; i < chatModels.length; i++) {
    var m = chatModels[i];
    var pid = m.provider_id || 'default';
    if (!grouped[pid]) grouped[pid] = { name: m.provider_name || pid, models: [] };
    grouped[pid].models.push(m);
  }

  var pids = Object.keys(grouped);
  for (var pi = 0; pi < pids.length; pi++) {
    var group = grouped[pids[pi]];
    if (pids.length > 1) {
      var section = document.createElement('div');
      section.className = 'paper-report-model-dropdown-section';
      section.textContent = group.name;
      dropdown.appendChild(section);
    }
    for (var mi = 0; mi < group.models.length; mi++) {
      var mod = group.models[mi];
      var item = document.createElement('div');
      item.className = 'paper-report-model-dropdown-item' + (mod.model_id === _paperReportModel ? ' active' : '');
      var shortName = (typeof _modelShortName === 'function') ? _modelShortName(mod.model_id) : mod.model_id;
      item.textContent = shortName;
      item.title = mod.model_id;
      (function(mid) {
        item.onclick = function() { _selectPaperReportModel(mid); };
      })(mod.model_id);
      dropdown.appendChild(item);
    }
  }
}

function _selectPaperReportModel(modelId) {
  _paperReportModel = modelId || '';
  // Update label — always show the actual model, never "Default"
  var label = document.getElementById('paperReportModelLabel');
  if (label) {
    if (modelId) {
      label.textContent = (typeof _modelShortName === 'function') ? _modelShortName(modelId) : modelId;
    } else {
      // No model available (empty model list) — keep the button usable.
      label.textContent = 'Select model';
    }
  }
  // Close dropdown
  var dropdown = document.getElementById('paperReportModelDropdown');
  if (dropdown) dropdown.classList.remove('open');
  // Update active state
  var items = dropdown ? dropdown.querySelectorAll('.paper-report-model-dropdown-item') : [];
  items.forEach(function(it) { it.classList.toggle('active', it.title === modelId); });
}

function _togglePaperReportModelDropdown(e) {
  e.stopPropagation();
  var dropdown = document.getElementById('paperReportModelDropdown');
  if (!dropdown) return;
  var isOpen = dropdown.classList.contains('open');
  if (!isOpen) _populatePaperReportModelDropdown();
  dropdown.classList.toggle('open');
}

// Close model dropdown on outside click
document.addEventListener('click', function() {
  var dropdown = document.getElementById('paperReportModelDropdown');
  if (dropdown) dropdown.classList.remove('open');
});


function _regeneratePaperReport() {
  // Abort any running server task, then start fresh with force=true so
  // the backend bypasses the DB cache too.
  var prevTaskId = _paperReportStream && _paperReportStream.taskId;
  _resetReportLocalState();
  _paperReportCache = '';
  if (prevTaskId) {
    fetch(apiUrl('/api/paper/report/abort'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: prevTaskId }),
    }).catch(function(e) { console.warn('[Paper:Report] abort request failed:', e); });
  }
  _generatePaperReport(true);
}

function _copyPaperReport() {
  if (!_paperReportCache) return;
  navigator.clipboard.writeText(_paperReportCache).then(function() { debugLog('Copied', 'success'); });
}

function _exportPaperReport() {
  if (!_paperReportCache) return;
  var a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([_paperReportCache], { type: 'text/markdown' }));
  a.download = 'paper_report_' + (_paperFileName || 'paper').replace(/[^\w]/g, '_') + '.md';
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
}

// ══════════════════════════════════════════════════════
//  ★ Tab 3: Babel PDF (Translation)
// ══════════════════════════════════════════════════════

var _babelTargetLang = '';
var _babelTranslatedPages = {};
var _babelTranslating = false;

function _initBabelPdfTab() {
  var container = document.getElementById('paperTranslateContent');
  if (!container) return;
  container.innerHTML =
    '<div class="babel-pdf-module">' +
      '<div class="babel-pdf-brand">' +
        '<svg class="babel-pdf-icon" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' +
          '<path d="M5 8l6 6"/><path d="M4 14l6-6 2-3"/><path d="M2 5h12"/><path d="M7 2v3"/>' +
          '<path d="M22 22l-5-10-5 10"/><path d="M14 18h6"/>' +
        '</svg>' +
        '<div class="babel-pdf-brand-text"><span class="babel-pdf-title">Babel PDF</span><span class="babel-pdf-subtitle">Academic paper translation</span></div>' +
      '</div>' +
      '<div class="babel-pdf-lang-bar">' +
        '<button class="babel-pdf-lang' + (!_babelTargetLang ? ' active' : '') + '" data-lang="" onclick="_switchBabelLang(\'\', this)">Original</button>' +
        '<button class="babel-pdf-lang' + (_babelTargetLang === 'zh' ? ' active' : '') + '" data-lang="zh" onclick="_switchBabelLang(\'zh\', this)">中文</button>' +
        '<button class="babel-pdf-lang' + (_babelTargetLang === 'en' ? ' active' : '') + '" data-lang="en" onclick="_switchBabelLang(\'en\', this)">English</button>' +
        '<button class="babel-pdf-lang' + (_babelTargetLang === 'ja' ? ' active' : '') + '" data-lang="ja" onclick="_switchBabelLang(\'ja\', this)">日本語</button>' +
      '</div>' +
      '<div class="babel-pdf-body" id="babelPdfBody"></div>' +
      '<div class="babel-pdf-status" id="babelPdfStatus"></div>' +
    '</div>';

  // Render cached result or empty state
  if (_babelTargetLang && _babelTranslatedPages[_babelTargetLang]) {
    _renderBabelResult(_babelTranslatedPages[_babelTargetLang]);
  } else if (_babelTargetLang && _paperParsedText) {
    _startBabelTranslation();
  } else {
    var body = document.getElementById('babelPdfBody');
    if (body) {
      body.innerHTML =
        '<div class="babel-pdf-empty">' +
          '<svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.4"><path d="M5 8l6 6"/><path d="M4 14l6-6 2-3"/><path d="M2 5h12"/><path d="M7 2v3"/><path d="M22 22l-5-10-5 10"/><path d="M14 18h6"/></svg>' +
          '<p>Select a target language to translate the paper</p>' +
          '<p class="babel-pdf-hint">Translation runs section by section via LLM</p>' +
        '</div>';
    }
  }
}

function _switchBabelLang(lang, btn) {
  document.querySelectorAll('.babel-pdf-lang').forEach(function(b) { b.classList.remove('active'); });
  if (btn) btn.classList.add('active');
  _babelTargetLang = lang;
  _startBabelTranslation();
}

function _startBabelTranslation() {
  var body = document.getElementById('babelPdfBody');
  var status = document.getElementById('babelPdfStatus');
  if (!body) return;

  if (!_babelTargetLang) {
    body.innerHTML = '<div class="babel-pdf-empty"><svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.4"><path d="M5 8l6 6"/><path d="M4 14l6-6 2-3"/><path d="M2 5h12"/><path d="M7 2v3"/><path d="M22 22l-5-10-5 10"/><path d="M14 18h6"/></svg><p>Select a target language to translate</p><p class="babel-pdf-hint">Translation runs section by section via LLM</p></div>';
    if (status) status.textContent = '';
    return;
  }

  if (!_paperParsedText) {
    body.innerHTML = '<div class="babel-pdf-empty"><p>No paper loaded. Upload a PDF first.</p></div>';
    return;
  }

  // Check cache
  if (_babelTranslatedPages[_babelTargetLang]) {
    _renderBabelResult(_babelTranslatedPages[_babelTargetLang]);
    if (status) status.textContent = 'Translation complete (cached)';
    return;
  }

  var langNames = { zh: '中文', en: 'English', ja: '日本語' };
  if (status) status.textContent = 'Translating to ' + (langNames[_babelTargetLang] || _babelTargetLang) + '…';

  body.innerHTML = '<div class="paper-loading"><div class="paper-loading-spinner"></div><div>Translating to ' + (langNames[_babelTargetLang] || _babelTargetLang) + '…</div><div class="babel-pdf-progress"><div class="babel-pdf-progress-bar" id="babelProgressBar" style="width:0%"></div></div></div>';

  _babelTranslateAllPages(_babelTargetLang);
}

async function _babelTranslateAllPages(lang) {
  if (_babelTranslating) return;
  _babelTranslating = true;

  var chunkSize = 2000;
  var text = _paperParsedText;
  var chunks = [];
  for (var i = 0; i < text.length; i += chunkSize) {
    chunks.push(text.slice(i, i + chunkSize));
  }

  var langNames = { zh: 'Chinese', en: 'English', ja: 'Japanese' };
  var translated = [];
  var bar = document.getElementById('babelProgressBar');

  for (var ci = 0; ci < chunks.length; ci++) {
    if (_babelTargetLang !== lang) break;

    try {
      var resp = await fetch(apiUrl('/api/paper/chat'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages: [
            { role: 'system', content: 'You are a professional academic translator. Translate the following text to ' + (langNames[lang] || lang) + '. Preserve all formatting, equations, and technical terms. Output ONLY the translation.' },
            { role: 'user', content: chunks[ci] }
          ],
          stream: false
        }),
      });

      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      var buf = '';
      var chunkResult = '';
      while (true) {
        var rd = await reader.read();
        if (rd.done) break;
        buf += decoder.decode(rd.value, { stream: true });
        var sseLines = buf.split('\n'); buf = sseLines.pop();
        for (var sl = 0; sl < sseLines.length; sl++) {
          if (!sseLines[sl].startsWith('data: ')) continue;
          var sd = sseLines[sl].slice(6).trim();
          if (sd === '[DONE]') continue;
          try { chunkResult += JSON.parse(sd).choices?.[0]?.delta?.content || ''; } catch (_) {}
        }
      }
      translated.push(chunkResult);
    } catch (e) {
      console.warn('[Babel] Chunk', ci, 'failed:', e);
      translated.push('[Translation error for this section]');
    }

    var pct = Math.round(((ci + 1) / chunks.length) * 100);
    if (bar) bar.style.width = pct + '%';
    var statusEl = document.getElementById('babelPdfStatus');
    if (statusEl) statusEl.textContent = 'Translated ' + (ci + 1) + '/' + chunks.length + ' sections';
  }

  _babelTranslating = false;

  if (_babelTargetLang === lang) {
    _babelTranslatedPages[lang] = translated.join('\n\n');
    _renderBabelResult(_babelTranslatedPages[lang]);
    _saveActivePaperState();
    var statusEl2 = document.getElementById('babelPdfStatus');
    if (statusEl2) statusEl2.textContent = 'Translation complete (' + chunks.length + ' sections)';
  }
}

function _renderBabelResult(text) {
  var body = document.getElementById('babelPdfBody');
  if (!body) return;
  body.innerHTML = typeof renderMarkdown === 'function' ? renderMarkdown(text) : '<pre style="white-space:pre-wrap;font-size:13px;line-height:1.7">' + escapeHtml(text) + '</pre>';
}

// ══════════════════════════════════════════════════════
//  ★ Keyboard Shortcuts
// ══════════════════════════════════════════════════════

function _handlePaperKeyDown(e) {
  if (!paperMode) return;
  if (e.key === 'Escape') { e.preventDefault(); exitPaperMode(); return; }
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') {
    if (e.key === 'Enter' && !e.shiftKey && e.target.id === 'paperQAInput') { e.preventDefault(); _sendPaperQuestion(); }
    return;
  }
  if (e.key === '+' || e.key === '=') { paperZoomIn(); e.preventDefault(); }
  if (e.key === '-') { paperZoomOut(); e.preventDefault(); }
  if (e.key === '0') { paperFitWidth(); e.preventDefault(); }
}

// ══════════════════════════════════════════════════════
//  ★ Init
// ══════════════════════════════════════════════════════

document.addEventListener('keydown', _handlePaperKeyDown);
document.addEventListener('mouseup', function() { if (paperMode) setTimeout(_handlePaperTextSelection, 10); });

document.addEventListener('DOMContentLoaded', function() {
  _loadPaperLibrary();

  // Drag-and-drop on PDF viewer + entire paper mode container + sidebar overlay
  function _addPaperDropZone(el) {
    if (!el) return;
    el.addEventListener('dragover', function(e) {
      if (paperMode && e.dataTransfer && e.dataTransfer.types.includes('Files')) {
        e.preventDefault(); e.stopPropagation();
        el.classList.add('paper-drag-over');
      }
    });
    el.addEventListener('dragleave', function(e) {
      // Only remove if leaving the element itself (not entering a child)
      if (e.relatedTarget && el.contains(e.relatedTarget)) return;
      el.classList.remove('paper-drag-over');
    });
    el.addEventListener('drop', async function(e) {
      e.preventDefault(); e.stopPropagation();
      el.classList.remove('paper-drag-over');
      if (!paperMode) return;
      var files = Array.from(e.dataTransfer?.files || []);
      for (var fi = 0; fi < files.length; fi++) {
        if (files[fi].type === 'application/pdf' || files[fi].name.toLowerCase().endsWith('.pdf')) {
          await _handlePaperFileDrop(files[fi]);
          break;
        }
      }
    });
  }

  _addPaperDropZone(document.getElementById('paperPdfViewer'));
  _addPaperDropZone(document.getElementById('paperModeContainer'));
  _addPaperDropZone(document.getElementById('paperSidebarOverlay'));

  // Ctrl+scroll zoom on PDF viewer
  var pdfViewer = document.getElementById('paperPdfViewer');
  if (pdfViewer) {
    pdfViewer.addEventListener('wheel', function(e) {
      if (!paperMode || !e.ctrlKey) return;
      e.preventDefault();
      var delta = e.deltaY > 0 ? -0.1 : 0.1;
      _paperScale = Math.max(0.25, Math.min(4.0, _paperScale + delta));
      _syncZoomUI();
      clearTimeout(_paperZoomDebounce);
      _paperZoomDebounce = setTimeout(function() { _renderAllPages(); }, 150);
    }, { passive: false });
  }
});
