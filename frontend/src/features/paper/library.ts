import { featureRegistry } from '../../feature-registry';
import type { I18nKey } from '../../i18n';
type JsonObject = Record<string, unknown>;

interface PaperFolder extends JsonObject {
  id: string;
  name: string;
  color?: string;
  collapsed?: boolean;
  order?: number;
  createdAt?: number;
}

interface PaperEntry extends JsonObject {
  id: string;
  title: string;
  pdfUrl?: string;
  pdfFilename?: string;
  arxivId?: string;
  parsedText?: string;
  qaHistory?: JsonObject[];
  paperHash?: string;
  images?: unknown[];
  babelCache?: JsonObject;
  createdAt?: number;
  pageCount?: number;
  recommendWhy?: string;
  folderId?: string;
  hasReport?: boolean;
  _persisted?: boolean;
}

interface RecommendCard extends JsonObject {
  arxiv_id?: string;
  title?: string;
  why?: string;
}

interface PaperLibraryApi {
  libraryUpsert(id: string, body: JsonObject): Promise<JsonObject | null>;
  libraryList(): Promise<JsonObject | null>;
  libraryDelete(id: string): Promise<unknown>;
}

interface PaperFolderApi {
  list(): Promise<PaperFolder[]>;
  create(name: string, color: string): Promise<PaperFolder | null>;
  update(id: string, updates: JsonObject): Promise<PaperFolder | null>;
  remove(id: string): Promise<boolean>;
}

type PaperLibraryWindow = Window & {
  Api?: { paper?: PaperLibraryApi; paperFolders?: PaperFolderApi };
  t?: (key: string, params?: Record<string, unknown>) => string;
  escapeHtml?: (value: unknown) => string;
  debugLog?: (message: string, level?: string) => void;
  paperMode?: boolean;
  _paperLibrary?: PaperEntry[];
  _paperLibraryLoading?: boolean;
  _activePaperId?: string;
  _paperFolders?: PaperFolder[];
  _paperFoldersLoaded?: boolean;
  _activePaperFolderId?: string | null;
  _paperPdfUrl?: string;
  _paperPdfFilename?: string;
  _paperFileName?: string;
  _paperParsedText?: string;
  _paperArxivId?: string;
  _paperQAHistory?: JsonObject[];
  _paperQAAbort?: AbortController | null;
  _paperReportCache?: string;
  _paperReportMeta?: unknown;
  _paperReviewCache?: string;
  _paperReviewMeta?: unknown;
  _paperReviewVenue?: string;
  _paperHash?: string;
  _paperImages?: unknown[];
  _babelTranslatedPages?: JsonObject;
  _paperTotalPages?: number;
  _paperActiveTab?: string;
  _resetAllReportViews?: () => void;
  _showPaperLanding?: () => void;
  _updatePaperTitles?: () => void;
  _loadPaperPdf?: (url: string) => Promise<unknown>;
  _switchPaperTab?: (tab: string) => void;
  _fetchArxivPaper?: (arxivId: string, entryId?: string) => Promise<unknown>;
  _loadPaperFolders?: typeof loadPaperFolders;
  _createPaperFolder?: typeof createPaperFolder;
  _updatePaperFolder?: typeof updatePaperFolder;
  _deletePaperFolder?: typeof deletePaperFolder;
  _assignPaperFolder?: typeof assignPaperFolder;
  _getPaperFolderById?: typeof getPaperFolderById;
  _readPaperFolderCollapse?: typeof readPaperFolderCollapse;
  _isPaperFolderCollapsed?: typeof isPaperFolderCollapsed;
  _togglePaperFolderCollapse?: typeof togglePaperFolderCollapse;
  _promptNewPaperFolder?: typeof promptNewPaperFolder;
  _renamePaperFolder?: typeof renamePaperFolder;
  _confirmDeletePaperFolder?: typeof confirmDeletePaperFolder;
  _setActivePaperFolder?: typeof setActivePaperFolder;
  _persistPaperEntry?: typeof persistPaperEntry;
  _migrateLegacyLibrary?: typeof migrateLegacyLibrary;
  _loadPaperLibrary?: typeof loadPaperLibrary;
  _setActivePaperId?: typeof setActivePaperId;
  _newPaperEntryId?: typeof newPaperEntryId;
  _normArxivId?: typeof normalizeArxivId;
  _isRecommendedEntry?: typeof isRecommendedEntry;
  _findLibraryEntryByArxiv?: typeof findLibraryEntryByArxiv;
  _persistRecommendedCard?: typeof persistRecommendedCard;
  _createPaperEntry?: typeof createPaperEntry;
  _getActivePaperEntry?: typeof getActivePaperEntry;
  _saveActivePaperState?: typeof saveActivePaperState;
  _deletePaperEntry?: typeof deletePaperEntry;
  _openPaperEntry?: typeof openPaperEntry;
  _renderPaperLibrary?: typeof renderPaperLibrary;
  _paperFolderBarHTML?: typeof paperFolderBarHTML;
  _paperLibItemHTML?: typeof paperLibItemHTML;
  _onPaperLibClick?: typeof onPaperLibClick;
  _formatPaperDate?: typeof formatPaperDate;
};

