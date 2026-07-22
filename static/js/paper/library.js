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

// ── Paper library folders (mirrors chat-mode conversation folders) ──
// Folder metadata {id,name,color,collapsed,order} lives in a per-user JSON
// store on the server (/api/v1/paper-folders); each paper's membership is the
// `folderId` field on its library entry. This is a copy-not-reuse of
// core/folders.js (that module is hard-wired to conversations), keeping the
// same shape so the UX matches.
var _paperFolders = [];               // [{id,name,color,collapsed,order,createdAt}]
var _paperFoldersLoaded = false;
// When non-null, the sidebar shows only this folder's papers (folder view).
var _activePaperFolderId = null;
var _PAPER_FOLDER_COLLAPSE_KEY = 'paper_folder_collapsed';  // local collapse memory

/** Load the paper-folder list from the server. Best-effort; on failure keeps
 *  whatever is already loaded so a flaky connection doesn't blank the rail. */
async function _loadPaperFolders() {
  try {
    var list = await Api.paperFolders.list();
    if (Array.isArray(list)) {
      _paperFolders = list;
      _paperFoldersLoaded = true;
    }
  } catch (e) {
    console.warn('[Paper:Folders] load failed:', e && e.message);
  }
  return _paperFolders;
}

async function _createPaperFolder(name, color) {
  var folder = await Api.paperFolders.create(name, color);
  if (folder && folder.id) _paperFolders.push(folder);
  return folder;
}

async function _updatePaperFolder(folderId, updates) {
  var updated = await Api.paperFolders.update(folderId, updates);
  if (updated && updated.id) {
    var idx = _paperFolders.findIndex(function(f) { return f.id === folderId; });
    if (idx >= 0) Object.assign(_paperFolders[idx], updated);
  }
  return updated;
}

async function _deletePaperFolder(folderId) {
  var ok = await Api.paperFolders.remove(folderId);
  if (!ok) return false;
  _paperFolders = _paperFolders.filter(function(f) { return f.id !== folderId; });
  // Unassign every paper that was in the deleted folder (client-side, matching
  // the conversation-folder delete semantics — the server does not touch rows).
  for (var i = 0; i < _paperLibrary.length; i++) {
    if (_paperLibrary[i].folderId === folderId) {
      _paperLibrary[i].folderId = '';
      _persistPaperEntry(_paperLibrary[i]);
    }
  }
  if (_activePaperFolderId === folderId) _activePaperFolderId = null;
  _renderPaperLibrary();
  return true;
}

/** Assign (or clear, when folderId is '') a paper to a folder + persist. */
function _assignPaperFolder(paperId, folderId) {
  var entry = null;
  for (var i = 0; i < _paperLibrary.length; i++) {
    if (_paperLibrary[i].id === paperId) { entry = _paperLibrary[i]; break; }
  }
  if (!entry) return;
  entry.folderId = folderId || '';
  _persistPaperEntry(entry);
  _renderPaperLibrary();
}

function _getPaperFolderById(id) {
  return _paperFolders.find(function(f) { return f.id === id; }) || null;
}

/** Read/write the local per-folder collapse map (server also stores collapsed,
 *  but we mirror it locally so a toggle feels instant + survives reload). */
function _readPaperFolderCollapse() {
  try {
    var raw = localStorage.getItem(_PAPER_FOLDER_COLLAPSE_KEY);
    return raw ? (JSON.parse(raw) || {}) : {};
  } catch (e) { return {}; }
}

function _isPaperFolderCollapsed(folderId) {
  var f = _getPaperFolderById(folderId);
  var local = _readPaperFolderCollapse();
  if (folderId in local) return !!local[folderId];
  return !!(f && f.collapsed);
}

function _togglePaperFolderCollapse(folderId) {
  var collapsed = !_isPaperFolderCollapsed(folderId);
  try {
    var map = _readPaperFolderCollapse();
    map[folderId] = collapsed;
    localStorage.setItem(_PAPER_FOLDER_COLLAPSE_KEY, JSON.stringify(map));
  } catch (e) { /* storage disabled — server value still applies next load */ }
  _updatePaperFolder(folderId, { collapsed: collapsed });
  _renderPaperLibrary();
}

/** Prompt-create a new folder, then re-render. */
async function _promptNewPaperFolder() {
  var name = (typeof prompt === 'function')
    ? prompt((typeof t === 'function') ? t('paper.folderNamePrompt') : 'Folder name') : '';
  if (name == null) return;
  name = String(name).trim();
  if (!name) return;
  await _createPaperFolder(name, '');
  _renderPaperLibrary();
}

