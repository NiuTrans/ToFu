import { mergeLiterature } from '../state/literature';
import { projectResearchProgress } from '../state/progress';
/** Single research task lifecycle; shares the existing task replay/push transport. */
import { projectResearchCycle } from '../../../paper/research-cycle';
import { featureRegistry } from '../../../../feature-registry';
import { _i18nLang } from '../../../../i18n';
import {
  paperAttachPush,
  paperDetachPush,
  paperIngestEvent,
  taskReplayIngestPage,
  type PaperPushEvent,
} from '../../../paper/push-transport';

import { newResearchStream, type ResearchStream, type JsonObject, type ResearchStatus } from '../state/stream';
export { newResearchStream, type ResearchStream } from '../state/stream';
import type { TaskApi, ResearchApi, EvolutionRunsPort } from '../api/ports';
type ResearchWindow = Window & {
  config?: { model?: string };
  Api?: { tasks?: TaskApi; research?: ResearchApi };
  _evolutionRunsApi?: EvolutionRunsPort;
  debugLog?: (message: string, level?: string) => void;
  _researchStream?: ResearchStream | null;
  _researchPollTimer?: number | null;
  _paintResearch?: () => void;
  enterPaperMode?: () => Promise<void>;
  _fetchArxivPaper?: (arxivId: string) => Promise<unknown>;
  _researchReader?: (arxivId?: string) => Promise<void>;
  _newResearchStream?: typeof newResearchStream;
  _researchAdoptServerClocks?: typeof researchAdoptServerClocks;
  _researchApplyEvent?: typeof researchApplyEvent;
  _researchIngestEvent?: typeof researchIngestEvent;
  _researchApplySnapshot?: typeof researchApplySnapshot;
  _submitResearchDirection?: typeof submitResearchDirection;
  _startResearchJob?: typeof startResearchJob;
  _pollResearchOnce?: typeof pollResearchOnce;
  _researchIsRunning?: typeof researchIsRunning;
  _stopResearchPoll?: typeof stopResearchPoll;
  _scheduleResearchPoll?: typeof scheduleResearchPoll;
  _abortResearchJob?: typeof abortResearchJob;
  _openResearchFolder?: typeof openResearchFolder;
  _openResearchCorpusPaper?: typeof openResearchCorpusPaper;
  _researchApplyArtifacts?: typeof researchApplyArtifacts;
  _hydrateResearchFromStore?: typeof hydrateResearchFromStore;
  _restoreResearchFromStore?: typeof restoreResearchFromStore;
  _resumeInflightResearch?: typeof resumeInflightResearch;
  _destroyResearchRuntime?: typeof destroyResearchRuntime;
};

function globals(): ResearchWindow {
  return featureRegistry as unknown as ResearchWindow;
}

function tasks(): TaskApi {
  const api = globals().Api?.tasks;
  if (!api) throw new Error('Task API unavailable');
  return api;
}

function researchApi(): ResearchApi {
  const api = globals().Api?.research;
  if (!api) throw new Error('Research API unavailable');
  return api;
}

function paint(): void {
  globals()._paintResearch?.();
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error ?? '');
}

export function researchAdoptServerClocks(
  stream: ResearchStream | null | undefined,
  source: JsonObject | null | undefined,
): void {
  if (!stream || !source) return;
  const started = Number(source.createdAt);
  if (Number.isFinite(started) && started > 1e12 && started <= Date.now() + 60000) {
    if (!stream.startedAt || started < stream.startedAt) stream.startedAt = started;
  }
  const updated = Number(source.updatedAt);
  if (Number.isFinite(updated) && updated > 1e12
      && updated >= stream.lastEventAt) stream.lastEventAt = updated;
}