const ACTIVE_KEY = 'paper_active_id';
const LEGACY_LIBRARY_KEY = 'paper_library';
const MIGRATED_KEY = 'paper_library_migrated_v1';
const FOLDER_COLLAPSE_KEY = 'paper_folder_collapsed';

function globals(): PaperLibraryWindow {
  return featureRegistry as unknown as PaperLibraryWindow;
}

function state(): PaperLibraryWindow {
  const target = globals();
  target._paperLibrary ??= [];
  target._paperLibraryLoading ??= false;
  target._activePaperId ??= '';
  target._paperFolders ??= [];
  target._paperFoldersLoaded ??= false;
  target._activePaperFolderId ??= null;
  return target;
}

function paperApi(): PaperLibraryApi {
  const api = globals().Api?.paper;
  if (!api) throw new Error('Paper library API unavailable');
  return api;
}

function folderApi(): PaperFolderApi {
  const api = globals().Api?.paperFolders;
  if (!api) throw new Error('Paper folder API unavailable');
  return api;
}

function escape(value: unknown): string {
  const helper = globals().escapeHtml;
  if (typeof helper === 'function') return helper(value);
  const span = document.createElement('span');
  span.textContent = value == null ? '' : String(value);
  return span.innerHTML;
}

function translate(
  key: I18nKey,
  fallback: string = key,
  params?: Record<string, unknown>,
): string {
  const helper = globals().t;
  return typeof helper === 'function' ? helper(key, params) : fallback;
}

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error ?? '');
}

export async function loadPaperFolders(): Promise<PaperFolder[]> {
  const shared = state();
  try {
    const folders = await folderApi().list();
    if (Array.isArray(folders)) {
      shared._paperFolders = folders;
      shared._paperFoldersLoaded = true;
    }
  } catch (error: unknown) {
    console.warn('[Paper:Folders] load failed:', message(error));
  }
  return shared._paperFolders ?? [];
}

export async function createPaperFolder(
  name: string,
  color = '',
): Promise<PaperFolder | null> {
  const folder = await folderApi().create(name, color);
  if (folder?.id) state()._paperFolders?.push(folder);
  return folder;
}

export async function updatePaperFolder(
  folderId: string,
  updates: JsonObject,
): Promise<PaperFolder | null> {
  const updated = await folderApi().update(folderId, updates);
  if (updated?.id) {
    const current = state()._paperFolders?.find((folder) => folder.id === folderId);
    if (current) Object.assign(current, updated);
  }
  return updated;
}

