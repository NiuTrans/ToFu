import { featureRegistry } from '../../feature-registry';
import { destroyPodcastArtifact } from './podcast-artifact';
import type { I18nKey } from '../../i18n';
import { createPaperTaskObserver, type PaperTaskSnapshot } from './push-transport';
import {
  adoptMediaModel,
  adoptServerClocks,
  seedMediaModel,
} from './media-model-ui';

type LooseObject = Record<string, any>;
type MediaState = LooseObject & { status: string; lastEventAt: number };
type MediaWindow = Window & Record<string, any>;

function globals(): MediaWindow {
  return featureRegistry as unknown as MediaWindow;
}

function podcast(): MediaState {
  const state = globals()._podcast;
  if (!state) throw new Error('Podcast view state is unavailable');
  return state as MediaState;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error ?? '');
}

function paperApi(): LooseObject | undefined {
  const api = globals().Api as LooseObject | undefined;
  return api?.paper as LooseObject | undefined;
}

function translate(key: I18nKey, fallback: string): string {
  const fn = globals()._pcT;
  return typeof fn === 'function' ? String(fn(key, fallback)) : fallback;
}

function requiredPresentationPort(name: string): (...args: any[]) => any {
  const port = globals()[name];
  if (typeof port !== 'function') {
    throw new Error(`Podcast presentation port ${name} is unavailable`);
  }
  return port;
}

function render(): void { requiredPresentationPort('_pcRender')(); }
function renderProgress(): void { requiredPresentationPort('_pcRenderProgress')(); }
function renderActivity(): void { requiredPresentationPort('_pcRenderActivity')(); }

export const podcastTaskObserver = createPaperTaskObserver({
  channel: 'paper', pollIntervalMs: 1200,
  state: podcast,
  taskId: () => podcast().taskId,
  read: async (taskId, cursor) => paperApi()?.podcastPoll(taskId, cursor),
  consume: consumePodcastEvent,
  snapshot(response) {
    const state = podcast();
    adoptServerClocks(state, response);
    state.progress = response.progress || state.progress;
  },
  terminal: settlePodcastTask,
  lost() {
    const state = podcast();
    state.taskId = '';
    state.status = 'lost';
    render();
  },
  presentation: { render, progress: renderProgress, activity: renderActivity },
  warn: (error) => console.warn('[Paper:Podcast] poll failed:', error),
});

export function resetPodcastRun(): void {
  const state = podcast();
  podcastTaskObserver.reset();
  if (state.sleepTimerId) window.clearTimeout(state.sleepTimerId);
  state.sleepTimerId = 0;
  state.sleepDeadline = 0;
  state.phases = [];
  state.phaseIndex = 0;
  state.currentPhase = '';
  state.scriptStep = '';
  state.scriptChars = 0;
  state.scriptSegments = 0;
  state.scriptCharTarget = 0;
  state.genStartedAt = Date.now();
  state.lastEventAt = Date.now();
  state._segFirstTick = 0;
  state.etaSec = 0;
}

export async function initPodcastTab(_force?: boolean): Promise<void> {
  const state = podcast();
  const host = document.getElementById('paperPodcastContent');
  if (!host) return;
  podcastTaskObserver.stop();
  state.paperHash = String(globals()._paperHash ?? '');
  const initHash = state.paperHash;
  if (state.sleepTimerId) window.clearTimeout(state.sleepTimerId);
  state.sleepTimerId = 0;
  state.sleepDeadline = 0;
  if (!state.paperHash) {
    state.status = 'idle';
    const escape = globals()._pcEsc ?? ((value: unknown) => String(value ?? ''));
    host.innerHTML = '<div class="paper-report-empty"><p>'
      + escape(translate('paper.reportNoText', 'No paper text available. Load a PDF first.'))
      + '</p></div>';
    return;
  }
  seedMediaModel('podcast');
  requiredPresentationPort('_pcSeedOptions')();
  state.status = 'loading';
  render();
  try {
    const api = paperApi();
    if (!api) throw new Error('Podcast API is unavailable');
    const status = await api.podcastStatus();
    if (state.paperHash !== initHash) return;
    if (status?.ok) {
      state.ttsAvailable = Boolean(status.tts_available);
      state.defaultVoice = status.default_voice || '';
    }
    const lookup = await api.podcastLookup({
      paper_hash: state.paperHash,
      mode: state.mode,
      lang: state.lang,
    });
    if (state.paperHash !== initHash) return;
    if (lookup?.ok && lookup.found && lookup.running) {
      state.taskId = lookup.task_id;
      state.status = 'generating';
      if (lookup.model) adoptMediaModel('podcast', lookup.model);
      resetPodcastRun();
      adoptServerClocks(state, lookup);
      render();
      podcastTaskObserver.schedule();
      return;
    }
    if (lookup?.ok && lookup.found && lookup.interrupted) {
      state.status = 'interrupted';
      render();
      return;
    }
    if (lookup?.ok && lookup.found && lookup.cached) {
      state.data = lookup;
      adoptMediaModel('podcast', lookup.model || '');
      state.status = lookup.scriptOnly ? 'script_only' : 'done';
      render();
      return;
    }
    if (lookup?.ok) {
      state.reportAvailable = Boolean(lookup.report_available);
      state.status = state.reportAvailable ? 'idle' : 'report_required';
    } else {
      state.status = 'lookup_failed';
      state.errorText = translate('paper.podcastLookupFailed',
        'Podcast status lookup failed — check the server log.');
    }
    render();
  } catch (error: unknown) {
    console.warn('[Paper:Podcast] lookup failed:', error);
    state.status = 'lookup_failed';
    state.errorText = errorMessage(error);
    render();
  }
}

