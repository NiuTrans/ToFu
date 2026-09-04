/**
 * Typed owner for Paper Report/Review generation, bounded preferences and
 * drafts, reconstructible reading state, and resumable task lifecycle.
 * Installs its retained-runtime command boundary through featureRegistry;
 * depends on the i18n and Paper API ports.
 */
import {
  featureRegistry,
  readLiveRuntimeBinding,
  writeLiveRuntimeBinding,
} from '../../feature-registry';
import { escapeHtml } from '../../html-safety';
import { _i18nLang } from '../../i18n';
import { BoundedMap } from '../../core/bounded-map';
import { paperSourceRetryRequired } from './source-start';
type LooseObject = Record<string, any>;
type ReportWindow = Window & Record<string, any>;
type ReviewLanguage = 'en' | 'zh';

const REPORT_LANGUAGE_STORAGE_KEY = 'paper_report_lang_by_id';
const REVIEW_LANGUAGE_STORAGE_KEY = 'paper_review_lang_by_id';
const READING_POSITION_STORAGE_KEY = 'paper_read_pos_by_key';
const LANGUAGE_PREFERENCE_MAX_ENTRIES = 2_048;
const READING_POSITION_MAX_ENTRIES = 2_048;
const REPORT_SNAPSHOT_MAX_ENTRIES = 12;
const PAPER_REPORT_SOURCE_MAX_CHARS = 120_000;
const REBUTTAL_TEXT_STORAGE_KEY = 'paper_rebuttal_text_by_id';
const REBUTTAL_TEXT_MAX_ENTRIES = 32;
const REBUTTAL_TEXT_MAX_CHARS = 40_000;
const reportSnapshots = new BoundedMap<string, LooseObject>(
  REPORT_SNAPSHOT_MAX_ENTRIES,
);
const reportStartGenerations = new Map<string, number>();
let reportLoadGeneration = 0;
let reviewTranslationGeneration = 0;
let reviewTranslationTaskId = '';
let reviewAbortRequest: Promise<void> | null = null;
let paperRebuttalInputText = '';

function globals(): ReportWindow {
  return featureRegistry as unknown as ReportWindow;
}

function activePaperHash(): string {
  return String(readLiveRuntimeBinding('_paperHash') || '').trim();
}

function setActivePaperHash(value: unknown): void {
  writeLiveRuntimeBinding('_paperHash', String(value || ''));
}

function paperApi(): LooseObject | undefined {
  const api = globals().Api as LooseObject | undefined;
  return api?.paper as LooseObject | undefined;
}

function reportView(kind = 'report'): LooseObject | null {
  return (globals()._reportView?.(kind) as LooseObject | null | undefined) ?? null;
}

function errorText(error: unknown, fallback: string): string {
  const helper = globals().errorEnvelopeMessage;
  const normalized = typeof helper === 'function' ? String(helper(error) || '') : '';
  return normalized || (typeof error === 'string' ? error : '') || fallback;
}

export function setReportRegenIntent(
  paperHash: string,
  lang = 'en',
  key?: string,
): void {
  const storageKey = key || globals()._REPORT_REGEN_INTENT_KEY;
  if (!storageKey) return;
  try {
    if (!paperHash) {
      localStorage.removeItem(storageKey);
      return;
    }
    localStorage.setItem(storageKey, JSON.stringify({ paperHash, lang, ts: Date.now() }));
  } catch (error: unknown) {
    console.warn('[Paper:Report] persist regen intent failed:', error);
  }
}

export function getReportRegenIntent(key?: string): LooseObject | null {
  const storageKey = key || globals()._REPORT_REGEN_INTENT_KEY;
  if (!storageKey) return null;
  try {
    const raw = localStorage.getItem(storageKey);
    return raw ? JSON.parse(raw) as LooseObject : null;
  } catch (error: unknown) {
    console.warn('[Paper:Report] read regen intent failed:', error);
    return null;
  }
}

export function clearReportRegenIntent(key?: string): void {
  const storageKey = key || globals()._REPORT_REGEN_INTENT_KEY;
  if (!storageKey) return;
  try { localStorage.removeItem(storageKey); }
  catch (error: unknown) {
    console.warn('[Paper:Report] clear regen intent failed:', error);
  }
}

export function hasReportRegenIntent(
  paperHash: string,
  lang = 'en',
  key?: string,
): boolean {
  const intent = getReportRegenIntent(key);
  return Boolean(intent?.paperHash === paperHash && (intent.lang || 'en') === lang);
}

/** Return a bounded, validated per-paper language preference map. */
function readLanguageMap(storageKey: string): Record<string, ReviewLanguage> {
  const result = Object.create(null) as Record<string, ReviewLanguage>;
  try {
    const raw = localStorage.getItem(storageKey);
    if (!raw) return result;
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return result;
    const entries = Object.entries(parsed as Record<string, unknown>);
    const start = Math.max(0, entries.length - LANGUAGE_PREFERENCE_MAX_ENTRIES);
    for (let index = start; index < entries.length; index += 1) {
      const [paperId, lang] = entries[index];
      if (paperId && paperId.length <= 256 && (lang === 'en' || lang === 'zh')) {
        result[paperId] = lang;
      }
    }
  } catch (error: unknown) {
    console.warn('[Paper:Review] read lang map failed:', error);
  }
  return result;
}

function persistLanguage(
  storageKey: string,
  paperId: string,
  lang: ReviewLanguage,
): void {
  if (!paperId || paperId.length > 256) return;
  try {
    const map = readLanguageMap(storageKey);
    delete map[paperId];
    map[paperId] = lang;
    const keys = Object.keys(map);
    for (
      let index = 0;
      index < keys.length - LANGUAGE_PREFERENCE_MAX_ENTRIES;
      index += 1
    ) {
      delete map[keys[index]];
    }
    localStorage.setItem(storageKey, JSON.stringify(map));
  } catch (error: unknown) {
    console.warn('[Paper:Review] persist lang failed:', error);
  }
}

function persistReviewLanguage(paperId: string, lang: ReviewLanguage): void {
  persistLanguage(REVIEW_LANGUAGE_STORAGE_KEY, paperId, lang);
}

export function activeReportLanguage(): ReviewLanguage {
  const fallback: ReviewLanguage = _i18nLang === 'zh' ? 'zh' : 'en';
  const paperId = String(globals()._activePaperId || '');
  return paperId
    ? readLanguageMap(REPORT_LANGUAGE_STORAGE_KEY)[paperId] ?? fallback
    : fallback;
}

