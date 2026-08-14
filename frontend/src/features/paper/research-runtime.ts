import { featureRegistry } from '../../feature-registry';
import {
  paperAttachPush,
  paperDetachPush,
  paperIngestEvent,
  taskReplayIngestPage,
  type PaperPushEvent,
  type PaperPushState,
} from './push-transport';

type JsonObject = Record<string, unknown>;
type ResearchStatus = 'pending' | 'running' | 'done' | 'error' | 'aborted';

interface ToolRound extends JsonObject {
  roundNum?: unknown;
  toolName?: unknown;
  query?: unknown;
  toolCallId?: unknown;
  status?: string;
  _elapsed?: string;
}

export interface ResearchStream extends PaperPushState, JsonObject {
  direction: string;
  taskId: string | null;
  status: ResearchStatus;
  phase: string;
  startedAt: number;
  lastEventAt: number;
  degraded: boolean;
  degradedReason: string;
  gateReached: string;
  accepted: number;
  rejected: number;
  corpusSize: number;
  corpusIds: string[];
  folderId: string;
  error: string;
  acceptedIdeas: JsonObject[];
  rejectedIdeas: JsonObject[];
  threshold: unknown;
  surveyMd: string;
  openGaps: JsonObject | null;
  evaluation: JsonObject | null;
  usage: JsonObject | null;
  toolRounds: ToolRound[];
  hydrated: boolean;
  lang?: string;
}

interface TaskApi {
  start(kind: string, payload: JsonObject): Promise<JsonObject>;
  events?(taskId: string, cursor: number): Promise<JsonObject>;
  get(taskId: string): Promise<JsonObject>;
  abort(taskId: string): Promise<unknown>;
}

interface ResearchApi {
  lookup(direction: string, lang: string): Promise<JsonObject>;
}

