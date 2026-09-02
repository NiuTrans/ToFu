/* ===== migrated source: paper-reader.js ===== */
/* ═══════════════════════════════════════════
   paper-reader.js — Paper Reading Mode v3

   Layout:  Sidebar  = paper library (persistent)
            Main L   = PDF (vertical scroll, largest)
            Main R   = Q&A / Report / Babel PDF
   ═══════════════════════════════════════════ */

// ── State ──
var paperMode = false;
Object.defineProperty(runtimeScope, 'paperMode', {
  configurable: true,
  get: () => paperMode,
  set: (enabled) => { paperMode = !!enabled; },
});
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
Object.defineProperties(runtimeScope, {
  _paperParsedText: {
    configurable: true,
    get: () => _paperParsedText,
    set: (value) => { _paperParsedText = String(value || ''); },
  },
  _paperHash: {
    configurable: true,
    get: () => _paperHash,
    set: (value) => { _paperHash = String(value || ''); },
  },
});

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
// Which Review-tab segment is on screen: 'review' (the peer review) or
// 'rebuttal' (the author-response → follow-up reply). The two are
// MUTUALLY-EXCLUSIVE full-height panels (see _setReviewSeg) so their two long
// documents never compete for the same flex-column height.
var _paperReviewSeg = 'review';
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

/** Switch the Review tab between its two mutually-exclusive segments:
 *   - 'review'   → the peer-review report (#paperReviewContent)
 *   - 'rebuttal' → the author-rebuttal paste + follow-up reply (#paperRebuttalContent)
 *  Only the chosen .paper-review-seg-panel stays in layout; the other is
 *  display:none. Because the two long documents are never co-laid-out, neither
 *  can be squeezed to 0 height by the other (the old stacked-<details> bug).
 *  Pure display toggle — containerId / cache / stream state are untouched, so
 *  the shared report-engine pipeline (_reportView, poll, paint) is unaffected.
 *  Flushes the active reading tracker on switch so one segment's reading
 *  position is never mis-attributed to the other (each keys its own langKey
 *  slot). */
