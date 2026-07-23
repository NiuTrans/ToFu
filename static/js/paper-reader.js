/* ═══════════════════════════════════════════
   paper-reader.js — Paper Reading Mode v3

   Layout:  Sidebar  = paper library (persistent)
            Main L   = PDF (vertical scroll, largest)
            Main R   = Q&A / Report / Babel PDF
   ═══════════════════════════════════════════ */

// ── State ──
var paperMode = false;
var _paperDescribeDraft = '';  // persists the landing "describe it" textarea across mode switches
var _paperPdfUrl = '';
var _paperFileName = '';
var _paperParsedText = '';
var _paperArxivId = '';
var _paperPdfDoc = null;
var _paperTotalPages = 0;
var _paperScale = 1.5;
var _paperCurrentUrl = '';          // resolved URL of the doc currently loaded (for {data} re-open)
var _paperViaData = false;          // true once the doc was (re)opened via the range-bypass {data} path
var _paperRenderToken = 0;          // bumped each _renderAllPages() so a stale in-flight loop self-cancels
var _paperLoadGen = 0;              // bumped each _loadPaperPdf() so a stale/slow load can't clobber a newer sidebar selection
var _paperIntersectionObserver = null;  // lazy-raster trigger for virtualized pages
var _paperReopenInFlight = false;   // single-flight guard for the raster-failure {data} re-open
/** Monotonic-ish clock for the load/render instrumentation (falls back to Date.now). */
function _paperNow() {
  try { return (typeof performance !== 'undefined' && performance.now) ? performance.now() : Date.now(); }
  catch (_) { return Date.now(); }
}
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
var _lastArxivSearchQuery = '';  // query for the last rendered candidate list (katex:loaded repaint)

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
// Rebuttal follow-up is a THIRD consumer of the same report engine: given the
// existing review + the author's pasted rebuttal, it produces a reviewer
// follow-up reply + a structured score-adjustment decision. It shares the
// venue (a rebuttal is always against the same venue's review) and is cached
// under the sibling composite key ``rebuttal:<venue>:<uilang>``. Its four
// state slots mirror the report/review ones so the shared poll loop drives it.
var _paperRebuttalCache = '';
var _paperRebuttalMeta = null;
var _paperRebuttalStream = null;
var _paperRebuttalModel = '';
var _paperRebuttalInputText = '';  // author rebuttal text the user pasted (per paper)
var _REBUTTAL_REGEN_INTENT_KEY = 'paper_rebuttal_regen_intent';
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
// Per-(paper, view-language) reading position, persisted so re-opening the
// Report/Review tab (tab switch OR hard refresh) restores where the reader
// left off instead of snapping to the top. Keyed by the SAME composite key as
// the report snapshots (``<paperId>::<langKey>``) so every language of every
// paper — and reports vs reviews vs venues — keeps its own place. The stored
// value is a ``_captureReadingAnchor`` anchor ({index,offset} or {frac}).
var _PAPER_READ_POS_KEY = 'paper_read_pos_by_key';

// View-context registry: the single seam that lets one set of report
// functions drive two independent tabs. Each context exposes the per-view
// DOM ids, the regenerate-intent localStorage key, the cache-key resolver,
// and get/set accessors onto that view's four state globals (so there is
// exactly ONE storage location per view — no mirrored copies that can drift).
/** Rehydrate the rebuttal sub-panel on Review-tab entry: fill the paste box
 *  from the per-paper persisted text, then resume-poll or repaint any existing
 *  rebuttal stream / in-memory cache so a tab switch or reload keeps the
 *  follow-up reply + decision on screen. Cheap + idempotent. */