export function persistReportLanguage(paperId: string, lang: string): void {
  if (lang === 'en' || lang === 'zh') {
    persistLanguage(REPORT_LANGUAGE_STORAGE_KEY, paperId, lang);
  }
}

export function activeReviewLanguage(): ReviewLanguage {
  const paperId = String(globals()._activePaperId || '');
  return paperId
    && readLanguageMap(REVIEW_LANGUAGE_STORAGE_KEY)[paperId] === 'zh'
    ? 'zh'
    : 'en';
}

export function reportSnapshotKey(viewArg?: LooseObject | null): string {
  const view = viewArg || reportView('report');
  return `${String(globals()._activePaperId || '')}::${String(view?.langKey?.() || '')}`;
}

export function rememberReportSnapshot(
  view: LooseObject,
  report: string,
  meta: unknown,
): void {
  if (!report) return;
  const key = reportSnapshotKey(view);
  reportSnapshots.set(key, { report, meta: meta || null });
}

export function getReportSnapshot(view: LooseObject): LooseObject | null {
  return reportSnapshots.get(reportSnapshotKey(view)) ?? null;
}

export function resetReportSnapshots(): void {
  reportSnapshots.clear();
}

export function syncReportLanguageToggle(viewArg?: LooseObject | null): void {
  const view = viewArg || reportView('report');
  if (!view) return;
  const wrap = document.getElementById(`${String(view.idPrefix || '')}LangToggle`);
  if (!wrap) return;
  const current = view.kind === 'review'
    ? activeReviewLanguage()
    : activeReportLanguage();
  wrap.querySelectorAll<HTMLButtonElement>('.paper-report-lang-opt').forEach((button) => {
    button.classList.toggle('active', button.dataset.lang === current);
  });
}

export function setReportLanguage(lang: string, kind = 'report'): void {
  if (lang !== 'en' && lang !== 'zh') return;
  const target = globals();
  const view = reportView(kind);
  if (!view) return;
  if (view.kind === 'review') {
    void setReviewLanguage(lang);
    syncReportLanguageToggle(view);
    return;
  }
  if (activeReportLanguage() === lang) return;
  if (view.cache) rememberReportSnapshot(view, view.cache, view.meta);
  const paperId = String(target._activePaperId || '');
  if (paperId) persistLanguage(REPORT_LANGUAGE_STORAGE_KEY, paperId, lang);
  syncReportLanguageToggle(view);
  target._resetReportLocalState?.(view);
  view.cache = '';
  const snapshot = getReportSnapshot(view);
  if (snapshot) {
    view.cache = snapshot.report;
    view.meta = snapshot.meta || null;
    if (target._paperActiveTab === 'report') {
      const container = document.getElementById(String(view.containerId || ''));
      if (container) target._renderFinalReport?.(
        container, snapshot.report, undefined, view,
      );
    }
    return;
  }
  if (target._paperActiveTab === 'report') {
    void loadOrGenerateReport(view);
  }
}

function escapeReportHtml(value: unknown): string {
  return escapeHtml(value);
}

function reportLoadIsCurrent(generation: number, paperId: string): boolean {
  return reportLoadGeneration === generation
    && String(globals()._activePaperId || '') === paperId;
}

async function resolveReportOpen(
  paperHash: string,
  langKey: string,
  paperText: string,
  allowLanguageFallback: boolean,
): Promise<LooseObject | null> {
  const api = paperApi();
  if (!api) return null;
  if (typeof api.reportResolve === 'function') {
    return await api.reportResolve(paperHash, langKey, paperText);
  }

  // Rolling-cache compatibility only. Current clients use reportResolve and
  // pay one HTTP request plus one preferred/fallback repository query.
  if (paperHash && typeof api.reportLookup === 'function') {
    try {
      const running = await api.reportLookup(paperHash, langKey);
      if (
        running?.ok
        && running.task_id
        && (running.status === 'running' || running.status === 'pending')
      ) {
        return running;
      }
    } catch (error: unknown) {
      console.warn('[Paper:Report] compatibility lookup failed:', error);
    }
  }
  if (typeof api.reportCache !== 'function') return null;
  try {
    const active = await api.reportCache(paperHash
      ? { paper_hash: paperHash, lang: langKey }
      : { paper_text: paperText, lang: langKey });
    if (active?.ok && active.report) return { ...active, lang: langKey };
  } catch (error: unknown) {
    console.warn('[Paper:Report] compatibility cache lookup failed:', error);
  }
  if (!allowLanguageFallback || !paperHash) return null;
  const otherLang = langKey === 'zh' ? 'en' : 'zh';
  try {
    const fallback = await api.reportCache({
      paper_hash: paperHash,
      lang: otherLang,
    });
    return fallback?.ok && fallback.report
      ? { ...fallback, lang: otherLang }
      : null;
  } catch (error: unknown) {
    console.warn('[Paper:Report] compatibility fallback lookup failed:', error);
    return null;
  }
}

function applyResolvedReport(
  view: LooseObject,
  container: HTMLElement,
  data: LooseObject,
  startPaperId: string,
  requestedLang: string,
): void {
  const target = globals();
  const resolvedLang = String(data.lang || requestedLang);
  if (
    view.kind === 'report'
    && (resolvedLang === 'en' || resolvedLang === 'zh')
    && resolvedLang !== requestedLang
  ) {
    persistReportLanguage(startPaperId, resolvedLang);
    syncReportLanguageToggle(view);
  }
  view.cache = String(data.report || '');
  view.meta = data.meta || null;
  if (typeof target._paperXpSet === 'function') {
    target._paperXpSet(view, '_xpInsight', data.insight || null);
    target._paperXpSet(view, '_xpCheckpoints', data.checkpoints || null);
  } else {
    view._xpInsight = data.insight || null;
    view._xpCheckpoints = data.checkpoints || null;
  }
  if (data.paper_hash) setActivePaperHash(data.paper_hash);
  rememberReportSnapshot(view, view.cache, view.meta);
  target._persistGeneratedReviewVenue?.(
    view, String(view.langKey?.() || resolvedLang), startPaperId,
  );
  target._saveActivePaperState?.('metadata');
  if (data.resolvedTitle) {
    target._applyResolvedTitle?.(data.resolvedTitle, startPaperId);
  }
  if (typeof target._renderFinalReport !== 'function') {
    throw new Error('Paper report renderer is unavailable');
  }
  target._renderFinalReport(container, view.cache, undefined, view);
}

