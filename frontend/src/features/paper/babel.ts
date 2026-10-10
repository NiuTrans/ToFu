/** Paper translation runtime: scoped runs, cached artifacts and explicit starts.
 * Retained Babel commands delegate here; babel-reader owns presentation only.
 * Every async result is fenced by run, source and language. Cancellation reuses
 * the server's paper-translate task; providers remain server-owned.
 */
import { featureRegistry } from '../../feature-registry';
import { renderTranslationReader, destroyTranslationReader, type TranslationView } from './babel-reader';
import { paperAttachPush, paperDetachPush, taskReplayIngestPage, paperIngestEvent,
  type PaperPushEvent, type PaperPushState } from './push-transport';
import type { PaperSaveScope } from './library';

type JsonObject = Record<string, unknown>;
interface PaperApi {
  translateCache(hash: string, lang: string): Promise<JsonObject | null>;
  translateStart(body: JsonObject): Promise<JsonObject>;
  translateAbort(id: string): Promise<JsonObject | null>;
  translatePoll(id: string, cursor: number): Promise<Response | null>;
}
interface Ports {
  Api?: { paper?: PaperApi }; _activePaperId?: string; _paperHash?: string;
  _paperFileName?: string; _paperParsedText?: string; _babelTargetLang?: string;
  _babelTranslatedPages?: Record<string, string>; _babelTranslating?: boolean;
  _saveActivePaperState?: (scope?: PaperSaveScope) => void;
  errorEnvelopeMessage?: (error: unknown) => string;
}
interface Run extends PaperPushState {
  paperId: string; hash: string; lang: string; taskId: string; cancelled: boolean;
  abortRequested: boolean; terminal: boolean; failure: string;
  waitTimer?: number; wake?: () => void;
}
const ports = (): Ports => featureRegistry as unknown as Ports;
const string = (value: unknown): string => typeof value === 'string' ? value : '';
let active: Run | null = null;
let lookupGeneration = 0;
let view: TranslationView = { lang: '', phase: 'idle', text: '', done: 0, total: 0 };
let sourceKey = '';
let stoppedRun: Run | null = null;
// Binding metadata must not retain an evicted library artifact.
let cacheScope: { key: string; pages: WeakRef<Record<string, string>> } | null = null;

