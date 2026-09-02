import { featureRegistry } from '../../feature-registry';
import type { I18nKey } from '../../i18n';
import {
  paperAttachPush,
  paperDetachPush,
  paperIngestEvent,
  taskReplayIngestPage,
  type PaperPushEvent,
  type PaperPushState,
} from './push-transport';

type JsonObject = Record<string, unknown>;

interface PollResponse {
  ok: boolean;
  status: number;
  json(): Promise<JsonObject>;
}

interface BabelPaperApi {
  translateCache(paperHash: string, lang: string): Promise<JsonObject>;
  translateStart(body: JsonObject): Promise<JsonObject>;
  translateAbort(taskId: string): Promise<unknown>;
  translatePoll(taskId: string, cursor: number): Promise<PollResponse>;
}

type Translator = (key: string, vars?: JsonObject) => string;

type LegacyPaperWindow = Window & {
  Api?: { paper?: BabelPaperApi };
  t?: Translator;
  escapeHtml?: (value: unknown) => string;
  renderMarkdown?: (text: string) => string;
  errorEnvelopeMessage?: (error: unknown) => string;
  _saveActivePaperState?: () => void;
  _paperParsedText?: string;
  _paperHash?: string;
  _babelTargetLang?: string;
  _babelTranslatedPages?: Record<string, string>;
  _babelTranslating?: boolean;
  _initBabelPdfTab?: () => void;
  _switchBabelLang?: (lang: string, button?: Element | null) => void;
  _startBabelTranslation?: () => void;
  _babelTranslateAllPages?: (lang: string) => Promise<void>;
  _renderBabelResult?: (text: string) => void;
};

function globals(): LegacyPaperWindow {
  return featureRegistry as unknown as LegacyPaperWindow;
}

function translate(key: I18nKey, vars?: JsonObject): string {
  const fn = globals().t;
  return typeof fn === 'function' ? fn(key, vars) : key;
}

function escape(value: unknown): string {
  const fn = globals().escapeHtml;
  if (typeof fn === 'function') return fn(value);
  const span = document.createElement('span');
  span.textContent = value == null ? '' : String(value);
  return span.innerHTML;
}

function errorMessage(error: unknown, fallback: string): string {
  const envelope = globals().errorEnvelopeMessage;
  if (typeof envelope === 'function') {
    const message = envelope(error);
    if (message) return message;
  }
  if (typeof error === 'string' && error) return error;
  if (error instanceof Error && error.message) return error.message;
  return fallback;
}

function api(): BabelPaperApi {
  const paper = globals().Api?.paper;
  if (!paper) throw new Error('Paper API unavailable');
  return paper;
}

function translatedPages(): Record<string, string> {
  const state = globals();
  if (!state._babelTranslatedPages) state._babelTranslatedPages = {};
  return state._babelTranslatedPages;
}

function emptyStateHtml(): string {
  return '<div class="babel-pdf-empty">'
    + '<svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.4"><path d="M5 8l6 6"/><path d="M4 14l6-6 2-3"/><path d="M2 5h12"/><path d="M7 2v3"/><path d="M22 22l-5-10-5 10"/><path d="M14 18h6"/></svg>'
    + `<p>${escape(translate('paper.babelEmptyTitle'))}</p>`
    + `<p class="babel-pdf-hint">${escape(translate('paper.babelEmptyHint'))}</p>`
    + '</div>';
}

export function renderBabelResult(text: string): void {
  const body = document.getElementById('babelPdfBody');
  if (!body) return;
  const render = globals().renderMarkdown;
  body.innerHTML = typeof render === 'function'
    ? render(text)
    : `<pre style="white-space:pre-wrap;font-size:13px;line-height:1.7">${escape(text)}</pre>`;
}

