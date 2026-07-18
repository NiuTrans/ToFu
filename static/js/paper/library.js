/* ════════════════════════════════════
   paper/library.js — Paper Library (bookshelf) layer
   Extracted from paper-reader.js (2026-07). Owns the client-side library
   cache + its persistence/migration/CRUD/render: _paperLibrary state,
   _persistPaperEntry, _loadPaperLibrary, _createPaperEntry, _deletePaperEntry,
   _openPaperEntry, _renderPaperLibrary, etc. Plain window-scope concatenation
   (NOT an IIFE) — shares state with paper-reader.js at runtime; all cross-refs
   are inside function bodies so load order among the paper files is free
   (all load before main.js). Companion to the existing paper/ subpackage
   (arxiv.js, pdf_viewer.js, qa.js, report.js).
   ════════════════════════════════════ */

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
var _paperLibraryLoading = false; // True while the initial server fetch is in flight
var _activePaperId = '';         // Currently viewed paper ID
var _PAPER_ACTIVE_KEY = 'paper_active_id';
var _PAPER_LEGACY_LIB_KEY = 'paper_library';  // pre-migration localStorage
var _PAPER_MIGRATED_FLAG = 'paper_library_migrated_v1';

/** Upsert this entry to the server. Per-paper PUT so one save can't
 *  clobber a concurrent save of another paper. Best-effort — failures
 *  are logged but don't block the UI. */
/** Persist client-owned mutable state for a paper to the server.
 *  parsedText / images / paperHash / pdfFilename are server-derived and
 *  ONLY sent on the first save (when ``_first`` is true) — afterwards
 *  the server preserves whatever it already has, so we don't keep
 *  re-uploading the parsed PDF text on every save.
 */