export async function deletePaperFolder(folderId: string): Promise<boolean> {
  if (!await folderApi().remove(folderId)) return false;
  const shared = state();
  shared._paperFolders = (shared._paperFolders ?? []).filter(
    (folder) => folder.id !== folderId,
  );
  (shared._paperLibrary ?? []).forEach((entry) => {
    if (entry.folderId === folderId) {
      entry.folderId = '';
      void persistPaperEntry(entry);
    }
  });
  if (shared._activePaperFolderId === folderId) shared._activePaperFolderId = null;
  renderPaperLibrary();
  return true;
}

export function assignPaperFolder(paperId: string, folderId: string): void {
  const entry = state()._paperLibrary?.find((paper) => paper.id === paperId);
  if (!entry) return;
  entry.folderId = folderId || '';
  void persistPaperEntry(entry);
  renderPaperLibrary();
}

export function getPaperFolderById(id: string): PaperFolder | null {
  return state()._paperFolders?.find((folder) => folder.id === id) ?? null;
}

export function readPaperFolderCollapse(): Record<string, boolean> {
  try {
    const raw = localStorage.getItem(FOLDER_COLLAPSE_KEY);
    const value = raw ? JSON.parse(raw) : {};
    return value && typeof value === 'object'
      ? value as Record<string, boolean> : {};
  } catch {
    return {};
  }
}

export function isPaperFolderCollapsed(folderId: string): boolean {
  const local = readPaperFolderCollapse();
  if (folderId in local) return Boolean(local[folderId]);
  return Boolean(getPaperFolderById(folderId)?.collapsed);
}

export function togglePaperFolderCollapse(folderId: string): void {
  const collapsed = !isPaperFolderCollapsed(folderId);
  try {
    const local = readPaperFolderCollapse();
    local[folderId] = collapsed;
    localStorage.setItem(FOLDER_COLLAPSE_KEY, JSON.stringify(local));
  } catch { /* server state remains the fallback */ }
  void updatePaperFolder(folderId, { collapsed });
  renderPaperLibrary();
}

export async function promptNewPaperFolder(): Promise<void> {
  const answer = typeof window.prompt === 'function'
    ? window.prompt(translate('paper.folderNamePrompt', 'Folder name')) : '';
  const name = answer == null ? '' : String(answer).trim();
  if (!name) return;
  await createPaperFolder(name);
  renderPaperLibrary();
}

export async function renamePaperFolder(folderId: string): Promise<void> {
  const folder = getPaperFolderById(folderId);
  if (!folder) return;
  const answer = typeof window.prompt === 'function'
    ? window.prompt(
      translate('paper.folderRenamePrompt', 'Rename folder'),
      folder.name,
    ) : '';
  const name = answer == null ? '' : String(answer).trim();
  if (!name || name === folder.name) return;
  await updatePaperFolder(folderId, { name });
  renderPaperLibrary();
}

export async function confirmDeletePaperFolder(folderId: string): Promise<void> {
  const folder = getPaperFolderById(folderId);
  if (!folder) return;
  const prompt = translate(
    'paper.folderDeleteConfirm',
    `Delete folder "${folder.name}"? Papers inside are moved out, not deleted.`,
    { name: folder.name },
  );
  if (typeof window.confirm === 'function' && !window.confirm(prompt)) return;
  await deletePaperFolder(folderId);
}

export function setActivePaperFolder(folderId?: string | null): void {
  state()._activePaperFolderId = folderId || null;
  renderPaperLibrary();
}

