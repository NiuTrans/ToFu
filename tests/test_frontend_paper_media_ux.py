#!/usr/bin/env python3
"""jsdom guards for the P-UX frontend contract (P-UX1~4).

Drives the shipped classic renderers together with the compiled native
Podcast/Video runtimes under jsdom with stubbed Api seams and probes
the progress-perception / anti-stuck behaviors
(docs/PAPER_MEDIA_UX_DESIGN.md §3.4):

  * P-UX1: 5 consecutive poll failures → honest `lost` terminal state
    (recheck + regenerate buttons wired); server-reaped worker_lost maps
    to the same state. NEUTER: amputating the fail-limit branch restores
    the old infinite-spinner behavior and flips the probe — the backstop
    is load-bearing.
  * P-UX2: generating renders the phase stepper + elapsed/last-activity
    line; phase_started advances the active step; script sub-step labels
    show; >30s silence tints the line stale.
  * P-UX3: video scene_done events incrementally fill the scene grid
    (pending cells light up); ETA appears for render/narrate phases.
  * P-UX4: lookup `interrupted` → honest interrupted card.

Static guards (no node): i18n zh+en keys, CSS classes, JS syntax.
Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

import pytest
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root

pytestmark = [pytest.mark.unit, pytest.mark.serial]

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = _legacy_test_root()
PODCAST_JS = os.path.join(ROOT, 'static', 'js', 'paper', 'podcast.js')
VIDEO_JS = os.path.join(ROOT, 'static', 'js', 'paper', 'video.js')
PODCAST_RUNTIME_TS = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'paper', 'podcast-runtime.ts')
VIDEO_RUNTIME_TS = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'paper', 'video-runtime.ts')
PUSH_TRANSPORT_TS = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'paper', 'push-transport.ts')
MEDIA_MODEL_TS = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'paper', 'media-model-ui.ts')
ESBUILD = os.path.join(ROOT, 'scripts', 'vite_test_bundle.mjs')
FEATURE_REGISTRY_TS = os.path.join(
    ROOT, 'frontend', 'src', 'feature-registry.ts')
_NODE_HARNESS_TIMEOUT_S = 180


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return (os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))
            and os.path.isfile(ESBUILD))


_PODCAST_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div id="paperPodcastContent"></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = global;
global.document = win.document;
const T_MAP = {
  'paper.podcastLost': 'LOST_TEXT',
  'paper.podcastInterrupted': 'INTERRUPTED_TEXT',
  'paper.podcastRecheck': 'RECHECK_BTN',
  'paper.podcastRegenerate': 'REGEN_BTN',
  'paper.podcastPhaseSource': 'STEP_SOURCE',
  'paper.podcastPhaseScript': 'STEP_SCRIPT',
  'paper.podcastPhaseAudio': 'STEP_AUDIO',
  'paper.podcastStepValidate': 'VALIDATING',
  'paper.podcastScriptPhase': 'WRITING_SCRIPT',
  'paper.podcastAudioPhase': 'SYNTH_AUDIO',
  'paper.mediaEtaPrefix': '≈',
  'paper.mediaElapsed': 'elapsed',
  'paper.mediaLastActive': 'last-active',
  'paper.mediaStillRunning': 'STILL_RUNNING',
};
win.t = global.t = (k) => T_MAP[k] || k;
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;');
win._paperHash = 'hash123abc';
global._paperHash = 'hash123abc';

const apiState = {
  statusResp: { ok: true, tts_available: true, default_voice: 'alloy' },
  lookupResp: { ok: true, found: false, report_available: true },
  startResp: { ok: true, task_id: 'podcast_x1' },
  pollQueue: [],
  pollNull: false,
};
global.Api = win.Api = { paper: {
  podcastStatus: async () => apiState.statusResp,
  podcastLookup: async () => apiState.lookupResp,
  podcastStart: async () => apiState.startResp,
  podcastPoll: async () => {
    if (apiState.pollNull) return null;
    return apiState.pollQueue.length
      ? apiState.pollQueue.shift()
      : { ok: true, done: false, cursor: 99, events: [] };  // no progress key → client keeps the last counts
  },
  podcastAbort: async () => ({}),
  podcastScript: async () => ({}),
}};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // paper/podcast.js (real)
Object.assign(global, { _podcast, _pcRender, _pcRenderProgress,
  _pcRenderActivity, _pcT, _pcEsc, _pcSeedOptions, _pcPersistOptions,
  _PODCAST_POLL_MS: 1, _PC_POLL_FAIL_LIMIT });
eval(fs.readFileSync(process.argv[4], 'utf8'));  // native push transport
eval(fs.readFileSync(process.argv[5], 'utf8'));  // native podcast runtime

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
function host() { return document.getElementById('paperPodcastContent'); }
async function settle(n) { for (let i = 0; i < n; i++) await new Promise(r => setTimeout(r, 3)); }

(async () => {
  // ── Case A: 5 consecutive poll failures → lost (P-UX1 backstop) ──
  apiState.lookupResp = { ok: true, found: false, report_available: true };
  await _initPodcastTab();
  apiState.pollNull = true;
  await _podcastGenerate();
  await settle(25);
  check('lost_after_5_fails', _podcast.status === 'lost');
  check('lost_render_text', host().innerHTML.includes('LOST_TEXT'));
  const recheckBtn = Array.from(host().querySelectorAll('button'))
    .find(b => (b.getAttribute('data-tofu-action') || '').includes('_initPodcastTab'));
  check('lost_recheck_wired', !!recheckBtn);
  const regenBtn = Array.from(host().querySelectorAll('button'))
    .find(b => (b.getAttribute('data-tofu-action') || '').includes('_podcastGenerate(true)'));
  check('lost_regen_wired', !!regenBtn);
  // spinner has a lifetime: no poll timer survives the lost state
  check('lost_stops_polling', _podcast.pollTimer === null && _podcast.taskId === '');

  // ── Case B: server-reaped worker_lost → same lost state ──
  apiState.pollNull = false;
  apiState.pollQueue = [
    { ok: true, done: true, status: 'error', cursor: 1, events: [],
      error: { kind: 'worker_lost', detail: 'no progress events' } },
  ];
  await _podcastGenerate(true);
  await settle(8);
  check('worker_lost_maps_to_lost', _podcast.status === 'lost');

  // ── Case C: stepper + phase events + sub-step + ETA + stale tint ──
  apiState.pollQueue = [
    { ok: true, done: false, cursor: 1, progress: { done: 0, total: 0 },
      events: [{ type: 'phase_started', phase: 'script', phase_index: 2,
                 phase_total: 3, phases: ['source', 'script', 'audio'] }] },
    { ok: true, done: false, cursor: 2, progress: { done: 0, total: 0 },
      events: [{ type: 'progress', phase: 'script', unit: 'pass', step: 'validate' }] },
    { ok: true, done: false, cursor: 3, progress: { done: 1, total: 4 },
      events: [{ type: 'phase_started', phase: 'audio', phase_index: 3,
                 phase_total: 3, phases: ['source', 'script', 'audio'] },
               { type: 'segment_done', done: 1, total: 4 }] },
    { ok: true, done: false, cursor: 4, progress: { done: 2, total: 4 },
      events: [{ type: 'segment_done', done: 2, total: 4 }] },
  ];
  await _podcastGenerate(true);
  await settle(14);
  const html = host().innerHTML;
  check('stepper_rendered', !!host().querySelector('.paper-stepper'));
  check('stepper_labels', html.includes('STEP_SOURCE') && html.includes('STEP_SCRIPT')
    && html.includes('STEP_AUDIO'));
  check('stepper_third_active', !!host().querySelectorAll('.paper-step.is-active')[0]
    && host().querySelectorAll('.paper-step.is-active')[0].textContent.includes('STEP_AUDIO'));
  check('stepper_first_two_done',
    host().querySelectorAll('.paper-step.is-done').length === 2);
  check('progress_counts', html.includes('2/4'));
  // ETA: fake an old first tick so the wall-clock rate is visible
  _podcast._segFirstTick = Date.now() - 60000;
  _pcConsumeEvent({ type: 'segment_done', done: 3, total: 4 });
  _pcRenderProgress();
  check('eta_shown', host().innerHTML.includes('≈'));
  // sub-step label rendered while in script phase
  _podcast.progress = { done: 0, total: 0 };
  _podcast.scriptStep = 'validate';
  _pcRenderProgress();
  check('script_substep_shown', host().innerHTML.includes('VALIDATING'));
  // activity line + stale tint after 30s of silence
  check('activity_line_present', !!document.getElementById('podcastActivityLine'));
  _podcast.lastEventAt = Date.now() - 31000;
  _pcRenderActivity();
  const act = document.getElementById('podcastActivityLine');
  check('activity_stale_tint', act.classList.contains('is-stale')
    && act.textContent.includes('STILL_RUNNING'));
  _pcStopPoll();

  // ── Case D: interrupted lookup → honest interrupted card (P-UX4) ──
  apiState.lookupResp = { ok: true, found: true, interrupted: true,
                          report_available: true };
  await _initPodcastTab(true);
  check('interrupted_state', _podcast.status === 'interrupted');
  check('interrupted_render', host().innerHTML.includes('INTERRUPTED_TEXT')
    && host().innerHTML.includes('REGEN_BTN'));

  /* ── Case L: liveness survives polling (the "looks stuck" regression) ──
   * A SILENT worker (polls succeed, zero events) is the normal shape of a
   * long LLM/TTS phase. Two independent things must hold, or the card looks
   * frozen for minutes:
   *   (1) the 1s ticker must survive _pcSchedulePoll()'s stop/re-arm, else
   *       elapsed freezes at 0:00 on the very first poll;
   *   (2) an empty-but-successful poll must NOT reset lastEventAt, else
   *       "last activity" reads 0:00 forever and the >30s stale tint (which
   *       tells the user "quiet, not dead") can never fire. */
  apiState.lookupResp = { ok: true, found: false, report_available: true };
  await _initPodcastTab(true);
  apiState.pollQueue = [];          // silent worker: polls succeed, no events
  await _podcastGenerate(true);
  await new Promise(r => setTimeout(r, 1200));   // real time: 1s tick must fire
  check('liveness_ticker_survives_polling',
    _podcast.tickTimer !== null && _podcast.tickTimer !== 0);
  const liveLine = document.getElementById('podcastActivityLine');
  check('liveness_elapsed_advances',
    !!liveLine && /0:0[1-9]|0:[1-9]\d/.test(liveLine.textContent));
  // Empty polls must not masquerade as worker activity.
  _podcast.lastEventAt = Date.now() - 31000;
  await _pcPollOnce();
  check('liveness_empty_poll_keeps_quiet_clock',
    Date.now() - _podcast.lastEventAt > 30000);
  _pcRenderActivity();
  check('liveness_stale_tint_reachable_while_polling',
    liveLine.classList.contains('is-stale'));
  // A real event DOES reset it.
  _pcConsumeEvent({ type: 'heartbeat', phase: 'script', elapsed_s: 10 });
  apiState.pollQueue = [{ ok: true, done: false, cursor: 7,
    events: [{ type: 'heartbeat', phase: 'script', elapsed_s: 20 }] }];
  await _pcPollOnce();
  check('liveness_real_event_resets_clock',
    Date.now() - _podcast.lastEventAt < 5000);
  _pcStopPolling();
  check('liveness_terminal_stops_ticker', _podcast.tickTimer === null);

  console.log(out.join('\n'));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""


_VIDEO_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div id="paperVideoContent"></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = global;
global.document = win.document;
const T_MAP = {
  'paper.podcastLost': 'LOST_TEXT',
  'paper.podcastInterrupted': 'INTERRUPTED_TEXT',
  'paper.podcastRecheck': 'RECHECK_BTN',
  'paper.podcastRegenerate': 'REGEN_BTN',
  'paper.videoPhaseStoryboard': 'PH_STORYBOARD',
  'paper.videoPhaseNarrate': 'PH_NARRATE',
  'paper.videoPhaseCompose': 'PH_COMPOSE',
  'paper.videoPhaseRender': 'PH_RENDER',
  'paper.videoPhaseConcat': 'PH_CONCAT',
  'paper.videoPhaseMux': 'PH_MUX',
  'paper.videoStarting': 'STARTING',
  'paper.videoScenesTitle': 'SCENES_TITLE',
  'paper.mediaEtaPrefix': '≈',
  'paper.mediaElapsed': 'elapsed',
  'paper.mediaLastActive': 'last-active',
  'paper.mediaStillRunning': 'STILL_RUNNING',
};
win.t = global.t = (k) => T_MAP[k] || k;
win.escapeHtml = global.escapeHtml = (s) =>
  String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;');
win._paperHash = 'hash123abc';
global._paperHash = 'hash123abc';

const PHASES = ['storyboard', 'narrate', 'compose', 'render', 'concat', 'mux'];
const apiState = {
  lookupResp: { ok: true, found: false, report_available: true },
  startResp: { ok: true, task_id: 'motion_x1' },
  pollQueue: [],
  pollNull: false,
  scenesCalls: 0,
  scenesResp: { ok: true, task_id: 'motion_x1', status: 'running', scenes: [
    { scene_id: 'scene-001', start: 0, end: 4, text: '第一句',
      shot_recipe: 'hook-counter-burst', motion_family: 'metric-impact', shot_energy: 5,
      has_composition: true, has_video: true, has_narration: true },
    { scene_id: 'scene-002', start: 4, end: 8, text: '第二句',
      has_composition: true, has_video: false, has_narration: false },
  ]},
};
global.Api = win.Api = {
  motion: {
    status: async () => ({ ok: true, tts_available: true }),
    poll: async () => {
      if (apiState.pollNull) return null;
      return apiState.pollQueue.length
        ? apiState.pollQueue.shift()
        : { ok: true, done: false, next_cursor: 99, events: [] };
    },
    abort: async () => ({}),
    scenes: async () => { apiState.scenesCalls++; return apiState.scenesResp; },
    regenScene: async () => ({ ok: true, task_id: 'regen_1' }),
    fileUrl: (tid) => '/file/' + tid,
    sceneFileUrl: (tid, sid) => '/file/' + tid + '/' + sid,
  },
  paper: {
    videoLookup: async () => apiState.lookupResp,
    videoStart: async () => apiState.startResp,
  },
};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // paper/video.js (real)
Object.assign(global, { _pvideo, _pvRender, _pvRenderProgress,
  _pvRenderActivity, _pvRenderSceneGrid, _pvT, _pvEsc,
  _PVIDEO_POLL_MS: 1, _PV_POLL_FAIL_LIMIT });
eval(fs.readFileSync(process.argv[4], 'utf8'));  // native push transport
eval(fs.readFileSync(process.argv[5], 'utf8'));  // native video runtime

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
function host() { return document.getElementById('paperVideoContent'); }
async function settle(n) { for (let i = 0; i < n; i++) await new Promise(r => setTimeout(r, 3)); }

(async () => {
  // ── Case A: generating → stepper + grid fills on scene_done (P-UX2/3) ──
  apiState.lookupResp = { ok: true, found: false, report_available: true };
  await _initVideoTab();
  apiState.pollQueue = [
    { ok: true, done: false, next_cursor: 1,
      events: [{ type: 'phase_started', phase: 'storyboard', phase_index: 1,
                 phase_total: 6, phases: PHASES }] },
    { ok: true, done: false, next_cursor: 2,
      events: [{ type: 'phase_started', phase: 'render', phase_index: 4,
                 phase_total: 6, phases: PHASES }] },
    { ok: true, done: false, next_cursor: 3,
      events: [{ type: 'scene_done', scene_id: 'scene-001', ok: true,
                 done: 1, total: 2 }] },
  ];
  await _videoGenerate();
  await settle(12);
  const html = host().innerHTML;
  check('stepper_rendered', !!host().querySelector('.paper-stepper'));
  check('stepper_render_active', (() => {
    const act = host().querySelector('.paper-step.is-active');
    return !!act && act.textContent.includes('PH_RENDER');
  })());
  check('stepper_three_done',
    host().querySelectorAll('.paper-step.is-done').length === 3);
  check('progress_counts', html.includes('1/2'));
  check('grid_present_in_generating', !!document.getElementById('paperVideoGrid'));
  check('scenes_refetched_on_scene_done', apiState.scenesCalls >= 1);
  const cells = host().querySelectorAll('.paper-video-cell');
  check('grid_cells_rendered', cells.length === 2);
  const shotMeta = host().querySelector('.paper-video-cell-meta');
  check('shot_recipe_metadata_visible', !!shotMeta
    && shotMeta.textContent.includes('hook-counter-burst')
    && shotMeta.textContent.includes('E5'));
  check('grid_pending_cell_marked',
    host().querySelectorAll('.paper-video-cell.is-pending').length === 1);
  check('grid_lit_cell_has_video',
    !!host().querySelector('.paper-video-cell video.paper-video-thumb'));
  check('no_regen_buttons_while_generating',
    host().querySelectorAll('.paper-video-regen').length === 0);
  // ETA in render phase (fake an old rate tick so the rate is visible)
  _pvideo._rateFirstTick = Date.now() - 60000;
  _pvideo._rateFirstDone = 1;
  _pvConsumeEvent({ type: 'scene_done', scene_id: 'scene-002', ok: true,
                    done: 2, total: 3 });
  _pvRenderProgress();
  check('eta_shown_in_render', host().innerHTML.includes('≈'));
  // activity line + stale tint
  check('activity_line_present', !!document.getElementById('videoActivityLine'));
  _pvideo.lastEventAt = Date.now() - 31000;
  _pvRenderActivity();
  const act2 = document.getElementById('videoActivityLine');
  check('activity_stale_tint', act2.classList.contains('is-stale')
    && act2.textContent.includes('STILL_RUNNING'));
  _pvStopPoll();

  // ── Case B: 5 consecutive poll failures → lost (P-UX1) ──
  apiState.pollNull = true;
  await _videoGenerate(true);
  await settle(25);
  check('lost_after_5_fails', _pvideo.status === 'lost');
  check('lost_render_text', host().innerHTML.includes('LOST_TEXT'));
  check('lost_stops_polling', _pvideo.pollTimer === null && _pvideo.taskId === '');

  // ── Case C: server-reaped worker_lost → lost ──
  apiState.pollNull = false;
  apiState.pollQueue = [
    { ok: true, done: true, status: 'error', next_cursor: 1, events: [],
      error: { kind: 'worker_lost', detail: 'no progress events' } },
  ];
  await _videoGenerate(true);
  await settle(8);
  check('worker_lost_maps_to_lost', _pvideo.status === 'lost');

  // ── Case D: interrupted lookup → honest card (P-UX4) ──
  apiState.lookupResp = { ok: true, found: true, interrupted: true,
                          task_id: 'motion_old', report_available: true };
  await _initVideoTab(true);
  check('interrupted_state', _pvideo.status === 'interrupted');
  check('interrupted_render', host().innerHTML.includes('INTERRUPTED_TEXT')
    && host().innerHTML.includes('REGEN_BTN'));

  // ── Case E: regenerate passes force (dedup bypass, §2.1) ──
  let startBody = null;
  Api.paper.videoStart = async (b) => { startBody = b; return { ok: true, task_id: 'motion_f' }; };
  await _videoGenerate(true);
  check('regenerate_sends_force', !!startBody && startBody.force === true);

  /* ── Case L: liveness survives polling (see podcast harness for why) ── */
  apiState.lookupResp = { ok: true, found: false, report_available: true };
  Api.paper.videoStart = async () => ({ ok: true, task_id: 'motion_live' });
  await _initVideoTab(true);
  apiState.pollQueue = [];          // silent worker: polls succeed, no events
  await _videoGenerate(true);
  await new Promise(r => setTimeout(r, 1200));   // real time: 1s tick must fire
  check('liveness_ticker_survives_polling',
    _pvideo.tickTimer !== null && _pvideo.tickTimer !== 0);
  const liveLine = document.getElementById('videoActivityLine');
  check('liveness_elapsed_advances',
    !!liveLine && /0:0[1-9]|0:[1-9]\d/.test(liveLine.textContent));
  _pvideo.lastEventAt = Date.now() - 31000;
  await _pvPollOnce();
  check('liveness_empty_poll_keeps_quiet_clock',
    Date.now() - _pvideo.lastEventAt > 30000);
  _pvRenderActivity();
  check('liveness_stale_tint_reachable_while_polling',
    liveLine.classList.contains('is-stale'));
  apiState.pollQueue = [{ ok: true, done: false, next_cursor: 7,
    events: [{ type: 'heartbeat', phase: 'render', elapsed_s: 20 }] }];
  await _pvPollOnce();
  check('liveness_real_event_resets_clock',
    Date.now() - _pvideo.lastEventAt < 5000);
  _pvStopPolling();
  check('liveness_terminal_stops_ticker', _pvideo.tickTimer === null);

  console.log(out.join('\n'));
  process.exit(0);
})().catch((e) => { console.error(e); process.exit(1); });
"""


