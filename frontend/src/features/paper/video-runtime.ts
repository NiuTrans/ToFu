import { destroyVideoArtifact } from './video-artifact';
import { featureRegistry } from '../../feature-registry';
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

function video(): MediaState {
  const state = globals()._pvideo;
  if (!state) throw new Error('Video view state is unavailable');
  return state as MediaState;
}

function apiDomain(name: 'paper' | 'motion'): LooseObject | undefined {
  const api = globals().Api as LooseObject | undefined;
  return api?.[name] as LooseObject | undefined;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error ?? '');
}

function translate(key: I18nKey, fallback: string): string {
  const fn = globals()._pvT;
  return typeof fn === 'function' ? String(fn(key, fallback)) : fallback;
}

function requiredPresentationPort(name: string): (...args: any[]) => any {
  const port = globals()[name];
  if (typeof port !== 'function') {
    throw new Error(`Video presentation port ${name} is unavailable`);
  }
  return port;
}

function render(): void { requiredPresentationPort('_pvRender')(); }
function renderProgress(): void { requiredPresentationPort('_pvRenderProgress')(); }
function renderActivity(): void { requiredPresentationPort('_pvRenderActivity')(); }

export const videoTaskObserver = createPaperTaskObserver({
  channel: 'motion', pollIntervalMs: 1500,
  state: video,
  taskId: () => video().regenTaskId || video().taskId,
  read: async (taskId, cursor) => apiDomain('motion')?.poll(taskId, cursor),
  consume: consumeVideoEvent,
  snapshot(response) {
    const state = video();
    adoptServerClocks(state, response);
  },
  terminal: settleVideoTask,
  lost() {
    const state = video();
    if (state.regenTaskId) {
      state.regenTaskId = ''; state.regenSceneId = '';
      state.revisionMessage = translate('paper.studioRevisionFailed', 'Scene update failed. Your current film is kept.');
      requiredPresentationPort('_pvRenderSceneGrid')(0);
      return;
    }
    state.taskId = '';
    state.status = 'lost';
    render();
  },
  presentation: { render, progress: renderProgress, activity: renderActivity },
  warn: (error) => console.warn('[Paper:Video] poll failed:', error),
});

export function resetVideoRun(): void {
  const state = video();
  videoTaskObserver.reset();
  state.phases = [];
  state.phaseIndex = 0;
  state.genStartedAt = Date.now();
  state.lastEventAt = Date.now();
  state._rateFirstTick = 0;
  state._rateFirstDone = 0;
  state.etaSec = 0;
  state._gridLoaded = false;
  state.scenes = [];
  state._sceneLoadGeneration = Number(state._sceneLoadGeneration || 0) + 1;
  state._revisionGeneration = Number(state._revisionGeneration || 0) + 1;
  state.revisionMessage = '';
}

export async function initVideoTab(force = false): Promise<void> {
  const state = video();
  const host = document.getElementById('paperVideoContent');
  if (!host) return;
  const selectedHash = String(globals()._paperHash ?? '');
  if (!force && state.status === 'done' && state.paperHash === selectedHash && state.result && state._doneTaskId) { render(); return; }
  videoTaskObserver.stop();
  state.scenes = []; state.scenesLoading = false; state.scenesError = ''; state.revisionMessage = '';
  state._revisionGeneration = Number(state._revisionGeneration || 0) + 1;
  state.paperHash = selectedHash;
  const initHash = state.paperHash;
  if (!state.paperHash) {
    state.status = 'idle';
    const escape = globals()._pvEsc ?? ((value: unknown) => String(value ?? ''));
    host.innerHTML = '<div class="paper-report-empty"><p>'
      + escape(translate('paper.reportNoText', 'No paper text available. Load a PDF first.'))
      + '</p></div>';
    return;
  }
  seedMediaModel('video');
  state.status = 'loading';
  render();
  try {
    const status = await apiDomain('motion')?.status();
    if (state.paperHash !== initHash) return;
    if (status?.ok) state.ttsAvailable = Boolean(status.tts_available);
    const lookup = await apiDomain('paper')?.videoLookup({ paper_hash: state.paperHash });
    if (state.paperHash !== initHash) return;
    if (lookup?.ok && lookup.found) {
      state._doneTaskId = lookup.task_id;
      if (lookup.running) {
        state.taskId = lookup.task_id;
          state.status = 'generating';
        if (lookup.model) adoptMediaModel('video', lookup.model);
        resetVideoRun();
        adoptServerClocks(state, lookup);
        render();
        videoTaskObserver.schedule();
        return;
      }
      if (lookup.interrupted) {
        state.status = 'interrupted';
        render();
        return;
      }
      if (lookup.result) {
        state.result = lookup.result;
        state.quality_axis = lookup.artifact_quality || null;
        adoptMediaModel('video', lookup.model || '');
        state.status = 'done';
        state.scenesLoading = true;
        render();
        void loadVideoScenes();
        return;
      }
    }
    if (lookup?.ok) {
      state.status = lookup.report_available ? 'idle' : 'report_required';
    } else {
      state.status = 'lookup_failed';
      state.errorText = translate('paper.videoLookupFailed',
        'Video status lookup failed — check the server log.');
    }
    render();
  } catch (error: unknown) {
    console.warn('[Paper:Video] lookup failed:', error);
    state.status = 'lookup_failed';
    state.errorText = errorMessage(error);
    render();
  }
}