function _restoreRebuttalPanel() {
  try {
    var ta = document.getElementById('paperRebuttalInput');
    var stored = '';
    try {
      var raw = localStorage.getItem('paper_rebuttal_text_by_id');
      var map = raw ? JSON.parse(raw) : {};
      stored = (_activePaperId && map[_activePaperId]) || '';
    } catch (e) { stored = ''; }
    _paperRebuttalInputText = stored;
    if (ta) ta.value = stored;
    var rv = _reportView('rebuttal');
    if (rv.stream && rv.stream.status === 'running') {
      // Only resume-poll when no poll chain is already live for this stream.
      // Without this guard, every Review↔Rebuttal segment toggle (or tab
      // re-entry) while a follow-up is generating stacks ANOTHER setTimeout
      // poll chain on the same stream — duplicate timers, racing repaints, and
      // eventually a wedged "Generate follow-up" button. Mirrors the
      // `!view.stream.pollTimer` guard in _loadOrGenerateReport.
      if (!rv.stream.pollTimer && typeof _pollReportTask === 'function') _pollReportTask(rv);
    } else if (rv.cache || (rv.stream && rv.stream.fullText)) {
      if (typeof _paintReportFromState === 'function' && rv.stream) _paintReportFromState(rv);
      else if (typeof _renderFinalReport === 'function' && rv.cache) {
        var c = document.getElementById(rv.containerId);
        if (c) _renderFinalReport(c, rv.cache, undefined, rv);
      }
    }
  } catch (e) { console.warn('[Paper:Rebuttal] restore panel failed:', e); }
}