async function _renamePaperFolder(folderId) {
  var f = _getPaperFolderById(folderId);
  if (!f) return;
  var name = (typeof prompt === 'function')
    ? prompt((typeof t === 'function') ? t('paper.folderRenamePrompt') : 'Rename folder', f.name) : '';
  if (name == null) return;
  name = String(name).trim();
  if (!name || name === f.name) return;
  await _updatePaperFolder(folderId, { name: name });
  _renderPaperLibrary();
}

async function _confirmDeletePaperFolder(folderId) {
  var f = _getPaperFolderById(folderId);
  if (!f) return;
  var msg = (typeof t === 'function')
    ? t('paper.folderDeleteConfirm', { name: f.name })
    : ('Delete folder "' + f.name + '"? Papers inside are moved out, not deleted.');
  if (typeof confirm === 'function' && !confirm(msg)) return;
  await _deletePaperFolder(folderId);
}

/** Enter/leave a folder's dedicated view (like the chat folder tabs). */
function _setActivePaperFolder(folderId) {
  _activePaperFolderId = folderId || null;
  _renderPaperLibrary();
}

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
    // folderId is a small mutable field (like title) — always send it so a
    // folder (re)assignment persists on the next save. '' = unfiled.
    folderId: entry.folderId || '',
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
  // Fetch folders in parallel with the bookshelf — best-effort, non-blocking
  // for the library load itself (a folder-fetch failure must not blank papers).
  var _foldersP = _loadPaperFolders().catch(function(e) {
    console.warn('[Paper:Folders] load (parallel) failed:', e && e.message);
  });
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
  try { await _foldersP; } catch (e) { /* already logged */ }
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
    folderId: (typeof _activePaperFolderId !== 'undefined' && _activePaperFolderId) || '',
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
    folderId: (typeof _activePaperFolderId !== 'undefined' && _activePaperFolderId) || '',
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
      _paperFolderBarHTML() +
      '<div class="paper-lib-empty">' +
        '<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.3"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>' +
        '<span>' + escapeHtml(_tte('paper.noPapersYet')) + '</span>' +
        '<span class="paper-lib-empty-hint">' + escapeHtml(_tte('paper.noPapersHint')) + '</span>' +
      '</div>';
    return;
  }

  // Folder-view mode: show only the active folder's papers, flat.
  if (_activePaperFolderId) {
    var af = _getPaperFolderById(_activePaperFolderId);
    var inFolder = _paperLibrary.filter(function(p) { return (p.folderId || '') === _activePaperFolderId; });
    var backLbl = (typeof t === 'function') ? t('paper.folderBackAll') : '← All papers';
    var body = inFolder.length
      ? inFolder.map(_paperLibItemHTML).join('')
      : '<div class="paper-lib-empty"><span>' +
          escapeHtml((typeof t === 'function') ? t('paper.folderEmpty') : 'No papers in this folder yet') +
        '</span></div>';
    listEl.innerHTML =
      '<div class="paper-folder-crumb" onclick="_setActivePaperFolder(null)">' +
        '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="15 18 9 12 15 6"/></svg>' +
        '<span>' + escapeHtml(backLbl) + '</span>' +
        '<span class="paper-folder-crumb-name">' + escapeHtml(af ? af.name : '') + '</span>' +
      '</div>' + body;
    return;
  }

  // Default grouped view: a bar of folder chips, then folders (collapsible)
  // with their papers, then the unfiled papers.
  var folders = _paperFolders.slice().sort(function(a, b) {
    return (a.order || 0) - (b.order || 0) || (a.createdAt || 0) - (b.createdAt || 0);
  });
  var byFolder = {};
  var unfiled = [];
  for (var i = 0; i < _paperLibrary.length; i++) {
    var fid = _paperLibrary[i].folderId || '';
    if (fid && _getPaperFolderById(fid)) {
      (byFolder[fid] = byFolder[fid] || []).push(_paperLibrary[i]);
    } else {
      unfiled.push(_paperLibrary[i]);
    }
  }

  var html = _paperFolderBarHTML();
  for (var fi = 0; fi < folders.length; fi++) {
    var f = folders[fi];
    var members = byFolder[f.id] || [];
    var collapsed = _isPaperFolderCollapsed(f.id);
    html +=
      '<div class="paper-folder-group' + (collapsed ? ' collapsed' : '') + '" data-folder="' + escapeHtml(f.id) + '">' +
        '<div class="paper-folder-head" onclick="_togglePaperFolderCollapse(\'' + f.id + '\')">' +
          '<svg class="paper-folder-caret" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polyline points="9 6 15 12 9 18"/></svg>' +
          '<svg class="paper-folder-ic" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>' +
          '<span class="paper-folder-name" title="' + escapeHtml(f.name) + '">' + escapeHtml(f.name) + '</span>' +
          '<span class="paper-folder-count">' + members.length + '</span>' +
          '<span class="paper-folder-open" title="' + escapeHtml((typeof t === 'function') ? t('paper.folderOpen') : 'Open folder') + '" onclick="event.stopPropagation();_setActivePaperFolder(\'' + f.id + '\')">' +
            '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M5 12h14"/><polyline points="12 5 19 12 12 19"/></svg>' +
          '</span>' +
          '<span class="paper-folder-rename" title="' + escapeHtml((typeof t === 'function') ? t('paper.folderRename') : 'Rename') + '" onclick="event.stopPropagation();_renamePaperFolder(\'' + f.id + '\')">' +
            '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>' +
          '</span>' +
          '<span class="paper-folder-del" title="' + escapeHtml((typeof t === 'function') ? t('paper.delete') : 'Delete') + '" onclick="event.stopPropagation();_confirmDeletePaperFolder(\'' + f.id + '\')">' +
            '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>' +
          '</span>' +
        '</div>' +
        '<div class="paper-folder-body">' + members.map(_paperLibItemHTML).join('') + '</div>' +
      '</div>';
  }
  html += unfiled.map(_paperLibItemHTML).join('');
  listEl.innerHTML = html;
}