export function updateVideoEta(done: number, total: number): void {
  const state = video();
  const now = Date.now();
  if (!state._rateFirstTick) {
    state._rateFirstTick = now;
    state._rateFirstDone = done;
  }
  const elapsed = (now - state._rateFirstTick) / 1000;
  const made = done - state._rateFirstDone;
  state.etaSec = made > 0 && total > done
    ? Math.round(elapsed / made * (total - done))
    : 0;
}

export function consumeVideoEvent(event: LooseObject): boolean | undefined {
  const state = video();
  if (event.type === 'phase_started') {
    state.phases = event.phases || state.phases;
    state.phaseIndex = event.phase_index || 0;
    state.progress.phase = event.phase || state.progress.phase;
    state._rateFirstTick = 0;
    state._rateFirstDone = 0;
    state.etaSec = 0;
    if (['compose', 'render'].includes(String(event.phase)) && !state._gridLoaded) {
      state._gridLoaded = true;
      void loadVideoScenes();
    }
    return true;
  }
  if (event.type === 'phase') {
    state.progress.phase = event.phase || state.progress.phase;
  } else if (event.type === 'progress') {
    state.progress = {
      done: event.done || 0,
      total: event.total || 0,
      phase: event.phase || state.progress.phase,
    };
    if (event.phase === 'narrate') updateVideoEta(event.done || 0, event.total || 0);
  } else if (event.type === 'scene_done') {
    state.progress = { done: event.done || 0, total: event.total || 0, phase: 'render' };
    updateVideoEta(event.done || 0, event.total || 0);
    void loadVideoScenes();
  }
  return undefined;
}

async function settleVideoTask(response: PaperTaskSnapshot): Promise<void> {
  const state = video();
  if (state.regenTaskId) {
    state.regenTaskId = '';
    state.regenSceneId = '';
    if (response.status !== 'done') {
      state.revisionMessage = translate('paper.studioRevisionFailed', 'Scene update failed. Your current film is kept.');
      requiredPresentationPort('_pvRenderSceneGrid')(0);
    } else if (response.result?.applied === false) {
      state.revisionMessage = translate('paper.studioRevisionKept', 'The current scene was kept.');
      requiredPresentationPort('_pvRenderSceneGrid')(0);
    } else {
      state.revisionMessage = translate('paper.studioRevisionApplied', 'Scene updated.');
      await loadVideoScenes(true);
    }
    return;
  }
  if (response.status === 'done') {
    state.result = response.result || null;
    state.quality_axis = response.artifact_quality || null;
    if (response.model) adoptMediaModel('video', response.model);
    state._doneTaskId = state.taskId;
    state.status = 'done';
    state.scenesLoading = true;
    state.taskId = '';
    render();
    void loadVideoScenes();
  } else if (response.status === 'aborted') {
    state.status = 'idle';
    state.taskId = '';
    render();
  } else if (response.error?.kind === 'worker_lost') {
    state.status = 'lost';
    state.taskId = '';
    render();
  } else {
    state.status = 'error';
    state.errorText = response.error?.detail
      || (typeof response.error === 'string' ? response.error : '')
      || translate('paper.videoFailed', 'Video generation failed');
    state.taskId = '';
    render();
  }
}

export async function generateVideo(force = false): Promise<void> {
  const state = video();
  if (!state.paperHash) return;
  const lang = document.getElementById('videoLangSel') as HTMLSelectElement | null;
  const voice = document.getElementById('videoVoiceInp') as HTMLInputElement | null;
  const narration = document.getElementById('videoNarrChk') as HTMLInputElement | null;
  const burnIn = document.getElementById('videoBurnChk') as HTMLInputElement | null;
  const quality = document.getElementById('videoQualSel') as HTMLSelectElement | null;
  const visual = document.getElementById('videoVisualSel') as HTMLSelectElement | null;
  state.lang = lang?.value || state.lang;
  state.voice = voice?.value.trim() || state.voice;
  state.narration = narration ? narration.checked : state.narration;
  state.burnIn = burnIn ? burnIn.checked : state.burnIn;
  state.quality = quality?.value || state.quality;
  state.visual = visual?.value || state.visual;
  seedMediaModel('video');
  state.artifactModel = state.model || '';
  state.quality_axis = null;
  state.status = 'generating';
  state.progress = { done: 0, total: 0, phase: '' };
  resetVideoRun();
  render();
  const generationHash = state.paperHash;
  const attempt = { cancelled: false }; state._startAttempt = attempt;
  try {
    const response = await apiDomain('paper')?.videoStart({
      paper_hash: state.paperHash,
      lang: state.lang,
      voice: state.voice,
      narration: state.narration,
      burn_in: state.burnIn,
      quality: state.quality,
      force: Boolean(force),
      model: state.model || undefined,
      scene_author: state.visual !== 'template',
    });
    if (attempt.cancelled) {
      if (response?.ok && response.task_id) {
        try { await apiDomain('motion')?.abort(response.task_id); }
        catch (error: unknown) { console.warn('[Paper:Video] late-start cancellation failed:', error); }
      }
      return;
    }
    if (state._startAttempt !== attempt || state.paperHash !== generationHash) return;
    if (response?.report_required) {
      state.status = 'report_required';
      render();
      return;
    }
    if (response?.ok && response.task_id) {
      state.taskId = response.task_id;
      renderProgress();
      videoTaskObserver.schedule();
      return;
    }
    state.status = 'error';
    state.errorText = response?.error
      || translate('paper.videoFailed', 'Video generation failed');
    render();
  } catch (error: unknown) {
    if (attempt.cancelled || state._startAttempt !== attempt || state.paperHash !== generationHash) return;
    state._startAttempt = null;
    console.warn('[Paper:Video] start failed:', error);
    state.status = 'error';
    state.errorText = errorMessage(error);
    render();
  } finally {
    if (state._startAttempt === attempt) state._startAttempt = null;
  }
}

