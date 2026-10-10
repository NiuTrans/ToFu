/** Source-bound, on-demand section depth. Owns drawer jobs and cancellation;
 * canonical report/review language and stored sections stay authoritative. */
import { featureRegistry } from '../../feature-registry';
import { escapeHtml as escape } from '../../html-safety';
import type { I18nKey } from '../../i18n';
import { createButton, createIconButton, createSpinner } from '../../ui';
import {
  paperAttachPush,
  paperDetachPush,
  paperIngestEvent,
  taskReplayIngestPage,
  type PaperPushEvent,
  type PaperPushState,
} from './push-transport';

type JsonObject = Record<string, unknown>;
type DeepenMode = 'deeper' | 'derive';
type DeepenStatus = 'running' | 'done' | 'error' | 'aborted';

interface PollResponse {
  ok: boolean;
  json(): Promise<JsonObject>;
}

interface DeepenApi {
  deepenStart(body: JsonObject): Promise<JsonObject>;
  deepenPoll(taskId: string, cursor: number): Promise<PollResponse>;
  deepenAbort(taskId: string): Promise<unknown>;
  reportCache(body: JsonObject): Promise<JsonObject>;
}

interface ReportView extends JsonObject {
  kind?: string;
  model?: string;
  stream?: JsonObject;
  langKey?: () => string | null;
}

interface DeepenJob extends PaperPushState {
  taskId: string;
  status: DeepenStatus;
  content: string;
  drawer: HTMLElement;
  mode: DeepenMode;
  secIdx: number;
  stopped: boolean;
  current?: () => boolean;
  sourceCurrent?: () => boolean;
  wait?: { timer: number; resolve: () => void };
}

type PaperWindow = Window & {
  Api?: { paper?: DeepenApi };
  t?: (key: string) => string;
  renderMarkdown?: (text: string) => string;
  errorEnvelopeMessage?: (error: unknown) => string;
  _paperHash?: string;
  _activePaperId?: string;
  showToast?: (message: string) => void;
  _paperParsedText?: string;
  _reportView?: (kind: string) => ReportView | null;
  _activeReportLang?: () => string;
  _paperXpApplyMetaEvent?: (
    stream: JsonObject,
    event: JsonObject,
    view: ReportView,
  ) => void;
  _paperDeepenAfterRender?: typeof paperDeepenAfterRender;
  _deepenReportHeadings?: typeof deepenReportHeadings;
  _deepenApplyEvent?: typeof deepenApplyEvent;
  _destroyPaperDeepen?: () => void;
  _paperDeepenClickWired?: boolean;
};

function globals(): PaperWindow {
  return featureRegistry as unknown as PaperWindow;
}

function translate(key: I18nKey): string {
  const translator = globals().t;
  return typeof translator === 'function' ? translator(key) : key;
}

function api(): DeepenApi {
  const paper = globals().Api?.paper;
  if (!paper) throw new Error('Paper deepen API unavailable');
  return paper;
}