export function renderReportStartPrompt(viewArg?: LooseObject | null): void {
  const view = viewArg || reportView('report');
  if (!view) return;
  const container = document.getElementById(String(view.containerId || ''));
  if (!container) return;
  const target = globals();
  target._syncReportToolbar?.(false, view);
  const isReview = view.kind === 'review';
  const generateAction = isReview
    ? '_generatePaperReview()'
    : '_generatePaperReport()';
  const translate = (key: string, fallback: string): string => (
    typeof target.t === 'function' ? String(target.t(key)) : fallback
  );
  const title = isReview
    ? translate('paper.reviewEmptyTitle', 'Generate peer review')
    : translate('paper.reportEmptyTitle', 'Generate report');
  const hint = isReview
    ? translate('paper.reviewEmptyHint', 'Choose a model and venue, then generate.')
    : translate('paper.reportEmptyHint', 'Choose a model and language, then generate.');
  const buttonLabel = isReview
    ? translate('paper.reviewGenerate', 'Generate review')
    : translate('paper.reportGenerate', 'Generate report');
  container.innerHTML =
    '<div class="paper-report-empty">'
      + `<p>${escapeReportHtml(title)}</p>`
      + `<p class="paper-report-hint">${escapeReportHtml(hint)}</p>`
      + `<button class="paper-report-generate-btn" data-tofu-action="${generateAction}">`
        + '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" '
          + 'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
          + 'stroke-linejoin="round"><path d="M5 3l14 9-14 9V3z"/></svg>'
        + `<span>${escapeReportHtml(buttonLabel)}</span>`
      + '</button>'
    + '</div>';
}

/** Resolve live work or persisted output without auto-starting paid work. */
export async function loadOrGenerateReport(
  viewArg?: LooseObject | null,
): Promise<void> {
  const view = viewArg || reportView('report');
  if (!view) return;
  const target = globals();
  const generation = ++reportLoadGeneration;
  const reportLang = String(view.uiLang?.() || 'en');
  const langKey = String(view.langKey?.() || reportLang);
  const startPaperId = String(target._activePaperId || '');

  if (view.stream?.paperId === startPaperId) {
    target._paintReportFromState?.(view);
    if (view.stream.status === 'running' && !view.stream.pollTimer) {
      target._attachReportPush?.(view, view.stream);
      void pollReportTask(view);
    } else if (view.stream.status === 'done') {
      restoreReviewReadingLanguage(view);
    }
    return;
  }

  if (hasReportRegenIntent(
    activePaperHash(), reportLang, view.regenIntentKey,
  )) {
    console.warn(
      '[Paper:Report] pending regenerate intent for hash='
      + activePaperHash()
      + ` lang=${reportLang} kind=${String(view.kind || '')}`
      + ' — resuming force-start (priority over lookup-reconnect)',
    );
    void generatePaperReport(true, view);
    return;
  }

  if (view.cache) {
    const container = document.getElementById(String(view.containerId || ''));
    if (container) {
      target._renderFinalReport?.(container, view.cache, undefined, view);
    }
    restoreReviewReadingLanguage(view);
    return;
  }

  const container = document.getElementById(String(view.containerId || ''));
  if (container) {
    const loading = typeof target.t === 'function'
      ? String(target.t('paper.loadingReport'))
      : 'Loading…';
    container.innerHTML =
      '<div class="paper-loading"><div class="paper-loading-spinner"></div>'
      + `<div>${escapeReportHtml(loading)}</div></div>`;
  }

  let resolved: LooseObject | null = null;
  try {
    resolved = await resolveReportOpen(
      activePaperHash(),
      langKey,
      String(target._paperParsedText || ''),
      view.kind === 'report',
    );
  } catch (error: unknown) {
    console.warn('[Paper:Report] fused lookup failed:', error);
  }
  if (!reportLoadIsCurrent(generation, startPaperId)) return;

  if (
    resolved?.ok
    && resolved.task_id
    && (resolved.status === 'running' || resolved.status === 'pending')
  ) {
    if (container) target._renderReportSkeleton?.(container, reportLang, view);
    view.stream = makeReportStreamState(
      startPaperId, reportLang, String(resolved.task_id), String(view.kind || 'report'),
    );
    target._syncReportToolbar?.(true, view);
    target._attachReportPush?.(view, view.stream);
    void pollReportTask(view);
    return;
  }
  if (resolved?.ok && resolved.report && container) {
    applyResolvedReport(view, container, resolved, startPaperId, langKey);
    return;
  }
  renderReportStartPrompt(view);
}

function reportStartKey(view: LooseObject): string {
  return String(view.kind || 'report');
}

function beginReportStart(view: LooseObject): number {
  const key = reportStartKey(view);
  const generation = (reportStartGenerations.get(key) ?? 0) + 1;
  reportStartGenerations.set(key, generation);
  return generation;
}

function invalidateReportStart(view: LooseObject): void {
  beginReportStart(view);
}

function reportStartIsCurrent(
  view: LooseObject,
  generation: number,
  paperId: string,
  provisionalStream?: LooseObject,
): boolean {
  return reportStartGenerations.get(reportStartKey(view)) === generation
    && String(globals()._activePaperId || '') === paperId
    && (!provisionalStream || view.stream === provisionalStream);
}

function reportGenerateAction(view: LooseObject): string {
  if (view.kind === 'review') return '_generatePaperReview()';
  if (view.kind === 'rebuttal') return '_generatePaperRebuttal()';
  return '_generatePaperReport()';
}

function readRebuttalTextMap(): Record<string, string> {
  const result = Object.create(null) as Record<string, string>;
  try {
    const raw = localStorage.getItem(REBUTTAL_TEXT_STORAGE_KEY);
    if (!raw) return result;
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return result;
    const entries = Object.entries(parsed as Record<string, unknown>);
    const start = Math.max(0, entries.length - REBUTTAL_TEXT_MAX_ENTRIES);
    for (let index = start; index < entries.length; index += 1) {
      const [paperId, value] = entries[index];
      if (paperId && paperId.length <= 256 && typeof value === 'string') {
        result[paperId] = value.slice(0, REBUTTAL_TEXT_MAX_CHARS);
      }
    }
  } catch (error: unknown) {
    console.warn('[Paper:Rebuttal] read input map failed:', error);
  }
  return result;
}