export async function abortVideo(): Promise<void> {
  const state = video();
  if (state._startAttempt) state._startAttempt.cancelled = true;
  state._startAttempt = null;
  if (state.taskId) {
    try { await apiDomain('motion')?.abort(state.taskId); }
    catch (error: unknown) { console.warn('[Paper:Video] abort failed:', error); }
  }
  videoTaskObserver.stop();
  state.taskId = '';
  state.regenTaskId = '';
  state.status = 'idle';
  render();
}

export async function loadVideoScenes(cacheBust = false): Promise<void> {
  const state = video();
  const taskId = state._doneTaskId || state.taskId || '';
  if (!taskId) return;
  const paperHash = state.paperHash;
  const generation = Number(state._sceneLoadGeneration || 0) + 1;
  state._sceneLoadGeneration = generation;
  state.scenesLoading = true; state.scenesError = '';
  requiredPresentationPort('_pvRenderSceneGrid')(0);
  try {
    const response = await apiDomain('motion')?.scenes(taskId);
    if (state._sceneLoadGeneration !== generation
        || state.paperHash !== paperHash
        || (state._doneTaskId || state.taskId || '') !== taskId) return;
    state.scenesLoading = false;
    if (response?.ok) state.scenes = Array.isArray(response.scenes) ? response.scenes : [];
    else state.scenesError = 'lookup_failed';
    requiredPresentationPort('_pvRenderSceneGrid')(response?.ok && cacheBust ? Date.now() : 0);
  } catch (error: unknown) {
    console.warn('[Paper:Video] scenes load failed:', error);
    if (state._sceneLoadGeneration === generation && state.paperHash === paperHash) {
      state.scenesLoading = false; state.scenesError = 'lookup_failed';
      requiredPresentationPort('_pvRenderSceneGrid')(0);
    }
  }
}

export async function regenerateVideoScene(sceneId: string): Promise<void> {
  const state = video();
  const taskId = state._doneTaskId || state.taskId;
  if (!taskId || state.regenSceneId || state.status !== 'done') return;
  const hash = state.paperHash;
  const generation = Number(state._revisionGeneration || 0) + 1; state._revisionGeneration = generation;
  const current = () => state._revisionGeneration === generation && state.paperHash === hash && state.status === 'done' && (state._doneTaskId || state.taskId) === taskId;
  state.revisionMessage = '';
  state.regenSceneId = sceneId;
  requiredPresentationPort('_pvRenderSceneGrid')(0);
  try {
    const response = await apiDomain('motion')?.regenScene(taskId, sceneId);
    if (!current()) return;
    if (response?.ok && response.task_id) {
      videoTaskObserver.reset();
      state.regenTaskId = response.task_id;
      videoTaskObserver.schedule();
      return;
    }
  } catch (error: unknown) {
    console.warn('[Paper:Video] regen failed:', error);
  }
  if (!current()) return;
  state.regenSceneId = '';
  state.revisionMessage = translate('paper.studioRevisionFailed', 'Scene update failed. Your current film is kept.');
  requiredPresentationPort('_pvRenderSceneGrid')(0);
}

export function destroyVideoRuntime(): void {
  destroyVideoArtifact();
  const state = globals()._pvideo as LooseObject | undefined;
  if (!state) return;
  state._startAttempt = null;
  videoTaskObserver.stop();
  state._sceneLoadGeneration = Number(state._sceneLoadGeneration || 0) + 1;
  state._revisionGeneration = Number(state._revisionGeneration || 0) + 1;
  state.taskId = '';
  state.regenTaskId = '';
  state.regenSceneId = '';
}

export function installVideoRuntime(): void {
  const target = globals();
  target._initVideoTab = initVideoTab;
  target._videoGenerate = generateVideo;
  target._videoAbort = abortVideo;
  target._pvLoadScenes = loadVideoScenes;
  target._videoRegenScene = regenerateVideoScene;
}

installVideoRuntime();