function _setReviewSeg(seg) {
  if (seg !== 'review' && seg !== 'rebuttal') seg = 'review';
  // Leaving a segment: flush its reading session so the tracker for the newly
  // shown segment starts clean (the paint path re-wires it against the right
  // scroller + view). Cheap + idempotent.
  if (_paperReviewSeg !== seg && typeof _teardownReadingTracker === 'function') {
    _teardownReadingTracker(true);
  }
  _paperReviewSeg = seg;
  var root = document.querySelector('.paper-tab-panel[data-tab="review"]');
  if (!root) return;
  root.querySelectorAll('.paper-review-seg-panel').forEach(function(p) {
    p.style.display = (p.dataset.seg === seg) ? '' : 'none';
  });
  root.querySelectorAll('#paperReviewSeg .paper-review-seg-btn').forEach(function(b) {
    var on = b.dataset.seg === seg;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  // When the rebuttal segment becomes visible its scroller finally has real
  // geometry; repaint any existing follow-up so the score card + reply land.
  if (seg === 'rebuttal') {
    try { _restoreRebuttalPanel(); } catch (e) { console.warn('[Paper:Rebuttal] seg repaint failed:', e); }
  }
}

/** Reflect rebuttal state on the segment switcher: light the Rebuttal tab's
 *  status dot when a follow-up reply exists for this paper (cache or a live
 *  stream), so the reviewer sees there is something to read without switching.
 *  Reset the active segment to 'review' on paper switch / tab (re)entry so a
 *  new paper never opens straight into a stale rebuttal panel. */
function _syncReviewSegState(resetToReview) {
  if (resetToReview) _setReviewSeg('review');
  else _setReviewSeg(_paperReviewSeg);
  try {
    var dot = document.getElementById('paperRebuttalSegDot');
    if (dot) {
      var rv = _reportView('rebuttal');
      var has = !!(rv && (rv.cache || (rv.stream && rv.stream.fullText)));
      dot.style.display = has ? '' : 'none';
    }
  } catch (e) { console.warn('[Paper:Rebuttal] sync seg state failed:', e); }
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
  // Topbar title reflects Paper Mode, not the previous conversation
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
//  Landing / Upload Screen
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
          '<input type="file" accept=".pdf,application/pdf" data-tofu-action-change="_handlePaperFileUpload(event)" style="display:none">' +
          '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>' +
          ' ' + escapeHtml(_tt('paper.uploadPdf')) +
        '</label>' +
        '<div class="paper-arxiv-input">' +
          '<svg class="paper-arxiv-search-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>' +
          '<input type="text" id="paperArxivUrl" placeholder="' + escapeHtml(_tt('paper.arxivPlaceholder')) + '"' +
                 ' data-tofu-action-keydown="if(event.key===\'Enter\')_submitArxivQuery()">' +
          '<button data-tofu-action="_submitArxivQuery()" class="paper-arxiv-btn">' + escapeHtml(_tt('paper.search')) + '</button>' +
        '</div>' +
        '<div class="paper-describe-divider"><span>' + escapeHtml(_tt('paper.describeOr')) + '</span></div>' +
        '<div class="paper-describe-box">' +
          '<div class="paper-describe-label">' +
            '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a7 7 0 0 0-4 12.7V17a1 1 0 0 0 1 1h6a1 1 0 0 0 1-1v-2.3A7 7 0 0 0 12 2Z"/><line x1="9" y1="21" x2="15" y2="21"/></svg>' +
            ' ' + escapeHtml(_tt('paper.describeLabel')) +
          '</div>' +
          '<textarea id="paperDescribeInput" class="paper-describe-input" rows="3" placeholder="' + escapeHtml(_tt('paper.describePlaceholder')) + '"' +
                 ' data-tofu-action-input="_paperDescribeDraft=this.value"' +
                 ' data-tofu-action-keydown="if(event.key===\'Enter\'&&(event.metaKey||event.ctrlKey))_submitPaperDescribe()">' + escapeHtml(_paperDescribeDraft) + '</textarea>' +
          '<button data-tofu-action="_submitPaperDescribe()" class="paper-describe-btn">' +
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
  runtimeScope._babelTranslatedPages = {};
  _paperTotalPages = 0;
  _updatePaperTitles();
  _renderPaperLibrary();
  _showPaperLanding();
}

async function _handlePaperFileDrop(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith('.pdf') && file.type !== 'application/pdf') return;
  if (!paperMode) {
    var enter = runtimeScope.enterPaperMode;
    if (typeof enter !== 'function') throw new Error('Paper session owner is unavailable');
    await enter();
  }
  await _paperUploadFile(file);
}

async function _handlePaperFileUpload(event) {
  var file = event.target.files[0];
  if (!file || !file.name.toLowerCase().endsWith('.pdf')) return;
  await _paperUploadFile(file);
}

async function _paperUploadFile(file) {
  _paperLoading = true;

  // These behaviors moved to typed Paper owners. Resolve them through the
  // private runtime service table; bare identifiers would target the removed
  // classic concatenation scope and fail only after a user selected a PDF.
  var createEntry = runtimeScope._createPaperEntry;
  var renderLibrary = runtimeScope._renderPaperLibrary;
  var loadPdf = runtimeScope._loadPaperPdf;
  var savePaper = runtimeScope._saveActivePaperState;
  if (typeof createEntry !== 'function' || typeof renderLibrary !== 'function' ||
      typeof loadPdf !== 'function' || typeof savePaper !== 'function') {
    _paperLoading = false;
    throw new Error('Paper runtime owners are unavailable');
  }

  // Create a new library entry for this paper (_createPaperEntry sets _activePaperId).
  // Its id is sent WITH the upload so the server persists the bookshelf row
  // itself (server-authoritative ingest) — the paper survives even if this
  // tab closes before the client save. On failure we remove this entry so a
  // failed upload never leaves a ghost row.
  var _uploadEntry = createEntry(file.name);
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
  runtimeScope._babelTranslatedPages = {};
  runtimeScope._paperFileName = _paperFileName;
  runtimeScope._paperParsedText = _paperParsedText;
  runtimeScope._paperQAHistory = _paperQAHistory;
  runtimeScope._paperReportCache = _paperReportCache;
  runtimeScope._paperReviewCache = _paperReviewCache;
  runtimeScope._paperReviewVenue = _paperReviewVenue;
  runtimeScope._paperHash = _paperHash;
  runtimeScope._paperPdfFilename = _paperPdfFilename;
  runtimeScope._paperImages = _paperImages;
  runtimeScope._paperTotalPages = 0;
  _updatePaperTitles();
  renderLibrary();

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
    runtimeScope._paperPdfUrl = _paperPdfUrl;
    runtimeScope._paperPdfFilename = _paperPdfFilename;
    runtimeScope._paperParsedText = _paperParsedText;
    runtimeScope._paperHash = _paperHash;
    runtimeScope._paperImages = _paperImages;
    runtimeScope._paperTotalPages = _paperTotalPages;

    if (uploadData.parse_error) {
      debugLog('[Paper] PDF text extraction failed: ' + uploadData.parse_error, 'warning');
    } else if (_paperParsedText) {
      debugLog('Paper parsed: ' + _paperTotalPages + ' pages, ' +
               (uploadData.text_length || _paperParsedText.length) + ' chars' +
               (_paperImages.length ? ' (' + _paperImages.length + ' figures)' : ''),
               'success');
    }

    _updatePaperTitles();
    await loadPdf(_paperPdfUrl);
    // The server already persisted the row at ingest; await this so an
    // immediate exit/refresh can't race the first save (beforeunload is a
    // backstop only). Marks the entry _persisted so later saves are incremental.
    await savePaper();

  } catch (e) {
    console.error('[Paper] Upload failed:', e);
    // A failed upload must NOT leave a ghost bookshelf entry (the qa=0/imgs=0
    // rows behind the vanishing-paper report). Drop the entry we optimistically
    // created; the server never persisted a row for a failed save.
    var library = Array.isArray(runtimeScope._paperLibrary)
      ? runtimeScope._paperLibrary : [];
    runtimeScope._paperLibrary = library.filter(function(p) {
      return p.id !== _uploadPaperId;
    });
    if (runtimeScope._activePaperId === _uploadPaperId &&
        typeof runtimeScope._setActivePaperId === 'function') {
      runtimeScope._setActivePaperId('');
    }
    renderLibrary();
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
//  Keyboard Shortcuts
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
//  Init
// ══════════════════════════════════════════════════════


/* Explicit renderer surface consumed by the native Paper owners. */
if (typeof window !== 'undefined') {
  runtimeScope._reportView = _reportView;
  runtimeScope._restoreRebuttalPanel = _restoreRebuttalPanel;
  runtimeScope._syncReviewSegState = _syncReviewSegState;
  runtimeScope._updatePaperTitles = _updatePaperTitles;
  runtimeScope._showPaperLanding = _showPaperLanding;
  runtimeScope._handlePaperKeyDown = _handlePaperKeyDown;
}