function persistRebuttalInput(): void {
  const paperId = String(globals()._activePaperId || '');
  if (!paperId || paperId.length > 256) return;
  try {
    const map = readRebuttalTextMap();
    delete map[paperId];
    const persisted = paperRebuttalInputText.slice(0, REBUTTAL_TEXT_MAX_CHARS);
    if (persisted) map[paperId] = persisted;
    const keys = Object.keys(map);
    for (
      let index = 0;
      index < keys.length - REBUTTAL_TEXT_MAX_ENTRIES;
      index += 1
    ) {
      delete map[keys[index]];
    }
    if (Object.keys(map).length) {
      localStorage.setItem(REBUTTAL_TEXT_STORAGE_KEY, JSON.stringify(map));
    } else {
      localStorage.removeItem(REBUTTAL_TEXT_STORAGE_KEY);
    }
  } catch (error: unknown) {
    console.warn('[Paper:Rebuttal] persist input failed:', error);
  }
}

export function onRebuttalInputChange(value: unknown): void {
  paperRebuttalInputText = String(value ?? '').slice(0, REBUTTAL_TEXT_MAX_CHARS);
  persistRebuttalInput();
}

export function restorePaperRebuttalInputText(): string {
  const paperId = String(globals()._activePaperId || '');
  paperRebuttalInputText = paperId ? (readRebuttalTextMap()[paperId] || '') : '';
  return paperRebuttalInputText;
}

function renderMissingPaperText(container: HTMLElement): void {
  const target = globals();
  const title = typeof target.t === 'function'
    ? String(target.t('paper.reportNoText'))
    : 'No paper text available.';
  container.innerHTML =
    `<div class="paper-report-empty"><p>${escapeReportHtml(title)}</p>`
    + '<p style="opacity:0.6;font-size:12px;margin-top:6px">'
    + 'The PDF may be scanned/image-only, or parsing failed. Try re-uploading.'
    + '</p></div>';
}

/** Start or join one report-family task with same-paper generation fencing. */
export async function generatePaperReport(
  force = false,
  viewArg?: LooseObject | null,
): Promise<void> {
  const view = viewArg || reportView('report');
  if (!view) return;
  const container = document.getElementById(String(view.containerId || ''));
  if (!container) return;
  const target = globals();
  const startPaperId = String(target._activePaperId || '');
  let generation = beginReportStart(view);
  reportLoadGeneration += 1;

  if (view.kind === 'review') {
    try {
      await target._resolveReviewVenue?.();
    } catch (error: unknown) {
      console.warn('[Paper:Review] venue resolve before generate failed:', error);
    }
    if (!reportStartIsCurrent(view, generation, startPaperId)) return;
  }

  const langKey = String(view.langKey?.() || 'en');
  const retryAction = reportGenerateAction(view);

  if (
    !force
    && view.stream?.paperId === startPaperId
    && view.stream?.status === 'running'
  ) {
    target._paintReportFromState?.(view);
    return;
  }
  if (view.cache && !force) {
    target._renderFinalReport?.(container, view.cache, undefined, view);
    restoreReviewReadingLanguage(view);
    return;
  }

  if (!target._paperParsedText) {
    container.innerHTML =
      '<div class="paper-loading"><div class="paper-loading-spinner"></div>'
      + '<div>Recovering paper text…</div></div>';
    let available = false;
    try {
      available = typeof target._ensurePaperText === 'function'
        ? Boolean(await target._ensurePaperText())
        : false;
    } catch (error: unknown) {
      console.warn('[Paper:Report] paper text recovery failed:', error);
    }
    if (!reportStartIsCurrent(view, generation, startPaperId)) return;
    if (!available) {
      renderMissingPaperText(container);
      return;
    }
  }

  const reportLang = String(view.uiLang?.() || 'en');
  if (!view.model) target._populatePaperReportModelDropdown?.(view);
  const reportModel = view.model || null;
  if (force || (view.stream && view.stream.paperId !== startPaperId)) {
    resetReportLocalState(view);
    generation = beginReportStart(view);
  }
  if (!reportStartIsCurrent(view, generation, startPaperId)) return;

  target._renderReportSkeleton?.(container, reportLang, view);
  const provisionalStream = makeReportStreamState(
    startPaperId, reportLang, '', String(view.kind || 'report'),
  );
  view.stream = provisionalStream;
  target._syncReportToolbar?.(true, view);

  try {
    const entry = target._getActivePaperEntry?.() as LooseObject | null | undefined;
    let clientTitle = String(
      entry?.title
      || target._paperFileName
      || String(target._paperPdfFilename || '').replace(/^\d+_/, ''),
    );
    if (clientTitle) clientTitle = clientTitle.replace(/\.pdf$/i, '').trim();
    const paperText = String(target._paperParsedText || '');
    const offeredPaperHash = activePaperHash();
    const paperHash = /^[a-f0-9]{8,64}$/.test(offeredPaperHash)
      ? offeredPaperHash
      : '';
    const startBody: LooseObject = {
      lang: langKey,
      model: reportModel,
      force: Boolean(force),
      title: clientTitle,
      filename: String(target._paperPdfFilename || ''),
    };
    if (paperHash) startBody.paper_hash = paperHash;
    else startBody.paper_text = paperText;
    if (view.kind === 'rebuttal') {
      startBody.author_rebuttal = paperRebuttalInputText.slice(
        0, REBUTTAL_TEXT_MAX_CHARS,
      );
    }
    const api = paperApi();
    if (typeof api?.reportStart !== 'function') {
      throw new Error('Paper report start API is unavailable');
    }
    let data: LooseObject;
    try {
      data = await api.reportStart(startBody) as LooseObject;
    } catch (error: unknown) {
      if (!reportStartIsCurrent(
        view, generation, startPaperId, provisionalStream,
      )) return;
      if (!paperHash || !paperText || !paperSourceRetryRequired(error)) throw error;
      console.warn(
        '[Paper:Report] Stored source unavailable — retrying start with paper text',
      );
      data = await api.reportStart({
        ...startBody,
        paper_text: paperText.slice(0, PAPER_REPORT_SOURCE_MAX_CHARS),
      }) as LooseObject;
    }
    if (!reportStartIsCurrent(
      view, generation, startPaperId, provisionalStream,
    )) return;
    if (!data?.ok) throw new Error(errorText(data?.error, 'Start failed'));

    const stopWasPending = Boolean(provisionalStream.pendingStop);
    if (data.cached && data.report) {
      view.stream = null;
      applyResolvedReport(view, container, data, startPaperId, langKey);
      return;
    }

    const taskId = String(data.task_id || '');
    if (!taskId) throw new Error('Report start response did not include a task id');
    if (data.paper_hash) setActivePaperHash(data.paper_hash);
    clearReportRegenIntent(view.regenIntentKey);
    const stream = makeReportStreamState(
      startPaperId, reportLang, taskId, String(view.kind || 'report'),
    );
    view.stream = stream;
    target._syncReportToolbar?.(true, view);
    target._attachReportPush?.(view, stream);
    void pollReportTask(view);
    if (stopWasPending) {
      console.warn(
        '[Paper:Report] Stop was pending during start — aborting task ' + taskId,
      );
      stopPaperReport(view);
    }
  } catch (error: unknown) {
    if (!reportStartIsCurrent(
      view, generation, startPaperId, provisionalStream,
    )) return;
    console.warn('[Paper:Report] start failed:', error);
    view.stream = null;
    target._syncReportToolbar?.(false, view);
    container.innerHTML =
      `<div class="paper-error">Failed: ${escapeReportHtml(errorText(error, 'Start failed'))}`
      + `<br><button data-tofu-action="${retryAction}" class="paper-retry-btn">`
      + `${escapeReportHtml(typeof target.t === 'function' ? target.t('paper.retry') : 'Retry')}`
      + '</button></div>';
  }
}