export function initBabelPdfTab(): void {
  const container = document.getElementById('paperTranslateContent');
  if (!container) return;
  const lang = globals()._babelTargetLang ?? '';
  container.innerHTML = '<div class="babel-pdf-module"><div class="babel-pdf-brand">'
    + '<svg class="babel-pdf-icon" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'
    + '<path d="M5 8l6 6"/><path d="M4 14l6-6 2-3"/><path d="M2 5h12"/><path d="M7 2v3"/>'
    + '<path d="M22 22l-5-10-5 10"/><path d="M14 18h6"/></svg>'
    + `<div class="babel-pdf-brand-text"><span class="babel-pdf-title">Babel PDF</span><span class="babel-pdf-subtitle">${escape(translate('paper.babelSubtitle'))}</span></div>`
    + '</div><div class="babel-pdf-lang-bar">'
    + `<button class="babel-pdf-lang${!lang ? ' active' : ''}" data-lang="" data-tofu-action="_switchBabelLang('', this)">${escape(translate('paper.babelOriginal'))}</button>`
    + `<button class="babel-pdf-lang${lang === 'zh' ? ' active' : ''}" data-lang="zh" data-tofu-action="_switchBabelLang('zh', this)">中文</button>`
    + `<button class="babel-pdf-lang${lang === 'en' ? ' active' : ''}" data-lang="en" data-tofu-action="_switchBabelLang('en', this)">English</button>`
    + `<button class="babel-pdf-lang${lang === 'ja' ? ' active' : ''}" data-lang="ja" data-tofu-action="_switchBabelLang('ja', this)">日本語</button>`
    + '</div><div class="babel-pdf-body" id="babelPdfBody"></div>'
    + '<div class="babel-pdf-status" id="babelPdfStatus"></div></div>';

  const cached = lang ? translatedPages()[lang] : '';
  if (cached) renderBabelResult(cached);
  else if (lang && globals()._paperParsedText) startBabelTranslation();
  else {
    const body = document.getElementById('babelPdfBody');
    if (body) body.innerHTML = emptyStateHtml();
  }
}

export function switchBabelLang(lang: string, button?: Element | null): void {
  document.querySelectorAll('.babel-pdf-lang').forEach((node) => {
    node.classList.remove('active');
  });
  button?.classList.add('active');
  globals()._babelTargetLang = lang;
  startBabelTranslation();
}

export function startBabelTranslation(): void {
  const body = document.getElementById('babelPdfBody');
  const status = document.getElementById('babelPdfStatus');
  if (!body) return;
  const state = globals();
  const lang = state._babelTargetLang ?? '';
  if (!lang) {
    body.innerHTML = emptyStateHtml();
    if (status) status.textContent = '';
    return;
  }
  if (!state._paperParsedText) {
    body.innerHTML = `<div class="babel-pdf-empty"><p>${escape(translate('paper.babelNoPaper'))}</p></div>`;
    return;
  }
  const cached = translatedPages()[lang];
  if (cached) {
    renderBabelResult(cached);
    if (status) status.textContent = translate('paper.babelCompleteCached');
    return;
  }
  const labels: Record<string, string> = { zh: '中文', en: 'English', ja: '日本語' };
  const message = translate('paper.babelTranslatingTo', { lang: labels[lang] ?? lang });
  if (status) status.textContent = message;
  body.innerHTML = '<div class="paper-loading"><div class="paper-loading-spinner"></div>'
    + `<div>${escape(message)}</div><div class="babel-pdf-progress">`
    + '<div class="babel-pdf-progress-bar" id="babelProgressBar" style="width:0%"></div>'
    + '</div></div>';
  void babelTranslateAllPages(lang);
}