export function researchApplyEvent(
  stream: ResearchStream,
  event: PaperPushEvent,
): boolean {
  if (event.type === 'paper_done' && event.result && typeof event.result === 'object') {
    mergeLiterature(stream, { papers: [event.result] });
  }
  mergeLiterature(stream, event);
  if (event.type === 'done' || event.type === 'error' || event.type === 'aborted') {
    stream.status = event.status === 'aborted' || event.type === 'aborted'
      ? 'aborted' : event.type === 'error' ? 'error' : 'done';
    stream.cancelling = false;
  }
  const progress = projectResearchProgress(event.research_progress);
  if (progress && (!stream.progress || progress.revision >= stream.progress.revision)
      && (!stream.taskId || progress.run_id === stream.taskId)) stream.progress = progress;
  if (event.type === 'final' || event.type === 'done') {
    const payload = event.result && typeof event.result === 'object'
      ? event.result as JsonObject : event;
    researchApplyArtifacts(stream, payload);
  }
  if (typeof event.phase === 'string') stream.phase = event.phase;
  if ((event.type === 'stage_started' || event.type === 'stage_skipped'
      || event.type === 'stage_done') && typeof event.stage === 'string') {
    stream.phase = event.stage;
  }
  // Publish is an explicit fifth stage in the durable recipe. The final
  // publication boundary may be too fast to produce its own tool round, but a
  // stage event still makes it visible in the workbench trajectory.
  if (event.type === 'final' || event.type === 'done') stream.phase = 'publish';
  if (event.type === 'tool_start') {
    if (stream.toolRounds.length >= 200) stream.toolRounds.shift();
    stream.toolRounds.push({
      roundNum: event.roundNum,
      toolName: event.toolName,
      query: event.query || event.toolName,
      toolCallId: event.toolCallId || '',
      toolArgs: event.toolArgs || '',
      attentionKind: event.attentionKind,
      parentToolCallId: event.parentToolCallId,
      status: 'searching',
      results: null,
    });
    return true;
  }
  if (event.type === 'tool_done') {
    const round = [...stream.toolRounds].reverse().find((row) => (
      (event.toolCallId && row.toolCallId === event.toolCallId)
      || (!event.toolCallId && row.roundNum === event.roundNum
        && row.toolName === event.toolName)
    ));
    if (round) {
      round.status = 'done';
      if (typeof event.elapsed === 'number') round._elapsed = `${event.elapsed.toFixed(1)}s`;
      for (const field of [
        'toolContent', 'results', 'searchDiag', 'engineBreakdown', 'verticals',
      ]) {
        if (event[field]) round[field] = event[field];
      }
    }
    return true;
  }
  return Boolean(progress || event.phase || event.stage || event.type === 'paper_done' || event.type === 'literature_discovered');
}

export function researchIngestEvent(
  stream: ResearchStream,
  event: PaperPushEvent,
): boolean {
  return Boolean(paperIngestEvent(stream, event, researchApplyEvent));
}