export async function generatePaperReview(force = false): Promise<void> {
  await generatePaperReport(force, reportView('review'));
}

export async function generatePaperRebuttal(force = false): Promise<void> {
  const target = globals();
  if (!paperRebuttalInputText.trim()) {
    target.showToast?.(
      typeof target.t === 'function'
        ? target.t('paper.rebuttalNeedText')
        : 'Paste the author rebuttal first',
    );
    return;
  }
  const reviewView = reportView('review');
  if (!reviewView?.cache && !reviewView?.stream?.fullText) {
    target.showToast?.(
      typeof target.t === 'function'
        ? target.t('paper.rebuttalNeedReview')
        : 'Generate the review first',
    );
    return;
  }
  await generatePaperReport(force, reportView('rebuttal'));
}

export async function regeneratePaperReport(
  viewArg?: LooseObject | null,
): Promise<void> {
  const view = viewArg || reportView('report');
  if (!view) return;
  const target = globals();
  setReportRegenIntent(
    activePaperHash(),
    String(view.uiLang?.() || 'en'),
    view.regenIntentKey,
  );
  resetReportLocalState(view);
  view.cache = '';
  await generatePaperReport(true, view);
}

export async function regeneratePaperReview(): Promise<void> {
  await regeneratePaperReport(reportView('review'));
}

export async function regeneratePaperRebuttal(): Promise<void> {
  await regeneratePaperReport(reportView('rebuttal'));
}

interface ReadingAnchor {
  frac?: number;
  index?: number;
  offset?: number;
}

function normalizeReadingAnchor(value: unknown): ReadingAnchor | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const offered = value as Record<string, unknown>;
  if (typeof offered.frac === 'number' && Number.isFinite(offered.frac)) {
    return { frac: Math.max(0, Math.min(1, offered.frac)) };
  }
  if (
    typeof offered.index === 'number'
    && Number.isInteger(offered.index)
    && offered.index >= 0
    && offered.index <= 100_000
    && typeof offered.offset === 'number'
    && Number.isFinite(offered.offset)
    && Math.abs(offered.offset) <= 1_000_000
  ) {
    return { index: offered.index, offset: offered.offset };
  }
  return null;
}

function readReadingPositionMap(): Record<string, ReadingAnchor> {
  const result = Object.create(null) as Record<string, ReadingAnchor>;
  try {
    const raw = localStorage.getItem(READING_POSITION_STORAGE_KEY);
    if (!raw) return result;
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return result;
    const entries = Object.entries(parsed as Record<string, unknown>);
    const start = Math.max(0, entries.length - READING_POSITION_MAX_ENTRIES);
    for (let index = start; index < entries.length; index += 1) {
      const [key, offered] = entries[index];
      const anchor = normalizeReadingAnchor(offered);
      if (key && key.length <= 512 && anchor) result[key] = anchor;
    }
  } catch (error: unknown) {
    console.warn('[Paper] read reading-position map failed:', error);
  }
  return result;
}

export function persistReadingPosition(
  viewArg: LooseObject | null | undefined,
  anchorValue: unknown,
): void {
  const view = viewArg || reportView('report');
  if (!view || !globals()._activePaperId) return;
  try {
    const map = readReadingPositionMap();
    const key = reportSnapshotKey(view);
    const anchor = normalizeReadingAnchor(anchorValue);
    delete map[key];
    if (anchor) map[key] = anchor;
    const keys = Object.keys(map);
    for (let index = 0; index < keys.length - READING_POSITION_MAX_ENTRIES; index += 1) {
      delete map[keys[index]];
    }
    localStorage.setItem(READING_POSITION_STORAGE_KEY, JSON.stringify(map));
  } catch (error: unknown) {
    console.warn('[Paper] persist reading-position failed:', error);
  }
}

export function loadReadingPosition(
  viewArg?: LooseObject | null,
): ReadingAnchor | null {
  const view = viewArg || reportView('report');
  if (!view || !globals()._activePaperId) return null;
  return readReadingPositionMap()[reportSnapshotKey(view)] ?? null;
}

export function captureReadingAnchor(scroller: HTMLElement | null): ReadingAnchor | null {
  try {
    if (!scroller || scroller.scrollTop <= 2) return null;
    const headings = scroller.querySelectorAll<HTMLElement>(
      '.paper-report-article h2, .paper-report-article h3',
    );
    if (!headings.length) {
      const maximum = scroller.scrollHeight - scroller.clientHeight;
      return maximum > 0 ? { frac: scroller.scrollTop / maximum } : null;
    }
    const scrollerTop = scroller.getBoundingClientRect().top;
    let best = 0;
    let bestAbove = Number.NEGATIVE_INFINITY;
    headings.forEach((heading, index) => {
      const relative = heading.getBoundingClientRect().top - scrollerTop;
      if (relative <= 1 && relative > bestAbove) {
        bestAbove = relative;
        best = index;
      }
    });
    const offset = headings[best].getBoundingClientRect().top - scrollerTop;
    return { index: best, offset };
  } catch (error: unknown) {
    console.debug('[Paper] captureReadingAnchor failed:', error);
    return null;
  }
}