function numberValue(value: unknown, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback;
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

interface BabelStreamState extends PaperPushState {
  terminal: boolean;
  failure: string;
}

export async function babelTranslateAllPages(lang: string): Promise<void> {
  const state = globals();
  if (state._babelTranslating) return;
  state._babelTranslating = true;
  const bar = document.getElementById('babelProgressBar') as HTMLElement | null;
  const status = document.getElementById('babelPdfStatus');
  const setProgress = (done: number, total: number): void => {
    if (bar && total > 0) bar.style.width = `${Math.round((done / total) * 100)}%`;
    if (status) status.textContent = translate('paper.babelTranslatedCount', { done, total });
  };

  try {
    if (state._paperHash) {
      try {
        const cached = await api().translateCache(state._paperHash, lang);
        const text = stringValue(cached.text);
        if (cached.ok === true && text) {
          if (state._babelTargetLang === lang) {
            translatedPages()[lang] = text;
            renderBabelResult(text);
            state._saveActivePaperState?.();
            if (status) status.textContent = translate('paper.babelCompleteCached');
          }
          return;
        }
      } catch (error: unknown) {
        console.warn('[Babel] Cache lookup failed:', error);
      }
    }

    const started = await api().translateStart({
      paper_text: state._paperParsedText ?? '',
      lang,
      paper_hash: state._paperHash ?? '',
    });
    if (started.ok !== true) throw new Error(errorMessage(started.error, 'Translate start failed'));
    const cachedText = stringValue(started.text);
    if (started.cached === true && cachedText) {
      if (state._babelTargetLang === lang) {
        translatedPages()[lang] = cachedText;
        renderBabelResult(cachedText);
        state._saveActivePaperState?.();
        if (status) status.textContent = translate('paper.babelCompleteCached');
      }
      return;
    }
    const paperHash = stringValue(started.paper_hash);
    if (paperHash) state._paperHash = paperHash;
    const taskId = stringValue(started.task_id);
    if (!taskId) throw new Error('Translate task did not return task_id');

    let cursor = 0;
    const aggregated: string[] = [];
    const stream: BabelStreamState = { terminal: false, failure: '' };
    const finishTranslation = (text: string): void => {
      if (stream.terminal) return;
      stream.terminal = true;
      if (state._babelTargetLang !== lang) return;
      const complete = text || aggregated.join('\n\n');
      translatedPages()[lang] = complete;
      renderBabelResult(complete);
      state._saveActivePaperState?.();
      if (status) status.textContent = translate('paper.babelComplete');
    };
    const applyEvent = (_stream: BabelStreamState, event: PaperPushEvent): boolean => {
      const type = stringValue(event.type);
      if (type === 'chunk') {
        aggregated.push(stringValue(event.text));
        setProgress(numberValue(event.index) + 1, numberValue(event.total));
        if (state._babelTargetLang === lang) {
          renderBabelResult(aggregated.join('\n\n'));
        }
        return true;
      }
      if (type === 'done') {
        finishTranslation(stringValue(event.text));
        return true;
      }
      if (type === 'error' || type === 'aborted') {
        stream.failure = errorMessage(event.error, 'Translation failed');
        stream.terminal = true;
        return true;
      }
      return false;
    };
    const ingest = (event: PaperPushEvent): void => {
      paperIngestEvent(stream, event, applyEvent);
    };
    paperAttachPush(stream, taskId, {
      channel: 'paper-translate',
      isCurrent: () => state._babelTargetLang === lang,
      onEvent: ingest,
    });

    try {
    while (true) {
      if (state._babelTargetLang !== lang) {
        try {
          await api().translateAbort(taskId);
        } catch (error: unknown) {
          console.debug('[Babel] translation abort failed:', error);
        }
        return;
      }
      if (stream.terminal) {
        if (stream.failure) throw new Error(stream.failure);
        return;
      }
      const response = await api().translatePoll(taskId, cursor);
      if (!response?.ok) throw new Error(`Poll HTTP ${response?.status ?? 'no response'}`);
      const polled = await response.json();
      if (polled.ok !== true) throw new Error(errorMessage(polled.error, 'Poll failed'));
      const replay = taskReplayIngestPage(
        stream, polled, (_state, event) => applyEvent(stream, event), cursor,
      );
      cursor = replay.nextCursor;
      if (stream.failure) throw new Error(stream.failure);
      if (stream.terminal) return;
      if (polled.status === 'done') {
        finishTranslation(stringValue(polled.text));
        return;
      }
      if (polled.status === 'error') {
        throw new Error(errorMessage(polled.error, 'Translation failed'));
      }
      await new Promise<void>((resolve) => window.setTimeout(resolve, 700));
    }
    } finally {
      paperDetachPush(stream);
    }
  } catch (error: unknown) {
    console.warn('[Babel] Translation failed:', error);
    const body = document.getElementById('babelPdfBody');
    if (body && state._babelTargetLang === lang) {
      body.innerHTML = `<div class="paper-error">${escape(translate('paper.babelFailed'))}: ${escape(errorMessage(error, 'Translation failed'))}`
        + `<br><button class="paper-retry-btn" data-tofu-action="_startBabelTranslation()">${escape(translate('paper.retry'))}</button></div>`;
    }
    if (status) status.textContent = translate('paper.babelFailed');
  } finally {
    state._babelTranslating = false;
    const pendingLang = state._babelTargetLang ?? '';
    if (pendingLang && pendingLang !== lang && !translatedPages()[pendingLang]) {
      startBabelTranslation();
    }
  }
}

const state = globals();
state._babelTargetLang ??= '';
state._babelTranslatedPages ??= {};
state._babelTranslating ??= false;
state._initBabelPdfTab = initBabelPdfTab;
state._switchBabelLang = switchBabelLang;
state._startBabelTranslation = startBabelTranslation;
state._babelTranslateAllPages = babelTranslateAllPages;
state._renderBabelResult = renderBabelResult;
