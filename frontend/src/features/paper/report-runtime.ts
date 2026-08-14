import { featureRegistry } from '../../feature-registry';
type LooseObject = Record<string, any>;
type ReportWindow = Window & Record<string, any>;

function globals(): ReportWindow {
  return featureRegistry as unknown as ReportWindow;
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

export function resetReportLocalState(viewArg?: LooseObject | null): void {
  const view = viewArg || reportView('report');
  if (!view) return;
  const stream = view.stream as LooseObject | null | undefined;
  if (stream?.pollTimer) window.clearTimeout(stream.pollTimer);
  if (stream) globals()._detachReportPush?.(stream);
  view.stream = null;
  view.meta = null;
  if (view.kind === 'review') {
    globals()._paperReviewShowTranslation = false;
    globals()._paperReviewTranslatedText = '';
    globals()._paperReviewTranslating = false;
  }
  globals()._teardownReadingTracker?.(true);
}

export function resetAllReportViews(): void {
  globals()._resetReportSnapshots?.();
  for (const kind of ['report', 'review', 'rebuttal']) {
    resetReportLocalState(reportView(kind));
  }
  globals()._paperRebuttalInputText = '';
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
          globals()._saveActivePaperState?.();
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
  for (const kind of ['report', 'review', 'rebuttal']) {
    const view = reportView(kind);
    const stream = view?.stream as LooseObject | null | undefined;
    if (stream?.pollTimer) window.clearTimeout(stream.pollTimer);
    if (stream) globals()._detachReportPush?.(stream);
    if (view) view.stream = null;
  }
}

export function installReportRuntime(): void {
  const target = globals();
  target._setReportRegenIntent = setReportRegenIntent;
  target._getReportRegenIntent = getReportRegenIntent;
  target._clearReportRegenIntent = clearReportRegenIntent;
  target._hasReportRegenIntent = hasReportRegenIntent;
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