export function researchApplySnapshot(
  stream: ResearchStream,
  snapshot: JsonObject,
): void {
  const terminal = !researchIsRunning(stream);
  const incomingRunning = snapshot.status === 'pending' || snapshot.status === 'running';
  if (terminal && incomingRunning) return;
  const cursor = snapshot.cursor && typeof snapshot.cursor === 'object'
    ? snapshot.cursor as JsonObject : null;
  const next = Number(cursor?.next ?? snapshot.next_cursor);
  if (Number.isFinite(next) && next > 0 && next < (stream._seqSeen ?? -1)) return;
  if (snapshot.meta && typeof snapshot.meta === 'object') {
    adoptEvolutionBinding(stream, snapshot.meta as JsonObject);
  }
  researchAdoptServerClocks(stream, snapshot);

  mergeLiterature(stream, snapshot);
  const progress = projectResearchProgress(snapshot.research_progress);
  if (progress && (!stream.progress || progress.revision >= stream.progress.revision)
      && (!stream.taskId || progress.run_id === stream.taskId)) stream.progress = progress;
  if (typeof snapshot.status === 'string') {
    // The task runtime exposes degraded as a terminal lifecycle status. Keep
    // the view's terminal predicate simple (done) while retaining the separate
    // product-quality axis that explains why the artifact needs scrutiny.
    if (snapshot.status === 'degraded') {
      stream.status = 'done';
      stream.degraded = true;
    } else {
      stream.status = snapshot.status as ResearchStatus;
    }
  }
  const quality = snapshot.artifact_quality;
  if (quality && typeof quality === 'object') {
    const row = quality as JsonObject;
    stream.degraded = Boolean(row.degraded) || stream.degraded;
    stream.degradedReason = typeof row.reason === 'string' ? row.reason : '';
  }
  const result = snapshot.result;
  if (result && typeof result === 'object') {
    const row = result as JsonObject;

    if (Array.isArray(row.accepted) || Array.isArray(row.rejected)) stream.verdictAvailable = true;

    if ('research_cycle' in row) stream.researchCycle = projectResearchCycle(row.research_cycle);
    const accepted = Array.isArray(row.accepted) ? row.accepted as JsonObject[] : [];
    const rejected = Array.isArray(row.rejected) ? row.rejected as JsonObject[] : [];
    stream.accepted = accepted.length;
    stream.rejected = rejected.length;
    stream.corpusSize = Number(row.corpus_size) || 0;
    if (Array.isArray(row.corpus_arxiv_ids)) {
      stream.corpusIds = row.corpus_arxiv_ids.map(String);
    }
    stream.gateReached = typeof row.gate_reached === 'string' ? row.gate_reached : '';
    if (typeof row.folder_id === 'string') stream.folderId = row.folder_id;
    if (Array.isArray(row.accepted)) stream.acceptedIdeas = accepted;
    if (Array.isArray(row.rejected)) stream.rejectedIdeas = rejected;
    if (row.threshold != null) stream.threshold = row.threshold;
    if (typeof row.survey_md === 'string') stream.surveyMd = row.survey_md;
    if (row.open_gaps && typeof row.open_gaps === 'object') {
      stream.openGaps = row.open_gaps as JsonObject;
    }
    if (row.evaluation && typeof row.evaluation === 'object') {
      stream.evaluation = row.evaluation as JsonObject;
    }
    if (row.usage && typeof row.usage === 'object') stream.usage = row.usage as JsonObject;
  }
  taskReplayIngestPage(
    stream,
    snapshot,
    (_current, event) => researchApplyEvent(stream, event),
    stream._replayCursor ?? 0,
  );
  const meta = snapshot.meta;
  if (meta && typeof meta === 'object') {
    const direction = (meta as JsonObject).direction;
    if (typeof direction === 'string' && !stream.direction) stream.direction = direction;
  }
}

function adoptEvolutionBinding(stream: ResearchStream, source: JsonObject): void {
  if (source.task_id != null && source.task_id !== stream.taskId) {
    throw new Error('Research record task identity changed');
  }
  const id = source.evolution_run_id;
  if (id == null) return;
  if (typeof id !== 'string' || !id.trim() || id.length > 160
      || (stream.evolutionRunId && stream.evolutionRunId !== id)) {
    throw new Error('Research Evolution binding is invalid or changed');
  }
  stream.evolutionRunId = id;
}

function directionFrom(id: string): string {
  const input = document.getElementById(id) as HTMLInputElement | HTMLTextAreaElement | null;
  return input?.value?.trim() ?? '';
}

export function submitResearchDirection(): void {
  const direction = directionFrom('paperResearchInput');
  if (!direction) {
    globals().debugLog?.('Describe the research direction to explore', 'warning');
    return;
  }
  void startResearchJob(direction);
}

