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
var _paperReportMeta = null;  // finish-tag: {model, costCny, costUsd, promptTokens, ...}
var _paperHash = '';  // server-side hash for DB report cache lookup

// Session-scoped snapshot of every report/review body already shown THIS
// session, keyed by ``<paperId>::<langKey>`` (e.g. 'p1::en', 'p1::zh',
// 'p1::review:neurips:en'). A language toggle is a PURE VIEW SWITCH: once a
// language has been rendered, flipping back to it repaints from this snapshot
// instantly — no server round-trip, so a toggle can NEVER re-trigger a fresh
// generation (the reported bug: switch en→zh regenerated the Chinese report
// even though it already existed, because the switch wiped view.cache and the
// DB round-trip missed). Explicit Regenerate still overwrites via force-start.
// Cleared on paper switch / mode exit (see _resetReportSnapshots).
var _paperReportSnapshots = {};  // '<paperId>::<langKey>' -> { report, meta }
// localStorage key holding a pending "regenerate in progress for (paperHash,
// lang)" intent. Written synchronously BEFORE the force /start round-trip so a
// refresh that interrupts that request still resumes the regenerate instead of
// reverting to the stale cached report (see _setReportRegenIntent).
var _REPORT_REGEN_INTENT_KEY = 'paper_report_regen_intent';

var _paperQAHistory = [];
var _paperLoading = false;
var _paperQAStreaming = false;
var _paperQAAbort = null;
var _paperQAAbortRequested = false;  // set by the Stop button to break the QA poll loop
var _paperReportModel = '';  // user-selected model for report generation
var _paperImages = [];  // [{url, caption, page, source, width, height}] — for embedding in report
var _paperPdfFilename = '';  // server-side PDF filename — handed back from /api/paper/upload and /api/paper/fetch-arxiv-stream
var _paperSearchResults = [];  // last arXiv search candidate list (rendered on the landing screen)

// ── Report streaming state (2026-04-18 rewrite) ──
// Server owns the report task; the frontend only polls.
// _paperReportStream mirrors the in-flight task's accumulated state for
// the currently-active paper. See the Tab 2 Report section below for
// the full lifecycle (start → poll → apply events → paint).
var _paperReportStream = null;

// ── Review Mode streaming state (2026-06-30) ──
// Review Mode is a SECOND consumer of the exact same report engine: the whole
// stream/poll/render/export stack below is parameterized by a `kind`
// ('report' | 'review') so the two never share cache, DOM, or export — but
// also never fork the polling logic (see `_reportView`). The review's server
// cache key is the composite ``review:<venue>:<uilang>``; everything else is
// identical. These four globals mirror the report's four, for the review view.
var _paperReviewCache = '';
var _paperReviewMeta = null;
var _paperReviewStream = null;
var _paperReviewModel = '';      // user-selected model for review generation
var _paperReviewVenue = '';      // selected venue key (e.g. 'neurips'); '' until resolved per-paper
var _paperReviewVenues = [];     // [{key, name}] fetched from /api/paper/review/venues
var _REVIEW_REGEN_INTENT_KEY = 'paper_review_regen_intent';
// Per-paper venue preference — persisted so the SAME paper re-opens on the
// venue the user last picked (survives tab switches AND hard refresh), like
// the model selection survives. Keyed by paper id in one localStorage map.
var _PAPER_REVIEW_VENUE_KEY = 'paper_review_venue_by_id';
// Review-translation reading view (2026-07-01). A peer review is ALWAYS
// generated in English (the language the authors/AC read); a Chinese UI can
// toggle a translated reading view. English stays canonical (cache/export);
// the translation is produced on demand by the Babel translate task under a
// DISTINCT composite lang key ``review:<venue>:<lang>`` so it never collides
// with the whole-paper Babel cache.
var _paperReviewShowTranslation = false;  // is the CN reading view currently shown?
var _paperReviewTranslatedText = '';      // cached translated markdown (for re-toggle)
var _paperReviewTranslating = false;      // guard against concurrent translate kicks
// Per-paper report language ('en' | 'zh'), persisted so the SAME paper
// re-opens on the language the user last generated it in (survives tab
// switches AND hard refresh). Keyed by paper id in one localStorage map,
// mirroring the review-venue map. Absent → default to the current UI language.
var _PAPER_REPORT_LANG_KEY = 'paper_report_lang_by_id';
// Review reading language ('en' | 'zh'). English stays the canonical
// generated/cached/exported text; 'zh' shows an on-demand translated reading
// view. Persisted per paper so the last-read language re-opens. The toggle is
// always available (both directions), independent of the app UI language.
var _PAPER_REVIEW_LANG_KEY = 'paper_review_lang_by_id';

// View-context registry: the single seam that lets one set of report
// functions drive two independent tabs. Each context exposes the per-view
// DOM ids, the regenerate-intent localStorage key, the cache-key resolver,
// and get/set accessors onto that view's four state globals (so there is
// exactly ONE storage location per view — no mirrored copies that can drift).
function _reportView(kind) {
  if (kind === 'review') {
    return {
      kind: 'review', idPrefix: 'review', containerId: 'paperReviewContent',
      stopBtnId: 'paperReviewStopBtn', regenBtnId: 'paperReviewRegenBtn',
      copyLabelId: 'paperReviewCopyLabel',
      exportMenuId: 'paperReviewExportMenu', exportDropdownId: 'paperReviewExportDropdown',
      modelDropdownId: 'paperReviewModelDropdown', modelLabelId: 'paperReviewModelLabel',
      regenIntentKey: _REVIEW_REGEN_INTENT_KEY,
      // A peer review is ALWAYS generated in English: conference submissions
      // and the review the authors/AC read are English, so English is the
      // canonical text (cache key, lookup, export). A Chinese UI reads a
      // translated view produced on demand by the Babel translate task (see
      // _toggleReviewTranslation) — the English review underneath is never
      // regenerated per-language.
      uiLang: function() { return 'en'; },
      // Composite cache key — what the server uses to pick the review prompt
      // AND to keep reviews out of the plain (paper_hash,'en') report cache.
      langKey: function() { return 'review:' + (_paperReviewVenue || 'generic') + ':' + this.uiLang(); },
      get cache() { return _paperReviewCache; }, set cache(v) { _paperReviewCache = v; },
      get meta() { return _paperReviewMeta; }, set meta(v) { _paperReviewMeta = v; },
      get stream() { return _paperReviewStream; }, set stream(v) { _paperReviewStream = v; },
      get model() { return _paperReviewModel; }, set model(v) { _paperReviewModel = v; },
    };
  }
  return {
    kind: 'report', idPrefix: 'report', containerId: 'paperReportContent',
    stopBtnId: 'paperReportStopBtn', regenBtnId: 'paperReportRegenBtn',
    copyLabelId: 'paperReportCopyLabel',
    exportMenuId: 'paperReportExportMenu', exportDropdownId: 'paperReportExportDropdown',
    modelDropdownId: 'paperReportModelDropdown', modelLabelId: 'paperReportModelLabel',
    regenIntentKey: _REPORT_REGEN_INTENT_KEY,
    // Report generation language is a PER-PAPER, persisted choice (like the
    // review venue) — a researcher reads different papers in different
    // languages, so a global toggle would fight the user. Derived fresh from
    // _activePaperId on every call (no module global to reset on paper switch);
    // defaults to the current UI language on first open. The report is really
    // generated + cached per (paper_hash, lang), so switching regenerates.
    uiLang: function() { return _activeReportLang(); },
    langKey: function() { return this.uiLang(); },
    get cache() { return _paperReportCache; }, set cache(v) { _paperReportCache = v; },
    get meta() { return _paperReportMeta; }, set meta(v) { _paperReportMeta = v; },
    get stream() { return _paperReportStream; }, set stream(v) { _paperReportStream = v; },
    get model() { return _paperReportModel; }, set model(v) { _paperReportModel = v; },
  };
}

/** Read the per-paper report-language map { paperId: 'en'|'zh' }. */
function _readReportLangMap() {
  try {
    var raw = localStorage.getItem(_PAPER_REPORT_LANG_KEY);
    return raw ? (JSON.parse(raw) || {}) : {};
  } catch (e) {
    console.warn('[Paper:Report] read lang map failed:', e);
    return {};
  }
}

/** The report language for the ACTIVE paper: a persisted per-paper choice if
 *  present, else the current UI language. Always derived (no global to drift). */
function _activeReportLang() {
  var uiDefault = (typeof _i18nLang !== 'undefined' && _i18nLang === 'zh') ? 'zh' : 'en';
  if (!_activePaperId) return uiDefault;
  var stored = _readReportLangMap()[_activePaperId];
  return (stored === 'en' || stored === 'zh') ? stored : uiDefault;
}

/** Persist the report language the user picked for the active paper. */
function _persistReportLang(paperId, lang) {
  if (!paperId || (lang !== 'en' && lang !== 'zh')) return;
  try {
    var map = _readReportLangMap();
    map[paperId] = lang;
    localStorage.setItem(_PAPER_REPORT_LANG_KEY, JSON.stringify(map));
  } catch (e) {
    console.warn('[Paper:Report] persist lang failed:', e);
  }
}

/** Snapshot-store key for a view's CURRENT language. Combines the active
 *  paper id with the view's composite cache key (plain 'en'/'zh' for reports,
 *  'review:<venue>:<uilang>' for reviews) so every language of every paper has
 *  its own slot. */
function _reportSnapshotKey(view) {
  view = view || _reportView('report');
  return (_activePaperId || '') + '::' + view.langKey();
}

/** Remember a rendered report/review body so a later toggle back to this
 *  language repaints instantly (no server round-trip → no accidental regen). */
function _rememberReportSnapshot(view, report, meta) {
  if (!report) return;
  _paperReportSnapshots[_reportSnapshotKey(view)] = { report: report, meta: meta || null };
}

/** The remembered body for the view's current language, or null. */
function _getReportSnapshot(view) {
  return _paperReportSnapshots[_reportSnapshotKey(view)] || null;
}

/** Drop ALL session snapshots. Called on paper switch / mode exit so one
 *  paper's reports never paint under another. */
function _resetReportSnapshots() {
  _paperReportSnapshots = {};
}

/** Sync the EN/中 segmented control for a view to reflect its current
 *  language. `view` defaults to the report view. */
function _syncReportLangToggle(view) {
  view = view || _reportView('report');
  var wrap = document.getElementById(view.idPrefix + 'LangToggle');
  if (!wrap) return;
  var cur = (view.kind === 'review') ? _activeReviewLang() : _activeReportLang();
  wrap.querySelectorAll('.paper-report-lang-opt').forEach(function(btn) {
    btn.classList.toggle('active', btn.dataset.lang === cur);
  });
}

/** User clicked EN or 中 in a view's segmented control.
 *   - Report: changes the generation language (per (paper_hash, lang) cache) →
 *     reset local state + load/generate in the new language.
 *   - Review: English stays canonical; 'zh' shows the translated reading view,
 *     'en' restores the original (bidirectional, always available). */
function _setReportLang(lang, kind) {
  if (lang !== 'en' && lang !== 'zh') return;
  var view = _reportView(kind || 'report');
  if (view.kind === 'review') {
    _setReviewLang(lang);
    _syncReportLangToggle(view);
    return;
  }
  var cur = _activeReportLang();
  if (cur === lang) return;
  // Before leaving the current language, snapshot whatever is on screen so a
  // later toggle back repaints it instantly (never regenerates).
  if (view.cache) _rememberReportSnapshot(view, view.cache, view.meta);
  if (_activePaperId) _persistReportLang(_activePaperId, lang);
  _syncReportLangToggle(view);
  // New language ⇒ different (paper_hash, lang) cache key. Drop local stream
  // state + in-memory cache so we paint/load the report in the new language
  // (each language cached separately server-side — no clobber).
  _resetReportLocalState(view);
  view.cache = '';
  // If we've already shown this language THIS session, it's a pure view switch:
  // repaint from the snapshot with no server round-trip. This is what stops a
  // toggle from ever re-triggering a fresh generation (the reported bug).
  var snap = _getReportSnapshot(view);
  if (snap) {
    view.cache = snap.report;
    view.meta = snap.meta || null;
    if (_paperActiveTab === 'report') {
      var c = document.getElementById(view.containerId);
      if (c) _renderFinalReport(c, snap.report, undefined, view);
    }
    return;
  }
  if (_paperActiveTab === 'report') _loadOrGenerateReport(view);
}

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

function _createPaperEntry(title, pdfUrl, parsedText, arxivId, explicitId) {
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
      _openPaperEntry(_paperLibrary[i]);
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
      _paperReviewCache = '';
      _paperReviewVenue = '';
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
      _paperReviewCache = '';
      _paperReviewVenue = '';
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
    // Swap icon to back-arrow; keep the topbar text label.
    pmBtn.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg><span class="topbar-tool-label">' + (typeof t === 'function' ? t('topbar.backToChat') : 'Back') + '</span>';
    pmBtn.title = 'Back to Chat';
  }

  if (_paperPdfUrl) {
    _loadPaperPdf(_paperPdfUrl);
  } else {
    _showPaperLanding();
  }

  _switchPaperTab('qa');
  _setPaperMobileView('pdf');

  // Seed the report + review model selections so each button label reflects
  // the actual model from the start (no more stale "Default" placeholder).
  try { _populatePaperReportModelDropdown(); } catch (e) {
    console.warn('[Paper] populate report model dropdown failed:', e);
  }
  try { _populatePaperReportModelDropdown(_reportView('review')); } catch (e) {
    console.warn('[Paper] populate review model dropdown failed:', e);
  }

  debugLog('Paper Mode: ENTER', 'success');
}

function exitPaperMode() {
  _saveActivePaperState();
  // Flush any in-progress reading session into the learning model.
  if (typeof _teardownReadingTracker === 'function') _teardownReadingTracker(true);
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

  // ★ Recompute toolbar width now the input area is visible again. Any reflow
  // that fired while paper mode hid .input-area was a no-op (offsetParent
  // null), so --toolbar-w may be stale/scrunched. Re-measure once.
  if (typeof _scheduleReflow === 'function') _scheduleReflow();

  var pmBtn = document.getElementById('paperModeBtn');
  if (pmBtn) {
    pmBtn.classList.remove('active');
    // Restore book icon + topbar text label.
    pmBtn.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/><line x1="8" y1="7" x2="16" y2="7"/><line x1="8" y1="11" x2="14" y2="11"/></svg><span class="topbar-tool-label">' + (typeof t === 'function' ? t('topbar.paper') : 'Paper') + '</span>';
    pmBtn.title = (typeof t === 'function' ? t('paper.title') : 'Paper Reader');
  }

  if (_paperResizeObserver) { _paperResizeObserver.disconnect(); _paperResizeObserver = null; }
  if (_paperPdfDoc) { _paperPdfDoc.destroy(); _paperPdfDoc = null; }
  if (_paperQAAbort) { _paperQAAbort.abort(); _paperQAAbort = null; }

  // Stop the report + review poll timers — the server tasks keep running and
  // will be re-attached on next entry via /api/paper/report/lookup.
  [_paperReportStream, _paperReviewStream].forEach(function(st) {
    if (st && st.pollTimer) { clearTimeout(st.pollTimer); st.pollTimer = null; }
  });

  var viewer = document.getElementById('paperPdfViewer');
  if (viewer) viewer.innerHTML = '';

  debugLog('Paper Mode: EXIT', 'info');
}

function togglePaperMode() {
  paperMode ? exitPaperMode() : enterPaperMode();
}

/**
 * Apply a server-resolved paper title to the active paper, live.
 *
 * The report backend self-heals library rows whose title is still a bare
 * `arXiv:<id>` (the up-front arXiv lookup failed) by extracting the real
 * title from the report's Paper Card and upserting it. It returns that title
 * in the report `done` / cache responses as `resolvedTitle`. We apply it to
 * the in-memory entry + sidebar immediately so the user never has to reload.
 *
 * Guards (mirror the backend): only overwrite when the current local title is
 * empty or itself a bare `arXiv:<id>` — never clobber a user-renamed title.
 * Scoped to the paper the resolution belongs to (paperId), so a background
 * stream for paper A can't rename the now-active paper B.
 */
function _applyResolvedTitle(resolvedTitle, paperId) {
  var title = (resolvedTitle || '').trim();
  if (!title) return;
  var pid = paperId || _activePaperId;
  var entry = null;
  for (var i = 0; i < _paperLibrary.length; i++) {
    if (_paperLibrary[i].id === pid) { entry = _paperLibrary[i]; break; }
  }
  if (!entry) return;
  var cur = (entry.title || '').trim();
  var isPlaceholder = !cur || /^arxiv[:\s]/i.test(cur);
  if (!isPlaceholder) return;       // respect a real / user-set title
  if (cur === title) return;        // no change
  entry.title = title;
  if (pid === _activePaperId) {
    _paperFileName = title;
    _updatePaperTitles();
  }
  _renderPaperLibrary();
  // Persist the healed title (server already updated its row, but this keeps
  // the per-entry PUT path consistent and covers the in-memory entry).
  if (pid === _activePaperId) _saveActivePaperState();
}