def _run_harness(harness_src: str, renderer_path: str,
                 runtime_path: str, name: str) -> str:
    # Keep generated harnesses off the shared/FUSE checkout. Fixed files in
    # tests/ made parallel workers contend with repository scans and could
    # consume the entire old 60-second budget before Node reached assertions.
    with tempfile.TemporaryDirectory(prefix=f'tofu-{name}-') as temp_dir:
        harness = os.path.join(temp_dir, f'{name}_harness.js')
        push_built = os.path.join(temp_dir, 'push-transport.js')
        runtime_built = os.path.join(temp_dir, 'media-runtime.js')
        with open(harness, 'w', encoding='utf-8') as f:
            f.write(harness_src)
        for source, output in ((PUSH_TRANSPORT_TS, push_built),
                               (runtime_path, runtime_built)):
            # Native owners register their legacy globals on the module-private
            # featureRegistry, so bundle through an entry that publishes both
            # the owner's exports and the registry onto globalThis — bare
            # `_initPodcastTab()` calls in the harness resolve exactly like
            # the production classic-script fallback.
            entry = os.path.join(
                temp_dir, os.path.basename(output) + '.entry.ts')
            with open(entry, 'w', encoding='utf-8') as f:
                f.write(
                    f'import * as owner from {source!r};\n'
                    f'import {{ featureRegistry }} from '
                    f'{FEATURE_REGISTRY_TS!r};\n'
                    'Object.assign(globalThis, owner, featureRegistry);\n')
            compiled = subprocess.run(
                [ESBUILD, entry, '--bundle', '--format=iife',
                 '--platform=browser', f'--outfile={output}'],
                capture_output=True, text=True, timeout=60)
            assert compiled.returncode == 0, compiled.stderr
        proc = subprocess.run(
            ['node', harness, renderer_path, ROOT, push_built, runtime_built],
            capture_output=True, text=True, timeout=_NODE_HARNESS_TIMEOUT_S)
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{proc.stdout}'
    return proc.stdout.strip()