export async function startResearchJob(direction: string): Promise<void> {
  const state = globals();
  if (researchIsRunning(state._researchStream)) return;
  if (state._researchStream) paperDetachPush(state._researchStream);
  stopResearchPoll();
  const stream = newResearchStream(direction);
  stream.status = 'submitting';
  stream.lang = _i18nLang;
  state._researchStream = stream;
  repaintResearchSurface();
  try {
    const research: { lang: string; harvest_n?: number; model?: string } = {
      lang: _i18nLang,
    };
    // Present only on the landing composer; retries omit it and the server
    // default applies.
    const harvest = Number((document.getElementById('researchHarvestN') as HTMLSelectElement | null)?.value);
    if (Number.isFinite(harvest) && harvest >= 3) research.harvest_n = Math.min(200, Math.round(harvest));
    const selectedModel = String(state.config?.model || '').trim();
    if (selectedModel) research.model = selectedModel;
    const port = state._evolutionRunsApi;
    if (!port?.createRun) throw new Error('Evolution runs API unavailable');
    // One idempotency key per distinct submission: an ambiguous retry of the
    // same direction re-joins the same run instead of minting a second one.
    // crypto.randomUUID needs a secure context; fall back for test harnesses.
    if (!stream.idempotencyKey) {
      stream.idempotencyKey = globalThis.crypto?.randomUUID?.()
        ?? `rs-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    }
    const created = await port.createRun(
      { objective: direction, domain: 'research', research },
      stream.idempotencyKey);
    const run = created?.run;
    const taskId = typeof run?.task_id === 'string' ? run.task_id : '';
    if (!taskId) {
      throw new Error(typeof run?.error === 'string' && run.error ? run.error : 'research start failed');
    }
    if (state._researchStream !== stream) return;
    if (!run?.run_id) throw new Error('Research Evolution run identity unavailable');
    adoptEvolutionBinding(stream, {evolution_run_id: run.run_id});
    stream.taskId = taskId;
    stream.status = 'running';
    attachStreamPush(stream);
    try { await pollResearchOnce(stream); } catch { stream.reconnecting = true; paint(); }
    scheduleResearchPoll(stream);
  } catch (error: unknown) {
    console.error('[Research] start failed:', error);
    if (state._researchStream === stream) {
      stream.status = 'error';
      stream.error = errorMessage(error);
      paint();
    }
  }
}

function researchTerminalEvent(event: PaperPushEvent): boolean {
  return event.type === 'done' || event.type === 'error' || event.type === 'aborted';
}

function attachStreamPush(stream: ResearchStream): void {
  if (!stream.taskId) return;
  paperAttachPush(stream, stream.taskId, {
    channel: 'research',
    isCurrent: () => globals()._researchStream === stream,
    isTerminal: researchTerminalEvent,
    onEvent(event) {
      if (researchIngestEvent(stream, event)) paint();
      if (researchTerminalEvent(event)) void pollResearchOnce(stream).catch(() => {
        stream.reconnecting = true;
        if (globals()._researchStream === stream) paint();
      });
    },
  });
}
const pollFlights = new WeakMap<ResearchStream, Promise<void>>();
export function pollResearchOnce(stream: ResearchStream): Promise<void> {
  const existing = pollFlights.get(stream);
  if (existing) return existing;
  const flight = readResearchSnapshot(stream).finally(() => pollFlights.delete(stream));
  pollFlights.set(stream, flight);
  return flight;
}
async function readResearchSnapshot(stream: ResearchStream): Promise<void> {
  if (!stream.taskId) return;
  const api = tasks();
  const cursor = stream._replayCursor ?? 0;
  const snapshot = typeof api.events === 'function'
    ? await api.events(stream.taskId, cursor)
    : await api.get(stream.taskId);
  if (globals()._researchStream !== stream) return;
  if (snapshot.ok === false) throw new Error(String(snapshot.error || 'Task snapshot unavailable'));
  stream.reconnecting = false;
  researchApplySnapshot(stream, snapshot);
  paint();
  if (!researchIsRunning(stream)) {
    stopResearchPoll();
    paperDetachPush(stream);
    if (!stream.hydrated) void hydrateResearchFromStore(stream);
  }
}

export function researchIsRunning(
  stream: ResearchStream | null | undefined,
): boolean {
  return stream?.status === 'submitting' || stream?.status === 'pending' || stream?.status === 'running';
}

export function stopResearchPoll(): void {
  const state = globals();
  if (state._researchPollTimer != null) {
    window.clearTimeout(state._researchPollTimer);
    state._researchPollTimer = null;
  }
}

export function scheduleResearchPoll(stream: ResearchStream): void {
  stopResearchPoll();
  if (globals()._researchStream !== stream || !researchIsRunning(stream)) return;
  globals()._researchPollTimer = window.setTimeout(async () => {
    if (globals()._researchStream !== stream) return;
    try { await pollResearchOnce(stream); } catch (error: unknown) {
      stream.reconnecting = true;
      if (globals()._researchStream === stream) paint();
      console.debug('[Research] poll failed:', error);
    }
    if (researchIsRunning(stream)) scheduleResearchPoll(stream);
  }, 2000);
}

export async function abortResearchJob(): Promise<void> {
  const stream = globals()._researchStream;
  if (!stream?.taskId) return;
  if (stream.cancelling) return;
  stream.cancelling = true;
  stream.commandError = '';
  paint();
  try {
    let result: JsonObject | null;
    if (stream.evolutionRunId) {
      const port = globals()._evolutionRunsApi;
      if (!port?.cancelRun) throw new Error('Evolution cancellation API unavailable');
      const response = await port.cancelRun(stream.evolutionRunId);
      if (response?.ok === false) throw new Error(String(response.error || 'Cancel failed'));
      if (response?.run?.run_id !== stream.evolutionRunId) throw new Error('Evolution cancellation identity changed');
      result = response;
    } else {
      result = await tasks().abort(stream.taskId) as JsonObject | null;
    }
    if (result?.ok === false) throw new Error(String(result.error || 'Cancel failed'));
    await pollResearchOnce(stream);
  } catch (error: unknown) {
    stream.commandError = errorMessage(error);
    stream.cancelling = false;
  }
  if (globals()._researchStream === stream) paint();
}

export function openResearchFolder(): void {
  if (!globals()._researchStream?.folderId) return;
  // The harvested corpus lives in the Paper library — that mode owns it.
  void globals()._researchReader?.();
}

/** The console lives on the Evolution workspace's research page; painting is
 * a no-op while that surface is unmounted, and the next mount repaints from
 * the authoritative stream, so events never force a navigation. */
function repaintResearchSurface(): void {
  paint();
}

export function openResearchCorpusPaper(index: number): void {
  const arxivId = globals()._researchStream?.corpusIds[index];
  if (arxivId) void globals()._researchReader?.(arxivId);
}

export function researchApplyArtifacts(
  stream: ResearchStream,
  payload: JsonObject | null | undefined,
): boolean {
  if (!payload || payload.found === false) return false;

  mergeLiterature(stream, payload);

  if ('research_cycle' in payload) stream.researchCycle = projectResearchCycle(payload.research_cycle);
  if (typeof payload.corpus_size === 'number') stream.corpusSize = payload.corpus_size;
  if (Array.isArray(payload.corpus_arxiv_ids)) stream.corpusIds = payload.corpus_arxiv_ids.map(String);
  if (Array.isArray(payload.accepted)) {

    stream.verdictAvailable = true;
    stream.acceptedIdeas = payload.accepted as JsonObject[];
    stream.accepted = payload.accepted.length;
  }
  if (Array.isArray(payload.rejected)) {

    stream.verdictAvailable = true;
    stream.rejectedIdeas = payload.rejected as JsonObject[];
    stream.rejected = payload.rejected.length;
  }
  if (payload.threshold != null) stream.threshold = payload.threshold;
  if (typeof payload.survey_md === 'string') stream.surveyMd = payload.survey_md;
  if (payload.open_gaps && typeof payload.open_gaps === 'object') {
    stream.openGaps = payload.open_gaps as JsonObject;
    const ids = (payload.open_gaps as JsonObject).surveyed_arxiv_ids;
    if (Array.isArray(ids)) {
      if (!stream.corpusIds.length) stream.corpusIds = ids.map(String);
      if (!stream.corpusSize) stream.corpusSize = ids.length;
    }
  }
  if (payload.evaluation && typeof payload.evaluation === 'object') {
    stream.evaluation = payload.evaluation as JsonObject;
  }
  if (payload.usage && typeof payload.usage === 'object') stream.usage = payload.usage as JsonObject;
  if (typeof payload.gate_reached === 'string') stream.gateReached = payload.gate_reached;
  if (payload.degraded) {
    stream.degraded = true;
    if (typeof payload.degraded_reason === 'string') {
      stream.degradedReason = payload.degraded_reason;
    }
  }
  stream.hydrated = true;
  return true;
}

export async function resumeSavedResearch(): Promise<void> {
  const stream = globals()._researchStream;
  if (!stream?.taskId || stream.status !== 'interrupted') return;
  try {
    const response = await researchApi().resumeRecord?.(stream.taskId);
    if (!response || response.ok === false) throw new Error(String(response?.error || 'Resume failed'));
    if (globals()._researchStream === stream) await openResearchRecord(stream.taskId);
  } catch (error) {
    if (globals()._researchStream === stream) { stream.commandError = errorMessage(error); paint(); }
  }
}

export async function openResearchRecord(taskId: string): Promise<boolean> {
  const state = globals();
  destroyResearchRuntime();
  const generation = runtimeGeneration;
  try {
    const response = await researchApi().record?.(taskId);
    if (generation !== runtimeGeneration) return false;
    const record = response?.record as JsonObject | undefined;
    if (!record || response?.ok === false) return false;
    const stream = newResearchStream(String(record.direction || ''));
    stream.taskId = taskId;
    adoptEvolutionBinding(stream, record);
    stream.lang = String(record.lang || 'en');
    stream.status = record.status as ResearchStatus;
    stream.startedAt = Number(record.created_at);
    stream.lastEventAt = Number(record.updated_at);
    stream.progress = projectResearchProgress(record.research_progress) || undefined;
    stream.phase = stream.progress?.stage || '';
    mergeLiterature(stream, record);
    stream.recordSaved = true;
    if (record.result && typeof record.result === 'object') researchApplyArtifacts(stream, record.result as JsonObject);
    if (stream.status === 'interrupted') stream.commandError = _i18nLang === 'zh' ? '执行已中断，论文和已完成阶段已保存。' : 'Execution interrupted. Papers and completed stages are saved.';
    state._researchStream = stream;
    repaintResearchSurface();
    if (researchIsRunning(stream)) { attachStreamPush(stream); scheduleResearchPoll(stream); }
    paint();
    return true;
  } catch (error) { console.warn('[Research] record reopen failed', error); return false; }
}

const hydrationFlights = new WeakMap<ResearchStream, Promise<void>>();
export function hydrateResearchFromStore(stream: ResearchStream): Promise<void> {
  const existing = hydrationFlights.get(stream);
  if (existing) return existing;
  const flight = readResearchRecord(stream).finally(() => hydrationFlights.delete(stream));
  hydrationFlights.set(stream, flight);
  return flight;
}
async function readResearchRecord(stream: ResearchStream): Promise<void> {
  if (!stream.direction) return;
  try {
    if (stream.taskId && researchApi().record) {
      const response = await researchApi().record!(stream.taskId);
      if (globals()._researchStream !== stream) return;
      const record = response.record as JsonObject | undefined;
      if (record) {
        adoptEvolutionBinding(stream, record);
        mergeLiterature(stream, record);
        stream.recordSaved = true;
        if (record.result && typeof record.result === 'object') researchApplyArtifacts(stream, record.result as JsonObject);
        // Running metadata can be hydrated again at settlement; terminal empty
        // results are intentional and must not borrow another run's verdict.
        stream.hydrated = !researchIsRunning(stream);
        paint();
      }
      return;
    }
    // Compatibility lookup is only for completed legacy tasks whose API has
    // no run-record capability. Never hydrate a live task from a past run.
    if (researchIsRunning(stream)) return;
    const payload = await researchApi().lookup(stream.direction, stream.lang || 'en');
    if (globals()._researchStream !== stream) return;
    if (researchApplyArtifacts(stream, payload)) paint();
  } catch (error: unknown) {
    if (globals()._researchStream === stream) {
      stream.commandError = errorMessage(error);
      paint();
    }
    console.debug('[Research] hydrate from store failed:', error);
  }
}

export async function restoreResearchFromStore(
  direction: string,
  lang = 'en',
): Promise<boolean> {
  if (!direction) return false;
  const state = globals();
  if (state._researchStream) paperDetachPush(state._researchStream);
  stopResearchPoll();
  const stream = newResearchStream(direction);
  stream.lang = lang || 'en';
  stream.status = 'done';
  state._researchStream = stream;
  repaintResearchSurface();
  try {
    const payload = await researchApi().lookup(direction, stream.lang);
    if (state._researchStream !== stream) return false;
    if (!researchApplyArtifacts(stream, payload)) {
      stream.status = 'error';
      stream.error = 'no stored research for this direction';
      paint();
      return false;
    }
  } catch (error: unknown) {
    console.error('[Research] restore failed:', error);
    if (state._researchStream === stream) {
      stream.status = 'error';
      stream.error = errorMessage(error);
      paint();
    }
    return false;
  }
  paint();
  return true;
}

/**
 * Re-attach to a research job that is still running server-side.
 *
 * Exiting Research mode tears down only client resources; the job stays
 * registered under the owner, so `GET /api/v4/tasks?kind=research` finds it
 * again. Without this the live console was unreachable after any navigation
 * (or a page reload) until the run finished and landed in the archive.
 */
export async function resumeInflightResearch(): Promise<boolean> {
  const state = globals();
  const generation = runtimeGeneration;
  if (state._researchStream) return true;
  const api = state.Api?.tasks;
  if (typeof api?.list !== 'function' || typeof api.get !== 'function') {
    return false;
  }
  let candidate: JsonObject | null = null;
  try {
    for (const status of ['running', 'pending']) {
      const data = await api.list('research', status);
      if (generation !== runtimeGeneration) return false;
      if (state._researchStream) return true;
      const rows = Array.isArray(data?.tasks) ? data.tasks as JsonObject[] : [];
      if (rows.length) {
        candidate = rows[0];
        break;
      }
    }
  } catch (error: unknown) {
    console.debug('[Research] inflight list failed:', error);
    return false;
  }
  const taskId = typeof candidate?.id === 'string' ? candidate.id : '';
  if (!candidate || !taskId) return false;
  const meta = candidate.meta && typeof candidate.meta === 'object'
    ? candidate.meta as JsonObject : {};
  const stream = newResearchStream(
    typeof meta.direction === 'string' ? meta.direction : '');
  stream.lang = typeof meta.lang === 'string' && meta.lang
    ? meta.lang : _i18nLang;
  stream.taskId = taskId;
  try { adoptEvolutionBinding(stream, meta); }
  catch (error) { console.warn('[Research] resume binding refused', error); return false; }
  stream.status = 'running';
  state._researchStream = stream;
  let snapshot: JsonObject | null = null;
  try {
    snapshot = await api.get(taskId);
  } catch (error: unknown) {
    console.debug('[Research] resume snapshot failed:', error);
  }
  if (state._researchStream !== stream) return true;
  if (!snapshot || snapshot.ok === false) {
    // The registry evicted the job between list and get (TTL/restart). Leave
    // no half-restored console behind; the archive restore path covers it
    // once artifacts were persisted.
    state._researchStream = null;
    return false;
  }
  researchApplySnapshot(stream, snapshot);
  if (researchIsRunning(stream)) {
    void hydrateResearchFromStore(stream);
    attachStreamPush(stream);
    scheduleResearchPoll(stream);
  } else if (!stream.hydrated) {
    void hydrateResearchFromStore(stream);
  }
  paint();
  return true;
}
let runtimeGeneration = 0;
export function destroyResearchRuntime(): void {
  runtimeGeneration += 1;
  const state = globals();
  stopResearchPoll();
  if (state._researchStream) paperDetachPush(state._researchStream);
  state._researchStream = null;
}

export function installResearchRuntimeGlobals(): void {
  const target = globals();
  target._newResearchStream = newResearchStream;
  target._researchAdoptServerClocks = researchAdoptServerClocks;
  target._researchApplyEvent = researchApplyEvent;
  target._researchIngestEvent = researchIngestEvent;
  target._researchApplySnapshot = researchApplySnapshot;
  target._submitResearchDirection = submitResearchDirection;
  target._startResearchJob = startResearchJob;
  target._pollResearchOnce = pollResearchOnce;
  target._researchIsRunning = researchIsRunning;
  target._stopResearchPoll = stopResearchPoll;
  target._scheduleResearchPoll = scheduleResearchPoll;
  target._abortResearchJob = abortResearchJob;
  target._openResearchFolder = openResearchFolder;
  target._openResearchCorpusPaper = openResearchCorpusPaper;
  target._researchApplyArtifacts = researchApplyArtifacts;
  target._hydrateResearchFromStore = hydrateResearchFromStore;
  target._restoreResearchFromStore = restoreResearchFromStore;
  target._resumeInflightResearch = resumeInflightResearch;
  target._destroyResearchRuntime = destroyResearchRuntime;
}

installResearchRuntimeGlobals();