function stringValue(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function numberValue(value: unknown, fallback = 0): number {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function errorMessage(error: unknown, fallback: string): string {
  const helper = globals().errorEnvelopeMessage;
  if (typeof helper === 'function') {
    const message = helper(error);
    if (message) return message;
  }
  if (error instanceof Error && error.message) return error.message;
  return stringValue(error) || fallback;
}

const jobs: Record<string, DeepenJob | undefined> = Object.create(null) as Record<
  string, DeepenJob
>;

/** The report's own h2/h3 in document order, excluding enrichment UI. */
export function deepenReportHeadings(article: Element): HTMLElement[] {
  return Array.from(article.querySelectorAll<HTMLElement>('h2, h3')).filter(
    (heading) => !heading.closest(
      '.paper-xp-section, .paper-xp-card, .paper-xp-flip, '
      + '.paper-deepen-drawer, .paper-xp-recap',
    ),
  );
}

function deepenButton(mode: DeepenMode, sectionIndex: number, label: string): HTMLButtonElement {
  const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  icon.setAttribute('viewBox', '0 0 24 24'); icon.setAttribute('fill', 'none'); icon.setAttribute('stroke', 'currentColor'); icon.setAttribute('stroke-width', '2'); icon.setAttribute('aria-hidden', 'true');
  icon.innerHTML = '<circle cx="10" cy="10" r="6"/><path d="m15 15 5 5"/>';
  const button = createIconButton({ label, icon });
  button.classList.add('paper-deepen-btn', `xp-deep-${mode}`);
  button.dataset.mode = mode; button.dataset.secIdx = String(sectionIndex);
  button.setAttribute('aria-label', label); return button;
}

/** Idempotently install section and display-math deepen actions. */
export function paperDeepenAfterRender(
  article: Element | null,
  _container?: Element | null,
  _view?: ReportView | null,
): void {
  if (!article) return;
  Object.entries(jobs).forEach(([key, job]) => { if (job?.current && !job.current()) abortJob(key, job); });
  article.querySelectorAll('.paper-deepen-btn').forEach((button) => button.remove());
  const headings = deepenReportHeadings(article);
  headings.forEach((heading, index) => {
    heading.append(deepenButton('deeper', index, translate('paper.deepenBtn')));
    heading.dataset.deepenSec = String(index);
  });

  article.querySelectorAll<HTMLElement>('.katex-display').forEach((math) => {
    let sectionIndex = -1;
    for (let index = 0; index < headings.length; index += 1) {
      const relation = headings[index].compareDocumentPosition(math);
      if ((relation & 4) || headings[index].contains(math)) {
        sectionIndex = index;
      } else {
        break;
      }
    }
    if (sectionIndex >= 0) {
      math.insertAdjacentElement('afterend', deepenButton('derive', sectionIndex, translate('paper.deriveBtn')));
    }
  });
}

function drawerKey(mode: DeepenMode, sectionIndex: number): string {
  return `${mode}:${sectionIndex}`;
}

function deepenDrawer(
  article: Element,
  mode: DeepenMode,
  sectionIndex: number,
  jobKey: string,
): HTMLElement | null {
  const heading = deepenReportHeadings(article)[sectionIndex];
  if (!heading) return null;
  const key = jobKey;
  const existing = Array.from(
    article.querySelectorAll<HTMLElement>('.paper-deepen-drawer'),
  ).find((node) => node.dataset.drawer === key);
  if (existing) return existing;
  const drawer = document.createElement('div');
  drawer.className = 'paper-deepen-drawer';
  drawer.dataset.drawer = key;
  heading.insertAdjacentElement('afterend', drawer);
  return drawer;
}

function paintRunning(drawer: HTMLElement): void {
  const status = document.createElement('div'); status.className = 'paper-deepen-status';
  status.setAttribute('role', 'status'); status.append(createSpinner({ label: translate('paper.deepenRunning') }), document.createTextNode(translate('paper.deepenRunning')));
  const stop = createButton({ label: translate('paper.reportStop') }); stop.classList.add('paper-deepen-stop');
  drawer.replaceChildren(status, stop);
}

function formatTokens(value: unknown): string {
  const number = Math.trunc(numberValue(value));
  return number >= 1000 ? `${(number / 1000).toFixed(1)}k` : String(number);
}

function paintContent(
  drawer: HTMLElement,
  content: string,
  usage: JsonObject | null,
  cached: boolean,
): void {
  let meta = '';
  if (usage && (usage.prompt_tokens || usage.completion_tokens)) {
    meta = '<div class="paper-deepen-meta">'
      + (cached ? `${escape(translate('paper.deepenCached'))} · ` : '')
      + `${formatTokens(usage.prompt_tokens)} → `
      + `${formatTokens(usage.completion_tokens)} tok</div>`;
  }
  const render = globals().renderMarkdown;
  const body = typeof render === 'function' ? render(content) : escape(content);
  drawer.innerHTML = `<div class="paper-deepen-body">${body}</div>${meta}`;
}

function paintError(drawer: HTMLElement, message: string): void {
  drawer.innerHTML = '<div class="paper-deepen-status paper-deepen-error">'
    + `${escape(message || 'deepen failed')}</div>`;
}

async function syncReportMeta(view: ReportView | null, job: DeepenJob): Promise<void> {
  const state = globals();
  const lang = view?.langKey?.();
  if (!view || !lang || !state._paperHash || !state._paperXpApplyMetaEvent) return;
  try {
    const data = await api().reportCache({
      lang,
      paper_hash: state._paperHash,
    });
    if (job.current?.() === false) return;
    if (data.ok === true && data.meta && typeof data.meta === 'object') {
      state._paperXpApplyMetaEvent(
        view.stream ?? { kind: view.kind || 'report' },
        { type: 'report_meta', meta: data.meta },
        view,
      );
    }
  } catch (error: unknown) {
    console.debug('[Paper:Deepen] meta sync failed (non-fatal):', error);
  }
}

export function deepenApplyEvent(
  job: DeepenJob | null | undefined,
  event: PaperPushEvent | null | undefined,
  view: ReportView | null,
): boolean {
  if (!job || !event || job.stopped || job.current?.() === false) return false;
  const type = stringValue(event.type);
  if (type === 'delta') {
    job.content += stringValue(event.delta);
    const body = job.drawer.querySelector<HTMLElement>('.paper-deepen-body');
    if (body) body.textContent = job.content;
    return true;
  }
  if (type === 'delta_reset') {
    job.content = '';
    return true;
  }
  if (type === 'done') {
    job.status = 'done';
    paintContent(
      job.drawer,
      stringValue(event.content) || job.content,
      event.usage && typeof event.usage === 'object' ? event.usage as JsonObject : null,
      false,
    );
    paperDetachPush(job);
    void syncReportMeta(view, job);
    return true;
  }
  if (type === 'error') {
    job.status = 'error';
    paperDetachPush(job);
    paintError(job.drawer, errorMessage(event.error, 'deepen failed'));
    return true;
  }
  if (type === 'aborted') {
    job.status = 'aborted';
    paperDetachPush(job);
    paintError(job.drawer, translate('paper.reportStopped'));
    return true;
  }
  return false;
}

async function poll(job: DeepenJob, view: ReportView | null): Promise<void> {
  let cursor = job._replayCursor ?? 0;
  while (!job.stopped && job.status === 'running') {
    const response = await api().deepenPoll(job.taskId, cursor);
    if (job.current?.() === false) return;
    if (!response?.ok) throw new Error('poll failed');
    const data = await response.json();
    if (job.current?.() === false) return;
    if (data.ok === false) throw new Error(errorMessage(data.error, 'poll failed'));
    const replay = taskReplayIngestPage(
      job,
      data,
      (_state, event) => deepenApplyEvent(job, event, view),
      cursor,
    );
    cursor = replay.nextCursor;
    if (replay.done) {
      paperDetachPush(job);
      if ((job.status as DeepenStatus) !== 'done') {
        if (replay.status === 'error') paintError(job.drawer, errorMessage(data.error, 'deepen failed'));
        else if (replay.status === 'aborted') paintError(job.drawer, translate('paper.reportStopped'));
        else paintContent(job.drawer, job.content, null, false);
      }
      if (replay.status === 'done' || replay.status === 'error'
          || replay.status === 'aborted') job.status = replay.status;
      return;
    }
    await new Promise<void>((resolve) => { job.wait = { resolve, timer: window.setTimeout(() => { job.wait = undefined; resolve(); }, 800) }; });
  }
}

function requestAbort(job: DeepenJob, taskId: string): void {
  const paper = globals().Api?.paper; if (!paper || !taskId) return;
  void (async () => {
    try {
      const acknowledgement = await paper.deepenAbort(taskId) as JsonObject | null;
      if (acknowledgement?.ok !== true) throw new Error('abort unconfirmed');
    } catch {
      if (job.sourceCurrent?.()) globals().showToast?.(translate('paper.deepenStopUnconfirmed'));
    }
  })();
}

function abortJob(key: string, job: DeepenJob, removeDrawer = true): void {
  const wasRunning = job.status === 'running';
  job.stopped = true;
  job.status = 'aborted';
  paperDetachPush(job);
  if (job.wait) { window.clearTimeout(job.wait.timer); job.wait.resolve(); job.wait = undefined; }
  if (wasRunning && job.taskId) requestAbort(job, job.taskId);
  if (removeDrawer) job.drawer.remove();
  delete jobs[key];
}

async function start(button: HTMLElement): Promise<void> {
  const container = button.closest<HTMLElement>('#paperReportContent, #paperReviewContent, #paperRebuttalContent, .paper-report-body');
  const article = container?.querySelector('.paper-report-article');
  if (!article) return;
  const mode: DeepenMode = button.dataset.mode === 'derive' ? 'derive' : 'deeper';
  const sectionIndex = Number.parseInt(button.dataset.secIdx ?? '-1', 10);
  if (sectionIndex < 0) return;
  const state = globals();
  const kind = container?.id === 'paperReviewContent' ? 'review' : container?.id === 'paperRebuttalContent' ? 'rebuttal' : 'report';
  const view = state._reportView?.(kind) ?? null;
  const lang = view?.langKey?.() || state._activeReportLang?.() || 'en';
  const paperId = state._activePaperId || '', paperHash = state._paperHash || '';
  const key = JSON.stringify([paperId, paperHash, lang, drawerKey(mode, sectionIndex)]);
  const existing = jobs[key];
  if (existing && (existing.status === 'running' || existing.status === 'done')) {
    abortJob(key, existing);
    return;
  }
  const drawer = deepenDrawer(article, mode, sectionIndex, key);
  if (!drawer) return;
  const job: DeepenJob = {
    taskId: '', status: 'running', content: '', drawer, mode,
    secIdx: sectionIndex, stopped: false, _seqSeen: -1, _replayCursor: 0,
  };
  jobs[key] = job;
  job.sourceCurrent = () => (state._activePaperId || '') === paperId && (state._paperHash || '') === paperHash
    && (view?.langKey?.() || state._activeReportLang?.() || 'en') === lang;
  job.current = () => jobs[key] === job && !job.stopped && drawer.isConnected && job.sourceCurrent!();
  paintRunning(drawer);

  try {
    const data = await api().deepenStart({
      paper_hash: paperHash,
      section_idx: sectionIndex,
      mode,
      lang,
      paper_text: state._paperParsedText || '',
      model: view?.model,
    });
    if (!job.current()) {
      const lateId = stringValue(data.task_id);
      if (lateId) requestAbort(job, lateId);
      return;
    }
    if (data.ok !== true) {
      throw new Error(stringValue(data.error) || 'deepen start failed');
    }
    if (data.cached === true) {
      job.status = 'done';
      paintContent(
        drawer,
        stringValue(data.content),
        data.usage && typeof data.usage === 'object' ? data.usage as JsonObject : null,
        true,
      );
      return;
    }
    job.taskId = stringValue(data.task_id);
    if (!job.taskId) throw new Error('deepen start did not return task_id');
    paperAttachPush(job, job.taskId, {
      channel: 'paper',
      isCurrent: () => job.current?.() === true,
      onEvent(event) {
        paperIngestEvent(
          job,
          event,
          (_current, accepted) => deepenApplyEvent(job, accepted, view),
        );
      },
    });
    await poll(job, view);
  } catch (error: unknown) {
    if (!job.current()) return;
    console.warn('[Paper:Deepen] failed:', error);
    job.status = 'error';
    paperDetachPush(job);
    paintError(drawer, errorMessage(error, 'deepen failed'));
  }
}

const clickHandler = (event: Event): void => {
  const target = event.target && 'closest' in event.target
    ? event.target as Element
    : null;
  const stopButton = target?.closest<HTMLElement>('.paper-deepen-stop');
  if (stopButton) {
    const drawer = stopButton.closest<HTMLElement>('.paper-deepen-drawer');
    const key = drawer?.dataset.drawer ?? '';
    const job = jobs[key];
    if (job) abortJob(key, job);
    return;
  }
  const deepenButtonElement = target?.closest<HTMLElement>('.paper-deepen-btn');
  if (deepenButtonElement) void start(deepenButtonElement);
};

export function cancelPaperDeepenJobs(): void {
  Object.entries(jobs).forEach(([key, job]) => {
    if (job) abortJob(key, job);
  });
}

export function destroyPaperDeepen(): void {
  document.removeEventListener('click', clickHandler);
  cancelPaperDeepenJobs();
  globals()._paperDeepenClickWired = false;
}

export function installPaperDeepenGlobals(): void {
  const target = globals();
  target._paperDeepenAfterRender = paperDeepenAfterRender;
  target._deepenReportHeadings = deepenReportHeadings;
  target._deepenApplyEvent = deepenApplyEvent;
  target._destroyPaperDeepen = destroyPaperDeepen;
  (target as PaperWindow & { _cancelPaperDeepenJobs: typeof cancelPaperDeepenJobs })._cancelPaperDeepenJobs = cancelPaperDeepenJobs;
  if (!target._paperDeepenClickWired) {
    target._paperDeepenClickWired = true;
    document.addEventListener('click', clickHandler);
  }
}

installPaperDeepenGlobals();
