import { featureRegistry } from '../../feature-registry';
import type { I18nKey } from '../../i18n';
import {
  adoptMediaModel,
  adoptServerClocks,
  seedMediaModel,
} from './media-model-ui';

type LooseObject = Record<string, any>;
type MediaWindow = Window & Record<string, any>;

function globals(): MediaWindow {
  return featureRegistry as unknown as MediaWindow;
}

function video(): LooseObject {
  const state = globals()._pvideo;
  if (!state) throw new Error('Video view state is unavailable');
  return state as LooseObject;
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

function render(): void { globals()._pvRender?.(); }
function renderProgress(): void { globals()._pvRenderProgress?.(); }
function renderActivity(): void { globals()._pvRenderActivity?.(); }

export function resetVideoRun(): void {
  const state = video();
  globals().paperDetachPush?.(state);
  state._seqSeen = -1;
  state._replayCursor = 0;
  state.pollFails = 0;
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
}

export function stopVideoPoll(): void {
  const state = video();
  if (state.pollTimer) window.clearTimeout(state.pollTimer);
  state.pollTimer = null;
}

export function stopVideoTick(): void {
  const state = video();
  if (state.tickTimer) window.clearInterval(state.tickTimer);
  state.tickTimer = null;
}

export function stopVideoPolling(): void {
  const state = video();
  stopVideoPoll();
  stopVideoTick();
  globals().paperDetachPush?.(state);
}

export function startVideoTick(): void {
  const state = video();
  if (state.tickTimer) return;
  state.tickTimer = window.setInterval(renderActivity, 1000);
}

export async function initVideoTab(_force?: boolean): Promise<void> {
  const state = video();
  const host = document.getElementById('paperVideoContent');
  if (!host) return;
  stopVideoPolling();
  state.paperHash = String(globals()._paperHash ?? '');
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
        state.cursor = 0;
        state.status = 'generating';
        if (lookup.model) adoptMediaModel('video', lookup.model);
        resetVideoRun();
        adoptServerClocks(state, lookup);
        render();
        scheduleVideoPoll();
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

export function attachVideoPush(): void {
  const state = video();
  const taskId = state.regenTaskId || state.taskId;
  if (!taskId) return;
  globals().paperAttachPush?.(state, taskId, {
    channel: 'motion',
    isCurrent: () => (state.regenTaskId || state.taskId) === taskId,
    onEvent: (event: LooseObject) => {
      if ((state.regenTaskId || state.taskId) !== taskId) return;
      state.lastEventAt = Date.now();
      const changed = globals().paperIngestEvent?.(
        state, event, (_target: LooseObject, row: LooseObject) => consumeVideoEvent(row),
      );
      if (changed) render(); else renderProgress();
      renderActivity();
      if (['done', 'error', 'aborted'].includes(String(event.type))) {
        void pollVideoOnce();
      }
    },
  });
}

export function scheduleVideoPoll(): void {
  const state = video();
  stopVideoPoll();
  attachVideoPush();
  const delay = Number(globals()._PVIDEO_POLL_MS) || 1500;
  state.pollTimer = window.setTimeout(() => void pollVideoOnce(), delay);
}

export function failVideoPoll(): void {
  const state = video();
  state.pollFails = Number(state.pollFails || 0) + 1;
  const limit = Number(globals()._PV_POLL_FAIL_LIMIT) || 5;
  if (state.pollFails >= limit) {
    stopVideoPolling();
    state.taskId = '';
    state.regenTaskId = '';
    state.status = 'lost';
    render();
    return;
  }
  scheduleVideoPoll();
}

export async function pollVideoOnce(): Promise<void> {
  const state = video();
  const taskId = state.regenTaskId || state.taskId;
  if (!taskId || state.pollBusy) return;
  state.pollBusy = true;
  try {
    const response = await apiDomain('motion')?.poll(taskId, state.cursor);
    const current = state.regenTaskId
      ? state.regenTaskId === taskId
      : state.taskId === taskId;
    if (!current) return;
    if (!response?.ok) {
      failVideoPoll();
      return;
    }
    state.pollFails = 0;
    const replay = globals().taskReplayIngestPage?.(
      state,
      response,
      (_target: LooseObject, event: LooseObject) => consumeVideoEvent(event),
      state.cursor,
    ) ?? { accepted: 0, nextCursor: state.cursor, changed: false };
    if (replay.accepted) state.lastEventAt = Date.now();
    adoptServerClocks(state, response);
    state.cursor = replay.nextCursor;
    if (response.done) {
      if (state.regenTaskId) {
        state.regenTaskId = '';
        state.regenSceneId = '';
        stopVideoPolling();
        await loadVideoScenes(true);
        return;
      }
      if (response.status === 'done') {
        state.result = response.result || null;
        state.quality_axis = response.artifact_quality || null;
        if (response.model) adoptMediaModel('video', response.model);
        state._doneTaskId = state.taskId;
        state.status = 'done';
        state.taskId = '';
        stopVideoPolling();
        render();
        void loadVideoScenes();
      } else if (response.status === 'aborted') {
        state.status = 'idle';
        state.taskId = '';
        stopVideoPolling();
        render();
      } else if (response.error?.kind === 'worker_lost') {
        state.status = 'lost';
        state.taskId = '';
        stopVideoPolling();
        render();
      } else {
        state.status = 'error';
        state.errorText = response.error?.detail
          || (typeof response.error === 'string' ? response.error : '')
          || translate('paper.videoFailed', 'Video generation failed');
        state.taskId = '';
        stopVideoPolling();
        render();
      }
      return;
    }
    if (replay.changed) render(); else renderProgress();
    if (state.status === 'generating') startVideoTick();
    renderActivity();
    scheduleVideoPoll();
  } catch (error: unknown) {
    console.warn('[Paper:Video] poll failed:', error);
    failVideoPoll();
  } finally {
    state.pollBusy = false;
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
    if (state.paperHash !== generationHash) return;
    if (response?.report_required) {
      state.status = 'report_required';
      render();
      return;
    }
    if (response?.ok && response.task_id) {
      state.taskId = response.task_id;
      state.cursor = 0;
      renderProgress();
      scheduleVideoPoll();
      return;
    }
    state.status = 'error';
    state.errorText = response?.error
      || translate('paper.videoFailed', 'Video generation failed');
    render();
  } catch (error: unknown) {
    console.warn('[Paper:Video] start failed:', error);
    state.status = 'error';
    state.errorText = errorMessage(error);
    render();
  }
}

export async function abortVideo(): Promise<void> {
  const state = video();
  if (state.taskId) {
    try { await apiDomain('motion')?.abort(state.taskId); }
    catch (error: unknown) { console.warn('[Paper:Video] abort failed:', error); }
  }
  stopVideoPolling();
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
  try {
    const response = await apiDomain('motion')?.scenes(taskId);
    if (state._sceneLoadGeneration !== generation
        || state.paperHash !== paperHash
        || (state._doneTaskId || state.taskId || '') !== taskId) return;
    if (response?.ok) {
      state.scenes = response.scenes || [];
      globals()._pvRenderSceneGrid?.(cacheBust ? Date.now() : 0);
    }
  } catch (error: unknown) {
    console.warn('[Paper:Video] scenes load failed:', error);
  }
}

export async function regenerateVideoScene(sceneId: string): Promise<void> {
  const state = video();
  const taskId = state._doneTaskId || state.taskId;
  if (!taskId || state.regenSceneId) return;
  state.regenSceneId = sceneId;
  globals()._pvRenderSceneGrid?.(0);
  try {
    const response = await apiDomain('motion')?.regenScene(taskId, sceneId);
    if (response?.ok && response.task_id) {
      globals().paperDetachPush?.(state);
      state._seqSeen = -1;
      state._replayCursor = 0;
      state.regenTaskId = response.task_id;
      state.cursor = 0;
      scheduleVideoPoll();
      return;
    }
  } catch (error: unknown) {
    console.warn('[Paper:Video] regen failed:', error);
  }
  state.regenSceneId = '';
  globals()._pvRenderSceneGrid?.(0);
}

export function destroyVideoRuntime(): void {
  const state = globals()._pvideo as LooseObject | undefined;
  if (!state) return;
  stopVideoPolling();
  state._sceneLoadGeneration = Number(state._sceneLoadGeneration || 0) + 1;
  state.taskId = '';
  state.regenTaskId = '';
  state.regenSceneId = '';
}

export function installVideoRuntime(): void {
  const target = globals();
  target._pvResetRun = resetVideoRun;
  target._pvStopPoll = stopVideoPoll;
  target._pvStopPolling = stopVideoPolling;
  target._pvStartTick = startVideoTick;
  target._pvStopTick = stopVideoTick;
  target._initVideoTab = initVideoTab;
  target._pvSchedulePoll = scheduleVideoPoll;
  target._pvAttachPush = attachVideoPush;
  target._pvEtaTick = updateVideoEta;
  target._pvConsumeEvent = consumeVideoEvent;
  target._pvPollFail = failVideoPoll;
  target._pvPollOnce = pollVideoOnce;
  target._videoGenerate = generateVideo;
  target._videoAbort = abortVideo;
  target._pvLoadScenes = loadVideoScenes;
  target._videoRegenScene = regenerateVideoScene;
  target._destroyVideoRuntime = destroyVideoRuntime;
}

installVideoRuntime();