def _portable_runtime_source(source: str) -> str:
    """Keep a mutated runtime's real dependencies resolvable outside src/."""
    return source.replace(
        "'./media-model-ui'", repr(MEDIA_MODEL_TS), 1).replace(
        "'../../feature-registry'", repr(FEATURE_REGISTRY_TS), 1)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_podcast_ux_state_machine():
    out = _run_harness(
        _PODCAST_HARNESS, PODCAST_JS, PODCAST_RUNTIME_TS, 'pux_podcast')
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'podcast P-UX failures:\n' + out
    assert out.count('PASS') >= 21, f'expected >=21 PASS lines, got:\n{out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_video_ux_state_machine():
    out = _run_harness(
        _VIDEO_HARNESS, VIDEO_JS, VIDEO_RUNTIME_TS, 'pux_video')
    fails = [ln for ln in out.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'video P-UX failures:\n' + out
    assert out.count('PASS') >= 22, f'expected >=22 PASS lines, got:\n{out}'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_podcast_fail_limit_loadbearing():
    """NEUTER: amputate the 5-strike branch from a COPY of the runtime →
    polls reschedule forever, `lost_after_5_fails` flips to FAIL — the
    backstop is what stands between a 404 and an infinite spinner."""
    src = open(PODCAST_RUNTIME_TS, encoding='utf-8').read()
    marker = 'if (state.pollFails >= limit) {'
    assert marker in src, 'fail-limit marker not found — test is stale'
    broken = src.replace(marker, 'if (false) {', 1)
    assert broken != src

    with tempfile.TemporaryDirectory(prefix='tofu-podcast-neuter-') as temp_dir:
        tmp = os.path.join(temp_dir, 'podcast-runtime.ts')
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(_portable_runtime_source(broken))
        out = _run_harness(
            _PODCAST_HARNESS, PODCAST_JS, tmp, 'pux_podcast_neuter')
        assert 'FAIL lost_after_5_fails' in out, \
            'amputating the fail limit did NOT flip the probe:\n' + out
    assert open(PODCAST_RUNTIME_TS, encoding='utf-8').read() == src, \
        'shipped file modified!'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_video_fail_limit_loadbearing():
    """NEUTER: same amputation on the Video runtime."""
    src = open(VIDEO_RUNTIME_TS, encoding='utf-8').read()
    marker = 'if (state.pollFails >= limit) {'
    assert marker in src, 'fail-limit marker not found — test is stale'
    broken = src.replace(marker, 'if (false) {', 1)
    assert broken != src

    with tempfile.TemporaryDirectory(prefix='tofu-video-neuter-') as temp_dir:
        tmp = os.path.join(temp_dir, 'video-runtime.ts')
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(_portable_runtime_source(broken))
        out = _run_harness(
            _VIDEO_HARNESS, VIDEO_JS, tmp, 'pux_video_neuter')
        assert 'FAIL lost_after_5_fails' in out, \
            'amputating the fail limit did NOT flip the probe:\n' + out
    assert open(VIDEO_RUNTIME_TS, encoding='utf-8').read() == src, \
        'shipped file modified!'


@pytest.mark.parametrize('module,name,stop_call', [
    ('podcast', 'pux_pc_tickneuter', 'stopPodcastTick();'),
    ('video', 'pux_pv_tickneuter', 'stopVideoTick();'),
])
@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_liveness_ticker_survives_polling(module, name, stop_call):
    """NEUTER: fold the ticker teardown back into the POLL-path stop (the
    pre-fix shape) → the 1s stopwatch dies on the first poll.

    Probe choice (updated 2026-07-27): this asserted ``FAIL
    liveness_elapsed_advances``, because a dead ticker used to leave the
    elapsed line frozen. The poll path now repaints the liveness line itself
    (so a clockless disk-fallback re-attach cannot sit at 0:00 until the next
    tick), which means a dead ticker no longer freezes the TEXT — it degrades
    it from 1s granularity to once-per-poll. That old assertion is therefore
    asserting something FALSE, and the honest probe for the damage this neuter
    does is the ticker liveness itself, which still flips.

    The split remains load-bearing: without it the stopwatch only moves when a
    poll happens to land, so between polls a silent-but-healthy worker shows a
    stalled clock — the reported "stuck" symptom in slower form.
    """
    renderer_path = PODCAST_JS if module == 'podcast' else VIDEO_JS
    runtime_path = (PODCAST_RUNTIME_TS if module == 'podcast'
                    else VIDEO_RUNTIME_TS)
    harness_src = _PODCAST_HARNESS if module == 'podcast' else _VIDEO_HARNESS
    src = open(runtime_path, encoding='utf-8').read()
    anchor = ('  if (state.pollTimer) window.clearTimeout(state.pollTimer);\n'
              '  state.pollTimer = null;\n}')
    assert anchor in src, 'poll-stop marker not found — test is stale'
    broken = src.replace(
        anchor,
        '  if (state.pollTimer) window.clearTimeout(state.pollTimer);\n'
        '  state.pollTimer = null;\n'
        f'  {stop_call}\n}}', 1)
    assert broken != src

    with tempfile.TemporaryDirectory(prefix=f'tofu-{name}-') as temp_dir:
        tmp = os.path.join(temp_dir, f'{module}-runtime.ts')
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(_portable_runtime_source(broken))
        out = _run_harness(harness_src, renderer_path, tmp, name)
        assert 'FAIL liveness_ticker_survives_polling' in out, \
            'restoring the ticker-in-poll-stop did NOT flip the probe:\n' + out
    assert open(runtime_path, encoding='utf-8').read() == src, \
        'shipped file modified!'


@pytest.mark.parametrize('module,name,state', [
    ('podcast', 'pux_pc_clockneuter', '_podcast'),
    ('video', 'pux_pv_clockneuter', '_pvideo'),
])
@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_empty_poll_must_not_fake_activity(module, name, state):
    """NEUTER: let an empty-but-successful poll bump lastEventAt again (the
    pre-fix shape) → "last activity" resets every poll, the >30s stale tint
    becomes unreachable, and the quiet-clock probe flips FAIL. Proves the
    event-gated reset is what distinguishes "server answers" from "worker is
    actually doing something"."""
    renderer_path = PODCAST_JS if module == 'podcast' else VIDEO_JS
    runtime_path = (PODCAST_RUNTIME_TS if module == 'podcast'
                    else VIDEO_RUNTIME_TS)
    harness_src = _PODCAST_HARNESS if module == 'podcast' else _VIDEO_HARNESS
    src = open(runtime_path, encoding='utf-8').read()
    anchor = 'if (replay.accepted) state.lastEventAt = Date.now();'
    assert anchor in src, 'event-gated liveness marker not found — test is stale'
    broken = src.replace(anchor, 'state.lastEventAt = Date.now();', 1)
    assert broken != src

    with tempfile.TemporaryDirectory(prefix=f'tofu-{name}-') as temp_dir:
        tmp = os.path.join(temp_dir, f'{module}-runtime.ts')
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(_portable_runtime_source(broken))
        out = _run_harness(harness_src, renderer_path, tmp, name)
        assert 'FAIL liveness_empty_poll_keeps_quiet_clock' in out, \
            'un-gating the liveness clock did NOT flip the probe:\n' + out
    assert open(runtime_path, encoding='utf-8').read() == src, \
        'shipped file modified!'


# ═══ Static guards (no node required) ═══


def test_static_i18n_keys():
    # i18n authority is the locale JSON pair, not the deleted static/js tree.
    import json
    locales = {}
    for lang in ('zh', 'en'):
        with open(os.path.join(
                ROOT, 'frontend', 'src', 'i18n', 'locales',
                f'{lang}.json'), encoding='utf-8') as f:
            locales[lang] = json.load(f)
    keys = ('paper.podcastPhaseSource', 'paper.podcastPhaseScript',
            'paper.podcastPhaseAudio', 'paper.podcastStepDraft',
            'paper.podcastStepValidate', 'paper.podcastStepRevise',
            'paper.podcastStepCritic', 'paper.podcastLost',
            'paper.podcastInterrupted', 'paper.podcastRecheck',
            'paper.mediaElapsed', 'paper.mediaLastActive',
            'paper.mediaStillRunning', 'paper.mediaEtaPrefix',
            'paper.videoPhaseRender')
    for key in keys:
        for lang in ('zh', 'en'):
            value = locales[lang].get(key)
            assert isinstance(value, str), f'i18n key {key} missing in {lang}'
            assert value.strip(), f'i18n key {key} has an empty {lang}'


def test_static_css_classes():
    css = open(os.path.join(ROOT, 'static', 'styles.css'), encoding='utf-8').read()
    for cls in ('.paper-stepper', '.paper-step.is-active',
                '.paper-step.is-done', '.paper-media-activity',
                '.paper-media-activity.is-stale', '.paper-video-cell.is-pending'):
        assert cls in css, f'CSS class {cls} missing'


def test_static_js_syntax():
    if not shutil.which('node'):
        pytest.skip('node not installed')
    for path in (PODCAST_JS, VIDEO_JS):
        proc = subprocess.run(['node', '--check', path],
                              capture_output=True, text=True, timeout=30)
        assert proc.returncode == 0, f'{path} syntax: {proc.stderr}'


if __name__ == '__main__':
    import sys
    test_static_i18n_keys()
    test_static_css_classes()
    if _node_deps_available():
        test_static_js_syntax()
        test_podcast_ux_state_machine()
        test_video_ux_state_machine()
        test_NEUTER_podcast_fail_limit_loadbearing()
        test_NEUTER_video_fail_limit_loadbearing()
    else:
        print('SKIP jsdom cases — node + jsdom not available')
    print('ALL PASSED')
    sys.exit(0)