/** Persist mutable fields; server-derived heavy fields are first-save only. */
export function persistPaperEntry(
  entry: PaperEntry | null | undefined,
  first = false,
): Promise<unknown> {
  if (!entry?.id) return Promise.resolve();
  const body: JsonObject = {
    title: entry.title || '',
    qaHistory: (entry.qaHistory || []).slice(-50),
    babelCache: entry.babelCache || {},
    pageCount: entry.pageCount || 0,
    createdAt: entry.createdAt || Date.now(),
    folderId: entry.folderId || '',
  };
  if (first) {
    Object.assign(body, {
      pdfUrl: entry.pdfUrl || '',
      pdfFilename: entry.pdfFilename || '',
      arxivId: entry.arxivId || '',
      paperHash: entry.paperHash || '',
      parsedText: (entry.parsedText || '').slice(0, 200000),
      images: Array.isArray(entry.images) ? entry.images.slice(0, 60) : [],
    });
  }
  return paperApi().libraryUpsert(entry.id, body).then((data) => {
    if (!data?.ok) console.warn('[Paper:Library] Upsert rejected:', data?.error);
    return data;
  }).catch((error: unknown) => {
    console.warn('[Paper:Library] Upsert failed:', error);
    return undefined;
  });
}

export async function migrateLegacyLibrary(): Promise<void> {
  if (localStorage.getItem(MIGRATED_KEY)) return;
  const raw = localStorage.getItem(LEGACY_LIBRARY_KEY);
  if (!raw) {
    localStorage.setItem(MIGRATED_KEY, '1');
    return;
  }
  let legacy: unknown;
  try {
    legacy = JSON.parse(raw);
  } catch (error: unknown) {
    console.warn('[Paper:Library] Legacy bookshelf parse failed, discarding:', error);
    localStorage.removeItem(LEGACY_LIBRARY_KEY);
    localStorage.setItem(MIGRATED_KEY, '1');
    return;
  }
  if (!Array.isArray(legacy) || legacy.length === 0) {
    localStorage.removeItem(LEGACY_LIBRARY_KEY);
    localStorage.setItem(MIGRATED_KEY, '1');
    return;
  }
  globals().debugLog?.(
    `[Paper] Migrating ${legacy.length} bookshelf entries to server…`,
    'info',
  );
  for (const row of legacy) {
    try { await persistPaperEntry(row as PaperEntry, true); } catch (error: unknown) {
      console.warn('[Paper:Library] Migrate entry failed:', error);
    }
  }
  localStorage.removeItem(LEGACY_LIBRARY_KEY);
  localStorage.setItem(MIGRATED_KEY, '1');
  globals().debugLog?.('[Paper] Migration complete.', 'success');
}

export async function loadPaperLibrary(): Promise<void> {
  const shared = state();
  shared._activePaperId = localStorage.getItem(ACTIVE_KEY) || '';
  const folders = loadPaperFolders().catch((error: unknown) => {
    console.warn('[Paper:Folders] load (parallel) failed:', message(error));
  });
  try {
    await migrateLegacyLibrary();
    const data = await paperApi().libraryList();
    if (data?.ok && Array.isArray(data.papers)) {
      shared._paperLibrary = (data.papers as PaperEntry[]).map((entry) => ({
        ...entry,
        _persisted: true,
      }));
    } else {
      shared._paperLibrary = [];
      console.warn('[Paper:Library] Unexpected server response:', data);
    }
  } catch (error: unknown) {
    console.warn('[Paper:Library] Load failed, falling back to empty:', error);
    shared._paperLibrary = [];
  }
  if (shared._activePaperId && !(shared._paperLibrary ?? []).some(
    (entry) => entry.id === shared._activePaperId,
  )) {
    shared._activePaperId = '';
    localStorage.removeItem(ACTIVE_KEY);
  }
  await folders;
}

export function setActivePaperId(id?: string | null): void {
  const value = id || '';
  state()._activePaperId = value;
  if (value) localStorage.setItem(ACTIVE_KEY, value);
  else localStorage.removeItem(ACTIVE_KEY);
}