function _persistPaperEntry(entry, _first) {
  if (!entry || !entry.id) return Promise.resolve();
  var body = {
    title: entry.title || '',
    qaHistory: (entry.qaHistory || []).slice(-50),
    babelCache: entry.babelCache || {},
    pageCount: entry.pageCount || 0,
    createdAt: entry.createdAt || Date.now(),
  };
  if (_first) {
    body.pdfUrl = entry.pdfUrl || '';
    body.pdfFilename = entry.pdfFilename || '';
    body.arxivId = entry.arxivId || '';
    body.paperHash = entry.paperHash || '';
    body.parsedText = (entry.parsedText || '').slice(0, 200000);
    body.images = Array.isArray(entry.images) ? entry.images.slice(0, 60) : [];
  }
  return Api.paper.libraryUpsert(entry.id, body)
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
    try { await _persistPaperEntry(legacy[i], true); }
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
    var data = await Api.paper.libraryList();
    if (data && data.ok && Array.isArray(data.papers)) {
      _paperLibrary = data.papers;
      // Loaded from server → row already exists, subsequent saves are
      // small-payload incremental updates (no parsed_text re-upload).
      for (var pi = 0; pi < _paperLibrary.length; pi++) _paperLibrary[pi]._persisted = true;
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

/** Generate a fresh bookshelf-entry id. Exposed so ingestion flows can mint
 *  the id BEFORE the network round-trip and hand it to the server, so the
 *  server-persisted row and the client entry share one id. */
function _newPaperEntryId() {
  return 'paper_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8);
}

/** Normalize an arXiv id for dedup: lowercase, trim, strip a trailing version
 *  suffix (2502.09992v3 → 2502.09992). Mirrors recommend_engine._norm_id so
 *  the client dedup key matches the id the engine grounds against. */
function _normArxivId(id) {
  var s = (id == null ? '' : String(id)).trim().toLowerCase();
  if (!s) return '';
  return s.split('v')[0].trim();
}

/** A "recommended, not-yet-read" entry: grounded (has an arxivId) but never
 *  ingested (no PDF, no parsed text). This is the lightweight state a saved
 *  recommendation lives in until the user opens it (lazy ingest fills it). */
function _isRecommendedEntry(entry) {
  return !!(entry && entry.arxivId && !entry.pdfUrl && !entry.parsedText);
}

/** Find an existing library entry whose normalized arxivId matches, else null.
 *  Dedup is whole-library (a card already present as EITHER a lightweight OR a
 *  fully-read paper must not spawn a second row). */
function _findLibraryEntryByArxiv(arxivId) {
  var key = _normArxivId(arxivId);
  if (!key) return null;
  for (var i = 0; i < _paperLibrary.length; i++) {
    if (_normArxivId(_paperLibrary[i].arxivId) === key) return _paperLibrary[i];
  }
  return null;
}

/**
 * Auto-persist a grounded recommend card as a lightweight bookshelf entry so
 * it survives a tab-close and the user can come back for it (the reported
 * "otherwise they'd be lost" objective). Discipline:
 *   - Only cards with a non-null arxiv_id are saved — a card without one can
 *     neither be lazily re-opened nor deduped, so it would be a dead row.
 *   - Deduped whole-library by normalized arxivId; an existing row (lightweight
 *     OR already-read) is left untouched — never downgrade a read paper back to
 *     "recommended".
 *   - Does NOT steal the active-paper pointer (unlike _createPaperEntry): a
 *     background save must not change what the viewer is showing.
 */
function _persistRecommendedCard(card) {
  if (!card || !card.arxiv_id) return null;   // ungrounded → skip (edge case #1)
  if (_findLibraryEntryByArxiv(card.arxiv_id)) return null;   // dedup (edge case #2)
  var entry = {
    id: _newPaperEntryId(),
    title: (card.title || ('arXiv:' + card.arxiv_id)),
    pdfUrl: '',
    pdfFilename: '',
    arxivId: card.arxiv_id,
    parsedText: '',
    qaHistory: [],
    paperHash: '',
    images: [],
    babelCache: {},
    createdAt: Date.now(),
    pageCount: 0,
    recommendWhy: card.why || '',
    _persisted: true,   // the PUT below seeds the row
  };
  _paperLibrary.unshift(entry);
  _renderPaperLibrary();
  // First (and only) persist: ship arxivId so the row is a real, reloadable
  // recommendation. Empty pdf/parsedText mark it lightweight; the backend
  // ghost-reaper keeps an empty-PDF row that carries an arxivId.
  _persistPaperEntry(entry, true);
  return entry;
}

function _createPaperEntry(title, pdfUrl, parsedText, arxivId, explicitId) {
  // Upgrade-in-place: if the id already exists (e.g. a lightweight recommended
  // row being lazily ingested), fill its server-derived fields into the SAME
  // row instead of minting a duplicate. This is what makes clicking a saved
  // recommendation reuse its row rather than fork a second one.
  if (explicitId) {
    var existing = null;
    for (var i = 0; i < _paperLibrary.length; i++) {
      if (_paperLibrary[i].id === explicitId) { existing = _paperLibrary[i]; break; }
    }
    if (existing) {
      existing.title = title || existing.title || 'Untitled Paper';
      existing.pdfUrl = pdfUrl || '';
      existing.parsedText = parsedText || '';
      if (arxivId) existing.arxivId = arxivId;
      existing._persisted = false;   // force a full re-persist of the heavy cols
      _setActivePaperId(existing.id);
      return existing;
    }
  }
  var entry = {
    id: explicitId || _newPaperEntryId(),
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
    _persisted: false,
  };
  _paperLibrary.unshift(entry);
  _setActivePaperId(entry.id);
  // Don't seed the row yet — parsed_text / images come from the upload
  // response. _saveActivePaperState() will do the first full persist.
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
  if (!entry) return Promise.resolve();
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
  // First save: include parsedText / images / paperHash / pdfFilename so
  // the row gets seeded. Subsequent saves only ship the small mutable
  // fields (qaHistory, babelCache, pageCount, title) — server preserves
  // the heavy columns.
  var first = !entry._persisted;
  entry._persisted = true;
  return _persistPaperEntry(entry, first);
}

function _deletePaperEntry(id) {
  _paperLibrary = _paperLibrary.filter(function(p) { return p.id !== id; });
  if (_activePaperId === id) {
    _setActivePaperId(_paperLibrary.length > 0 ? _paperLibrary[0].id : '');
  }
  Api.paper.libraryDelete(id)
    .catch(function(e) { console.warn('[Paper:Library] Delete failed:', e); });
  _renderPaperLibrary();

  // If we deleted the active paper, load the next one or show landing
  if (paperMode) {
    var next = _getActivePaperEntry();
    if (next) {
      _openPaperEntry(next);
    } else {
      _resetAllReportViews();
      _paperPdfUrl = '';
      _paperPdfFilename = '';
      _paperFileName = '';
      _paperParsedText = '';
      _paperQAHistory = [];
      _paperReportCache = '';
      _paperReviewCache = '';
      _paperReviewVenue = '';
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
  _resetAllReportViews();

  _setActivePaperId(entry.id);
  _paperPdfUrl = entry.pdfUrl || '';
  _paperPdfFilename = entry.pdfFilename || '';
  _paperFileName = entry.title || 'Untitled';
  _paperParsedText = entry.parsedText || '';
  _paperArxivId = entry.arxivId || '';
  _paperQAHistory = entry.qaHistory || [];
  _paperReportCache = '';  // Report is loaded from server DB on demand
  _paperReportMeta = null; // finish tag is re-fetched with the cached report
  _paperReviewCache = '';  // Review (per-venue) loaded from server DB on demand
  _paperReviewMeta = null;
  _paperReviewVenue = '';  // re-resolved per-paper (persisted choice → first)
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
  if (countEl) countEl.textContent = String(_paperLibrary.length || '');

  // Initial fetch still in flight and nothing cached yet → skeleton, so the
  // overlay can paint instantly on click and the bookshelf hydrates when the
  // /api/paper/library round-trip lands (see enterPaperMode).
  if (_paperLibraryLoading && _paperLibrary.length === 0) {
    var _ttl = (typeof t === 'function') ? t : function(k){ return k; };
    listEl.innerHTML =
      '<div class="paper-lib-loading">' +
        '<span class="paper-lib-loading-spinner"></span>' +
        '<span>' + escapeHtml(_ttl('paper.loadingLibrary')) + '</span>' +
      '</div>';
    return;
  }

  if (_paperLibrary.length === 0) {
    var _tte = (typeof t === 'function') ? t : function(k){ return k; };
    listEl.innerHTML =
      '<div class="paper-lib-empty">' +
        '<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.3"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>' +
        '<span>' + escapeHtml(_tte('paper.noPapersYet')) + '</span>' +
        '<span class="paper-lib-empty-hint">' + escapeHtml(_tte('paper.noPapersHint')) + '</span>' +
      '</div>';
    return;
  }

  var html = '';
  for (var i = 0; i < _paperLibrary.length; i++) {
    var p = _paperLibrary[i];
    var isActive = p.id === _activePaperId;
    var isRec = _isRecommendedEntry(p);
    var dateStr = _formatPaperDate(p.createdAt);
    var pageStr = p.pageCount ? p.pageCount + 'p' : '';
    var hasReport = p.hasReport ? ' · report' : '';
    var _ttr = (typeof t === 'function') ? t : function(k){ return k; };
    // A recommended-but-unread entry shows a "推荐" badge instead of a page
    // count, so it reads as "saved, open to fetch" rather than a normal paper.
    var metaHtml = isRec
      ? '<span class="paper-lib-rec-badge">' + escapeHtml(_ttr('paper.recommended')) + '</span>' + dateStr
      : dateStr + (pageStr ? ' · ' + pageStr : '') + hasReport;

    html +=
      '<div class="paper-lib-item' + (isActive ? ' active' : '') + (isRec ? ' paper-lib-item-rec' : '') + '" data-id="' + p.id + '" onclick="_onPaperLibClick(\'' + p.id + '\')">' +
        '<div class="paper-lib-item-icon">' +
          '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>' +
        '</div>' +
        '<div class="paper-lib-item-info">' +
          '<span class="paper-lib-item-title" title="' + escapeHtml(p.title) + '">' + escapeHtml(p.title) + '</span>' +
          '<span class="paper-lib-item-meta">' + metaHtml + '</span>' +
        '</div>' +
        '<button class="paper-lib-item-del" onclick="event.stopPropagation();_deletePaperEntry(\'' + p.id + '\')" title="' + escapeHtml((typeof t === 'function') ? t('paper.delete') : 'Delete') + '">' +
          '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>' +
        '</button>' +
      '</div>';
  }
  listEl.innerHTML = html;
}

function _onPaperLibClick(id) {
  for (var i = 0; i < _paperLibrary.length; i++) {
    if (_paperLibrary[i].id === id) {
      var entry = _paperLibrary[i];
      // A lightweight recommendation (arxivId set, never ingested) has no PDF
      // to open — lazily ingest it, REUSING this row's id so the PDF/parsed
      // text/hash fill into the same row instead of minting a duplicate.
      if (_isRecommendedEntry(entry)) {
        _setActivePaperId(entry.id);
        _fetchArxivPaper(entry.arxivId, entry.id);
      } else {
        _openPaperEntry(entry);
      }
      return;
    }
  }
}

function _formatPaperDate(ts) {
  if (!ts) return '';
  var d = new Date(ts);
  var now = new Date();
  var diff = now.getTime() - d.getTime();
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