export function restoreReadingAnchor(
  scroller: HTMLElement | null,
  article: HTMLElement | null,
  anchorValue: unknown,
): void {
  const anchor = normalizeReadingAnchor(anchorValue);
  if (!scroller || !article || !anchor) return;
  try {
    if (anchor.frac != null) {
      const maximum = scroller.scrollHeight - scroller.clientHeight;
      scroller.scrollTop = Math.max(0, Math.round(anchor.frac * maximum));
      return;
    }
    const headings = article.querySelectorAll<HTMLElement>('h2, h3');
    if (!headings.length || anchor.index == null) return;
    const index = Math.min(anchor.index, headings.length - 1);
    const scrollerTop = scroller.getBoundingClientRect().top;
    const headingTop = headings[index].getBoundingClientRect().top
      - scrollerTop + scroller.scrollTop;
    scroller.scrollTop = Math.max(0, Math.round(headingTop - (anchor.offset || 0)));
  } catch (error: unknown) {
    console.debug('[Paper] restoreReadingAnchor failed:', error);
  }
}

function syncReviewTranslationButton(): void {
  const target = globals();
  const wrap = document.getElementById('reviewLangToggle');
  const view = reportView('review');
  if (!wrap || !view) return;
  const hasReview = Boolean(view.cache);
  const current = activeReviewLanguage();
  wrap.style.opacity = hasReview ? '' : '0.5';
  wrap.querySelectorAll<HTMLButtonElement>('.paper-report-lang-opt').forEach((button) => {
    const isChinese = button.dataset.lang === 'zh';
    button.classList.toggle('active', button.dataset.lang === current);
    button.disabled = !hasReview || (isChinese && Boolean(target._paperReviewTranslating));
    if (!isChinese) return;
    button.classList.toggle('loading', Boolean(target._paperReviewTranslating));
    button.title = target._paperReviewTranslating
      ? (target.t?.('paper.reviewTranslating') || 'Translating…')
      : (target.t?.('paper.reviewTranslateTitle') || 'Read in Chinese');
  });
}

function reviewTranslationIsCurrent(generation: number, paperId: string): boolean {
  return reviewTranslationGeneration === generation
    && String(globals()._activePaperId || '') === paperId;
}

function renderReviewTranslation(
  text: unknown,
  generation: number,
  paperId: string,
  container: HTMLElement,
  view: LooseObject,
): boolean {
  if (!reviewTranslationIsCurrent(generation, paperId)) return false;
  const complete = typeof text === 'string' ? text : '';
  if (!complete) throw new Error('translation completed without text');
  const target = globals();
  if (typeof target._renderFinalReport !== 'function') {
    throw new Error('Paper review renderer is unavailable');
  }
  target._paperReviewTranslatedText = complete;
  target._paperReviewShowTranslation = true;
  target._renderFinalReport(container, complete, view.meta, view);
  return true;
}

function abortReviewTranslation(): void {
  reviewTranslationGeneration += 1;
  const taskId = reviewTranslationTaskId;
  reviewTranslationTaskId = '';
  globals()._paperReviewTranslating = false;
  if (!taskId) return;
  const request = Promise.resolve(paperApi()?.translateAbort(taskId))
    .then(() => undefined)
    .catch((error: unknown) => {
      console.debug('[Paper:Review] translation abort failed:', error);
    });
  reviewAbortRequest = request;
  void request.finally(() => {
    if (reviewAbortRequest === request) reviewAbortRequest = null;
  });
}

/** Restore the persisted reading language without regenerating the review. */
export function restoreReviewReadingLanguage(viewArg?: LooseObject | null): void {
  const view = viewArg || reportView('review');
  if (view?.kind === 'review' && view.cache && activeReviewLanguage() === 'zh') {
    void setReviewLanguage('zh');
  }
}