export function newPaperEntryId(): string {
  return `paper_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

export function normalizeArxivId(value: unknown): string {
  const normalized = String(value ?? '').trim().toLowerCase();
  return normalized ? normalized.split('v')[0].trim() : '';
}

export function isRecommendedEntry(
  entry: PaperEntry | null | undefined,
): boolean {
  return Boolean(entry?.arxivId && !entry.pdfUrl && !entry.parsedText);
}

export function findLibraryEntryByArxiv(value: unknown): PaperEntry | null {
  const key = normalizeArxivId(value);
  if (!key) return null;
  return state()._paperLibrary?.find(
    (entry) => normalizeArxivId(entry.arxivId) === key,
  ) ?? null;
}

export function persistRecommendedCard(
  card: RecommendCard | null | undefined,
): PaperEntry | null {
  if (!card?.arxiv_id || findLibraryEntryByArxiv(card.arxiv_id)) return null;
  const entry: PaperEntry = {
    id: newPaperEntryId(),
    title: card.title || `arXiv:${card.arxiv_id}`,
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
    folderId: state()._activePaperFolderId || '',
    _persisted: true,
  };
  state()._paperLibrary?.unshift(entry);
  renderPaperLibrary();
  void persistPaperEntry(entry, true);
  return entry;
}

export function createPaperEntry(
  title?: string,
  pdfUrl?: string,
  parsedText?: string,
  arxivId?: string,
  explicitId?: string,
): PaperEntry {
  const shared = state();
  if (explicitId) {
    const existing = shared._paperLibrary?.find((entry) => entry.id === explicitId);
    if (existing) {
      existing.title = title || existing.title || 'Untitled Paper';
      existing.pdfUrl = pdfUrl || '';
      existing.parsedText = parsedText || '';
      if (arxivId) existing.arxivId = arxivId;
      existing._persisted = false;
      setActivePaperId(existing.id);
      return existing;
    }
  }
  const entry: PaperEntry = {
    id: explicitId || newPaperEntryId(),
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
    folderId: shared._activePaperFolderId || '',
    _persisted: false,
  };
  shared._paperLibrary?.unshift(entry);
  setActivePaperId(entry.id);
  return entry;
}

export function getActivePaperEntry(): PaperEntry | null {
  const shared = state();
  if (!shared._activePaperId) return null;
  return shared._paperLibrary?.find(
    (entry) => entry.id === shared._activePaperId,
  ) ?? null;
}

export function saveActivePaperState(): Promise<unknown> {
  const entry = getActivePaperEntry();
  if (!entry) return Promise.resolve();
  const shared = state();
  entry.pdfUrl = shared._paperPdfUrl || '';
  entry.pdfFilename = shared._paperPdfFilename || entry.pdfFilename || '';
  entry.title = shared._paperFileName || entry.title;
  entry.parsedText = shared._paperParsedText || '';
  entry.arxivId = shared._paperArxivId || '';
  entry.qaHistory = shared._paperQAHistory || [];
  entry.paperHash = shared._paperHash || '';
  entry.images = Array.isArray(shared._paperImages) ? shared._paperImages : [];
  entry.babelCache = shared._babelTranslatedPages || {};
  entry.pageCount = shared._paperTotalPages || 0;
  const first = !entry._persisted;
  entry._persisted = true;
  return persistPaperEntry(entry, first);
}

export function deletePaperEntry(id: string): void {
  const shared = state();
  shared._paperLibrary = (shared._paperLibrary ?? []).filter(
    (entry) => entry.id !== id,
  );
  if (shared._activePaperId === id) {
    setActivePaperId(shared._paperLibrary[0]?.id || '');
  }
  void paperApi().libraryDelete(id).catch((error: unknown) => {
    console.warn('[Paper:Library] Delete failed:', error);
  });
  renderPaperLibrary();
  if (!shared.paperMode) return;
  const next = getActivePaperEntry();
  if (next) {
    openPaperEntry(next);
    return;
  }
  shared._resetAllReportViews?.();
  shared._paperPdfUrl = '';
  shared._paperPdfFilename = '';
  shared._paperFileName = '';
  shared._paperParsedText = '';
  shared._paperQAHistory = [];
  shared._paperReportCache = '';
  shared._paperReviewCache = '';
  shared._paperReviewVenue = '';
  shared._paperHash = '';
  shared._paperImages = [];
  shared._babelTranslatedPages = {};
  shared._showPaperLanding?.();
  shared._updatePaperTitles?.();
}

export function openPaperEntry(entry: PaperEntry): void {
  const shared = state();
  void saveActivePaperState();
  try { shared._paperQAAbort?.abort(); } catch { /* best-effort prior stream */ }
  shared._paperQAAbort = null;
  shared._resetAllReportViews?.();
  setActivePaperId(entry.id);
  shared._paperPdfUrl = entry.pdfUrl || '';
  shared._paperPdfFilename = entry.pdfFilename || '';
  shared._paperFileName = entry.title || 'Untitled';
  shared._paperParsedText = entry.parsedText || '';
  shared._paperArxivId = entry.arxivId || '';
  shared._paperQAHistory = entry.qaHistory || [];
  shared._paperReportCache = '';
  shared._paperReportMeta = null;
  shared._paperReviewCache = '';
  shared._paperReviewMeta = null;
  shared._paperReviewVenue = '';
  shared._paperHash = entry.paperHash || '';
  shared._paperImages = Array.isArray(entry.images) ? entry.images : [];
  shared._babelTranslatedPages = entry.babelCache || {};
  shared._paperTotalPages = entry.pageCount || 0;
  const report = document.getElementById('paperReportContent');
  if (report) {
    report.innerHTML = '<div class="paper-loading"><div class="paper-loading-spinner">'
      + '</div><div>Loading…</div></div>';
  }
  const qa = document.getElementById('paperQAMessages');
  if (qa) qa.innerHTML = '';
  shared._updatePaperTitles?.();
  renderPaperLibrary();
  if (shared._paperPdfUrl) void shared._loadPaperPdf?.(shared._paperPdfUrl);
  else shared._showPaperLanding?.();
  shared._switchPaperTab?.(shared._paperActiveTab || 'qa');
}

export function renderPaperLibrary(): void {
  const list = document.getElementById('paperLibraryList');
  if (!list) return;
  const shared = state();
  const papers = shared._paperLibrary ?? [];
  const count = document.getElementById('paperLibCount');
  if (count) count.textContent = String(papers.length || '');
  if (shared._paperLibraryLoading && papers.length === 0) {
    list.innerHTML = '<div class="paper-lib-loading">'
      + '<span class="paper-lib-loading-spinner"></span>'
      + `<span>${escape(translate('paper.loadingLibrary'))}</span></div>`;
    return;
  }
  if (papers.length === 0) {
    list.innerHTML = paperFolderBarHTML()
      + '<div class="paper-lib-empty">'
      + '<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.3"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>'
      + `<span>${escape(translate('paper.noPapersYet'))}</span>`
      + `<span class="paper-lib-empty-hint">${escape(translate('paper.noPapersHint'))}</span>`
      + '</div>';
    return;
  }
  if (shared._activePaperFolderId) {
    const folder = getPaperFolderById(shared._activePaperFolderId);
    const members = papers.filter(
      (entry) => (entry.folderId || '') === shared._activePaperFolderId,
    );
    const body = members.length
      ? members.map(paperLibItemHTML).join('')
      : `<div class="paper-lib-empty"><span>${escape(
        translate('paper.folderEmpty', 'No papers in this folder yet'),
      )}</span></div>`;
    list.innerHTML = '<div class="paper-folder-crumb" data-tofu-action="_setActivePaperFolder(null)">'
      + '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><polyline points="15 18 9 12 15 6"/></svg>'
      + `<span>${escape(translate('paper.folderBackAll', '← All papers'))}</span>`
      + `<span class="paper-folder-crumb-name">${escape(folder?.name || '')}</span>`
      + `</div>${body}`;
    return;
  }
  const folders = [...(shared._paperFolders ?? [])].sort((left, right) => (
    (left.order || 0) - (right.order || 0)
    || (left.createdAt || 0) - (right.createdAt || 0)
  ));
  const byFolder: Record<string, PaperEntry[] | undefined> = Object.create(null) as Record<
    string,
    PaperEntry[]
  >;
  const unfiled: PaperEntry[] = [];
  papers.forEach((entry) => {
    const folderId = entry.folderId || '';
    if (folderId && getPaperFolderById(folderId)) {
      (byFolder[folderId] ??= []).push(entry);
    } else {
      unfiled.push(entry);
    }
  });
  let html = paperFolderBarHTML();
  folders.forEach((folder) => {
    const members = byFolder[folder.id] ?? [];
    const collapsed = isPaperFolderCollapsed(folder.id);
    html += `<div class="paper-folder-group${collapsed ? ' collapsed' : ''}"`
      + ` data-folder="${escape(folder.id)}">`
      + `<div class="paper-folder-head" data-tofu-action="_togglePaperFolderCollapse('${folder.id}')">`
      + '<svg class="paper-folder-caret" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><polyline points="9 6 15 12 9 18"/></svg>'
      + '<svg class="paper-folder-ic" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>'
      + `<span class="paper-folder-name" title="${escape(folder.name)}">${escape(folder.name)}</span>`
      + `<span class="paper-folder-count">${members.length}</span>`
      + `<span class="paper-folder-open" title="${escape(translate('paper.folderOpen', 'Open folder'))}" data-tofu-action="event.stopPropagation();_setActivePaperFolder('${folder.id}')">`
      + '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M5 12h14"/><polyline points="12 5 19 12 12 19"/></svg></span>'
      + `<span class="paper-folder-rename" title="${escape(translate('paper.folderRename', 'Rename'))}" data-tofu-action="event.stopPropagation();_renamePaperFolder('${folder.id}')">`
      + '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/></svg></span>'
      + `<span class="paper-folder-del" title="${escape(translate('paper.delete', 'Delete'))}" data-tofu-action="event.stopPropagation();_confirmDeletePaperFolder('${folder.id}')">`
      + '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></span>'
      + `</div><div class="paper-folder-body">${members.map(paperLibItemHTML).join('')}</div></div>`;
  });
  html += unfiled.map(paperLibItemHTML).join('');
  list.innerHTML = html;
}

export function paperFolderBarHTML(): string {
  const label = translate('paper.newFolder', 'New folder');
  return '<div class="paper-folder-bar">'
    + `<button class="paper-folder-new-btn" data-tofu-action="_promptNewPaperFolder()" title="${escape(label)}">`
    + '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><line x1="12" y1="10" x2="12" y2="16"/><line x1="9" y1="13" x2="15" y2="13"/></svg>'
    + `<span>${escape(label)}</span></button></div>`;
}

export function paperLibItemHTML(entry: PaperEntry): string {
  const active = entry.id === state()._activePaperId;
  const recommended = isRecommendedEntry(entry);
  const date = formatPaperDate(entry.createdAt);
  const page = entry.pageCount ? `${entry.pageCount}p` : '';
  const meta = recommended
    ? `<span class="paper-lib-rec-badge">${escape(translate('paper.recommended'))}</span>${date}`
    : `${date}${page ? ` · ${page}` : ''}${entry.hasReport ? ' · report' : ''}`;
  const folderId = entry.folderId || '';
  let options = `<option value=""${folderId ? '' : ' selected'}>`
    + `${escape(translate('paper.folderNone', 'No folder'))}</option>`;
  (state()._paperFolders ?? []).forEach((folder) => {
    options += `<option value="${escape(folder.id)}"`
      + `${folder.id === folderId ? ' selected' : ''}>${escape(folder.name)}</option>`;
  });
  return `<div class="paper-lib-item${active ? ' active' : ''}`
    + `${recommended ? ' paper-lib-item-rec' : ''}" data-id="${entry.id}"`
    + ` data-tofu-action="_onPaperLibClick('${entry.id}')">`
    + '<div class="paper-lib-item-icon"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg></div>'
    + '<div class="paper-lib-item-info">'
    + `<span class="paper-lib-item-title" title="${escape(entry.title)}">${escape(entry.title)}</span>`
    + `<span class="paper-lib-item-meta">${meta}</span></div>`
    + `<select class="paper-lib-item-folder" title="${escape(translate('paper.folderMoveTo', 'Move to folder'))}" data-tofu-action="event.stopPropagation()" data-tofu-action-change="event.stopPropagation();_assignPaperFolder('${entry.id}', this.value)">${options}</select>`
    + `<button class="paper-lib-item-del" data-tofu-action="event.stopPropagation();_deletePaperEntry('${entry.id}')" title="${escape(translate('paper.delete', 'Delete'))}">`
    + '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg></button></div>';
}