export function consumePodcastEvent(event: LooseObject): boolean | undefined {
  const state = podcast();
  if (event.type === 'phase_started') {
    state.phases = event.phases || state.phases;
    state.phaseIndex = event.phase_index || 0;
    state.currentPhase = event.phase || '';
    if (event.phase !== 'audio') {
      state._segFirstTick = 0;
      state.etaSec = 0;
    }
    return true;
  }
  if (event.type === 'progress' && event.phase === 'script') {
    state.scriptStep = event.step || '';
    if (typeof event.chars === 'number') state.scriptChars = event.chars;
    if (typeof event.segments === 'number') state.scriptSegments = event.segments;
    if (typeof event.char_target === 'number') state.scriptCharTarget = event.char_target;
  } else if (event.type === 'segment_done') {
    state.progress = { done: event.done, total: event.total };
    const now = Date.now();
    if (!state._segFirstTick) state._segFirstTick = now;
    state.etaSec = event.done > 0 && event.total > event.done
      ? Math.round((now - state._segFirstTick) / 1000 / event.done
        * (event.total - event.done))
      : 0;
  }
  return undefined;
}

function settlePodcastTask(response: PaperTaskSnapshot): void {
  const state = podcast();
  if (response.status === 'done') {
    state.data = response;
    adoptMediaModel('podcast', response.model || '');
    state.status = response.scriptOnly ? 'script_only' : 'done';
  } else if (response.status === 'aborted') {
    state.status = 'idle';
  } else if (response.error?.kind === 'worker_lost') {
    state.status = 'lost';
  } else {
    state.status = 'error';
    state.errorText = response.error?.detail
      || (typeof response.error === 'string' ? response.error : '')
      || translate('paper.podcastFailed', 'Podcast generation failed');
  }
  state.taskId = '';
  render();
}

export async function generatePodcast(force = false): Promise<void> {
  const state = podcast();
  if (!state.paperHash) return;
  const mode = document.getElementById('podcastModeSel') as HTMLSelectElement | null;
  const lang = document.getElementById('podcastLangSel') as HTMLSelectElement | null;
  const voice = document.getElementById('podcastVoiceInp') as HTMLInputElement | null;
  state.mode = mode?.value || state.mode;
  state.lang = lang?.value || state.lang;
  state.voice = voice?.value.trim() || state.voice;
  requiredPresentationPort('_pcPersistOptions')();
  seedMediaModel('podcast');
  state.artifactModel = state.model || '';
  state.status = 'generating';
  state.progress = { done: 0, total: 0 };
  resetPodcastRun();
  render();
  const generationHash = state.paperHash;
  try {
    const response = await paperApi()?.podcastStart({
      paper_hash: state.paperHash,
      mode: state.mode,
      lang: state.lang,
      voice: state.voice,
      model: state.model || undefined,
      force: Boolean(force),
    });
    if (state.paperHash !== generationHash) return;
    if (response?.report_required) {
      state.status = 'report_required';
      render();
      return;
    }
    if (response?.ok && response.cached) {
      state.data = response;
      state.status = response.scriptOnly ? 'script_only' : 'done';
      render();
      return;
    }
    if (response?.ok && response.task_id) {
      state.taskId = response.task_id;
      renderProgress();
      podcastTaskObserver.schedule();
      return;
    }
    state.status = 'error';
    state.errorText = response?.error
      || translate('paper.podcastFailed', 'Podcast generation failed');
    render();
  } catch (error: unknown) {
    console.warn('[Paper:Podcast] start failed:', error);
    state.status = 'error';
    state.errorText = errorMessage(error);
    render();
  }
}

export async function abortPodcast(): Promise<void> {
  const state = podcast();
  if (state.taskId) {
    try { await paperApi()?.podcastAbort(state.taskId); }
    catch (error: unknown) { console.warn('[Paper:Podcast] abort failed:', error); }
  }
  podcastTaskObserver.stop();
  state.taskId = '';
  state.status = 'idle';
  render();
}

export function destroyPodcastRuntime(): void {
  destroyPodcastArtifact();
  const state = globals()._podcast as LooseObject | undefined;
  if (!state) return;
  podcastTaskObserver.stop();
  if (state.sleepTimerId) window.clearTimeout(state.sleepTimerId);
  state.sleepTimerId = 0;
  state.sleepDeadline = 0;
  state.taskId = '';
}

export function installPodcastRuntime(): void {
  const target = globals();
  target._initPodcastTab = initPodcastTab;
  target._podcastGenerate = generatePodcast;
  target._podcastAbort = abortPodcast;
}

installPodcastRuntime();