/** Translate only the canonical English review and generation-fence stale UI. */
export async function setReviewLanguage(lang: string): Promise<void> {
  if (lang !== 'en' && lang !== 'zh') return;
  const target = globals();
  const view = reportView('review');
  const container = view
    ? document.getElementById(String(view.containerId || ''))
    : null;
  if (!view || !container) return;
  const paperId = String(target._activePaperId || '');
  if (paperId) persistReviewLanguage(paperId, lang);

  if (lang === 'en') {
    abortReviewTranslation();
    target._paperReviewShowTranslation = false;
    if (view.cache && typeof target._renderFinalReport === 'function') {
      target._renderFinalReport(container, view.cache, view.meta, view);
    }
    syncReviewTranslationButton();
    return;
  }
  if (!view.cache) {
    syncReviewTranslationButton();
    return;
  }
  if (target._paperReviewTranslatedText) {
    target._paperReviewShowTranslation = true;
    target._renderFinalReport?.(
      container, target._paperReviewTranslatedText, view.meta, view,
    );
    syncReviewTranslationButton();
    return;
  }
  if (target._paperReviewTranslating) return;

  const generation = ++reviewTranslationGeneration;
  target._paperReviewTranslating = true;
  syncReviewTranslationButton();
  const translationKey = `review:${target._paperReviewVenue || 'generic'}:zh`;
  const api = paperApi();

  try {
    if (!api) throw new Error('Paper translation API unavailable');
    const paperHash = activePaperHash();
    if (paperHash) {
      try {
        const cached = await api.translateCache(paperHash, translationKey);
        if (cached?.ok && cached.text) {
          renderReviewTranslation(cached.text, generation, paperId, container, view);
          return;
        }
      } catch (error: unknown) {
        console.warn('[Paper:Review] translate cache lookup failed:', error);
      }
    }
    if (!reviewTranslationIsCurrent(generation, paperId)) return;
    if (reviewAbortRequest) await reviewAbortRequest;
    if (!reviewTranslationIsCurrent(generation, paperId)) return;

    const started = await api.translateStart({
      paper_text: view.cache,
      lang: translationKey,
      paper_hash: paperHash,
    });
    if (!started?.ok) throw new Error(errorText(started?.error, 'translate start failed'));
    if (started.cached && started.text) {
      renderReviewTranslation(started.text, generation, paperId, container, view);
      return;
    }
    if (started.paper_hash) setActivePaperHash(started.paper_hash);
    const taskId = String(started.task_id || '');
    if (!taskId) throw new Error('translate task returned no task_id');
    if (!reviewTranslationIsCurrent(generation, paperId)) {
      await api.translateAbort(taskId);
      return;
    }
    reviewTranslationTaskId = taskId;

    let cursor = 0;
    const parts: string[] = [];
    while (true) {
      if (!reviewTranslationIsCurrent(generation, paperId)) {
        try { await api.translateAbort(taskId); }
        catch (error: unknown) {
          console.debug('[Paper:Review] stale translation abort failed:', error);
        }
        return;
      }
      const response = await api.translatePoll(taskId, cursor);
      if (!response?.ok) throw new Error(`poll HTTP ${response?.status ?? 'none'}`);
      const data = await response.json() as LooseObject;
      if (!data.ok) throw new Error(errorText(data.error, 'poll failed'));
      const nextCursor = Number(data.next_cursor);
      if (Number.isFinite(nextCursor) && nextCursor >= cursor) cursor = nextCursor;
      const events = Array.isArray(data.events) ? data.events as LooseObject[] : [];
      for (const event of events) {
        if (event.type === 'chunk') {
          parts.push(typeof event.text === 'string' ? event.text : '');
        } else if (event.type === 'done') {
          renderReviewTranslation(
            event.text || parts.join('\n\n'), generation, paperId, container, view,
          );
          return;
        } else if (event.type === 'error') {
          throw new Error(errorText(event.error, 'translation failed'));
        }
      }
      if (data.status === 'error') {
        throw new Error(errorText(data.error, 'translation failed'));
      }
      if (data.status === 'aborted') throw new Error('translation aborted');
      if (data.status === 'done') {
        renderReviewTranslation(
          data.text || parts.join('\n\n'), generation, paperId, container, view,
        );
        return;
      }
      await new Promise<void>((resolve) => window.setTimeout(resolve, 700));
    }
  } catch (error: unknown) {
    if (!reviewTranslationIsCurrent(generation, paperId)) return;
    console.warn('[Paper:Review] translate failed:', error);
    target.showToast?.(
      target.t?.('paper.reviewTranslateFailed') || 'Translation failed',
      'error',
    );
  } finally {
    if (reviewTranslationGeneration === generation) {
      reviewTranslationTaskId = '';
      target._paperReviewTranslating = false;
      syncReviewTranslationButton();
    }
  }
}

export function resetReportLocalState(viewArg?: LooseObject | null): void {
  reportLoadGeneration += 1;
  const view = viewArg || reportView('report');
  if (!view) return;
  invalidateReportStart(view);
  const stream = view.stream as LooseObject | null | undefined;
  if (stream?.pollTimer) window.clearTimeout(stream.pollTimer);
  if (stream) globals()._detachReportPush?.(stream);
  view.stream = null;
  view.meta = null;
  if (view.kind === 'review') {
    abortReviewTranslation();
    globals()._paperReviewShowTranslation = false;
    globals()._paperReviewTranslatedText = '';
    globals()._paperReviewTranslating = false;
  }
  globals()._teardownReadingTracker?.(true);
}

export function resetAllReportViews(): void {
  const resetSnapshots = globals()._resetReportSnapshots;
  if (typeof resetSnapshots !== 'function') {
    throw new Error('Paper report snapshot-reset port is unavailable');
  }
  resetSnapshots();
  for (const kind of ['report', 'review', 'rebuttal']) {
    resetReportLocalState(reportView(kind));
  }
  paperRebuttalInputText = '';
}

export function makeReportStreamState(
  paperId = '',
  lang = 'en',
  taskId = '',
  kind = 'report',
): LooseObject {
  return {
    paperId,
    lang,
    kind,
    taskId,
    cursor: 0,
    status: 'running',
    pendingStop: false,
    stopRequested: false,
    fullText: '',
    thinkingText: '',
    toolRounds: [],
    segments: [],
    _segInferRound: 0,
    contentStarted: false,
    insightText: '',
    _insightRunning: false,
    _insightApplied: false,
    termfillText: '',
    _termfillApplied: false,
    meta: null,
    error: '',
    pollTimer: null,
    pollBusy: false,
    _lastRenderedLen: -1,
    _lastRenderedStatus: '',
    _lastToolKey: '',
  };
}

function schedulePoll(view: LooseObject, stream: LooseObject, delay: number): void {
  if (view.stream !== stream || stream.status !== 'running') return;
  if (stream.pollTimer) window.clearTimeout(stream.pollTimer);
  stream.pollTimer = window.setTimeout(() => void pollReportTask(view), delay);
}

export async function pollReportTask(viewArg?: LooseObject | null): Promise<void> {
  const view = viewArg || reportView('report');
  const stream = view?.stream as LooseObject | null | undefined;
  if (!view || !stream?.taskId || view.stream !== stream || stream.pollBusy) return;
  stream.pollBusy = true;
  try {
    const response = await paperApi()?.reportPoll(stream.taskId, stream.cursor);
    if (view.stream !== stream) return;
    if (!response?.ok) {
      if (response?.status === 404) {
        stream.status = 'error';
        stream.error = 'Task no longer available on server. Please regenerate.';
        globals()._paintReportFromState?.(view);
        return;
      }
      throw new Error(`HTTP ${response?.status ?? '?'}`);
    }
    const data = await response.json() as LooseObject;
    if (view.stream !== stream) return;
    if (!data.ok) {
      stream.status = 'error';
      stream.error = errorText(data.error, 'Poll failed');
      globals()._paintReportFromState?.(view);
      return;
    }
    const replay = globals().taskReplayIngestPage?.(
      stream,
      data,
      globals()._applyReportEventRaw,
      stream.cursor,
    ) ?? { nextCursor: stream.cursor };
    stream.cursor = replay.nextCursor;

    if (['done', 'aborted', 'error'].includes(String(data.status))) {
      clearReportRegenIntent(view.regenIntentKey);
    }
    if (data.status === 'done') {
      stream.status = 'done';
      if (data.report) {
        stream.fullText = data.report;
        if (stream.paperId === globals()._activePaperId) {
          view.cache = data.report;
          if (data.meta) {
            stream.meta = data.meta;
            view.meta = data.meta;
          }
          globals()._rememberReportSnapshot?.(view, data.report, data.meta);
          globals()._persistGeneratedReviewVenue?.(
            view, view.langKey?.(), stream.paperId,
          );
          globals()._saveActivePaperState?.('metadata');
        }
      }
      if (data.resolvedTitle) {
        globals()._applyResolvedTitle?.(data.resolvedTitle, stream.paperId);
      }
    } else if (data.status === 'aborted') {
      stream.status = 'aborted';
      if (typeof data.partial === 'string' && data.partial) {
        stream.fullText = data.partial;
        stream.contentStarted = true;
      }
    } else if (data.status === 'error') {
      stream.status = 'error';
      stream.error = errorText(data.error, stream.error || 'Report failed');
    }

    if (stream.paperId === globals()._activePaperId) {
      globals()._paintReportFromState?.(view);
    }
    if (stream.status === 'running' && view.stream === stream) {
      schedulePoll(view, stream, Number(globals()._REPORT_POLL_MS) || 1200);
    } else {
      stream.pollTimer = null;
      globals()._detachReportPush?.(stream);
    }
  } catch (error: unknown) {
    console.warn('[Paper:Report] Poll failed:', error);
    if (stream.status === 'running' && view.stream === stream) {
      schedulePoll(view, stream, Number(globals()._REPORT_POLL_BACKOFF_MS) || 3000);
    }
  } finally {
    stream.pollBusy = false;
  }
}