function _reportView(kind) {
  if (kind === 'rebuttal') {
    return {
      kind: 'rebuttal', idPrefix: 'rebuttal', containerId: 'paperRebuttalContent',
      stopBtnId: 'paperRebuttalStopBtn', regenBtnId: 'paperRebuttalRegenBtn',
      copyLabelId: 'paperRebuttalCopyLabel',
      exportMenuId: 'paperRebuttalExportMenu', exportDropdownId: 'paperRebuttalExportDropdown',
      modelDropdownId: 'paperRebuttalModelDropdown', modelLabelId: 'paperRebuttalModelLabel',
      regenIntentKey: _REBUTTAL_REGEN_INTENT_KEY,
      // A rebuttal reply, like the review, is generated in English (the venue
      // discussion language); a Chinese UI reads a translated view on demand.
      uiLang: function() { return 'en'; },
      // Sibling composite key ``rebuttal:<venue>:<uilang>`` — same venue as the
      // review, never collides with the ``review:…`` row.
      langKey: function() { return 'rebuttal:' + (_paperReviewVenue || 'generic') + ':' + this.uiLang(); },
      get cache() { return _paperRebuttalCache; }, set cache(v) { _paperRebuttalCache = v; },
      get meta() { return _paperRebuttalMeta; }, set meta(v) { _paperRebuttalMeta = v; },
      get stream() { return _paperRebuttalStream; }, set stream(v) { _paperRebuttalStream = v; },
      get model() { return _paperRebuttalModel; }, set model(v) { _paperRebuttalModel = v; },
    };
  }
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

// ══════════════════════════════════════════════════════
//  ★ Enter / Exit Paper Mode
// ══════════════════════════════════════════════════════

async function enterPaperMode(pdfUrl, fileName, parsedText, arxivId) {
  if (typeof imageGenMode !== 'undefined' && imageGenMode) exitImageGenMode();

  paperMode = true;

  // ── Phase 1: paint the overlay IMMEDIATELY (synchronous, no await) ──
  // The bookshelf comes from the server (/api/paper/library) and used to be
  // awaited BEFORE the view swap, so on a slow connection the click looked
  // dead — the chat view stayed up until the fetch resolved. We now swap the
  // view first and hydrate the library below, so the mode switch is instant.

  // Sidebar → show paper library, hide conversations
  var sidebar = document.getElementById('sidebar');
  if (sidebar) {
    sidebar.classList.add('paper-active');
    if (sidebar.classList.contains('collapsed') && typeof toggleSidebar === 'function') toggleSidebar();
  }
  // Paper mode is a full-screen overlay in the same SPA; suppress the global
  // Project-Brain docked bars (#presenceStrip + #convInfluenceBar) via a body
  // class so their own push-driven visibility toggling is not fought. See CSS.
  try { document.body.classList.add('paper-mode-active'); } catch (e) { /* no body */ }

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

  // Bookshelf skeleton (only if nothing is cached yet) + a landing placeholder
  // in the viewer, so the overlay is fully painted before the fetch resolves.
  _paperLibraryLoading = (_paperLibrary.length === 0);
  _renderPaperLibrary();
  _showPaperLanding();

  // ── Phase 2: load the bookshelf, then resolve/restore the active paper ──
  try { await _loadPaperLibrary(); }
  catch (e) { console.warn('[Paper] loadPaperLibrary failed:', e); }
  finally { _paperLibraryLoading = false; }

  // The user may have left paper mode while the library was loading. Bail so we
  // don't clobber the chat view we've since returned to.
  if (!paperMode) return;

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

  _updatePaperTitles();
  _renderPaperLibrary();

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

  // Apply the reader's persisted comfort settings (text size + reading width)
  // to the report/review containers so the choice survives reload + spans papers.
  try { _applyReaderPrefs(); } catch (e) {
    console.warn('[Paper] applyReaderPrefs failed:', e);
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
  try { document.body.classList.remove('paper-mode-active'); } catch (e) { /* no body */ }

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
  if (_paperIntersectionObserver) { _paperIntersectionObserver.disconnect(); _paperIntersectionObserver = null; }
  _paperRenderToken++;  // cancel any in-flight lazy render loop
  _paperLoadGen++;      // supersede any in-flight _loadPaperPdf so it can't repaint after exit
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
        '<div class="paper-describe-divider"><span>' + escapeHtml(_tt('paper.describeOr')) + '</span></div>' +
        '<div class="paper-describe-box">' +
          '<div class="paper-describe-label">' +
            '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a7 7 0 0 0-4 12.7V17a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1v-2.3A7 7 0 0 0 12 2Z"/><line x1="9" y1="21" x2="15" y2="21"/></svg>' +
            ' ' + escapeHtml(_tt('paper.describeLabel')) +
          '</div>' +
          '<textarea id="paperDescribeInput" class="paper-describe-input" rows="3" placeholder="' + escapeHtml(_tt('paper.describePlaceholder')) + '"' +
                 ' oninput="_paperDescribeDraft=this.value"' +
                 ' onkeydown="if(event.key===\'Enter\'&&(event.metaKey||event.ctrlKey))_submitPaperDescribe()">' + escapeHtml(_paperDescribeDraft) + '</textarea>' +
          '<button onclick="_submitPaperDescribe()" class="paper-describe-btn">' +
            '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"/><path d="m12 5 7 7-7 7"/></svg>' +
            ' ' + escapeHtml(_tt('paper.describeBtn')) +
          '</button>' +
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
    // Enter the load path whenever ANY recoverable source exists: parsed text,
    // a server hash, OR a PDF still on disk. A library entry restored after a
    // restart can have empty parsedText + empty hash (saved before server-side
    // parsing, or a scanned/failed parse) while its PDF is intact under
    // PAPER_DIR — in that case _loadOrGenerateReport → _generatePaperReport
    // runs _ensurePaperText() (POST /api/paper/reparse) to recover the text.
    // Gating on text/hash alone dead-ended those papers on the "load a PDF"
    // message and never reached that recovery. Only show the message when
    // there is truly no PDF to recover from.
    if (_paperParsedText || _paperHash || _paperPdfUrl || _paperPdfFilename) {
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
        // Rehydrate the author-rebuttal paste for this paper and resume/paint
        // any existing rebuttal stream or cache (the rebuttal sub-panel lives
        // inside the Review tab, so it shares this entry point).
        _restoreRebuttalPanel();
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
  // arXiv candidate list / recommend stream — titles + abstracts carry inline
  // math that painted as `math-pending` fallback before KaTeX arrived.
  // Gate each repaint on WHICH list is actually on screen: `_recStream` is
  // sticky (never cleared once "describe" ran), so keying off it alone would
  // let _paintRecommendNow rebuild the recommend shell over a live plain-search
  // list. Only repaint recommend when its shell is still in the DOM.
  if (_recStream && document.querySelector('[data-rec-shell]') &&
      typeof _paintRecommendFromState === 'function') {
    // Invalidate the compare-before-swap sigs so grounded cards actually
    // re-render (their math was painted as math-pending fallback).
    var _rl = document.querySelector('[data-rec-list]');
    if (_rl) { Array.prototype.forEach.call(_rl.children, function(n){ n._recSig = ''; }); }
    _paintRecommendFromState();
  } else if (_paperSearchResults && _paperSearchResults.length &&
             document.querySelector('.paper-search .paper-result-list') &&
             typeof _lastArxivSearchQuery === 'string') {
    _renderArxivSearchResults(_lastArxivSearchQuery, _paperSearchResults);
  }
});

(window._onReady || function (f) { document.addEventListener('DOMContentLoaded', f); })(function() {
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