export function onPaperLibClick(id: string): void {
  const entry = state()._paperLibrary?.find((paper) => paper.id === id);
  if (!entry) return;
  if (isRecommendedEntry(entry)) {
    setActivePaperId(entry.id);
    void globals()._fetchArxivPaper?.(entry.arxivId || '', entry.id);
  } else {
    openPaperEntry(entry);
  }
}

export function formatPaperDate(timestamp?: number): string {
  if (!timestamp) return '';
  const date = new Date(timestamp);
  const difference = Date.now() - date.getTime();
  if (difference < 86400000) {
    return `${date.getHours().toString().padStart(2, '0')}:`
      + date.getMinutes().toString().padStart(2, '0');
  }
  if (difference < 86400000 * 7) return `${Math.floor(difference / 86400000)}d ago`;
  return `${date.getMonth() + 1}/${date.getDate()}`;
}

export function installPaperLibraryGlobals(): void {
  const target = state();
  target._loadPaperFolders = loadPaperFolders;
  target._createPaperFolder = createPaperFolder;
  target._updatePaperFolder = updatePaperFolder;
  target._deletePaperFolder = deletePaperFolder;
  target._assignPaperFolder = assignPaperFolder;
  target._getPaperFolderById = getPaperFolderById;
  target._readPaperFolderCollapse = readPaperFolderCollapse;
  target._isPaperFolderCollapsed = isPaperFolderCollapsed;
  target._togglePaperFolderCollapse = togglePaperFolderCollapse;
  target._promptNewPaperFolder = promptNewPaperFolder;
  target._renamePaperFolder = renamePaperFolder;
  target._confirmDeletePaperFolder = confirmDeletePaperFolder;
  target._setActivePaperFolder = setActivePaperFolder;
  target._persistPaperEntry = persistPaperEntry;
  target._migrateLegacyLibrary = migrateLegacyLibrary;
  target._loadPaperLibrary = loadPaperLibrary;
  target._setActivePaperId = setActivePaperId;
  target._newPaperEntryId = newPaperEntryId;
  target._normArxivId = normalizeArxivId;
  target._isRecommendedEntry = isRecommendedEntry;
  target._findLibraryEntryByArxiv = findLibraryEntryByArxiv;
  target._persistRecommendedCard = persistRecommendedCard;
  target._createPaperEntry = createPaperEntry;
  target._getActivePaperEntry = getActivePaperEntry;
  target._saveActivePaperState = saveActivePaperState;
  target._deletePaperEntry = deletePaperEntry;
  target._openPaperEntry = openPaperEntry;
  target._renderPaperLibrary = renderPaperLibrary;
  target._paperFolderBarHTML = paperFolderBarHTML;
  target._paperLibItemHTML = paperLibItemHTML;
  target._onPaperLibClick = onPaperLibClick;
  target._formatPaperDate = formatPaperDate;
}

installPaperLibraryGlobals();