export function stopPaperReport(viewArg?: LooseObject | null): void {
  const view = viewArg || reportView('report');
  const stream = view?.stream as LooseObject | null | undefined;
  if (!view || !stream || stream.status !== 'running') return;
  stream.stopRequested = true;
  const stopButton = document.getElementById(String(view.stopBtnId || ''));
  if (stopButton instanceof HTMLButtonElement) {
    stopButton.disabled = true;
    const label = stopButton.querySelector('span');
    if (label) {
      label.textContent = globals().t?.('paper.reportStopping') || 'Stopping…';
    }
  }
  if (!stream.taskId) {
    stream.pendingStop = true;
    return;
  }
  void Promise.resolve(paperApi()?.reportAbort(stream.taskId)).catch((error: unknown) => {
    console.warn('[Paper:Report] stop request failed:', error);
  });
  const grace = Number(globals()._REPORT_ABORT_GRACE_MS) || 8000;
  window.setTimeout(() => {
    if (view.stream !== stream || stream.status !== 'running' || !stream.stopRequested) return;
    console.warn('[Paper:Report] abort not confirmed by server — forcing local aborted state for task '
      + stream.taskId);
    stream.status = 'aborted';
    if (stream.pollTimer) window.clearTimeout(stream.pollTimer);
    stream.pollTimer = null;
    globals()._detachReportPush?.(stream);
    if (stream.paperId === globals()._activePaperId) {
      globals()._paintReportFromState?.(view);
    }
  }, grace);
}

export function stopPaperReview(): void {
  stopPaperReport(reportView('review'));
}

export function stopPaperRebuttal(): void {
  stopPaperReport(reportView('rebuttal'));
}

export function destroyReportRuntime(): void {
  reportLoadGeneration += 1;
  abortReviewTranslation();
  for (const kind of ['report', 'review', 'rebuttal']) {
    const view = reportView(kind);
    if (view) invalidateReportStart(view);
    const stream = view?.stream as LooseObject | null | undefined;
    if (stream?.pollTimer) window.clearTimeout(stream.pollTimer);
    if (stream) globals()._detachReportPush?.(stream);
    if (view) view.stream = null;
  }
}

export function installReportRuntime(): void {
  const target = globals();
  Object.defineProperty(target, '_paperRebuttalInputText', {
    configurable: true,
    get: () => paperRebuttalInputText,
    set: (value: unknown) => {
      paperRebuttalInputText = String(value ?? '').slice(0, REBUTTAL_TEXT_MAX_CHARS);
    },
  });
  target._paperReviewShowTranslation ??= false;
  target._paperReviewTranslatedText ??= '';
  target._paperReviewTranslating ??= false;
  target._setReportRegenIntent = setReportRegenIntent;
  target._getReportRegenIntent = getReportRegenIntent;
  target._clearReportRegenIntent = clearReportRegenIntent;
  target._hasReportRegenIntent = hasReportRegenIntent;
  target._activeReportLang = activeReportLanguage;
  target._activeReviewLang = activeReviewLanguage;
  target._captureReadingAnchor = captureReadingAnchor;
  target._getReportSnapshot = getReportSnapshot;
  target._loadReadingPosition = loadReadingPosition;
  target._loadOrGenerateReport = loadOrGenerateReport;
  target._generatePaperReport = generatePaperReport;
  target._generatePaperReview = generatePaperReview;
  target._generatePaperRebuttal = generatePaperRebuttal;
  target._regeneratePaperReport = regeneratePaperReport;
  target._regeneratePaperReview = regeneratePaperReview;
  target._regeneratePaperRebuttal = regeneratePaperRebuttal;
  target._onRebuttalInputChange = onRebuttalInputChange;
  target._restorePaperRebuttalInputText = restorePaperRebuttalInputText;
  target._persistReadingPosition = persistReadingPosition;
  target._persistReportLang = persistReportLanguage;
  target._rememberReportSnapshot = rememberReportSnapshot;
  target._reportSnapshotKey = reportSnapshotKey;
  target._resetReportSnapshots = resetReportSnapshots;
  target._renderReportStartPrompt = renderReportStartPrompt;
  target._restoreReadingAnchor = restoreReadingAnchor;
  target._restoreReviewReadingLang = restoreReviewReadingLanguage;
  target._setReportLang = setReportLanguage;
  target._setReviewLang = setReviewLanguage;
  target._syncReportLangToggle = syncReportLanguageToggle;
  target._syncReviewTranslateBtn = syncReviewTranslationButton;
  target._resetReportLocalState = resetReportLocalState;
  target._resetAllReportViews = resetAllReportViews;
  target._makeReportStreamState = makeReportStreamState;
  target._pollReportTask = pollReportTask;
  target._stopPaperReport = stopPaperReport;
  target._stopPaperReview = stopPaperReview;
  target._stopPaperRebuttal = stopPaperRebuttal;
  target._destroyReportRuntime = destroyReportRuntime;
}

installReportRuntime();