function _updatePaperTitles() {
  var _tt = (typeof t === 'function') ? t : function(k){ return k; };
  var noPaper = _tt('paper.noPaperOpen');
  var name = _paperFileName || noPaper;
  var stitle = document.getElementById('paperSidebarTitle');
  if (stitle) {
    stitle.textContent = name;
    stitle.title = name;
    stitle.classList.toggle('paper-sidebar-title-empty', !_paperFileName);
  }
  var pageCount = document.getElementById('paperPageCount');
  if (pageCount && _paperTotalPages) {
    pageCount.textContent = _tt('paper.pages', { count: _paperTotalPages });
  } else if (pageCount) {
    pageCount.textContent = '';
  }
  // ★ Topbar title reflects Paper Mode, not the previous conversation
  if (paperMode) {
    var topbar = document.getElementById('topbarTitle');
    if (topbar) {
      var label = _paperFileName ? _paperFileName : _tt('paper.title');
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
      wrapper.dataset.page = String(i);
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
  var _tt = (typeof t === 'function') ? t : function(k){ return k; };
  viewer.innerHTML =
    '<div class="paper-landing">' +
      '<div class="paper-landing-icon">' + Icon('file', 40) + '</div>' +
      '<h3>' + escapeHtml(_tt('paper.title')) + '</h3>' +
      '<p>' + escapeHtml(_tt('paper.landingDesc')) + '</p>' +
      '<div class="paper-landing-actions">' +
        '<label class="paper-upload-btn">' +
          '<input type="file" accept=".pdf,application/pdf" onchange="_handlePaperFileUpload(event)" style="display:none">' +
          '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>' +
          ' ' + escapeHtml(_tt('paper.uploadPdf')) +
        '</label>' +
        '<div class="paper-arxiv-input">' +
          '<svg class="paper-arxiv-search-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>' +
          '<input type="text" id="paperArxivUrl" placeholder="' + escapeHtml(_tt('paper.arxivPlaceholder')) + '"' +
                 ' onkeydown="if(event.key===\'Enter\')_submitArxivQuery()">' +
          '<button onclick="_submitArxivQuery()" class="paper-arxiv-btn">' + escapeHtml(_tt('paper.search')) + '</button>' +
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
  _paperReviewCache = '';
  _paperReviewVenue = '';
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

  // Create a new library entry for this paper (_createPaperEntry sets _activePaperId).
  // Its id is sent WITH the upload so the server persists the bookshelf row
  // itself (server-authoritative ingest) — the paper survives even if this
  // tab closes before the client save. On failure we remove this entry so a
  // failed upload never leaves a ghost row.
  var _uploadEntry = _createPaperEntry(file.name);
  var _uploadPaperId = _uploadEntry.id;
  _paperFileName = file.name;
  _paperParsedText = '';
  _paperQAHistory = [];
  _paperReportCache = '';
  _paperReviewCache = '';
  _paperReviewVenue = '';
  _paperHash = '';
  _paperPdfFilename = '';
  _paperImages = [];
  _babelTranslatedPages = {};
  _updatePaperTitles();
  _renderPaperLibrary();

  var viewer = document.getElementById('paperPdfViewer');
  if (viewer) viewer.innerHTML = '<div class="paper-loading"><div class="paper-loading-spinner"></div><div>Uploading & parsing PDF…</div></div>';

  try {
    // Single round-trip: server saves the PDF, parses text, AND extracts
    // figures synchronously — no client-side parse fallback, no race with
    // a background image-extraction call.
    var formData = new FormData();
    formData.append('file', file);
    formData.append('paper_id', _uploadPaperId);
    var uploadData = await Api.paper.upload(formData);
    if (!uploadData || !uploadData.ok) throw new Error((uploadData && uploadData.error) || 'Upload failed');

    _paperPdfUrl = apiUrl(uploadData.pdf_url);
    _paperPdfFilename = uploadData.filename || '';
    _paperParsedText = uploadData.parsed_text || '';
    _paperHash = uploadData.paper_hash || '';
    _paperImages = Array.isArray(uploadData.images) ? uploadData.images : [];
    _paperTotalPages = uploadData.total_pages || 0;

    if (uploadData.parse_error) {
      debugLog('[Paper] PDF text extraction failed: ' + uploadData.parse_error, 'warning');
    } else if (_paperParsedText) {
      debugLog('Paper parsed: ' + _paperTotalPages + ' pages, ' +
               (uploadData.text_length || _paperParsedText.length) + ' chars' +
               (_paperImages.length ? ' (' + _paperImages.length + ' figures)' : ''),
               'success');
    }

    _updatePaperTitles();
    await _loadPaperPdf(_paperPdfUrl);
    // The server already persisted the row at ingest; await this so an
    // immediate exit/refresh can't race the first save (beforeunload is a
    // backstop only). Marks the entry _persisted so later saves are incremental.
    await _saveActivePaperState();

  } catch (e) {
    console.error('[Paper] Upload failed:', e);
    // A failed upload must NOT leave a ghost bookshelf entry (the qa=0/imgs=0
    // rows behind the vanishing-paper report). Drop the entry we optimistically
    // created; the server never persisted a row for a failed save.
    _paperLibrary = _paperLibrary.filter(function(p) { return p.id !== _uploadPaperId; });
    if (_activePaperId === _uploadPaperId) _setActivePaperId('');
    _renderPaperLibrary();
    if (viewer) viewer.innerHTML = '<div class="paper-error">Upload failed: ' + escapeHtml(e.message) + '</div>';
  } finally {
    _paperLoading = false;
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

/** Heuristic: does this input look like a direct arXiv ID / URL (vs a title query)? */
function _looksLikeArxivRef(s) {
  s = (s || '').trim();
  if (/arxiv\.org\//i.test(s)) return true;
  if (/^\d{4}\.\d{4,5}(v\d+)?$/.test(s)) return true;          // 2301.12345
  if (/^[a-z-]+\/\d{7}(v\d+)?$/i.test(s)) return true;          // hep-th/0601001
  return false;
}

/**
 * Entry point from the landing input. Routes a direct arXiv ID/URL straight
 * to download, or a free-text title to the arXiv search results list.
 */
function _submitArxivQuery() {
  var input = document.getElementById('paperArxivUrl');
  var q = input?.value?.trim();
  if (!q) { debugLog('Please enter a title to search, or an arXiv URL / ID', 'warning'); return; }
  if (_looksLikeArxivRef(q)) {
    _fetchArxivPaper(q);
  } else {
    _searchArxivPapers(q);
  }
}

/** Search arXiv by title/keywords and render candidate cards. */
async function _searchArxivPapers(query) {
  var viewer = document.getElementById('paperPdfViewer');
  var _tt = (typeof t === 'function') ? t : function(k){ return k; };
  if (viewer) {
    viewer.innerHTML =
      '<div class="paper-loading paper-search-loading">' +
        '<div class="paper-loading-spinner"></div>' +
        '<div>' + escapeHtml(_tt('paper.searching')) + '</div>' +
      '</div>';
  }

  try {
    var data = await Api.paper.searchArxiv(query, 12);
    var results = (data && data.ok && Array.isArray(data.results)) ? data.results : [];
    _paperSearchResults = results;
    _renderArxivSearchResults(query, results);
  } catch (e) {
    console.error('[Paper] arXiv search failed:', e);
    if (viewer) {
      viewer.innerHTML =
        '<div class="paper-error">' + escapeHtml(_tt('paper.searchFailed')) +
        '<br><button onclick="_showPaperLanding()" class="paper-retry-btn">' +
        escapeHtml(_tt('paper.searchBack')) + '</button></div>';
    }
  }
}

/** Render the list of arXiv search-result cards. */
function _renderArxivSearchResults(query, results) {
  var viewer = document.getElementById('paperPdfViewer');
  if (!viewer) return;
  var _tt = (typeof t === 'function') ? t : function(k){ return k; };

  var header =
    '<div class="paper-search-head">' +
      '<button class="paper-search-back" onclick="_showPaperLanding()" title="' + escapeHtml(_tt('paper.searchBack')) + '">' +
        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="19" y1="12" x2="5" y2="12"/><polyline points="12 19 5 12 12 5"/></svg>' +
      '</button>' +
      '<div class="paper-search-head-text">' +
        '<div class="paper-search-head-title">' + escapeHtml(_tt('paper.searchResultsTitle')) + '</div>' +
        '<div class="paper-search-head-q">“' + escapeHtml(query) + '”</div>' +
      '</div>' +
    '</div>';

  if (!results.length) {
    viewer.innerHTML =
      '<div class="paper-search">' + header +
        '<div class="paper-search-empty">' + escapeHtml(_tt('paper.searchNoResults')) + '</div>' +
      '</div>';
    return;
  }

  var hint = '<div class="paper-search-hint">' + escapeHtml(_tt('paper.searchResultsHint')) + '</div>';

  var cards = results.map(function(r, i) {
    var authors = Array.isArray(r.authors) ? r.authors : [];
    var authorStr = authors.slice(0, 4).join(', ') + (authors.length > 4 ? ' et al.' : '');
    var meta = [];
    if (r.primary_category) meta.push('<span class="paper-card-cat">' + escapeHtml(r.primary_category) + '</span>');
    if (r.published) meta.push('<span class="paper-card-date">' + escapeHtml(r.published) + '</span>');
    meta.push('<span class="paper-card-id">arXiv:' + escapeHtml(r.arxiv_id) + '</span>');
    return '' +
      '<div class="paper-result-card" role="button" tabindex="0" data-idx="' + i + '"' +
           ' onclick="_openArxivResult(' + i + ')"' +
           ' onkeydown="if(event.key===\'Enter\'||event.key===\' \'){event.preventDefault();_openArxivResult(' + i + ')}">' +
        '<div class="paper-result-num">' + (i + 1) + '</div>' +
        '<div class="paper-result-body">' +
          '<div class="paper-result-title">' + escapeHtml(r.title || r.arxiv_id) + '</div>' +
          (authorStr ? '<div class="paper-result-authors">' + escapeHtml(authorStr) + '</div>' : '') +
          (r.summary ? '<div class="paper-result-summary">' + escapeHtml(r.summary) + '</div>' : '') +
          '<div class="paper-result-meta">' + meta.join('') + '</div>' +
        '</div>' +
        '<div class="paper-result-arrow">' +
          '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>' +
        '</div>' +
      '</div>';
  }).join('');

  viewer.innerHTML =
    '<div class="paper-search">' + header + hint +
      '<div class="paper-result-list">' + cards + '</div>' +
    '</div>';
}

/** Load the arXiv paper at index `idx` of the last search results. */
function _openArxivResult(idx) {
  var r = _paperSearchResults && _paperSearchResults[idx];
  if (!r || !r.arxiv_id) return;
  _fetchArxivPaper(r.arxiv_id);
}

async function _fetchArxivPaper(directUrl) {
  var url = directUrl;
  if (url == null) {
    var input = document.getElementById('paperArxivUrl');
    url = input?.value?.trim();
  }
  url = (url || '').trim();
  if (!url) { debugLog('Please enter an arXiv URL or ID', 'warning'); return; }

  _paperLoading = true;
  _renderArxivFetchProgress({ stage: 'resolve' });

  // Mint the bookshelf id up front and send it WITH the fetch so the server
  // persists the library row itself at the 'done' stage (server-authoritative
  // ingest) — the paper survives even if this tab closes mid-stream.
  var _arxivPaperId = _newPaperEntryId();

  try {
    var resp = await Api.paper.fetchArxivStream(url, _arxivPaperId);
    if (!resp || !resp.ok || !resp.body) {
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
    // Extract filename from pdf_url (e.g. "/api/paper/pdf/arxiv_2301.12345.pdf")
    var _pdfMatch = /\/api\/paper\/pdf\/([^?#]+)/.exec(doneData.pdf_url || '');
    _paperPdfFilename = _pdfMatch ? decodeURIComponent(_pdfMatch[1]) : '';
    _paperArxivId = doneData.arxiv_id || curArxivId || '';
    _paperFileName = (doneData.title || '').trim() || ('arXiv:' + _paperArxivId);
    _paperParsedText = doneData.parsed_text || '';
    _paperTotalPages = doneData.total_pages || 0;
    _paperHash = doneData.paper_hash || '';
    _paperImages = Array.isArray(doneData.images) ? doneData.images : [];

    // Create library entry now that we have everything (sets _activePaperId).
    // Reuse the id we sent to the server so the client entry and the
    // server-persisted row are the same row.
    _createPaperEntry(_paperFileName, _paperPdfUrl, _paperParsedText, _paperArxivId, _arxivPaperId);
    _paperQAHistory = [];
    _paperReportCache = '';
    _paperReviewCache = '';
    _paperReviewVenue = '';
    _babelTranslatedPages = {};
    _updatePaperTitles();
    _renderPaperLibrary();

    if (doneData.parse_error) {
      debugLog('[Paper] PDF text extraction failed: ' + doneData.parse_error, 'warning');
    } else if (_paperParsedText) {
      debugLog('arXiv parsed: ' + _paperTotalPages + ' pages, ' +
               (doneData.text_length || _paperParsedText.length) + ' chars' +
               (_paperImages.length ? ' (' + _paperImages.length + ' figures)' : ''),
               'success');
    } else {
      debugLog('[Paper] arXiv PDF loaded but no text extracted — Q&A and Report unavailable', 'warning');
    }

    await _loadPaperPdf(_paperPdfUrl);
    // Await the first persist so an immediate exit/refresh can't race it
    // (the server already persisted at ingest; this reconciles client state).
    await _saveActivePaperState();

    debugLog('Fetched arXiv:' + _paperArxivId + (doneData.cached ? ' (cached)' : ''), 'success');
  } catch (e) {
    console.error('[Paper] arXiv fetch failed:', e);
    // A failed fetch must not leave a ghost bookshelf entry. If we already
    // created one (id minted up front), drop it — the server only persists a
    // row at the 'done' stage, so a failed fetch never wrote one.
    _paperLibrary = _paperLibrary.filter(function(p) { return p.id !== _arxivPaperId; });
    if (_activePaperId === _arxivPaperId) _setActivePaperId('');
    _renderPaperLibrary();
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
  // Leaving the Report/Review tab → flush the reading session into the model.
  if ((_paperActiveTab === 'report' || _paperActiveTab === 'review') && tab !== _paperActiveTab
      && typeof _teardownReadingTracker === 'function') {
    _teardownReadingTracker(true);
  }
  _paperActiveTab = tab;
  document.querySelectorAll('.paper-tab-btn').forEach(function(btn) {
    btn.classList.toggle('active', btn.dataset.tab === tab);
  });
  document.querySelectorAll('.paper-tab-panel').forEach(function(panel) {
    panel.style.display = panel.dataset.tab === tab ? '' : 'none';
  });
  if (tab === 'report' || tab === 'review') {
    // Collapse the sidebar so the report/review gets the full width. Paper
    // mode expands the sidebar on entry (to show the library), so we only
    // collapse it here when the user opens a full-width reading tab.
    try {
      var _sb = document.getElementById('sidebar');
      if (_sb && !_sb.classList.contains('collapsed') && typeof toggleSidebar === 'function') {
        toggleSidebar();
      }
    } catch (e) {
      console.warn('[Paper] auto-collapse sidebar failed:', e);
    }
    var _view = _reportView(tab);
    // Server owns the task. The frontend always asks _loadOrGenerateReport()
    // which (a) resumes local poll if any, (b) looks up a running server
    // task, (c) hits DB cache, or (d) starts.
    if (_paperParsedText || _paperHash) {
      if (tab === 'review') {
        // CRITICAL: resolve the venue (persisted-per-paper → first) BEFORE
        // generating, so the displayed venue and the generation langKey are
        // consistent. Without this await, _loadOrGenerateReport would fire
        // synchronously with venue='' → langKey 'review:generic:…' while the
        // async dropdown later snapped the label to NeurIPS — label/key skew.
        _populateReviewVenueDropdown()
          .then(function() { _loadOrGenerateReport(_view); })
          .catch(function(e) {
            console.warn('[Paper:Review] venue resolve failed, loading with fallback:', e);
            _loadOrGenerateReport(_view);
          });
      } else {
        _loadOrGenerateReport(_view);
      }
    } else {
      var _empty = document.getElementById(_view.containerId);
      if (_empty) {
        _empty.innerHTML = '<div class="paper-report-empty"><p>' + escapeHtml((typeof t === 'function') ? t('paper.reportNoText') : 'No paper text available. Load a PDF first.') + '</p></div>';
      }
    }
  }
  if (tab === 'qa') _renderPaperQA();
  if (tab === 'translate') _initBabelPdfTab();
}

/** Mobile-only: toggle which full-screen pane is shown — the PDF ('pdf') or
 *  the Reader pane ('reader', i.e. the Q&A/Report/Babel tabs). On desktop the
 *  split view shows both at once and this is a no-op for layout (the switcher
 *  bar is hidden by CSS), but we still track the attribute harmlessly. */
function _setPaperMobileView(view) {
  if (view !== 'pdf' && view !== 'reader') view = 'pdf';
  var body = document.querySelector('.paper-body');
  if (body) body.setAttribute('data-paper-view', view);
  document.querySelectorAll('.paper-mobile-switch-btn').forEach(function(btn) {
    btn.classList.toggle('active', btn.dataset.view === view);
  });
  // When (re)showing the PDF on a phone it may have been laid out while hidden
  // (offsetParent null → fit math is wrong), so refit to the now-visible width.
  if (view === 'pdf' && _paperPdfDoc && typeof paperFitWidth === 'function') {
    // Defer one frame so the pane has its final width before measuring.
    requestAnimationFrame(function() {
      try { paperFitWidth(); } catch (e) { console.warn('[Paper] mobile fit-width failed:', e); }
    });
  }
}

// ══════════════════════════════════════════════════════
//  ★ Tab 1: Q&A
// ══════════════════════════════════════════════════════

/** Build the inner HTML for one Q&A message bubble. */
function _qaMsgInnerHtml(msg) {
  var isUser = msg.role === 'user';
  var inner = '';
  // Tool-activity panel (web_search / fetch_url) — reuse chat's renderer so
  // the look matches the report tab + chat bubbles.
  if (!isUser && Array.isArray(msg.toolRounds) && msg.toolRounds.length &&
      typeof renderToolRoundsHTML === 'function') {
    inner += '<div class="paper-qa-tools">' +
      renderToolRoundsHTML(msg.toolRounds, msg.status === 'running') + '</div>';
  }
  if (isUser) {
    inner += '<div class="paper-qa-msg-content">' + escapeHtml(msg.content) + '</div>';
  } else if (msg.content) {
    inner += '<div class="paper-qa-msg-content">' +
      (typeof renderMarkdown === 'function' ? renderMarkdown(msg.content) : escapeHtml(msg.content)) +
      '</div>';
  } else if (msg.status === 'running') {
    // Thinking / searching, no prose yet — show a small pulse.
    inner += '<div class="paper-qa-msg-content paper-qa-thinking">' +
      '<span class="thinking-dot"></span></div>';
  }
  return inner;
}

// Reconcile the Q&A message list in place. Streaming polls call this every
// ~700ms; rebuilding the whole innerHTML each time tore down and recreated
// every bubble (flicker + scroll jump + markdown re-parse). Instead we keep
// one DOM node per message and only rewrite a node whose rendered content
// actually changed — during streaming that's just the last assistant bubble.
function _renderPaperQA() {
  var container = document.getElementById('paperQAMessages');
  if (!container) return;
  if (!_paperQAHistory || _paperQAHistory.length === 0) {
    var _ttq = (typeof t === 'function') ? t : function(k){ return k; };
    container.innerHTML =
      '<div class="paper-qa-empty"><div class="paper-qa-empty-icon">' + Icon('messageCircle', 32) + '</div>' +
      '<p>' + escapeHtml(_ttq('paper.qaEmptyTitle')) + '</p>' +
      '<p class="paper-qa-hint">' + escapeHtml(_ttq('paper.qaEmptyHint')) + '</p></div>';
    return;
  }
  // Drop the empty-state placeholder (or any stale non-message node) before reconciling.
  var first = container.firstElementChild;
  if (first && !first.classList.contains('paper-qa-msg')) container.innerHTML = '';

  var nearBottom = (container.scrollHeight - container.scrollTop - container.clientHeight) < 80;
  var changed = false;

  // Remove surplus nodes (e.g. history was trimmed or a paper switch left extras).
  while (container.children.length > _paperQAHistory.length) {
    container.removeChild(container.lastElementChild);
    changed = true;
  }

  for (var j = 0; j < _paperQAHistory.length; j++) {
    var msg = _paperQAHistory[j];
    var cls = 'paper-qa-msg ' + (msg.role === 'user' ? 'paper-qa-user' : 'paper-qa-assistant');
    var inner = _qaMsgInnerHtml(msg);
    var node = container.children[j];
    if (!node) {
      node = document.createElement('div');
      container.appendChild(node);
    }
    if (node._qaCls !== cls) { node.className = cls; node._qaCls = cls; }
    if (node._qaSig !== inner) { node.innerHTML = inner; node._qaSig = inner; changed = true; }
  }

  if (changed && nearBottom) container.scrollTop = container.scrollHeight;
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
    var data = await Api.paper.reparse(fname);
    if (!data || !data.ok || !data.text) {
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

  // Recent dialogue (exclude the question we're about to add) for context.
  var historyForServer = _paperQAHistory.slice(-10).map(function(m) {
    return { role: m.role, content: m.content };
  });

  _paperQAHistory.push({ role: 'user', content: question, timestamp: Date.now() });
  // Assistant placeholder carries live tool-round state for this answer.
  var asst = { role: 'assistant', content: '', timestamp: Date.now(),
               toolRounds: [], status: 'running' };
  _paperQAHistory.push(asst);
  input.value = '';
  _paperQAStreaming = true;
  _renderPaperQA();

  var startPaperId = _activePaperId;
  try {
    var startData = await Api.paper.qaStart({
      question: question,
      paper_text: _paperParsedText,
      paper_hash: _paperHash || '',
      lang: (typeof _i18nLang !== 'undefined' && _i18nLang === 'zh') ? 'zh' : 'en',
      history: historyForServer,
      model: (typeof _paperReportModel !== 'undefined') ? _paperReportModel : undefined,
      title: _paperFileName || '',
    });
    if (!startData || !startData.ok || !startData.task_id) {
      throw new Error((startData && startData.error) || 'Q&A start failed');
    }
    await _pollQATask(startData.task_id, asst, startPaperId);
  } catch (e) {
    asst.status = 'error';
    asst.content = (asst.content || '') + '\n\n' + Icon('alertTriangle', 14) + ' ' +
      ((typeof t === 'function') ? t('paper.qaError') : 'Error') + ': ' + (e.message || e);
    _renderPaperQA();
    console.warn('[Paper:QA] failed:', e);
  } finally {
    _paperQAStreaming = false; _paperQAAbort = null; _saveActivePaperState();
  }
}

/** Poll a Q&A task to completion, applying events to the assistant message.
 *  Mirrors _pollReportTask but writes into the QA history entry `asst`. */
async function _pollQATask(taskId, asst, startPaperId) {
  var cursor = 0;
  var POLL_MS = 700;
  while (true) {
    if (_paperQAAbortRequested) { _paperQAAbortRequested = false; break; }
    var resp = await Api.paper.qaPoll(taskId, cursor);
    if (!resp || !resp.ok) {
      if (resp && resp.status === 404) {
        asst.status = 'error';
        asst.content = asst.content ||
          ((typeof t === 'function') ? t('paper.qaExpired') : 'Q&A task expired.');
        break;
      }
      throw new Error('HTTP ' + (resp ? resp.status : '?'));
    }
    var data = await resp.json();
    if (!data.ok) throw new Error((typeof data.error === 'string' ? data.error : 'Poll failed'));

    var events = data.events || [];
    for (var i = 0; i < events.length; i++) _applyQAEvent(asst, events[i]);
    cursor = data.next_cursor;

    if (data.status === 'done') {
      asst.status = 'done';
      if (data.answer) asst.content = data.answer;
      if (startPaperId === _activePaperId) _renderPaperQA();
      break;
    }
    if (data.status === 'error') {
      asst.status = 'error';
      asst.content = (asst.content || '') + '\n\n' + Icon('alertTriangle', 14) + ' ' +
        ((typeof errorEnvelopeMessage === 'function') ? errorEnvelopeMessage(data.error) : (data.error || 'Error'));
      if (startPaperId === _activePaperId) _renderPaperQA();
      break;
    }
    if (startPaperId === _activePaperId) _renderPaperQA();
    await new Promise(function(r) { setTimeout(r, POLL_MS); });
  }
}

/** Apply one Q&A event to the assistant message state (chat-compatible). */
function _applyQAEvent(asst, ev) {
  switch (ev.type) {
    case 'tool_start':
      asst.toolRounds.push({
        roundNum: ev.roundNum, toolName: ev.toolName, query: ev.query,
        toolCallId: ev.toolCallId, toolArgs: ev.toolArgs,
        status: 'searching', results: null,
      });
      return;
    case 'tool_done': {
      for (var j = 0; j < asst.toolRounds.length; j++) {
        var r = asst.toolRounds[j];
        if (r.roundNum === ev.roundNum) {
          r.status = 'done';
          r._elapsed = (ev.elapsed != null) ? (ev.elapsed + 's') : r._elapsed;
          r.toolContent = ev.toolContent || r.toolContent;
          if (ev.results) r.results = ev.results;
          if (ev.searchDiag) r.searchDiag = ev.searchDiag;
          if (ev.engineBreakdown) r.engineBreakdown = ev.engineBreakdown;
          if (ev.vertical) r.vertical = ev.vertical;
          if (ev.verticals) r.verticals = ev.verticals;
          break;
        }
      }
      return;
    }
    case 'delta':
      asst.content += (ev.delta || '');
      return;
    case 'delta_reset':
      // Interim draft emitted alongside a tool call — discard it (the model
      // rewrites the full answer after the tool result lands).
      asst.content = '';
      return;
    default:
      return;
  }
}

function _quotePaperSelection() {
  var sel = window.getSelection();
  var text = sel?.toString()?.trim();
  if (!text) return;
  var input = document.getElementById('paperQAInput');
  if (!input) return;
  if (_paperActiveTab !== 'qa') _switchPaperTab('qa');
  _setPaperMobileView('reader');
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
  _setPaperMobileView('reader');
  input.value = '> ' + text.replace(/\n/g, '\n> ') + '\n\nExplain this part of the paper.';
  sel.removeAllRanges();
  _hidePaperQuoteBar();
  // Auto-send after a brief delay for tab switch to settle
  setTimeout(function() { _sendPaperQuestion(); }, 100);
}

function _hidePaperQuoteBar() {
  var q = document.getElementById('paperQuoteBtn');
  if (q) q.style.display = 'none';
  var qr = document.getElementById('paperReportQuoteBtn');
  if (qr) qr.style.display = 'none';
}

function _handlePaperTextSelection() {
  var sel = window.getSelection();
  var text = sel?.toString()?.trim();
  var q = document.getElementById('paperQuoteBtn');
  var qr = document.getElementById('paperReportQuoteBtn');
  if (qr) qr.style.display = 'none';
  if (q) q.style.display = 'none';
  if (!text || text.length < 3) return;

  // Source A: selection inside the PDF sidebar → anchor the toolbar in
  // .paper-left (existing behaviour).
  var viewer = document.getElementById('paperPdfViewer');
  if (q && viewer && viewer.contains(sel.anchorNode)) {
    var range = sel.getRangeAt(0);
    var rect = range.getBoundingClientRect();
    var leftEl = document.querySelector('.paper-left');
    if (!leftEl) return;
    var lr = leftEl.getBoundingClientRect();
    q.style.display = 'flex';
    q.style.top = (rect.top - lr.top - 40) + 'px';
    q.style.left = Math.max(4, rect.left - lr.left + rect.width / 2 - 80) + 'px';
    return;
  }

  // Source B: selection inside the generated REPORT → anchor a sibling
  // toolbar in .paper-right so a confusing report passage becomes a
  // one-click question (the central UX ask).
  var reportEl = document.getElementById('paperReportContent');
  if (qr && reportEl && reportEl.contains(sel.anchorNode)) {
    var rrange = sel.getRangeAt(0);
    var rrect = rrange.getBoundingClientRect();
    var rightEl = document.querySelector('.paper-right');
    if (!rightEl) return;
    var rr = rightEl.getBoundingClientRect();
    qr.style.display = 'flex';
    qr.style.top = Math.max(4, rrect.top - rr.top - 40) + 'px';
    qr.style.left = Math.max(4, rrect.left - rr.left + rrect.width / 2 - 80) + 'px';
  }
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

/** Persist a "regenerate in progress for (paperHash, lang)" intent to
 *  localStorage. Written synchronously the instant the user clicks Regenerate
 *  — BEFORE the force /start network round-trip — so a refresh that lands in
 *  that sub-second window still knows to resume the regenerate (re-issuing
 *  force /start) instead of falling through to the stale DB-cached report.
 *  Mirrors the chat edit-resend "persist truncation before the atomic call"
 *  fix. A null/empty hash clears the intent. */
function _setReportRegenIntent(paperHash, lang, key) {
  key = key || _REPORT_REGEN_INTENT_KEY;
  try {
    if (!paperHash) { localStorage.removeItem(key); return; }
    localStorage.setItem(key,
      JSON.stringify({ paperHash: paperHash, lang: lang || 'en', ts: Date.now() }));
  } catch (e) {
    console.warn('[Paper:Report] persist regen intent failed:', e);
  }
}

/** Read the pending regenerate intent, or null. */
function _getReportRegenIntent(key) {
  try {
    var raw = localStorage.getItem(key || _REPORT_REGEN_INTENT_KEY);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch (e) {
    console.warn('[Paper:Report] read regen intent failed:', e);
    return null;
  }
}

/** Clear the regenerate intent once it has been fulfilled (a task was
 *  attached) or reached a terminal state. Re-entrancy guard against a
 *  refresh→infinite-regenerate loop: the moment we attach to a task_id the
 *  intent is considered honoured and removed. */
function _clearReportRegenIntent(key) {
  try { localStorage.removeItem(key || _REPORT_REGEN_INTENT_KEY); }
  catch (e) { console.warn('[Paper:Report] clear regen intent failed:', e); }
}

/** True when a pending regenerate intent matches the active paper + lang. */
function _hasReportRegenIntent(paperHash, lang, key) {
  var it = _getReportRegenIntent(key);
  return !!(it && it.paperHash && it.paperHash === paperHash
            && (it.lang || 'en') === (lang || 'en'));
}

/** Reset a view's stream state (called when switching paper / force regen).
 *  `view` defaults to the report view — the historical behaviour. */
function _resetReportLocalState(view) {
  view = view || _reportView('report');
  if (view.stream && view.stream.pollTimer) {
    clearTimeout(view.stream.pollTimer);
  }
  view.stream = null;
  view.meta = null;  // drop stale finish tag from the previous paper/run
  // Review: drop the per-paper translated reading view so it never leaks
  // across papers (the English review is reloaded from server cache on demand).
  if (view.kind === 'review') {
    _paperReviewShowTranslation = false;
    _paperReviewTranslatedText = '';
    _paperReviewTranslating = false;
  }
  // Flush the current reading session into the learning model (the report
  // we were reading is going away — switching paper / regenerating).
  if (typeof _teardownReadingTracker === 'function') _teardownReadingTracker(true);
}

/** Reset BOTH report + review view state. Used on paper switch / mode exit so
 *  neither view leaks across papers. */
function _resetAllReportViews() {
  _resetReportSnapshots();  // session snapshots are per-paper; drop on switch
  _resetReportLocalState(_reportView('report'));
  _resetReportLocalState(_reportView('review'));
}

function _makeReportStreamState(paperId, lang, taskId, kind) {
  return {
    paperId: paperId || '',
    lang: lang || 'en',
    kind: kind || 'report',
    taskId: taskId || '',
    cursor: 0,
    status: 'running',
    // Set when the user presses Stop BEFORE the /start round-trip has returned
    // a task_id (start can take 10–40s). Honoured the moment the task_id lands
    // so a Stop in that window is not silently dropped.
    pendingStop: false,
    fullText: '',
    thinkingText: '',
    toolRounds: [],      // chat-compatible: [{roundNum, toolName, query, toolCallId, toolArgs, status, toolContent, _elapsed}]
    contentStarted: false,
    meta: null,          // finish-tag {model, costCny, ...} from the done event
    error: '',
    pollTimer: null,
    pollBusy: false,
    _lastRenderedLen: -1,
    _lastRenderedStatus: '',
    _lastToolKey: '',
  };
}

/** Skeleton DOM that gets populated by event application. The inner element
 *  ids are PREFIXED per view (report→`reportToolZone`, review→`reviewToolZone`)
 *  so a report skeleton and a review skeleton can coexist in the DOM (only one
 *  tab is visible at a time, but both containers persist) without
 *  getElementById collisions. */
function _renderReportSkeleton(container, lang, view) {
  view = view || _reportView('report');
  var px = view.idPrefix;
  var genTxt = view.kind === 'review'
    ? (lang === 'zh' ? '正在生成评审…' : 'Generating review…')
    : (lang === 'zh' ? '正在生成报告…' : 'Generating report…');
  container.innerHTML =
    '<div class="paper-report-tools" id="' + px + 'ToolZone"></div>' +
    '<details class="paper-report-thinking" id="' + px + 'ThinkingBlock" open style="display:none">' +
      '<summary><span class="thinking-dot"></span>' +
        (lang === 'zh' ? '思考中…' : 'Thinking…') +
      '</summary>' +
      '<div class="paper-report-thinking-body" id="' + px + 'ThinkingBody"></div>' +
    '</details>' +
    '<div class="paper-report-body" id="' + px + 'BodyContent">' +
      '<div class="paper-loading"><div class="paper-loading-spinner"></div><div>' +
        genTxt +
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
        if (ev.results) r.results = ev.results;
        if (ev.searchDiag) r.searchDiag = ev.searchDiag;
        if (ev.engineBreakdown) r.engineBreakdown = ev.engineBreakdown;
        if (ev.vertical) r.vertical = ev.vertical;
        if (ev.verticals) r.verticals = ev.verticals;
      }
      return true;
    }

    case 'tool_progress': {
      var rp = null;
      for (var j = 0; j < s.toolRounds.length; j++) {
        if (s.toolRounds[j].roundNum === ev.roundNum) { rp = s.toolRounds[j]; break; }
      }
      if (rp) {
        if (typeof rp._partialOutput !== 'string') rp._partialOutput = '';
        rp._partialOutput += (ev.chunk || '');
      }
      return true;
    }

    case 'delta':
      s.fullText += (ev.delta || '');
      s.contentStarted = true;
      return true;

    case 'delta_reset':
      // The model emitted an interim draft alongside a tool call; the
      // backend discards it and will rewrite the full report after the tool
      // results land. Clear the accumulated text so the draft + final report
      // don't concatenate (report rendered twice).
      s.fullText = '';
      s.contentStarted = false;
      s._lastRenderedLen = -1;
      return true;

    case 'enriched':
      s.fullText = ev.text || s.fullText;
      // Only mutate global hash when this stream still belongs to the active paper —
      // a stream that was started for paper A and is now polling in the background
      // must not stomp paper B's hash.
      if (ev.paperHash && s.paperId === _activePaperId) _paperHash = ev.paperHash;
      return true;

    case 'done': {
      s.status = 'done';
      var _vDone = _reportView(s.kind);
      if (ev.report) {
        s.fullText = ev.report;
        if (s.paperId === _activePaperId) {
          _vDone.cache = ev.report;
          _rememberReportSnapshot(_vDone, ev.report, ev.meta || s.meta);
        }
      }
      if (ev.meta) {
        s.meta = ev.meta;
        if (s.paperId === _activePaperId) _vDone.meta = ev.meta;
      }
      if (ev.paperHash && s.paperId === _activePaperId) _paperHash = ev.paperHash;
      if (ev.resolvedTitle) _applyResolvedTitle(ev.resolvedTitle, s.paperId);
      return true;
    }

    case 'aborted':
      s.status = 'aborted';
      // Keep whatever partial text was produced so the user sees how far the
      // model got before they stopped it. The frontend renders it read-only
      // under a "stopped" banner (never persisted / cached).
      if (typeof ev.partial === 'string' && ev.partial) {
        s.fullText = ev.partial;
        s.contentStarted = true;
      }
      return true;

    case 'error':
      s.status = 'error';
      // ev.error is a typed error envelope dict from routes/paper.py.
      // Display surfaces use the short ``message`` field; keep the full
      // envelope on s._errorEnv for future kind-aware rendering.
      s._errorEnv = (typeof normalizeErrorEnvelope === 'function')
        ? normalizeErrorEnvelope(ev.error)
        : null;
      s.error = (typeof errorEnvelopeMessage === 'function'
                 ? errorEnvelopeMessage(ev.error) : '')
                || (typeof ev.error === 'string' ? ev.error : '')
                || 'Unknown error';
      return true;
  }
  return false;
}

/* ── Report render-layer enhancement ──────────────────────────────────
 * The report is plain Markdown rendered by renderMarkdown(). To make the
 * finished report richer WITHOUT moving layout responsibility onto the model
 * (which would break streaming, theming, caching and safety), we post-process
 * the rendered DOM: heading anchors, a sticky TOC sidebar, styled callout
 * boxes (blockquotes that open with a keyword) and framed figures.
 * Intermediate streaming frames stay as plain renderMarkdown() — enhancement
 * only runs on the final / cached render. */

// Order matters: multi-char / more-specific keywords (takeaway) are tested
// before the broad ones (important's bare "关键") so "关键结论：" classifies as
// a takeaway, not important. The trailing (?:[:：]|\b) accepts a colon (the
// form the prompt asks for, and the only thing that works after CJK since \b
// does not fire between two CJK chars) OR an ASCII word boundary (English
// keywords without a colon).
var _REPORT_CALLOUT_KEYWORDS = [
  { cls: 'takeaway', re: /^(key takeaway|takeaway|key point|key finding|summary|bottom line|关键结论|核心结论|要点|总结|小结)(?:[:：]|\b)/i },
  { cls: 'warning', re: /^(warning|caution|caveat|limitation|警告|注意|局限|风险)(?:[:：]|\b)/i },
  { cls: 'important', re: /^(important|critical|重要|关键)(?:[:：]|\b)/i },
  { cls: 'tip', re: /^(tip|pro tip|提示|建议)(?:[:：]|\b)/i },
  { cls: 'note', re: /^(note|nb|备注|说明)(?:[:：]|\b)/i },
];

function _slugifyHeading(text, used) {
  var base = String(text || '')
    .toLowerCase().trim()
    .replace(/[^\w\u4e00-\u9fff\s-]/g, '')
    .replace(/\s+/g, '-')
    .replace(/-+/g, '-')
    .replace(/^-|-$/g, '') || 'section';
  var slug = base, n = 2;
  while (used[slug]) { slug = base + '-' + n; n++; }
  used[slug] = true;
  return slug;
}

/** Decorate blockquotes that open with a keyword into themed callout boxes. */
function _decorateCallouts(article) {
  var quotes = article.querySelectorAll('blockquote');
  for (var i = 0; i < quotes.length; i++) {
    var bq = quotes[i];
    if (bq.closest('.paper-callout')) continue;
    var lead = (bq.textContent || '').trimStart();
    var match = null;
    for (var k = 0; k < _REPORT_CALLOUT_KEYWORDS.length; k++) {
      if (_REPORT_CALLOUT_KEYWORDS[k].re.test(lead)) { match = _REPORT_CALLOUT_KEYWORDS[k]; break; }
    }
    if (!match) continue;
    bq.classList.add('paper-callout', 'paper-callout-' + match.cls);
  }
}

/** Wrap image-only paragraphs into <figure> with a <figcaption>. */
function _frameFigures(article) {
  var imgs = article.querySelectorAll('img');
  for (var i = 0; i < imgs.length; i++) {
    var img = imgs[i];
    if (img.closest('figure')) continue;
    var p = img.closest('p');
    if (!p) continue;
    // Only wrap when the paragraph is essentially just the image (+ caption em)
    var hasOtherText = (p.textContent || '').trim().length > 0
      && !p.querySelector('em') && !img.getAttribute('alt');
    if (hasOtherText) continue;
    var fig = document.createElement('figure');
    fig.className = 'paper-figure';
    fig.appendChild(img.cloneNode(true));
    var capText = '';
    var em = p.querySelector('em');
    if (em && em.textContent.trim()) capText = em.textContent.trim();
    else if (img.getAttribute('alt')) capText = img.getAttribute('alt').trim();
    if (capText) {
      var cap = document.createElement('figcaption');
      cap.textContent = capText;
      fig.appendChild(cap);
    }
    p.parentNode.replaceChild(fig, p);
  }
}

/* ── Glossary hover-definitions ───────────────────────────────────────
 * The report opens with a "Core Terminology" table. A reader deep in the
 * Method or Experiments section has long forgotten those definitions, so the
 * report stops being self-contained exactly where it matters most. We parse
 * that table once, then turn LATER mentions of each term into a subtly
 * underlined span whose definition appears on hover/focus — no scrolling back.
 * To keep it from becoming visual noise we decorate each term at most once
 * per top-level (h2) section. */

/** Plain-text of a rendered table cell WITHOUT KaTeX's duplicated LaTeX
 *  source. KaTeX emits both a visual .katex-html tree and a hidden
 *  .katex-mathml annotation that carries the raw TeX; a naive textContent
 *  concatenates both (→ "T/F=生成长度/NFE\text{T/F}=…"). We clone the node,
 *  drop the mathml duplicates, then read textContent. */
function _cellPlainText(cell) {
  if (!cell) return '';
  var txt;
  try {
    var clone = cell.cloneNode(true);
    var dup = clone.querySelectorAll('.katex-mathml, annotation');
    for (var i = 0; i < dup.length; i++) {
      if (dup[i].parentNode) dup[i].parentNode.removeChild(dup[i]);
    }
    txt = clone.textContent || '';
  } catch (e) {
    txt = cell.textContent || '';
  }
  return txt.replace(/\s+/g, ' ').trim();
}

/** Find the "Core Terminology / 核心术语" table, tag it, and return its rows
 *  as [{term, def, defHtml}]. Returns [] when the report has no such table. */
function _extractGlossary(article) {
  var tables = article.querySelectorAll('table');
  for (var ti = 0; ti < tables.length; ti++) {
    var table = tables[ti];
    var head = table.querySelector('thead th, tr:first-child th');
    var first = (head && head.textContent || '').trim().toLowerCase();
    // The prompt fixes the first column header to "Term" (EN) / "术语" (ZH).
    if (first !== 'term' && first.indexOf('术语') < 0) continue;
    table.classList.add('paper-glossary');
    var rows = table.querySelectorAll('tbody tr');
    var out = [];
    for (var ri = 0; ri < rows.length; ri++) {
      var cells = rows[ri].querySelectorAll('td');
      if (cells.length < 2) continue;
      var term = (cells[0].textContent || '').trim();
      // The definition cell is part of the already-rendered report, so it may
      // hold live KaTeX / <strong> / <code> DOM. Capture its HTML so the hover
      // card can render it, and derive a CLEAN plain-text form for aria-label:
      // a naive textContent would garble math because KaTeX duplicates its
      // LaTeX source into a hidden .katex-mathml annotation node.
      var defCell = cells[1];
      var defHtml = (defCell.innerHTML || '').trim();
      var defText = _cellPlainText(defCell);
      if (!term || !defText) continue;
      // Skip the prompt's own placeholder rows: "(term)", "（术语）", "...".
      if (/^[(（].*[)）]$/.test(term) || term === '...' || term === '…') continue;
      if (defText.length > 260) defText = defText.slice(0, 257) + '…';
      out.push({ term: term, def: defText, defHtml: defHtml });
    }
    return out;  // only the first matching table
  }
  return [];
}

/** Expand a glossary term cell into matchable aliases, e.g.
 *  "Test-Time Scaling (TTS)" → ["Test-Time Scaling", "TTS"],
 *  "Best@K / Oracle Pass@K / Random@K" → [3 variants],
 *  "Agentic Rubrics（本文首创）" → ["Agentic Rubrics"] (meta note dropped). */
function _glossaryAliases(term) {
  var raw = [];
  raw.push(term);
  var base = term.replace(/[(（][^)）]*[)）]/g, '').trim();   // strip parentheticals
  raw.push(base);
  var paren = term.match(/[(（]([^)）]+)[)）]/);
  if (paren) {
    var inner = paren[1].trim();
    // Drop meta annotations the prompt may add; keep real abbreviations.
    if (!/本文首创|首创|借鉴|新增|高\/低|效用|introduced|borrowed|coined/i.test(inner)) raw.push(inner);
  }
  base.split(/\s*[\/、，]\s*/).forEach(function (p) { raw.push(p); });

  var seen = {}, out = [];
  for (var i = 0; i < raw.length; i++) {
    var a = (raw[i] || '').trim();
    if (!a) continue;
    var key = a.toLowerCase();
    if (seen[key]) continue;
    var hasCjk = /[\u3400-\u4dbf\u4e00-\u9fff]/.test(a);
    var isAbbrev = /^[A-Z0-9][A-Z0-9@+\-]{1,}$/.test(a);   // e.g. TTS, RL, Best@K
    // Length gate: CJK ≥2 chars, Latin ≥3 chars (abbreviations ≥2).
    if (hasCjk) { if (a.length < 2) continue; }
    else if (!isAbbrev && a.length < 3) continue;
    seen[key] = true;
    out.push(a);
  }
  return out;
}

function _escapeRegExp(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

/** Decorate later mentions of glossary terms with hover-definition spans. */
function _decorateGlossaryTerms(article, glossary) {
  if (!glossary || !glossary.length || typeof document === 'undefined') return;

  // Build alias → {row, def} map and a combined matcher (longest alias first
  // so "Oracle Pass@K" wins over a bare "Oracle").
  var map = {}, aliases = [];
  for (var r = 0; r < glossary.length; r++) {
    var al = _glossaryAliases(glossary[r].term);
    for (var j = 0; j < al.length; j++) {
      var key = al[j].toLowerCase();
      if (map[key]) continue;       // first row to claim an alias keeps it
      map[key] = { row: r, def: glossary[r].def, defHtml: glossary[r].defHtml };
      aliases.push(al[j]);
    }
  }
  if (!aliases.length) return;
  aliases.sort(function (a, b) { return b.length - a.length; });
  var re;
  try {
    re = new RegExp(aliases.map(_escapeRegExp).join('|'), 'gi');
  } catch (e) {
    console.warn('[Paper:Glossary] regex build failed:', e);
    return;
  }

  var SKIP = { H1: 1, H2: 1, H3: 1, H4: 1, H5: 1, H6: 1, CODE: 1, PRE: 1,
               A: 1, FIGCAPTION: 1, SCRIPT: 1, STYLE: 1, BUTTON: 1 };
  var seen = {};   // row → already decorated in the current section

  function decorateText(node) {
    var text = node.nodeValue;
    if (!text || text.length < 2 || !/\S/.test(text)) return;
    re.lastIndex = 0;
    var picks = [], m, pos = 0;
    while ((m = re.exec(text))) {
      var matched = m[0], idx = m.index, key = matched.toLowerCase();
      var entry = map[key];
      if (re.lastIndex === idx) re.lastIndex++;   // zero-width safety
      if (!entry || seen[entry.row]) continue;
      // Latin word-boundary guard (avoid matching inside a larger word).
      var headLatin = /[A-Za-z0-9]/.test(matched.charAt(0));
      var tailLatin = /[A-Za-z0-9]/.test(matched.charAt(matched.length - 1));
      if (headLatin && idx > 0 && /[A-Za-z0-9]/.test(text.charAt(idx - 1))) continue;
      if (tailLatin && /[A-Za-z0-9]/.test(text.charAt(idx + matched.length))) continue;
      if (idx < pos) continue;       // overlaps a prior pick
      picks.push({ idx: idx, len: matched.length, text: matched, def: entry.def, defHtml: entry.defHtml });
      seen[entry.row] = true;
      pos = idx + matched.length;
    }
    if (!picks.length) return;
    var frag = document.createDocumentFragment(), cursor = 0;
    for (var p = 0; p < picks.length; p++) {
      var pk = picks[p];
      if (pk.idx > cursor) frag.appendChild(document.createTextNode(text.slice(cursor, pk.idx)));
      var span = document.createElement('span');
      span.className = 'paper-term';
      span.setAttribute('tabindex', '0');
      span.setAttribute('aria-label', pk.text + ': ' + pk.def);
      span.appendChild(document.createTextNode(pk.text));
      // Real DOM hover card so the definition renders its Markdown/KaTeX
      // (a CSS attr(data-def) ::after can only show plain text). The HTML
      // came from the already-sanitized report body, so it's safe to reuse.
      var card = document.createElement('span');
      card.className = 'paper-term-card';
      card.setAttribute('aria-hidden', 'true');   // aria-label on the span already carries the def
      if (pk.defHtml) card.innerHTML = pk.defHtml;
      else card.textContent = pk.def;
      span.appendChild(card);
      frag.appendChild(span);
      cursor = pk.idx + pk.len;
    }
    if (cursor < text.length) frag.appendChild(document.createTextNode(text.slice(cursor)));
    node.parentNode.replaceChild(frag, node);
  }

  function walk(node) {
    var children = Array.prototype.slice.call(node.childNodes);
    for (var i = 0; i < children.length; i++) {
      var c = children[i];
      if (c.nodeType === 3) { decorateText(c); continue; }
      if (c.nodeType !== 1) continue;
      var tag = c.tagName;
      if (tag === 'H1' || tag === 'H2') seen = {};   // new section → re-allow terms
      if (SKIP[tag]) continue;
      if (c.classList && (c.classList.contains('paper-glossary') ||
          c.classList.contains('paper-term') || c.classList.contains('katex'))) continue;
      walk(c);
    }
  }

  try { walk(article); }
  catch (e) { console.warn('[Paper:Glossary] decoration failed:', e); }
}

/** Assign stable ids to h2/h3 and return the TOC entry list. */
function _indexHeadings(article) {
  var heads = article.querySelectorAll('h2, h3');
  var used = {}, entries = [];
  for (var i = 0; i < heads.length; i++) {
    var h = heads[i];
    var text = (h.textContent || '').trim();
    if (!text) continue;
    if (!h.id) h.id = 'report-' + _slugifyHeading(text, used);
    entries.push({ id: h.id, text: text, level: h.tagName === 'H3' ? 3 : 2 });
  }
  return entries;
}

function _buildReportTOC(entries) {
  if (entries.length < 3) return '';  // not worth a sidebar for a tiny report
  var label = (typeof _i18nLang !== 'undefined' && _i18nLang === 'zh') ? '目录' : 'Contents';
  var html = '<nav class="paper-report-toc" aria-label="' + label + '">'
    + '<div class="paper-report-toc-title">' + label + '</div><ul>';
  for (var i = 0; i < entries.length; i++) {
    var e = entries[i];
    html += '<li class="toc-l' + e.level + '"><a href="#' + e.id + '" data-target="' + e.id
      + '" onclick="_scrollReportToHeading(event,\'' + e.id + '\')">' + escapeHtml(e.text) + '</a></li>';
  }
  html += '</ul></nav>';
  return html;
}

function _scrollReportToHeading(ev, id) {
  if (ev) ev.preventDefault();
  var el = document.getElementById(id);
  if (!el) return;
  // Scroll ONLY the report's own scroll container. el.scrollIntoView() would
  // scroll every scrollable ancestor — including the outer overflow:hidden
  // containers (.paper-tab-panel / .paper-body / .paper-mode-container), which
  // are still programmatically scrollable. That pushes the .paper-tabs bar out
  // of view with no scrollbar to bring it back. Scroll the inner container
  // manually so the chrome above the report never moves.
  var scroller = el.closest('.paper-report-content, .paper-report-body');
  if (!scroller) { el.scrollIntoView({ behavior: 'smooth', block: 'start' }); return; }
  var TOP_MARGIN = 16;  // matches h2/h3 scroll-margin-top
  var target = scroller.scrollTop
    + (el.getBoundingClientRect().top - scroller.getBoundingClientRect().top) - TOP_MARGIN;
  scroller.scrollTo({ top: Math.max(0, target), behavior: 'smooth' });
}

/** Scroll-spy: highlight the TOC entry for the heading currently in view. */
function _wireReportScrollSpy(scrollEl, article, toc) {
  if (!scrollEl || !toc || typeof IntersectionObserver === 'undefined') return;
  var links = {};
  toc.querySelectorAll('a[data-target]').forEach(function(a) { links[a.getAttribute('data-target')] = a; });
  var heads = article.querySelectorAll('h2, h3');
  if (!heads.length) return;
  var visible = {};
  var obs = new IntersectionObserver(function(items) {
    items.forEach(function(it) { visible[it.target.id] = it.isIntersecting; });
    var firstActive = null;
    for (var i = 0; i < heads.length; i++) { if (visible[heads[i].id]) { firstActive = heads[i].id; break; } }
    Object.keys(links).forEach(function(k) { links[k].classList.toggle('active', k === firstActive); });
  }, { root: scrollEl, rootMargin: '0px 0px -70% 0px', threshold: 0 });
  for (var i = 0; i < heads.length; i++) obs.observe(heads[i]);
  // Stash so a later re-render can disconnect the stale observer.
  if (scrollEl._reportSpyObs) { try { scrollEl._reportSpyObs.disconnect(); } catch (e) {} }
  scrollEl._reportSpyObs = obs;
}

/** Build the citation-integrity card. Rendered ONLY when the server attached a
 *  `citationAudit` to the report meta — which it does ONLY when at least one
 *  cited identifier is suspicious. `unverifiable` entries are never surfaced
 *  here (they are coverage gaps, not hallucinations). Returns '' when absent.
 *  `audit` = {total, counts:{verified,suspicious,unverifiable}, suspicious:[…]} */
function _renderCitationAuditCard(audit) {
  if (!audit || !audit.suspicious || !audit.suspicious.length) return '';
  var zh = (typeof _i18nLang !== 'undefined' && _i18nLang === 'zh');
  var c = audit.counts || {};
  var n = audit.suspicious.length;
  var title = zh
    ? ('引用完整性警告 — ' + n + ' 条可疑引用')
    : ('Citation integrity — ' + n + ' suspicious reference' + (n === 1 ? '' : 's'));
  var sub = zh
    ? ('已核验 ' + (audit.total || 0) + ' 条引用标识符：' +
       (c.verified || 0) + ' 条确认存在、' + (c.suspicious || 0) + ' 条可疑、' +
       (c.unverifiable || 0) + ' 条无法核验（无法核验 ≠ 虚构）。')
    : ('Checked ' + (audit.total || 0) + ' cited identifiers: ' +
       (c.verified || 0) + ' verified, ' + (c.suspicious || 0) + ' suspicious, ' +
       (c.unverifiable || 0) + ' unverifiable (unverifiable ≠ fabricated).');
  var rows = audit.suspicious.map(function (it) {
    var idLabel = escapeHtml((it.kind || '') + ' ' + (it.identifier || ''));
    var reason = escapeHtml(it.reason || (zh ? '未能解析' : 'did not resolve'));
    var checked = it.checked
      ? ('<a class="paper-cite-checked" href="' + escapeHtml(it.checked) +
         '" target="_blank" rel="noopener noreferrer">' +
         escapeHtml(zh ? '查证来源' : 'checked source') + '</a>')
      : '';
    var titles = '';
    if (it.matchedTitle && it.claimedTitle) {
      titles = '<div class="paper-cite-titles">' +
        '<span class="paper-cite-claimed">' + escapeHtml(zh ? '声称：' : 'claimed: ') +
        escapeHtml(it.claimedTitle) + '</span>' +
        '<span class="paper-cite-matched">' + escapeHtml(zh ? '实际解析为：' : 'resolves to: ') +
        escapeHtml(it.matchedTitle) + '</span></div>';
    }
    return '<li class="paper-cite-item">' +
      '<code class="paper-cite-id">' + idLabel + '</code>' +
      '<span class="paper-cite-reason">' + reason + '</span>' +
      checked + titles + '</li>';
  }).join('');
  return '<aside class="paper-citation-audit" role="alert">' +
    '<div class="paper-cite-head">' +
      '<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>' +
      '<span class="paper-cite-title">' + escapeHtml(title) + '</span>' +
    '</div>' +
    '<p class="paper-cite-sub">' + escapeHtml(sub) + '</p>' +
    '<ul class="paper-cite-list">' + rows + '</ul>' +
    '</aside>';
}

/** Build the "finish tag" badge: which model generated the report + its cost.
 *  Visually subtle, sits at the END of the report so it never disrupts content.
 *  `meta` is the server-supplied dict ({model, costCny, costUsd, promptTokens,
 *  completionTokens, rounds, elapsedSec}). Returns '' when meta is absent. */
function _renderReportFinishTag(meta) {
  if (!meta || !meta.model) return '';
  var zh = (typeof _i18nLang !== 'undefined' && _i18nLang === 'zh');
  var parts = [];
  // Model — the headline of the tag.
  parts.push('<span class="paper-finish-model" title="' +
    escapeHtml(zh ? '生成本报告的模型' : 'Model that generated this report') + '">' +
    '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v1a3 3 0 0 0-3 3 3 3 0 0 0 0 6 3 3 0 0 0 3 3v1a3 3 0 0 0 6 0v-1a3 3 0 0 0 3-3 3 3 0 0 0 0-6 3 3 0 0 0-3-3V5a3 3 0 0 0-3-3z"/></svg>' +
    escapeHtml(meta.model) + '</span>');
  // Cost — prefer CNY (matches the rest of the app), fall back to USD.
  var costStr = '';
  if (typeof meta.costCny === 'number' && meta.costCny > 0) {
    costStr = (typeof formatCny === 'function') ? formatCny(meta.costCny)
      : ('¥' + meta.costCny.toFixed(4));
  } else if (typeof meta.costUsd === 'number' && meta.costUsd > 0) {
    costStr = '$' + meta.costUsd.toFixed(4);
  }
  if (costStr) {
    parts.push('<span class="paper-finish-cost" title="' +
      escapeHtml(zh ? '本次生成的预估费用' : 'Estimated cost of this generation') +
      '">' + escapeHtml(costStr) + '</span>');
  }
  // Tokens (compact) — secondary detail.
  var inTok = meta.promptTokens || 0;
  var outTok = meta.completionTokens || 0;
  if (inTok || outTok) {
    var fmt = function (n) {
      if (n >= 1000000) return (n / 1000000).toFixed(1) + 'M';
      if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
      return String(n);
    };
    parts.push('<span class="paper-finish-tokens" title="' +
      escapeHtml(zh ? '输入 / 输出 tokens' : 'input / output tokens') + '">' +
      fmt(inTok) + ' \u2192 ' + fmt(outTok) + ' tok</span>');
  }
  var label = zh ? '由以下模型生成' : 'Generated by';
  return '<div class="paper-report-finish-tag" role="contentinfo">' +
    '<span class="paper-finish-label">' + escapeHtml(label) + '</span>' +
    parts.join('') + '</div>';
}

/* ── Reading-time estimate + progress bar (Report tab) ───────────────────
 *
 * We show, sticky at the top of the rendered report, an estimated reading
 * time + a progress bar that fills as the user scrolls, with a live
 * "remaining time" readout.
 *
 * The estimate uses a LEARNING reading-speed model:
 *   • Cold start  → a sensible default WPM for dense technical prose.
 *   • Over time   → an exponentially-weighted moving average (EWMA) of the
 *     user's OBSERVED speed, measured from real reading sessions (words
 *     covered ÷ active time spent on the report). Persisted in localStorage,
 *     so it improves across papers and survives reloads.
 *
 * "Words" are counted in a script-aware way: CJK characters are counted
 * individually (people read CJK roughly per-character, much slower per
 * "word"), Latin words by whitespace runs. The model stores a single WPM in
 * a normalized Latin-word equivalent, and we convert CJK char counts to that
 * equivalent with a fixed ratio so one EWMA covers mixed-language reports.
 */

var _READ_SPEED_KEY = 'paper_reading_wpm_v1';
var _READ_WPM_DEFAULT = 220;   // dense technical prose, conservative
var _READ_WPM_MIN = 60;        // clamp learned speed to a sane band
var _READ_WPM_MAX = 700;
var _READ_EWMA_ALPHA = 0.25;   // weight of a fresh observation
// One CJK character ≈ this many Latin-word-equivalents (CJK is read slower
// per glyph than an English word, so a char is a fraction of a "word").
var _READ_CJK_CHAR_TO_WORD = 0.6;

// Live tracking state for the currently-displayed report.
var _readTracker = null;

/** Load the learned reading speed (Latin-word WPM). Falls back to default. */
function _loadReadingWpm() {
  try {
    var raw = localStorage.getItem(_READ_SPEED_KEY);
    if (!raw) return { wpm: _READ_WPM_DEFAULT, samples: 0 };
    var o = JSON.parse(raw);
    var wpm = Number(o && o.wpm);
    if (!isFinite(wpm) || wpm <= 0) return { wpm: _READ_WPM_DEFAULT, samples: 0 };
    return { wpm: Math.max(_READ_WPM_MIN, Math.min(_READ_WPM_MAX, wpm)),
             samples: (o && o.samples) | 0 };
  } catch (e) {
    console.warn('[Paper:ReadTime] load wpm failed:', e);
    return { wpm: _READ_WPM_DEFAULT, samples: 0 };
  }
}

/** Persist a new observation into the EWMA reading-speed model. */
function _recordReadingObservation(observedWpm) {
  if (!isFinite(observedWpm) || observedWpm <= 0) return;
  observedWpm = Math.max(_READ_WPM_MIN, Math.min(_READ_WPM_MAX, observedWpm));
  var cur = _loadReadingWpm();
  var next;
  if (cur.samples <= 0) {
    // First-ever real sample: blend gently with the default so one quirky
    // session can't swing the estimate wildly.
    next = _READ_WPM_DEFAULT * 0.5 + observedWpm * 0.5;
  } else {
    next = cur.wpm * (1 - _READ_EWMA_ALPHA) + observedWpm * _READ_EWMA_ALPHA;
  }
  next = Math.max(_READ_WPM_MIN, Math.min(_READ_WPM_MAX, next));
  try {
    localStorage.setItem(_READ_SPEED_KEY, JSON.stringify({
      wpm: Math.round(next), samples: cur.samples + 1, updatedAt: Date.now(),
    }));
  } catch (e) {
    console.warn('[Paper:ReadTime] persist wpm failed:', e);
  }
}

/** Count reading workload of an element as Latin-word equivalents. */
function _countReadingWords(el) {
  var text = '';
  if (el) {
    // Exclude glossary hover-card text: it's a duplicate of the report body
    // shown only on hover, so counting it would inflate the reading estimate.
    if (el.querySelector && el.querySelector('.paper-term-card')) {
      try {
        var clone = el.cloneNode(true);
        var cards = clone.querySelectorAll('.paper-term-card');
        for (var i = 0; i < cards.length; i++) {
          if (cards[i].parentNode) cards[i].parentNode.removeChild(cards[i]);
        }
        text = clone.textContent || '';
      } catch (e) { text = el.textContent || ''; }
    } else {
      text = el.textContent || '';
    }
  }
  if (!text) return 0;
  // CJK (incl. kana) — counted per character.
  var cjk = (text.match(/[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]/g) || []).length;
  // Strip CJK, then count Latin/numeric word runs.
  var latin = text.replace(/[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]/g, ' ');
  var latinWords = (latin.match(/[A-Za-z0-9][A-Za-z0-9'\u2019-]*/g) || []).length;
  return latinWords + cjk * _READ_CJK_CHAR_TO_WORD;
}

/** Format a minutes value into a localized human string. */
function _formatReadMinutes(min) {
  var _tt = (typeof t === 'function') ? t : function(k, p){ return k; };
  if (min < 1) return _tt('paper.readTimeLessMin');
  if (min < 60) return _tt('paper.readTimeMin', { n: Math.round(min) });
  var h = Math.floor(min / 60);
  var m = Math.round(min - h * 60);
  return _tt('paper.readTimeHour', { h: h, m: m });
}

/** Build the sticky reading-time header for a freshly rendered report.
 *  `article` is the rendered <article>; `scroller` is the scroll container.
 *  Returns the header element (not yet attached). */
function _buildReadingTimeBar(article, scroller) {
  var words = _countReadingWords(article);
  var model = _loadReadingWpm();
  var totalMin = words / model.wpm;
  var _tt = (typeof t === 'function') ? t : function(k, p){ return k; };

  var bar = document.createElement('div');
  bar.className = 'paper-read-time';
  bar.setAttribute('role', 'progressbar');
  bar.setAttribute('aria-valuemin', '0');
  bar.setAttribute('aria-valuemax', '100');

  var calib = (model.samples > 0)
    ? _tt('paper.readTimeAdapted', { wpm: Math.round(model.wpm) })
    : _tt('paper.readTimeDefault');

  bar.innerHTML =
    '<div class="paper-read-time-row">' +
      '<span class="paper-read-time-icon" title="' + escapeHtml(calib) + '">' +
        '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15.5 14"/></svg>' +
      '</span>' +
      '<span class="paper-read-time-total"></span>' +
      '<span class="paper-read-time-sep">·</span>' +
      '<span class="paper-read-time-left"></span>' +
      (model.samples > 0 ? '<span class="paper-read-time-badge" title="' + escapeHtml(calib) + '">' +
        '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg></span>' : '') +
    '</div>' +
    '<div class="paper-read-time-track"><div class="paper-read-time-fill"></div></div>';

  bar._readWords = words;
  bar._readTotalMin = totalMin;
  return bar;
}

/** Wire scroll → progress/remaining updates + the learning tracker.
 *  Disconnects any previous tracker. */
function _wireReadingTimeTracking(bar, scroller) {
  if (!bar || !scroller) return;
  // Tear down a previous session (flush its sample first).
  _teardownReadingTracker(true);

  var totalEl = bar.querySelector('.paper-read-time-total');
  var leftEl = bar.querySelector('.paper-read-time-left');
  var fillEl = bar.querySelector('.paper-read-time-fill');
  var totalMin = bar._readTotalMin || 0;
  var words = bar._readWords || 0;

  if (totalEl) totalEl.textContent =
    (typeof t === 'function' ? t('paper.readTimeTotal', { min: _formatReadMinutes(totalMin) })
                             : _formatReadMinutes(totalMin));

  var tracker = {
    bar: bar, scroller: scroller, words: words, totalMin: totalMin,
    lastProgress: 0,     // max scroll fraction reached [0..1]
    activeMs: 0,         // accumulated active reading time
    lastTickTs: 0,       // timestamp of last scroll/visibility tick while active
    flushed: false,
    onScroll: null,
    rafPending: false,
  };

  function _progressFraction() {
    var max = scroller.scrollHeight - scroller.clientHeight;
    if (max <= 0) return 1;  // whole report fits — nothing to scroll → fully "covered"
    return Math.max(0, Math.min(1, scroller.scrollTop / max));
  }

  function _paint(frac) {
    if (fillEl) fillEl.style.width = (frac * 100).toFixed(1) + '%';
    bar.setAttribute('aria-valuenow', Math.round(frac * 100));
    var remainMin = totalMin * (1 - frac);
    if (leftEl) {
      if (frac >= 0.999) {
        leftEl.textContent = (typeof t === 'function') ? t('paper.readTimeDone') : 'Finished';
        bar.classList.add('done');
      } else {
        bar.classList.remove('done');
        leftEl.textContent = (typeof t === 'function')
          ? t('paper.readTimeLeft', { min: _formatReadMinutes(remainMin) })
          : _formatReadMinutes(remainMin);
      }
    }
  }

  function _tick() {
    tracker.rafPending = false;
    var now = Date.now();
    var frac = _progressFraction();
    // Accumulate active time only across short gaps between scroll events
    // (a long idle gap = user stepped away / read elsewhere → don't count it).
    if (tracker.lastTickTs && (now - tracker.lastTickTs) < 12000) {
      tracker.activeMs += (now - tracker.lastTickTs);
    }
    tracker.lastTickTs = now;
    if (frac > tracker.lastProgress) tracker.lastProgress = frac;
    _paint(Math.max(frac, 0));
  }

  tracker.onScroll = function() {
    if (tracker.rafPending) return;
    tracker.rafPending = true;
    requestAnimationFrame(_tick);
  };

  scroller.addEventListener('scroll', tracker.onScroll, { passive: true });
  _readTracker = tracker;

  // Initial paint (report may already fit without scrolling).
  _paint(_progressFraction());
}

/** Flush the current reading session into the learning model and detach.
 *  Only records a sample when the session is substantial enough to be a
 *  meaningful signal (enough words covered + enough active time). */
function _teardownReadingTracker(silent) {
  var tk = _readTracker;
  _readTracker = null;
  if (!tk || tk.flushed) return;
  tk.flushed = true;
  try {
    if (tk.scroller && tk.onScroll) tk.scroller.removeEventListener('scroll', tk.onScroll);
  } catch (e) { /* detached node */ }

  var coveredWords = tk.words * tk.lastProgress;
  var activeMin = tk.activeMs / 60000;
  // Need a real session: covered ≥ ~120 word-equivalents over ≥ 20s of
  // active scrolling. Otherwise it's noise (a glance, an instant scroll-to-end).
  if (coveredWords >= 120 && activeMin >= (20 / 60)) {
    var observedWpm = coveredWords / activeMin;
    _recordReadingObservation(observedWpm);
    if (!silent) {
      console.debug('[Paper:ReadTime] session: %d words / %.2f min → %d wpm',
                    Math.round(coveredWords), activeMin, Math.round(observedWpm));
    }
  }
}

/** Render a FINAL report into `container`: markdown + TOC sidebar + callouts +
 *  framed figures + finish-tag badge. `container` is the scroll element
 *  (.paper-report-content or #reportBodyContent). `meta` (optional) drives the
 *  finish tag; defaults to the module-global `_paperReportMeta`.
 *  Safe to call repeatedly (full rebuild). */
function _renderFinalReport(container, text, meta, view) {
  if (!container) return;
  view = view || _reportView('report');
  if (typeof _syncReportToolbar === 'function') _syncReportToolbar(false, view);
  if (meta === undefined) meta = view.meta;
  if (typeof renderMarkdown !== 'function') {
    container.innerHTML = '<pre>' + escapeHtml(text || '') + '</pre>';
    return;
  }
  if (container._reportSpyObs) { try { container._reportSpyObs.disconnect(); } catch (e) {} container._reportSpyObs = null; }

  var article = document.createElement('article');
  article.className = 'paper-report-article';
  article.innerHTML = renderMarkdown(text || '');
  // Citation-integrity card — only present when the server flagged suspicious
  // citations (meta.citationAudit attached only in that case). Prepended so a
  // hallucinated-reference warning sits at the very top of the report.
  if (meta && meta.citationAudit) {
    var auditHtml = _renderCitationAuditCard(meta.citationAudit);
    if (auditHtml) article.insertAdjacentHTML('afterbegin', auditHtml);
  }
  _decorateCallouts(article);
  _frameFigures(article);
  _decorateGlossaryTerms(article, _extractGlossary(article));
  var finishTag = _renderReportFinishTag(meta);
  if (finishTag) {
    var tagWrap = document.createElement('div');
    tagWrap.innerHTML = finishTag;
    if (tagWrap.firstChild) article.appendChild(tagWrap.firstChild);
  }
  var entries = _indexHeadings(article);
  var tocHTML = _buildReportTOC(entries);

  container.classList.add('paper-report-enhanced');
  // Reading-time bar: sticky at the top of the scroll container, above the
  // doc/article. Built before mount so we can measure the article's word
  // count, then tracking is wired after the DOM is in place (so scrollHeight
  // is real).
  var readBar = _buildReadingTimeBar(article, container);
  if (tocHTML) {
    var doc = document.createElement('div');
    doc.className = 'paper-report-doc';
    doc.innerHTML = tocHTML;
    doc.appendChild(article);
    container.innerHTML = '';
    if (readBar) container.appendChild(readBar);
    container.appendChild(doc);
    _wireReportScrollSpy(container, article, doc.querySelector('.paper-report-toc'));
  } else {
    container.innerHTML = '';
    if (readBar) container.appendChild(readBar);
    container.appendChild(article);
  }
  if (readBar) _wireReadingTimeTracking(readBar, container);
}

/** Paint a view's tab DOM from its current stream state. `view` defaults to
 *  the report view (historical callers pass nothing). */
function _paintReportFromState(view) {
  view = view || _reportView('report');
  var container = document.getElementById(view.containerId);
  if (!container || !view.stream) return;
  var s = view.stream;
  var px = view.idPrefix;
  var retryFn = view.kind === 'review' ? '_generatePaperReview()' : '_generatePaperReport()';

  // Keep the toolbar's Stop/Regenerate affordance in sync with every paint.
  _syncReportToolbar(s.status === 'running', view);

  // Terminal: done → render the final, enhanced report (once).
  if (s.status === 'done' && s.fullText && !s.toolRounds.some(r => r.status === 'searching')) {
    if (s._lastRenderedLen !== s.fullText.length || s._lastRenderedStatus !== 'done') {
      _renderFinalReport(container, s.fullText, undefined, view);
      s._lastRenderedLen = s.fullText.length;
      s._lastRenderedStatus = 'done';
    }
    return;
  }

  // Terminal: aborted → freeze the partial report (if any) under a "stopped"
  // banner. Never persisted; a Regenerate is required to produce a full report.
  if (s.status === 'aborted') {
    if (s._lastRenderedStatus !== 'aborted') {
      var bannerHtml =
        '<div class="paper-report-stopped-banner">' +
          '<svg width="15" height="15" viewBox="0 0 24 24" fill="currentColor" stroke="none"><rect x="6" y="6" width="12" height="12" rx="2.5"/></svg>' +
          '<span>' + escapeHtml((typeof t === 'function') ? t('paper.reportStopped') : 'Generation stopped') + '</span>' +
        '</div>';
      if (s.fullText && s.contentStarted) {
        container.innerHTML = bannerHtml +
          '<div class="paper-report-body">' +
            (typeof renderMarkdown === 'function' ? renderMarkdown(s.fullText) : '<pre>' + escapeHtml(s.fullText) + '</pre>') +
          '</div>';
      } else {
        container.innerHTML =
          '<div class="paper-report-empty">' + bannerHtml +
            '<p class="paper-report-hint">' + escapeHtml((typeof t === 'function') ? t('paper.reportStoppedHint') : 'Click Regenerate to start over') + '</p>' +
          '</div>';
      }
      s._lastRenderedStatus = 'aborted';
    }
    return;
  }

  // Ensure skeleton exists
  if (!document.getElementById(px + 'ToolZone')) {
    _renderReportSkeleton(container, s.lang, view);
  }

  // Tool rounds — reuse chat's unified renderer for identical look & feel
  var toolZone = document.getElementById(px + 'ToolZone');
  if (toolZone) {
    var toolCount = s.toolRounds.length;
    var searchingCount = s.toolRounds.filter(r => r.status === 'searching').length;
    var toolKey = toolCount + ':' + searchingCount;
    if (s._lastToolKey !== toolKey) {
      if (toolCount > 0 && typeof renderToolRoundsHTML === 'function') {
        toolZone.innerHTML = renderToolRoundsHTML(s.toolRounds, s.status === 'running');
      } else {
        toolZone.innerHTML = '';
      }
      s._lastToolKey = toolKey;
    }
  }

  // Thinking
  if (s.thinkingText) {
    var thBlock = document.getElementById(px + 'ThinkingBlock');
    var thBody = document.getElementById(px + 'ThinkingBody');
    if (thBlock) {
      thBlock.style.display = '';
      if (s.contentStarted) thBlock.open = false;
    }
    if (thBody && thBody.textContent.length !== s.thinkingText.length) {
      thBody.textContent = s.thinkingText;
      thBody.scrollTop = thBody.scrollHeight;
    }
  }

  // Report body — only re-render when content actually changed
  var bodyEl = document.getElementById(px + 'BodyContent');
  if (bodyEl) {
    if (s.contentStarted) {
      if (s._lastRenderedLen !== s.fullText.length) {
        bodyEl.innerHTML = typeof renderMarkdown === 'function' ? renderMarkdown(s.fullText) : '<pre>' + escapeHtml(s.fullText) + '</pre>';
        s._lastRenderedLen = s.fullText.length;
      }
    } else if (s.status === 'error' && !s.fullText) {
      bodyEl.innerHTML = '<div class="paper-error">' + escapeHtml(s.error || 'Failed') +
        '<br><button onclick="' + retryFn + '" class="paper-retry-btn">' + escapeHtml((typeof t === 'function') ? t('paper.retry') : 'Retry') + '</button></div>';
    }
    // Otherwise keep the loading spinner from the skeleton
  }
}

/** Poll /api/paper/report/poll once; schedule next if still running.
 *  Parameterized by `view` so ONE poll loop drives both Report and Review
 *  tabs (shared engine/endpoints; only cache key, state slots, DOM differ). */
async function _pollReportTask(view) {
  view = view || _reportView('report');
  var s = view.stream;
  if (!s || !s.taskId) return;
  if (s.pollBusy) return;
  s.pollBusy = true;
  try {
    var resp = await Api.paper.reportPoll(s.taskId, s.cursor);
    if (!resp || !resp.ok) {
      if (resp && resp.status === 404) {
        // Task expired or server restarted
        s.status = 'error';
        s.error = 'Task no longer available on server. Please regenerate.';
        _paintReportFromState(view);
        return;
      }
      throw new Error('HTTP ' + resp.status);
    }
    var data = await resp.json();
    if (!data.ok) {
      s.status = 'error';
      s.error = (typeof errorEnvelopeMessage === 'function'
                 ? errorEnvelopeMessage(data.error) : '')
                || (typeof data.error === 'string' ? data.error : '')
                || 'Poll failed';
      _paintReportFromState(view);
      return;
    }

    // Apply new events
    var events = data.events || [];
    for (var i = 0; i < events.length; i++) {
      _applyReportEvent(s, events[i]);
    }
    s.cursor = data.next_cursor;

    // Any terminal status means a regenerate (if one was pending) has been
    // honoured end-to-end — clear the intent. (The attach point already
    // cleared it; this is a defensive backstop in case attach was skipped.)
    if (data.status === 'done' || data.status === 'aborted' || data.status === 'error') {
      _clearReportRegenIntent(view.regenIntentKey);
    }

    // Update status from server authoritative status
    if (data.status === 'done') {
      s.status = 'done';
      if (data.report) {
        s.fullText = data.report;
        // Only persist into the global cache + library entry when this poll's
        // stream is still bound to the active paper. Otherwise we'd overwrite
        // a different paper's report (e.g. user regenerated paper A then
        // switched to paper B before the task finished).
        if (s.paperId === _activePaperId) {
          view.cache = data.report;
          if (data.meta) { s.meta = data.meta; view.meta = data.meta; }
          _rememberReportSnapshot(view, data.report, data.meta);
          _saveActivePaperState();
        }
      }
      if (data.resolvedTitle) _applyResolvedTitle(data.resolvedTitle, s.paperId);
    } else if (data.status === 'aborted') {
      s.status = 'aborted';
      if (typeof data.partial === 'string' && data.partial) {
        s.fullText = data.partial;
        s.contentStarted = true;
      }
    } else if (data.status === 'error') {
      s.status = 'error';
      s.error = (typeof errorEnvelopeMessage === 'function'
                 ? errorEnvelopeMessage(data.error) : '')
                || (typeof data.error === 'string' ? data.error : '')
                || s.error;
    }

    // Only repaint DOM when the user is actually on this paper (and this tab)
    if (s.paperId === _activePaperId) {
      _paintReportFromState(view);
    }

    // Schedule next poll if still running
    if (s.status === 'running') {
      s.pollTimer = setTimeout(function() { _pollReportTask(view); }, 1200);
    }
  } catch (e) {
    console.warn('[Paper:Report] Poll failed:', e);
    // Transient network error — retry with backoff
    if (s && s.status === 'running') {
      s.pollTimer = setTimeout(function() { _pollReportTask(view); }, 3000);
    }
  } finally {
    s.pollBusy = false;
  }
}

/** Start (or join) a server-side report task, begin polling. Parameterized by
 *  `view` so the Review tab reuses this verbatim (only cache key / state slots
 *  / DOM container differ). */
async function _generatePaperReport(force, view) {
  view = view || _reportView('report');
  var container = document.getElementById(view.containerId);
  if (!container) return;
  var langKey = view.langKey();
  var retryFn = view.kind === 'review' ? '_generatePaperReview()' : '_generatePaperReport()';

  // Snapshot which paper this generation is for. If the user switches paper
  // mid-await, every continuation below must bail — otherwise paper A's
  // task_id / report / hash leak into paper B's state. (See bug 2026-05-20.)
  var startPaperId = _activePaperId;

  // Already polling a live task for this paper and not forcing → just paint
  if (!force && view.stream
      && view.stream.paperId === _activePaperId
      && view.stream.status === 'running') {
    _paintReportFromState(view);
    return;
  }

  // In-memory cache — instant path
  if (view.cache && !force) {
    _renderFinalReport(container, view.cache, undefined, view);
    // Reopen restore: re-apply a persisted Chinese reading view (translate-only,
    // never regenerates the English review).
    _restoreReviewReadingLang(view);
    return;
  }

  if (!_paperParsedText) {
    container.innerHTML =
      '<div class="paper-loading"><div class="paper-loading-spinner"></div>' +
      '<div>Recovering paper text…</div></div>';
    var ok = await _ensurePaperText();
    if (_activePaperId !== startPaperId) return;
    if (!ok) {
      container.innerHTML =
        '<div class="paper-report-empty"><p>' + escapeHtml((typeof t === 'function') ? t('paper.reportNoText') : 'No paper text available.') + '</p>' +
        '<p style="opacity:0.6;font-size:12px;margin-top:6px">The PDF may be scanned/image-only, or parsing failed. Try re-uploading.</p></div>';
      return;
    }
  }

  var reportLang = view.uiLang();
  if (!view.model) _populatePaperReportModelDropdown(view);
  var reportModel = view.model || null;

  // Discard any prior stream state (force path or new paper path)
  if (force || (view.stream && view.stream.paperId !== _activePaperId)) {
    _resetReportLocalState(view);
  }

  _renderReportSkeleton(container, reportLang, view);

  // Show the Stop affordance IMMEDIATELY. The /start round-trip below does
  // synchronous server prep (image-manifest extraction, injection sanitize,
  // prompt build) that legitimately runs 10–40s before it returns a task_id.
  // Without a provisional running stream, only Regenerate shows during that
  // whole window — so a user who picked the wrong model has NO way to stop
  // (exactly the reported bug). A Stop pressed now sets `pendingStop`, honoured
  // the instant the task_id lands.
  view.stream = _makeReportStreamState(startPaperId, reportLang, '', view.kind);
  _syncReportToolbar(true, view);

  try {
    // Title fallback: paper_library may not have been upserted yet (the PUT
    // is fire-and-forget) so the server can't always look up the title from
    // paper_hash. Send the active entry's title (without `.pdf`) so the
    // backend can still prepend `# Title` even when the DB is empty.
    var entryNow = _getActivePaperEntry();
    var clientTitle = (entryNow && entryNow.title)
      || _paperFileName
      || (_paperPdfFilename || '').replace(/^\d+_/, '');
    if (clientTitle) clientTitle = String(clientTitle).replace(/\.pdf$/i, '').trim();

    // Images are loaded from the server-side manifest by paper_hash —
    // the client doesn't forward them. filename is a fallback path the
    // server can use if no manifest exists yet (rare). `lang` carries the
    // composite key for reviews (``review:<venue>:<uilang>``), opaquely.
    var data = await Api.paper.reportStart({
      paper_text: _paperParsedText,
      lang: langKey,
      model: reportModel,
      force: !!force,
      title: clientTitle || '',
      filename: _paperPdfFilename || '',
    });
    if (_activePaperId !== startPaperId) return;
    if (!data || !data.ok) throw new Error((data && data.error) || 'Start failed');

    // Did the user press Stop while the (slow) /start was in flight? The
    // provisional stream created above carries the intent forward.
    var stopWasPending = !!(view.stream && view.stream.pendingStop);

    // DB cache hit — done in one round-trip. Drop the provisional running
    // stream first so a later tab re-entry paints the cached report, not a
    // stuck empty skeleton left behind by the provisional (taskId-less) state.
    if (data.cached && data.report) {
      view.stream = null;
      view.cache = data.report;
      view.meta = data.meta || null;
      if (data.paper_hash) _paperHash = data.paper_hash;
      _rememberReportSnapshot(view, data.report, data.meta);
      _saveActivePaperState();
      if (data.resolvedTitle) _applyResolvedTitle(data.resolvedTitle, startPaperId);
      _renderFinalReport(container, data.report, undefined, view);
      return;
    }

    // Task started (or joined) — begin polling from cursor 0 so we replay all
    if (data.paper_hash) _paperHash = data.paper_hash;
    // Re-entrancy guard: a task is now attached, so the regenerate intent is
    // fulfilled. Clear it so a refresh can't re-trigger an endless regenerate.
    _clearReportRegenIntent(view.regenIntentKey);
    view.stream = _makeReportStreamState(startPaperId, reportLang, data.task_id, view.kind);
    _syncReportToolbar(true, view);
    _pollReportTask(view);
    // Honour a Stop pressed before the task_id existed: now that we know the
    // id, abort the just-started task instead of silently dropping the intent.
    if (stopWasPending) {
      console.warn('[Paper:Report] Stop was pending during start — aborting task ' + data.task_id);
      _stopPaperReport(view);
    }

  } catch (e) {
    if (_activePaperId !== startPaperId) return;
    console.warn('[Paper:Report] start failed:', e);
    // Drop the provisional running stream so the error state is terminal — a
    // later tab re-entry must not resume-poll a task_id-less zombie stream.
    view.stream = null;
    // Reset the toolbar to a clean Regenerate-only state. Without this, a Stop
    // pressed during the (now-failed) start left the Stop button disabled with
    // the "Stopping…" label (via pendingStop) — the poll loop's terminal
    // repaint never runs on a start FAILURE, so the toolbar would stay stuck.
    _syncReportToolbar(false, view);
    container.innerHTML = '<div class="paper-error">Failed: ' + escapeHtml(e.message) +
      '<br><button onclick="' + retryFn + '" class="paper-retry-btn">' + escapeHtml((typeof t === 'function') ? t('paper.retry') : 'Retry') + '</button></div>';
  }
}

/** Review-Mode entry: identical pipeline, review view-context. */
async function _generatePaperReview(force) {
  return _generatePaperReport(force, _reportView('review'));
}


/** The review reading language for the ACTIVE paper: persisted per-paper if
 *  present, else 'en' (English is always the canonical generated language). */
function _activeReviewLang() {
  if (!_activePaperId) return 'en';
  var stored = _readReviewLangMap()[_activePaperId];
  return (stored === 'zh') ? 'zh' : 'en';
}

/** Read the per-paper review-reading-language map { paperId: 'en'|'zh' }. */
function _readReviewLangMap() {
  try {
    var raw = localStorage.getItem(_PAPER_REVIEW_LANG_KEY);
    return raw ? (JSON.parse(raw) || {}) : {};
  } catch (e) {
    console.warn('[Paper:Review] read lang map failed:', e);
    return {};
  }
}

/** Persist the review reading language for the active paper. */
function _persistReviewLang(paperId, lang) {
  if (!paperId || (lang !== 'en' && lang !== 'zh')) return;
  try {
    var map = _readReviewLangMap();
    map[paperId] = lang;
    localStorage.setItem(_PAPER_REVIEW_LANG_KEY, JSON.stringify(map));
  } catch (e) {
    console.warn('[Paper:Review] persist lang failed:', e);
  }
}

/** After the canonical ENGLISH review is (re)rendered on a REOPEN (page reload
 *  or paper switch — both wipe the in-memory translation state via
 *  _resetAllReportViews), restore the per-paper persisted READING language: if
 *  the user last read this paper's review in Chinese, re-apply the translated
 *  view.
 *
 *  This ONLY translates the already-rendered English review — on a warm cache
 *  it is instant, on a cold cache it kicks the Babel translate task. It NEVER
 *  regenerates the English review (no report/start) and is invoked ONLY from
 *  terminal English-render sites (cache hits), so it never fights an in-flight
 *  generation. */
function _restoreReviewReadingLang(view) {
  if (!view || view.kind !== 'review') return;
  if (!view.cache) return;                    // no English review to translate
  if (_activeReviewLang() !== 'zh') return;   // last read in English → nothing to do
  // _setReviewLang('zh') translates the current English cache (cache-hit →
  // instant); it never issues a review generation request.
  _setReviewLang('zh');
}

/** Set the review READING language (bidirectional, always available — NOT
 *  gated on the app UI language).
 *
 *  English is always the canonical generated/cached/exported review (the
 *  language the authors/AC read). Selecting '中' shows an on-demand translated
 *  reading view (produced by the Babel translate task under a DISTINCT
 *  composite key ``review:<venue>:zh`` so it never collides with the
 *  whole-paper Babel cache) and caches it client-side for instant re-toggle;
 *  selecting 'EN' restores the canonical English render. The choice is
 *  persisted per paper so re-opening the Review tab restores the last language. */
async function _setReviewLang(lang) {
  if (lang !== 'en' && lang !== 'zh') return;
  var view = _reportView('review');
  var container = document.getElementById(view.containerId);
  if (!container) return;
  var english = view.cache;
  if (_activePaperId) _persistReviewLang(_activePaperId, lang);

  // English → restore the canonical render.
  if (lang === 'en') {
    _paperReviewShowTranslation = false;
    if (english) _renderFinalReport(container, english, view.meta, view);
    _syncReviewTranslateBtn();
    return;
  }

  if (!english) { _syncReviewTranslateBtn(); return; }  // nothing generated yet

  // Chinese, already translated → instant.
  if (_paperReviewTranslatedText) {
    _paperReviewShowTranslation = true;
    _renderFinalReport(container, _paperReviewTranslatedText, view.meta, view);
    _syncReviewTranslateBtn();
    return;
  }

  if (_paperReviewTranslating) return;
  _paperReviewTranslating = true;
  _syncReviewTranslateBtn();

  // Distinct cache key: venue + target lang, so the translated review is
  // cached per (paper, venue, target-lang) and never collides with the Babel
  // whole-paper translation cache keyed on the bare lang. Target is always
  // 'zh' (English is canonical, so we only ever translate INTO Chinese).
  var trKey = 'review:' + (_paperReviewVenue || 'generic') + ':zh';
  var startPaperId = _activePaperId;

  try {
    // (1) Client-server cache hit → instant.
    if (_paperHash) {
      try {
        var cd = await Api.paper.translateCache(_paperHash, trKey);
        if (cd && cd.ok && cd.text) {
          if (_activePaperId !== startPaperId) return;
          _paperReviewTranslatedText = cd.text;
          _paperReviewShowTranslation = true;
          _renderFinalReport(container, cd.text, view.meta, view);
          return;
        }
      } catch (e) { console.warn('[Paper:Review] translate cache lookup failed:', e); }
    }

    // (2) Start (or join) the translate task on the ENGLISH review markdown.
    var startData = await Api.paper.translateStart({
      paper_text: english, lang: trKey, paper_hash: _paperHash || '',
    });
    if (!startData || !startData.ok) throw new Error((startData && startData.error) || 'translate start failed');
    if (startData.cached && startData.text) {
      if (_activePaperId !== startPaperId) return;
      _paperReviewTranslatedText = startData.text;
      _paperReviewShowTranslation = true;
      _renderFinalReport(container, startData.text, view.meta, view);
      return;
    }
    if (startData.paper_hash) _paperHash = startData.paper_hash;
    var taskId = startData.task_id;
    if (!taskId) throw new Error('translate task returned no task_id');

    // (3) Poll to completion. Reuses the Babel event schema (chunk/done/error).
    var cursor = 0;
    var parts = [];
    while (true) {
      if (_activePaperId !== startPaperId) { try { await Api.paper.translateAbort(taskId); } catch (_) {} return; }
      var pollResp = await Api.paper.translatePoll(taskId, cursor);
      if (!pollResp || !pollResp.ok) throw new Error('poll HTTP ' + (pollResp ? pollResp.status : 'none'));
      var pollData = await pollResp.json();
      if (!pollData.ok) throw new Error(pollData.error || 'poll failed');
      cursor = pollData.next_cursor || cursor;
      var events = pollData.events || [];
      for (var ei = 0; ei < events.length; ei++) {
        var ev = events[ei];
        if (ev.type === 'chunk') {
          parts.push(ev.text || '');
        } else if (ev.type === 'done') {
          if (_activePaperId !== startPaperId) return;
          _paperReviewTranslatedText = ev.text || parts.join('\n\n');
          _paperReviewShowTranslation = true;
          _renderFinalReport(container, _paperReviewTranslatedText, view.meta, view);
          return;
        } else if (ev.type === 'error') {
          var m = (typeof errorEnvelopeMessage === 'function') ? errorEnvelopeMessage(ev.error)
            : (typeof ev.error === 'string' ? ev.error : '');
          throw new Error(m || 'translation failed');
        }
      }
      if (pollData.status === 'error') throw new Error('translation failed');
      if (pollData.status === 'done' && !events.length) return;
      await new Promise(function(r) { setTimeout(r, 700); });
    }
  } catch (e) {
    console.warn('[Paper:Review] translate failed:', e);
    if (typeof showToast === 'function') {
      showToast((typeof t === 'function') ? t('paper.reviewTranslateFailed') : 'Translation failed', 'error');
    }
  } finally {
    _paperReviewTranslating = false;
    _syncReviewTranslateBtn();
  }
}

/** Back-compat: flip the review reading language to the OTHER one. Retained so
 *  any external caller keeps working; the UI now uses the EN/中 segmented
 *  control (_setReviewLang) directly. */
function _toggleReviewTranslation() {
  return _setReviewLang(_paperReviewShowTranslation ? 'en' : 'zh');
}

/** Sync the review EN/中 segmented control: active option reflects the current
 *  reading language; the '中' option shows a spinner + is disabled while a
 *  translation is in flight. The control is ALWAYS available (both directions),
 *  independent of the app UI language — it's only disabled until a review
 *  exists to read. */
function _syncReviewTranslateBtn() {
  var wrap = document.getElementById('reviewLangToggle');
  if (!wrap) return;
  var view = _reportView('review');
  var hasReview = !!view.cache;
  var cur = _activeReviewLang();
  wrap.style.opacity = hasReview ? '' : '0.5';
  wrap.querySelectorAll('.paper-report-lang-opt').forEach(function(btn) {
    var isZh = btn.dataset.lang === 'zh';
    btn.classList.toggle('active', btn.dataset.lang === cur);
    // Disable interactions until a review exists; disable '中' while translating.
    btn.disabled = !hasReview || (isZh && _paperReviewTranslating);
    if (isZh) {
      btn.classList.toggle('loading', !!_paperReviewTranslating);
      btn.title = _paperReviewTranslating
        ? ((typeof t === 'function') ? t('paper.reviewTranslating') : 'Translating…')
        : ((typeof t === 'function') ? t('paper.reviewTranslateTitle') : 'Read in Chinese');
    }
  });
}

/** Called when the user opens the Report tab. Priority:
 *   1. Have stream state for active paper → paint + resume poll if running.
 *   2. Look up server-side running task by paper_hash → attach + poll.
 *   3. Try DB cache lookup.
 *   4. Start a new task.
 */
async function _loadOrGenerateReport(view) {
  view = view || _reportView('report');
  var reportLang = view.uiLang();
  var langKey = view.langKey();
  var startPaperId = _activePaperId;

  // (1) Existing local stream state for this paper
  if (view.stream && view.stream.paperId === _activePaperId) {
    _paintReportFromState(view);
    if (view.stream.status === 'running' && !view.stream.pollTimer) {
      _pollReportTask(view);
    } else if (view.stream.status === 'done') {
      // Terminal English render on tab re-entry — re-apply a persisted Chinese
      // reading view (translate-only; cached translation is instant, never
      // regenerates). Guarded to 'done' so it never fires mid-generation.
      _restoreReviewReadingLang(view);
    }
    return;
  }

  // (1.5) Pending regenerate intent — MUST take priority over the step-2
  // lookup-reconnect. The user clicked Regenerate and a refresh interrupted
  // the force /start before the new task registered. The OLD task is still
  // RUNNING (cooperative abort) and the dedup index (paper_hash, lang) still
  // points to it, so step-2's lookup would re-attach to exactly the task the
  // user asked to REPLACE — silently swallowing the regenerate. Re-issuing
  // force /start is the right move: it atomically aborts the old task AND
  // starts a fresh one. _generatePaperReport(true) clears the intent the
  // moment it attaches the new task_id (re-entrancy guard against a
  // refresh→endless-regenerate loop).
  if (_hasReportRegenIntent(_paperHash, reportLang, view.regenIntentKey)) {
    console.warn('[Paper:Report] pending regenerate intent for hash=' + _paperHash
                 + ' lang=' + reportLang + ' kind=' + view.kind
                 + ' — resuming force-start (priority over lookup-reconnect)');
    _generatePaperReport(true, view);
    return;
  }

  // (2) Server-side task lookup (survives chat-mode round-trips). Uses the
  // composite langKey so a review reconnects to the review task, not the
  // plain report.
  if (_paperHash) {
    try {
      var lookupData = await Api.paper.reportLookup(_paperHash, langKey);
      if (_activePaperId !== startPaperId) return;
      if (lookupData && lookupData.ok && lookupData.task_id
          && (lookupData.status === 'running' || lookupData.status === 'pending')) {
        // Attach to the running server-side task
        var container = document.getElementById(view.containerId);
        if (container) _renderReportSkeleton(container, reportLang, view);
        view.stream = _makeReportStreamState(startPaperId, reportLang, lookupData.task_id, view.kind);
        _syncReportToolbar(true, view);
        _pollReportTask(view);
        return;
      }
    } catch (e) {
      if (_activePaperId !== startPaperId) return;
      console.warn('[Paper:Report] lookup failed (non-fatal):', e);
    }
  }

  // (3) Try server DB cache by hash (avoids re-sending text)
  try {
    var cacheBody = { lang: langKey };
    if (_paperHash) cacheBody.paper_hash = _paperHash;
    else cacheBody.paper_text = _paperParsedText;
    var cacheData = await Api.paper.reportCache(cacheBody);
    if (_activePaperId !== startPaperId) return;
    if (cacheData && cacheData.ok && cacheData.report) {
      view.cache = cacheData.report;
      view.meta = cacheData.meta || null;
      if (cacheData.paper_hash) _paperHash = cacheData.paper_hash;
      _rememberReportSnapshot(view, cacheData.report, cacheData.meta);
      _saveActivePaperState();
      var c2 = document.getElementById(view.containerId);
      if (c2) _renderFinalReport(c2, cacheData.report, undefined, view);
      // Reopen restore: if the user last read THIS review in Chinese, re-apply
      // the translated view (translate-only, never regenerates the English).
      _restoreReviewReadingLang(view);
      return;
    }
  } catch (e) {
    if (_activePaperId !== startPaperId) return;
    console.warn('[Paper:Report] Cache lookup failed:', e);
  }

  // (4) No cache, no running task — DO NOT auto-start. Show a manual-start
  // prompt so the user can first adjust the model / language / venue in the
  // toolbar, then click Generate. (User preference: never auto-generate on
  // tab open, otherwise the settings can't be tuned before the run begins.)
  if (_activePaperId !== startPaperId) return;
  _renderReportStartPrompt(view);
}

/** Manual-start prompt: rendered when a paper is loaded but no report/review
 *  exists yet (no cache, no running task). The toolbar (model / language /
 *  venue) is already visible and tunable; the user clicks Generate to begin.
 *  This is the seam that keeps generation user-initiated. */
function _renderReportStartPrompt(view) {
  view = view || _reportView('report');
  var container = document.getElementById(view.containerId);
  if (!container) return;
  _syncReportToolbar(false, view);
  var isReview = view.kind === 'review';
  var genFn = isReview ? '_generatePaperReview()' : '_generatePaperReport()';
  var _tt = (typeof t === 'function') ? t : function(k) { return k; };
  var title = _tt(isReview ? 'paper.reviewEmptyTitle' : 'paper.reportEmptyTitle');
  var hint = _tt(isReview ? 'paper.reviewEmptyHint' : 'paper.reportEmptyHint');
  var btnLabel = _tt(isReview ? 'paper.reviewGenerate' : 'paper.reportGenerate');
  container.innerHTML =
    '<div class="paper-report-empty">' +
      '<p>' + escapeHtml(title) + '</p>' +
      '<p class="paper-report-hint">' + escapeHtml(hint) + '</p>' +
      '<button class="paper-report-generate-btn" onclick="' + genFn + '">' +
        '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 3l14 9-14 9V3z"/></svg>' +
        '<span>' + escapeHtml(btnLabel) + '</span>' +
      '</button>' +
    '</div>';
}

/** Review-Mode entry: load-or-generate against the review view-context. */
async function _loadOrGenerateReview() {
  return _loadOrGenerateReport(_reportView('review'));
}


// ── Report Model Picker ──

/** Populate a view's model dropdown from _registeredModels (set by main.js).
 *  `view` defaults to the report view (historical callers pass nothing). */
function _populatePaperReportModelDropdown(view) {
  view = view || _reportView('report');
  var dropdown = document.getElementById(view.modelDropdownId);
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

  // No "Default (auto)" option — generation should always use a specific,
  // user-visible model. When nothing has been chosen yet, default to the
  // model the user picked in the frontend toolbar preset (config.model, then
  // the configured serverModel), so Reading/Review Mode stays consistent with
  // the rest of the app. Fall back to the first visible chat model only when
  // that preset isn't among the available chat models.
  if (!view.model && chatModels.length > 0) {
    var availableIds = {};
    for (var ci = 0; ci < chatModels.length; ci++) availableIds[chatModels[ci].model_id] = true;
    var preset = (typeof config !== 'undefined' && config && config.model)
      ? config.model
      : ((typeof serverModel !== 'undefined' && serverModel) ? serverModel : '');
    var seed = (preset && availableIds[preset]) ? preset : chatModels[0].model_id;
    _selectPaperReportModel(seed, view);
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
      item.className = 'paper-report-model-dropdown-item' + (mod.model_id === view.model ? ' active' : '');
      var shortName = (typeof _modelShortName === 'function') ? _modelShortName(mod.model_id) : mod.model_id;
      item.textContent = shortName;
      item.title = mod.model_id;
      (function(mid) {
        item.onclick = function() { _selectPaperReportModel(mid, view); };
      })(mod.model_id);
      dropdown.appendChild(item);
    }
  }
}

function _selectPaperReportModel(modelId, view) {
  view = view || _reportView('report');
  view.model = modelId || '';
  // Update label — always show the actual model, never "Default"
  var label = document.getElementById(view.modelLabelId);
  if (label) {
    if (modelId) {
      label.textContent = (typeof _modelShortName === 'function') ? _modelShortName(modelId) : modelId;
    } else {
      // No model available (empty model list) — keep the button usable.
      label.textContent = (typeof t === 'function') ? t('paper.reportSelectModel') : 'Select model';
    }
  }
  // Close dropdown
  var dropdown = document.getElementById(view.modelDropdownId);
  if (dropdown) dropdown.classList.remove('open');
  // Update active state
  var items = dropdown ? dropdown.querySelectorAll('.paper-report-model-dropdown-item') : [];
  items.forEach(function(it) { it.classList.toggle('active', it.title === modelId); });
}

function _togglePaperReportModelDropdown(e, view) {
  e.stopPropagation();
  view = view || _reportView('report');
  var dropdown = document.getElementById(view.modelDropdownId);
  if (!dropdown) return;
  var isOpen = dropdown.classList.contains('open');
  if (!isOpen) _populatePaperReportModelDropdown(view);
  dropdown.classList.toggle('open');
}

/** Review-Mode model dropdown toggle (inline onclick passes the event). */
function _togglePaperReviewModelDropdown(e) {
  return _togglePaperReportModelDropdown(e, _reportView('review'));
}

// Close model dropdowns on outside click (both views).
document.addEventListener('click', function() {
  ['paperReportModelDropdown', 'paperReviewModelDropdown'].forEach(function(id) {
    var dropdown = document.getElementById(id);
    if (dropdown) dropdown.classList.remove('open');
  });
});

// Click-to-enlarge for figures/tables embedded in the paper report. CSS
// already shows ``cursor:zoom-in`` on these images; this handler wires
// them up to the shared fullscreen overlay used by image-gen.
document.addEventListener('click', function(e) {
  var img = e.target;
  if (!img || img.tagName !== 'IMG') return;
  if (!img.closest('.paper-report-body, .paper-report-content')) return;
  if (typeof _openImageFullscreen === 'function') {
    _openImageFullscreen(img.src);
  }
});


/** Show the Stop button while a report task is running; otherwise show
 *  Regenerate. Mirrors the chat composer's send↔stop morph so the affordance
 *  is consistent across the app. `running` defaults to the live stream status. */
function _syncReportToolbar(running, view) {
  view = view || _reportView('report');
  if (running === undefined) {
    running = !!(view.stream && view.stream.status === 'running');
  }
  var stopBtn = document.getElementById(view.stopBtnId);
  var regenBtn = document.getElementById(view.regenBtnId);
  if (stopBtn) {
    stopBtn.style.display = running ? '' : 'none';
    if (running) {
      // Restore the resting label/enabled state for a fresh run (a prior
      // run may have left it disabled + "Stopping…").
      stopBtn.disabled = false;
      var lbl = stopBtn.querySelector('span');
      if (lbl) lbl.textContent = (typeof t === 'function') ? t('paper.reportStop') : 'Stop';
    }
  }
  if (regenBtn) regenBtn.style.display = running ? 'none' : '';
  // Keep the EN/中 segmented control in sync on every paint (both views).
  if (typeof _syncReportLangToggle === 'function') _syncReportLangToggle(view);
  // Review-only: a fresh run invalidates any cached translation reading view;
  // keep the translate toggle's state in sync on every paint.
  if (view.kind === 'review') {
    if (running) {
      _paperReviewShowTranslation = false;
      _paperReviewTranslatedText = '';
    }
    if (typeof _syncReviewTranslateBtn === 'function') _syncReviewTranslateBtn();
  }
}

/** Stop an in-flight generation. Signals the server-side task to abort
 *  (best-effort) and reflects the stopping state immediately; the `aborted`
 *  terminal event then arrives via the normal poll loop and freezes whatever
 *  partial output was produced. */
function _stopPaperReport(view) {
  view = view || _reportView('report');
  var s = view.stream;
  if (!s || s.status !== 'running') return;
  var stopBtn = document.getElementById(view.stopBtnId);
  if (stopBtn) {
    stopBtn.disabled = true;
    var lbl = stopBtn.querySelector('span');
    if (lbl) lbl.textContent = (typeof t === 'function') ? t('paper.reportStopping') : 'Stopping…';
  }
  // Stop pressed while /start is still in flight (no task_id yet): record the
  // intent so the post-start attach can abort the task the instant its id is
  // known. Without this the Stop is silently dropped and the task runs on.
  if (!s.taskId) {
    s.pendingStop = true;
    return;
  }
  Api.paper.reportAbort(s.taskId).catch(function(e) {
    console.warn('[Paper:Report] stop request failed:', e);
  });
  // Don't flip status locally — the server emits the authoritative `aborted`
  // event, which the poll loop applies and repaints. Re-enable the label on
  // the next paint cycle so a failed abort doesn't leave the button stuck.
}

function _stopPaperReview() { return _stopPaperReport(_reportView('review')); }

async function _regeneratePaperReport(view) {
  view = view || _reportView('report');
  // Atomic regenerate: the backend's force=true /start aborts the old task
  // AND registers the new task_id over the dedup index in ONE transaction
  // (routes/paper.py). So we do NOT fire a separate /abort — that split the
  // "stop + restart" into two fire-and-forget requests, and a refresh between
  // them left the old task half-aborted with no new task registered, so
  // re-entry reverted to the stale DB cache (orphan running task).
  var reportLang = view.uiLang();
  // ★ Persist the regenerate INTENT synchronously, BEFORE the await below, so
  //   a refresh that interrupts the force /start round-trip still resumes the
  //   regenerate on re-entry instead of showing the old report.
  _setReportRegenIntent(_paperHash, reportLang, view.regenIntentKey);
  _resetReportLocalState(view);
  view.cache = '';
  await _generatePaperReport(true, view);
}

async function _regeneratePaperReview() { return _regeneratePaperReport(_reportView('review')); }

function _copyPaperReport(view) {
  view = view || _reportView('report');
  if (!view.cache) return;
  navigator.clipboard.writeText(view.cache).then(function() { debugLog((typeof t === 'function') ? t('paper.reportCopied') : 'Copied', 'success'); });
}

function _copyPaperReview() { return _copyPaperReport(_reportView('review')); }

function _togglePaperReportExportMenu(ev, view) {
  if (ev) { ev.preventDefault(); ev.stopPropagation(); }
  view = view || _reportView('report');
  var dd = document.getElementById(view.exportDropdownId);
  if (!dd) return;
  var menuSel = '#' + view.exportMenuId;
  var willOpen = !dd.classList.contains('open');
  dd.classList.toggle('open', willOpen);
  if (willOpen) {
    var closeOnClick = function(e) {
      if (!dd.contains(e.target) && !e.target.closest(menuSel)) {
        dd.classList.remove('open');
        document.removeEventListener('click', closeOnClick, true);
      }
    };
    setTimeout(function() { document.addEventListener('click', closeOnClick, true); }, 0);
  }
}

function _togglePaperReviewExportMenu(ev) { return _togglePaperReportExportMenu(ev, _reportView('review')); }

/** Export the report/review. format ∈ {'md','html','pdf'}. Defaults to 'md'.
 *  All rendering happens server-side via /api/paper/report/export so the
 *  same Markdown→HTML pipeline serves both download and live view, and
 *  there's no client/server skew. PDF is rendered by the browser's
 *  built-in print engine over the server-generated HTML. The composite
 *  langKey routes a review export to the stored review row. */
function _exportPaperReport(format, view) {
  view = view || _reportView('report');
  if (!_paperHash) {
    debugLog('No report to export yet', 'warning');
    return;
  }
  var dd = document.getElementById(view.exportDropdownId);
  if (dd) dd.classList.remove('open');
  format = format || 'md';
  var url = Api.paper.exportUrl(_paperHash, view.langKey(), format);

  if (format === 'pdf') {
    // Server returns inline HTML with an embedded window.print() bootstrap
    // that fires after all images load. The user picks "Save as PDF" in
    // their browser's print dialog. (Returning Content-Disposition:
    // attachment for HTML would download the file instead of opening it,
    // so the format=pdf path is explicitly served inline by the server.)
    var w = window.open(url, '_blank');
    if (!w) {
      debugLog('Pop-up blocked — please allow pop-ups to print/export PDF', 'warning');
    }
    return;
  }

  // Markdown / HTML — direct browser download via Content-Disposition.
  var a = document.createElement('a');
  a.href = url;
  a.rel = 'noopener';
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
}

function _exportPaperReview(format) { return _exportPaperReport(format, _reportView('review')); }

// ── Review venue picker ──

/** Read the per-paper venue map { paperId: venueKey } from localStorage. */
function _readVenueMap() {
  try {
    var raw = localStorage.getItem(_PAPER_REVIEW_VENUE_KEY);
    return raw ? (JSON.parse(raw) || {}) : {};
  } catch (e) {
    console.warn('[Paper:Review] read venue map failed:', e);
    return {};
  }
}

/** Persist the venue the user picked for the active paper, so re-opening the
 *  Review tab (or hard-refreshing) restores it instead of snapping back to the
 *  first venue — same durability the model selection gets. */
function _persistReviewVenue(paperId, venueKey) {
  if (!paperId || !venueKey) return;
  try {
    var map = _readVenueMap();
    map[paperId] = venueKey;
    localStorage.setItem(_PAPER_REVIEW_VENUE_KEY, JSON.stringify(map));
  } catch (e) {
    console.warn('[Paper:Review] persist venue failed:', e);
  }
}

/** Fetch the venue list (once) into _paperReviewVenues. Idempotent — a second
 *  call after the list is cached resolves immediately with no network. */
async function _ensureReviewVenues() {
  if (_paperReviewVenues.length) return _paperReviewVenues;
  try {
    // Api.paper.reviewVenues() uses the default JSON parse, so it resolves to
    // the parsed {ok, venues} body — NOT a raw Response. (Calling .json() on
    // it threw, silently leaving the venue list empty → dropdown unavailable.)
    var data = await Api.paper.reviewVenues();
    if (data && data.ok && Array.isArray(data.venues)) _paperReviewVenues = data.venues;
  } catch (e) {
    console.warn('[Paper:Review] venue fetch failed:', e);
  }
  return _paperReviewVenues;
}

/** Resolve the venue to use for the ACTIVE paper BEFORE any generation. Order:
 *  (1) an explicit in-session choice; (2) the persisted per-paper venue;
 *  (3) the first venue in the registry. Returns the resolved key (also sets
 *  _paperReviewVenue + the dropdown label) so the displayed venue and the
 *  generation langKey are ALWAYS consistent — closing the first-entry race
 *  where label said NeurIPS but the key was the generic fallback. */
async function _resolveReviewVenue() {
  await _ensureReviewVenues();
  if (!_paperReviewVenues.length) return _paperReviewVenue;  // nothing to pick from
  if (!_paperReviewVenue) {
    var stored = _readVenueMap()[_activePaperId];
    var valid = stored && _paperReviewVenues.some(function(v) { return v.key === stored; });
    _selectReviewVenue(valid ? stored : _paperReviewVenues[0].key, true);
  }
  return _paperReviewVenue;
}

/** Populate the review venue dropdown from /api/paper/review/venues (single
 *  source of truth: REVIEW_VENUES in lib/paper/review.py). Cached after first
 *  fetch. Auto-selects the first venue if none is chosen yet. */
async function _populateReviewVenueDropdown() {
  var dropdown = document.getElementById('paperReviewVenueDropdown');
  if (!dropdown) return;
  // Resolve the per-paper venue (persisted → first) BEFORE rendering so the
  // label and the generation langKey agree from the very first paint.
  await _resolveReviewVenue();
  dropdown.innerHTML = '';
  for (var i = 0; i < _paperReviewVenues.length; i++) {
    var v = _paperReviewVenues[i];
    var item = document.createElement('div');
    item.className = 'paper-report-model-dropdown-item' + (v.key === _paperReviewVenue ? ' active' : '');
    item.textContent = v.name;
    item.title = v.key;
    (function(key) { item.onclick = function() { _selectReviewVenue(key); }; })(v.key);
    dropdown.appendChild(item);
  }
}

/** Select a review venue. `silent` skips the label/dropdown DOM churn during
 *  auto-init. Changing the venue changes the composite cache key, so a fresh
 *  review is loaded/generated for the new venue (each venue cached separately). */
function _selectReviewVenue(key, silent) {
  var changed = (_paperReviewVenue !== key);
  _paperReviewVenue = key || '';
  // Persist per-paper ONLY for an EXPLICIT user choice (not the silent
  // auto-default). The auto-default is re-derived identically on every entry
  // (first-in-registry), so persisting it would (a) pin every merely-viewed
  // paper to the current default and (b) write localStorage on every tab open.
  // We only want to remember a venue the user deliberately picked.
  if (key && _activePaperId && !silent) _persistReviewVenue(_activePaperId, key);
  var label = document.getElementById('paperReviewVenueLabel');
  if (label) {
    var found = _paperReviewVenues.find(function(v) { return v.key === key; });
    label.textContent = found ? found.name : ((typeof t === 'function') ? t('paper.reviewSelectVenue') : 'Select venue');
  }
  var dropdown = document.getElementById('paperReviewVenueDropdown');
  if (dropdown) {
    dropdown.classList.remove('open');
    dropdown.querySelectorAll('.paper-report-model-dropdown-item').forEach(function(it) {
      it.classList.toggle('active', it.title === key);
    });
  }
  // A real venue switch (not the silent auto-init) re-loads the review for the
  // newly-selected venue — its cache key differs, so this never clobbers
  // another venue's stored review.
  if (changed && !silent && _paperActiveTab === 'review') {
    _resetReportLocalState(_reportView('review'));
    _paperReviewCache = '';
    _loadOrGenerateReview();
  }
}

function _toggleReviewVenueDropdown(e) {
  if (e) e.stopPropagation();
  var dropdown = document.getElementById('paperReviewVenueDropdown');
  if (!dropdown) return;
  var isOpen = dropdown.classList.contains('open');
  if (!isOpen) _populateReviewVenueDropdown();
  dropdown.classList.toggle('open');
}

// Close the venue dropdown on outside click.
document.addEventListener('click', function() {
  var dd = document.getElementById('paperReviewVenueDropdown');
  if (dd) dd.classList.remove('open');
});

// ══════════════════════════════════════════════════════
//  ★ Tab 3: Babel PDF (Translation)
// ══════════════════════════════════════════════════════

var _babelTargetLang = '';
var _babelTranslatedPages = {};
var _babelTranslating = false;

function _initBabelPdfTab() {
  var container = document.getElementById('paperTranslateContent');
  if (!container) return;
  var _ttb = (typeof t === 'function') ? t : function(k){ return k; };
  container.innerHTML =
    '<div class="babel-pdf-module">' +
      '<div class="babel-pdf-brand">' +
        '<svg class="babel-pdf-icon" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' +
          '<path d="M5 8l6 6"/><path d="M4 14l6-6 2-3"/><path d="M2 5h12"/><path d="M7 2v3"/>' +
          '<path d="M22 22l-5-10-5 10"/><path d="M14 18h6"/>' +
        '</svg>' +
        '<div class="babel-pdf-brand-text"><span class="babel-pdf-title">Babel PDF</span><span class="babel-pdf-subtitle">' + escapeHtml(_ttb('paper.babelSubtitle')) + '</span></div>' +
      '</div>' +
      '<div class="babel-pdf-lang-bar">' +
        '<button class="babel-pdf-lang' + (!_babelTargetLang ? ' active' : '') + '" data-lang="" onclick="_switchBabelLang(\'\', this)">' + escapeHtml(_ttb('paper.babelOriginal')) + '</button>' +
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
          '<p>' + escapeHtml(_ttb('paper.babelEmptyTitle')) + '</p>' +
          '<p class="babel-pdf-hint">' + escapeHtml(_ttb('paper.babelEmptyHint')) + '</p>' +
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

  var _ttb = (typeof t === 'function') ? t : function(k){ return k; };
  var langNames = { zh: '中文', en: 'English', ja: '日本語' };
  if (!_babelTargetLang) {
    body.innerHTML = '<div class="babel-pdf-empty"><svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.4"><path d="M5 8l6 6"/><path d="M4 14l6-6 2-3"/><path d="M2 5h12"/><path d="M7 2v3"/><path d="M22 22l-5-10-5 10"/><path d="M14 18h6"/></svg><p>' + escapeHtml(_ttb('paper.babelEmptyTitle')) + '</p><p class="babel-pdf-hint">' + escapeHtml(_ttb('paper.babelEmptyHint')) + '</p></div>';
    if (status) status.textContent = '';
    return;
  }

  if (!_paperParsedText) {
    body.innerHTML = '<div class="babel-pdf-empty"><p>' + escapeHtml(_ttb('paper.babelNoPaper')) + '</p></div>';
    return;
  }

  // Check cache
  if (_babelTranslatedPages[_babelTargetLang]) {
    _renderBabelResult(_babelTranslatedPages[_babelTargetLang]);
    if (status) status.textContent = _ttb('paper.babelCompleteCached');
    return;
  }

  var _langLabel = langNames[_babelTargetLang] || _babelTargetLang;
  var _translatingMsg = _ttb('paper.babelTranslatingTo', { lang: _langLabel });
  if (status) status.textContent = _translatingMsg;

  body.innerHTML = '<div class="paper-loading"><div class="paper-loading-spinner"></div><div>' + escapeHtml(_translatingMsg) + '</div><div class="babel-pdf-progress"><div class="babel-pdf-progress-bar" id="babelProgressBar" style="width:0%"></div></div></div>';

  _babelTranslateAllPages(_babelTargetLang);
}

/** Server-owned translation: chunking, retry, persistence, dedup all live
 *  on the backend. The frontend just kicks off the task and polls events. */
async function _babelTranslateAllPages(lang) {
  if (_babelTranslating) return;
  _babelTranslating = true;

  var bar = document.getElementById('babelProgressBar');
  var statusEl = document.getElementById('babelPdfStatus');

  function _setProgress(done, total) {
    if (bar && total > 0) bar.style.width = Math.round((done / total) * 100) + '%';
    if (statusEl) statusEl.textContent = (typeof t === 'function') ? t('paper.babelTranslatedCount', { done: done, total: total }) : ('Translated ' + done + '/' + total + ' sections');
  }

  try {
    // (1) Try server-side cache first — instant if the same translation was
    //     done before (even on a different machine).
    if (_paperHash) {
      try {
        var cacheData = await Api.paper.translateCache(_paperHash, lang);
        if (cacheData && cacheData.ok && cacheData.text) {
          if (_babelTargetLang === lang) {
            _babelTranslatedPages[lang] = cacheData.text;
            _renderBabelResult(cacheData.text);
            _saveActivePaperState();
            if (statusEl) statusEl.textContent = (typeof t === 'function') ? t('paper.babelCompleteCached') : 'Translation complete (cached)';
          }
          return;
        }
      } catch (e) {
        console.warn('[Babel] Cache lookup failed:', e);
      }
    }

    // (2) Start (or join) the server task.
    var startData = await Api.paper.translateStart({
      paper_text: _paperParsedText,
      lang: lang,
      paper_hash: _paperHash || '',
    });
    if (!startData || !startData.ok) throw new Error((startData && startData.error) || 'Translate start failed');

    if (startData.cached && startData.text) {
      if (_babelTargetLang === lang) {
        _babelTranslatedPages[lang] = startData.text;
        _renderBabelResult(startData.text);
        _saveActivePaperState();
        if (statusEl) statusEl.textContent = (typeof t === 'function') ? t('paper.babelCompleteCached') : 'Translation complete (cached)';
      }
      return;
    }

    if (startData.paper_hash) _paperHash = startData.paper_hash;
    var taskId = startData.task_id;
    if (!taskId) throw new Error('Translate task did not return task_id');

    // (3) Poll until the task completes (or the user switches language).
    var cursor = 0;
    var aggregated = [];
    while (true) {
      if (_babelTargetLang !== lang) {
        // User switched away — abort the server task to free resources.
        try {
          await Api.paper.translateAbort(taskId);
        } catch (_) {}
        return;
      }
      var pollResp = await Api.paper.translatePoll(taskId, cursor);
      if (!pollResp || !pollResp.ok) throw new Error('Poll HTTP ' + (pollResp ? pollResp.status : 'no response'));
      var pollData = await pollResp.json();
      if (!pollData.ok) throw new Error(pollData.error || 'Poll failed');
      cursor = pollData.next_cursor || cursor;

      var events = pollData.events || [];
      for (var ei = 0; ei < events.length; ei++) {
        var ev = events[ei];
        if (ev.type === 'chunk') {
          aggregated.push(ev.text || '');
          _setProgress(ev.index + 1, ev.total);
          if (_babelTargetLang === lang) {
            _renderBabelResult(aggregated.join('\n\n'));
          }
        } else if (ev.type === 'done') {
          if (_babelTargetLang === lang) {
            _babelTranslatedPages[lang] = ev.text || aggregated.join('\n\n');
            _renderBabelResult(_babelTranslatedPages[lang]);
            _saveActivePaperState();
            if (statusEl) statusEl.textContent = (typeof t === 'function') ? t('paper.babelComplete') : 'Translation complete';
          }
          return;
        } else if (ev.type === 'error') {
          var _evMsg = (typeof errorEnvelopeMessage === 'function')
            ? errorEnvelopeMessage(ev.error)
            : (typeof ev.error === 'string' ? ev.error : '');
          throw new Error(_evMsg || 'Translation failed');
        }
      }

      if (pollData.status === 'done') return;
      if (pollData.status === 'error') {
        var _pdMsg = (typeof errorEnvelopeMessage === 'function')
          ? errorEnvelopeMessage(pollData.error)
          : (typeof pollData.error === 'string' ? pollData.error : '');
        throw new Error(_pdMsg || 'Translation failed');
      }

      await new Promise(function(r) { setTimeout(r, 700); });
    }
  } catch (e) {
    console.warn('[Babel] Translation failed:', e);
    var body = document.getElementById('babelPdfBody');
    var _ttf = (typeof t === 'function') ? t : function(k){ return k; };
    if (body && _babelTargetLang === lang) {
      body.innerHTML = '<div class="paper-error">' + escapeHtml(_ttf('paper.babelFailed')) + ': ' +
                       escapeHtml(e.message || String(e)) +
                       '<br><button class="paper-retry-btn" onclick="_startBabelTranslation()">' + escapeHtml(_ttf('paper.retry')) + '</button></div>';
    }
    if (statusEl) statusEl.textContent = _ttf('paper.babelFailed');
  } finally {
    _babelTranslating = false;
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

// Flush an in-progress reading session into the learning model if the user
// closes / reloads the tab while still reading the report.
window.addEventListener('beforeunload', function() {
  if (typeof _teardownReadingTracker === 'function') _teardownReadingTracker(true);
});

// When KaTeX finishes lazy-loading after the report/QA already painted
// math-pending fallback markup, repaint these surfaces so inline formulas
// stop showing as gray <code> spans. (Chat is repainted by core.js itself.)
window.addEventListener('katex:loaded', function() {
  if (!paperMode) return;
  // Report + Review tabs — prefer the stream-driven painter if a stream
  // exists (handles tool rounds / thinking too). Reset dedup markers so the
  // next paint actually re-renders the body.
  ['report', 'review'].forEach(function(kind) {
    var view = _reportView(kind);
    if (view.stream) {
      view.stream._lastRenderedLen = -1;
      view.stream._lastRenderedStatus = '';
      if (typeof _paintReportFromState === 'function') _paintReportFromState(view);
    } else {
      var rc = document.getElementById(view.containerId);
      if (rc && view.cache) {
        _renderFinalReport(rc, view.cache, undefined, view);
      }
    }
  });
  // QA tab.
  if (typeof _renderPaperQA === 'function') _renderPaperQA();
});

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