function identity(): string { return JSON.stringify([ports()._activePaperId || '', ports()._paperHash || '']); }
function api(): PaperApi { const value = ports().Api?.paper; if (!value) throw new Error('Paper API unavailable'); return value; }
function cached(): Record<string, string> {
  const key = identity();
  let pages = ports()._babelTranslatedPages ??= {};
  // Library hydration supplies a different owned map. Reusing the same map
  // after a hash/id replacement must never reuse the previous source's text.
  if (cacheScope && cacheScope.key !== key && cacheScope.pages.deref() === pages) {
    pages = {}; ports()._babelTranslatedPages = pages;
  }
  cacheScope = { key, pages: new WeakRef(pages) }; return pages;
}
function current(run: Run): boolean {
  return active === run && !run.cancelled && (ports()._activePaperId || '') === run.paperId
    && (ports()._paperHash || '') === run.hash && ports()._babelTargetLang === run.lang;
}
function errorMessage(error: unknown): string {
  return ports().errorEnvelopeMessage?.(error) || (error instanceof Error ? error.message : string(error));
}
function paint(): void {
  const host = document.getElementById('paperTranslateContent');
  if (host) renderTranslationReader(host, view, { select: switchBabelLang, generate: startBabelTranslation, stop: stopBabelTranslation });
}
function releaseWait(run: Run): void {
  if (run.waitTimer != null) window.clearTimeout(run.waitTimer);
  run.waitTimer = undefined; run.wake?.(); run.wake = undefined;
}
async function abortTask(run: Run): Promise<void> {
  if (!run.taskId || run.abortRequested || run.terminal) return;
  run.abortRequested = true;
  try {
    const result = await api().translateAbort(run.taskId);
    if (!result?.ok) throw new Error('Translation stop was not acknowledged');
  } catch (error: unknown) {
    console.warn('[Paper:Translation] stop failed:', error);
    if (stoppedRun === run && identity() === JSON.stringify([run.paperId, run.hash]) && view.phase === 'stopped') {
      view = { ...view, stopUnconfirmed: true }; paint();
    }
  }
}
function cancelActive(): void {
  const run = active; active = null; ports()._babelTranslating = false;
  if (!run) return;
  run.cancelled = true; releaseWait(run); paperDetachPush(run); void abortTask(run);
}
export function destroyBabelRuntime(): void {
  lookupGeneration++; stoppedRun = null; cancelActive(); destroyTranslationReader(); sourceKey = '';
  view = { lang: '', phase: 'idle', text: '', done: 0, total: 0 };
}
export function stopBabelTranslation(): void {
  lookupGeneration++; stoppedRun = active; cancelActive(); view = { ...view, phase: 'stopped', failure: '', stopUnconfirmed: false }; paint();
}
export function renderBabelResult(text: string): void {
  view = { ...view, text, phase: 'done', failure: '' }; paint();
}
export function initBabelPdfTab(): void {
  if (!document.getElementById('paperTranslateContent')) return;
  const key = identity();
  if (sourceKey !== key) { destroyBabelRuntime(); sourceKey = key; }
  const lang = ports()._babelTargetLang || '';
  if (view.lang !== lang || (!active && view.phase === 'idle')) {
    const text = lang ? cached()[lang] || '' : '';
    view = { lang, phase: text ? 'done' : 'idle', text, done: 0, total: 0 };
  }
  paint();
  if (lang && !view.text && !active && view.phase === 'idle') void lookupTranslation(lang);
}
async function lookupTranslation(lang: string): Promise<void> {
  const token = ++lookupGeneration, key = identity(), hash = ports()._paperHash || '';
  if (!hash) return;
  view = { ...view, phase: 'loading' }; paint();
  try {
    const result = await api().translateCache(hash, lang);
    if (token !== lookupGeneration || key !== identity() || ports()._babelTargetLang !== lang) return;
    const text = result?.ok ? string(result.text) : '';
    if (text) cached()[lang] = text;
    view = { ...view, text, phase: text ? 'done' : 'idle', lookupFailed: result == null || result.ok === false }; paint();
  } catch {
    if (token !== lookupGeneration || key !== identity() || ports()._babelTargetLang !== lang) return;
    view = { ...view, phase: 'idle', lookupFailed: true }; paint();
  }
}
export function switchBabelLang(lang: string, _button?: Element | null): void {
  if (!['', 'zh', 'en', 'ja'].includes(lang) || lang === ports()._babelTargetLang) return;
  lookupGeneration++; stoppedRun = null; cancelActive(); ports()._babelTargetLang = lang;
  const text = cached()[lang] || '';
  view = { lang, text, phase: text ? 'done' : 'idle', done: 0, total: 0 }; paint();
  if (lang && !text) void lookupTranslation(lang);
}
export function startBabelTranslation(): void { void babelTranslateAllPages(ports()._babelTargetLang || ''); }
export async function babelTranslateAllPages(lang: string): Promise<void> {
  if (!['zh', 'en', 'ja'].includes(lang) || active || !ports()._paperParsedText || ports()._babelTargetLang !== lang) return;
  if (cached()[lang]) { renderBabelResult(cached()[lang]); return; }
  lookupGeneration++; stoppedRun = null;
  const run: Run = { paperId: ports()._activePaperId || '', hash: ports()._paperHash || '', lang,
    taskId: '', cancelled: false, abortRequested: false, terminal: false, failure: '' };
  const sourceText = ports()._paperParsedText || '';
  active = run; ports()._babelTranslating = true;
  view = { lang, text: '', phase: 'running', done: 0, total: 0 }; paint();
  const finish = (text: string): void => {
    if (run.terminal || !current(run)) return;
    // Progress never substitutes for the validated terminal artifact.
    if (!text) { run.failure = 'Translation task returned no completed artifact'; run.terminal = true; paperDetachPush(run); view = { ...view, phase: 'error', failure: run.failure }; paint(); return; }
    run.terminal = true; cached()[lang] = text;
    view = { ...view, text, phase: 'done' }; paint();
    ports()._saveActivePaperState?.('babel'); paperDetachPush(run); releaseWait(run);
  };
  const apply = (_state: Run, event: PaperPushEvent): boolean => {
    if (!current(run) || run.terminal) return false;
    if (event.type === 'chunk') {
      const total = Number(event.total), done = Number(event.index) + 1;
      view = { ...view, done: Number.isFinite(done) ? Math.max(0, done) : 0,
        total: Number.isFinite(total) ? Math.max(0, total) : 0 }; paint(); return true;
    }
    if (event.type === 'done') { finish(string(event.text)); return true; }
    if (event.type === 'error' || event.type === 'aborted') {
      run.failure = errorMessage(event.error) || (event.type === 'aborted' ? 'Translation was interrupted' : 'Translation failed');
      run.terminal = true; paperDetachPush(run); releaseWait(run); view = { ...view, phase: 'error', failure: run.failure }; paint(); return true;
    }
    return false;
  };
  try {
    if (run.hash) {
      try {
        const result = await api().translateCache(run.hash, lang);
        if (!current(run)) return;
        if (result?.ok && string(result.text)) { finish(string(result.text)); return; }
      } catch (error: unknown) { console.debug('[Paper:Translation] cache lookup failed:', error); }
    }
    if (!current(run)) return;
    const started = await api().translateStart({ paper_text: sourceText, paper_hash: run.hash, lang });
    run.taskId = string(started.task_id);
    if (!current(run)) { await abortTask(run); return; }
    if (!started.ok) throw new Error(errorMessage(started.error) || 'Translation start failed');
    const hash = string(started.paper_hash);
    if (hash && run.hash && hash !== run.hash) { await abortTask(run); throw new Error('Translation source identity mismatch'); }
    if (hash && !run.hash) { run.hash = hash; ports()._paperHash = hash; sourceKey = identity(); }
    if (started.cached && string(started.text)) { finish(string(started.text)); return; }
    if (!run.taskId) throw new Error('Translation task did not return task_id');
    paperAttachPush(run, run.taskId, { channel: 'paper-translate', isCurrent: () => current(run),
      onEvent: event => { paperIngestEvent(run, event, apply); } });
    let cursor = 0;
    while (current(run) && !run.terminal) {
      const response = await api().translatePoll(run.taskId, cursor);
      if (!current(run) || run.terminal) break;
      if (!response?.ok) throw new Error(`Poll HTTP ${response?.status ?? 'no response'}`);
      const result = await response.json();
      if (!current(run) || run.terminal) break;
      if (!result.ok) throw new Error(errorMessage(result.error) || 'Poll failed');
      const replay = taskReplayIngestPage(run, result, apply, cursor); cursor = replay.nextCursor;
      if (!run.terminal && result.status === 'done') finish(string(result.text));
      if (!run.terminal && ['error', 'aborted'].includes(string(result.status))) {
        run.failure = errorMessage(result.error) || 'Translation was interrupted'; run.terminal = true;
      }
      if (!run.terminal) await new Promise<void>(resolve => {
        run.wake = resolve; run.waitTimer = window.setTimeout(() => { run.waitTimer = undefined; run.wake = undefined; resolve(); }, 700);
      });
    }
    if (run.failure) throw new Error(run.failure);
  } catch (error: unknown) {
    if (current(run) && !view.text) { view = { ...view, phase: 'error', failure: errorMessage(error) }; paint(); }
  } finally {
    releaseWait(run); paperDetachPush(run);
    if (active === run) { active = null; ports()._babelTranslating = false; paint(); }
  }
}
Object.assign(featureRegistry, { _initBabelPdfTab: initBabelPdfTab, _switchBabelLang: switchBabelLang,
  _startBabelTranslation: startBabelTranslation, _babelTranslateAllPages: babelTranslateAllPages,
  _renderBabelResult: renderBabelResult, _destroyBabelRuntime: destroyBabelRuntime });