type ResearchWindow = Window & {
  Api?: { tasks?: TaskApi; research?: ResearchApi };
  debugLog?: (message: string, level?: string) => void;
  _researchStream?: ResearchStream | null;
  _researchPollTimer?: number | null;
  _paintResearch?: () => void;
  _showPaperLanding?: () => void;
  _renderPaperLibrary?: () => void;
  _fetchArxivPaper?: (arxivId: string) => Promise<unknown>;
  _newResearchStream?: typeof newResearchStream;
  _researchAdoptServerClocks?: typeof researchAdoptServerClocks;
  _researchApplyEvent?: typeof researchApplyEvent;
  _researchIngestEvent?: typeof researchIngestEvent;
  _researchApplySnapshot?: typeof researchApplySnapshot;
  _submitResearchDirection?: typeof submitResearchDirection;
  _startResearchFromDescribe?: typeof startResearchFromDescribe;
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

export function newResearchStream(direction: string): ResearchStream {
  return {
    direction,
    taskId: null,
    status: 'pending',
    phase: '',
    startedAt: 0,
    lastEventAt: 0,
    degraded: false,
    degradedReason: '',
    gateReached: '',
    accepted: 0,
    rejected: 0,
    corpusSize: 0,
    corpusIds: [],
    folderId: '',
    error: '',
    acceptedIdeas: [],
    rejectedIdeas: [],
    threshold: null,
    surveyMd: '',
    openGaps: null,
    evaluation: null,
    usage: null,
    toolRounds: [],
    hydrated: false,
    _seqSeen: -1,
    _replayCursor: 0,
  };
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
  if (typeof event.phase === 'string') stream.phase = event.phase;
  if ((event.type === 'stage_started' || event.type === 'stage_skipped'
      || event.type === 'stage_done') && typeof event.stage === 'string') {
    stream.phase = event.stage;
  }
  if (event.type === 'tool_start') {
    stream.toolRounds.push({
      roundNum: event.roundNum,
      toolName: event.toolName,
      query: event.query || event.toolName,
      toolCallId: event.toolCallId || '',
      toolArgs: event.toolArgs || '',
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
  return Boolean(event.phase || event.stage);
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
  researchAdoptServerClocks(stream, snapshot);
  if (typeof snapshot.status === 'string') {
    stream.status = snapshot.status as ResearchStatus;
  }
  const quality = snapshot.artifact_quality;
  if (quality && typeof quality === 'object') {
    const row = quality as JsonObject;
    stream.degraded = Boolean(row.degraded);
    stream.degradedReason = typeof row.reason === 'string' ? row.reason : '';
  }
  const result = snapshot.result;
  if (result && typeof result === 'object') {
    const row = result as JsonObject;
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

export function startResearchFromDescribe(): void {
  const direction = directionFrom('paperDescribeInput');
  if (!direction) {
    globals().debugLog?.('Describe the research direction to explore', 'warning');
    return;
  }
  void startResearchJob(direction);
}

export async function startResearchJob(direction: string): Promise<void> {
  const state = globals();
  if (state._researchStream) paperDetachPush(state._researchStream);
  stopResearchPoll();
  const stream = newResearchStream(direction);
  state._researchStream = stream;
  paint();
  try {
    const data = await tasks().start('research', { direction });
    const taskId = typeof data.taskId === 'string' ? data.taskId : '';
    if (data.ok !== true || !taskId) {
      throw new Error(typeof data.error === 'string' ? data.error : 'research start failed');
    }
    if (state._researchStream !== stream) return;
    stream.taskId = taskId;
    stream.status = 'running';
    paperAttachPush(stream, taskId, {
      channel: 'research',
      isCurrent: () => globals()._researchStream === stream,
      isTerminal: (event) => event.type === 'final' || event.type === 'done'
        || event.type === 'error' || event.type === 'aborted',
      onEvent(event) {
        if (researchIngestEvent(stream, event)) paint();
        if (event.type === 'final' || event.type === 'done'
            || event.type === 'error' || event.type === 'aborted') {
          void pollResearchOnce(stream);
        }
      },
    });
    await pollResearchOnce(stream);
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

export async function pollResearchOnce(stream: ResearchStream): Promise<void> {
  if (!stream.taskId) return;
  const api = tasks();
  const cursor = stream._replayCursor ?? 0;
  const snapshot = typeof api.events === 'function'
    ? await api.events(stream.taskId, cursor)
    : await api.get(stream.taskId);
  if (globals()._researchStream !== stream || snapshot.ok === false) return;
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
  return stream?.status === 'pending' || stream?.status === 'running';
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
  if (!researchIsRunning(stream)) return;
  globals()._researchPollTimer = window.setTimeout(async () => {
    if (globals()._researchStream !== stream) return;
    try { await pollResearchOnce(stream); } catch (error: unknown) {
      console.debug('[Research] poll failed:', error);
    }
    if (researchIsRunning(stream)) scheduleResearchPoll(stream);
  }, 2000);
}

export async function abortResearchJob(): Promise<void> {
  const stream = globals()._researchStream;
  if (!stream?.taskId) return;
  try { await tasks().abort(stream.taskId); } catch (error: unknown) {
    console.debug('[Research] abort failed:', error);
  }
  stream.status = 'aborted';
  stopResearchPoll();
  paperDetachPush(stream);
  paint();
}

export function openResearchFolder(): void {
  if (!globals()._researchStream?.folderId) return;
  globals()._showPaperLanding?.();
  globals()._renderPaperLibrary?.();
}

export function openResearchCorpusPaper(index: number): void {
  const arxivId = globals()._researchStream?.corpusIds[index];
  if (arxivId) void globals()._fetchArxivPaper?.(arxivId);
}

export function researchApplyArtifacts(
  stream: ResearchStream,
  payload: JsonObject | null | undefined,
): boolean {
  if (!payload || payload.found === false) return false;
  if (Array.isArray(payload.accepted)) {
    stream.acceptedIdeas = payload.accepted as JsonObject[];
    stream.accepted = payload.accepted.length;
  }
  if (Array.isArray(payload.rejected)) {
    stream.rejectedIdeas = payload.rejected as JsonObject[];
    stream.rejected = payload.rejected.length;
  }
  if (payload.threshold != null) stream.threshold = payload.threshold;
  if (typeof payload.survey_md === 'string') stream.surveyMd = payload.survey_md;
  if (payload.open_gaps && typeof payload.open_gaps === 'object') {
    stream.openGaps = payload.open_gaps as JsonObject;
    const ids = (payload.open_gaps as JsonObject).surveyed_arxiv_ids;
    if (Array.isArray(ids)) {
      stream.corpusIds = ids.map(String);
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

export async function hydrateResearchFromStore(stream: ResearchStream): Promise<void> {
  if (!stream.direction) return;
  try {
    const payload = await researchApi().lookup(stream.direction, stream.lang || 'en');
    if (globals()._researchStream !== stream) return;
    if (researchApplyArtifacts(stream, payload)) paint();
  } catch (error: unknown) {
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

export function destroyResearchRuntime(): void {
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
  target._startResearchFromDescribe = startResearchFromDescribe;
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
  target._destroyResearchRuntime = destroyResearchRuntime;
}

installResearchRuntimeGlobals();