/** The folder-management bar shown atop the bookshelf: a "+ Folder" button.
 *  Kept tiny; folder chips are the collapsible groups below. */
function _paperFolderBarHTML() {
  var lbl = (typeof t === 'function') ? t('paper.newFolder') : 'New folder';
  return '<div class="paper-folder-bar">' +
    '<button class="paper-folder-new-btn" onclick="_promptNewPaperFolder()" title="' + escapeHtml(lbl) + '">' +
      '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><line x1="12" y1="10" x2="12" y2="16"/><line x1="9" y1="13" x2="15" y2="13"/></svg>' +
      '<span>' + escapeHtml(lbl) + '</span>' +
    '</button>' +
  '</div>';
}

/** Render one paper row. Extracted so both the grouped view, the folder view,
 *  and the flat empty-folder view share ONE markup path. Includes a
 *  "move to folder" control (a native <select>) so assignment needs no
 *  drag-and-drop plumbing. */
function _paperLibItemHTML(p) {
  var isActive = p.id === _activePaperId;
  var isRec = _isRecommendedEntry(p);
  var dateStr = _formatPaperDate(p.createdAt);
  var pageStr = p.pageCount ? p.pageCount + 'p' : '';
  var hasReport = p.hasReport ? ' · report' : '';
  var _ttr = (typeof t === 'function') ? t : function(k){ return k; };
  var metaHtml = isRec
    ? '<span class="paper-lib-rec-badge">' + escapeHtml(_ttr('paper.recommended')) + '</span>' + dateStr
    : dateStr + (pageStr ? ' · ' + pageStr : '') + hasReport;

  // Folder <select>: "no folder" + one option per folder, current selected.
  var curFid = p.folderId || '';
  var opts = '<option value=""' + (curFid ? '' : ' selected') + '>' +
    escapeHtml((typeof t === 'function') ? t('paper.folderNone') : 'No folder') + '</option>';
  for (var i = 0; i < _paperFolders.length; i++) {
    var f = _paperFolders[i];
    opts += '<option value="' + escapeHtml(f.id) + '"' + (f.id === curFid ? ' selected' : '') + '>' +
      escapeHtml(f.name) + '</option>';
  }
  var moveTitle = (typeof t === 'function') ? t('paper.folderMoveTo') : 'Move to folder';

  return '<div class="paper-lib-item' + (isActive ? ' active' : '') + (isRec ? ' paper-lib-item-rec' : '') + '" data-id="' + p.id + '" onclick="_onPaperLibClick(\'' + p.id + '\')">' +
      '<div class="paper-lib-item-icon">' +
        '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>' +
      '</div>' +
      '<div class="paper-lib-item-info">' +
        '<span class="paper-lib-item-title" title="' + escapeHtml(p.title) + '">' + escapeHtml(p.title) + '</span>' +
        '<span class="paper-lib-item-meta">' + metaHtml + '</span>' +
      '</div>' +
      '<select class="paper-lib-item-folder" title="' + escapeHtml(moveTitle) + '" onclick="event.stopPropagation()" onchange="event.stopPropagation();_assignPaperFolder(\'' + p.id + '\', this.value)">' +
        opts +
      '</select>' +
      '<button class="paper-lib-item-del" onclick="event.stopPropagation();_deletePaperEntry(\'' + p.id + '\')" title="' + escapeHtml((typeof t === 'function') ? t('paper.delete') : 'Delete') + '">' +
        '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>' +
      '</button>' +
    '</div>';
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
