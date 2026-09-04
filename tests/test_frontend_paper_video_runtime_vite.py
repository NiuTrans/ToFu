"""Compiled Video task owner: replay, scene races and teardown."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
SOURCE = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'paper', 'video-runtime.ts')
ESBUILD = os.path.join(ROOT, 'scripts', 'vite_test_bundle.mjs')


_HARNESS = r"""
const fs = require('fs'), path = require('path');
const ROOT = process.argv[1], BUILT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="paperVideoContent"></div>' +
  '<select id="videoLangSel"><option value="en">en</option></select>' +
  '<input id="videoVoiceInp" value="voice-v"><input id="videoNarrChk" type="checkbox" checked>' +
  '<input id="videoBurnChk" type="checkbox"><select id="videoQualSel">' +
  '<option value="high">high</option></select><select id="videoVisualSel">' +
  '<option value="authored">authored</option></select></body>', { url:'http://localhost/' });
global.window = global;
global.location = dom.window.location;
global.localStorage = dom.window.localStorage;
global.document = dom.window.document;
global._paperHash = 'hash-a';
global._PVIDEO_POLL_MS = 999999;
global._PV_POLL_FAIL_LIMIT = 5;
global._pvideo = { paperHash:'', lang:'zh', voice:'', model:'m1', artifactModel:'',
  narration:true, burnIn:false, quality:'standard', visual:'authored',
  quality_axis:null, taskId:'', cursor:0, pollTimer:null, pollBusy:false,
  status:'idle', errorText:'', progress:{done:0,total:0,phase:''}, result:null,
  scenes:[], regenSceneId:'', regenTaskId:'', ttsAvailable:true, pollFails:0,
  phases:[], phaseIndex:0, genStartedAt:0, lastEventAt:0, tickTimer:null,
  _rateFirstTick:0, _rateFirstDone:0, etaSec:0, _gridLoaded:false };
const calls = { renders:0, progress:0, detach:0, scenePaints:0,
  aborts:[], starts:[], regens:[] };
global._pvRender = () => { calls.renders++; };
global._pvRenderProgress = () => { calls.progress++; };
global._pvRenderActivity = () => {};
global._pvRenderSceneGrid = () => { calls.scenePaints++; };
global._pvT = (_key, fallback) => fallback;
global._pvEsc = (value) => String(value);
global._pmSeedModel = () => {};
global._pmAdoptModel = (_kind, model) => { _pvideo.artifactModel = model; };
global.paperDetachPush = () => { calls.detach++; };
let push = null;
global.paperAttachPush = (_state, taskId, options) => { push = { taskId, options }; };
global.paperIngestEvent = (state, event, consume) => consume(state, event);
global.taskReplayIngestPage = (state, page, consume, cursor) => {
  let changed = false;
  for (const event of page.events || []) changed = Boolean(consume(state, event)) || changed;
  return { accepted:(page.events || []).length,
    nextCursor:page.next_cursor ?? cursor, changed };
};
const oldCreated = Date.now() - 180000, oldUpdated = Date.now() - 45000;
let pollResponse = { ok:true, done:false, next_cursor:1,
  events:[{ type:'phase_started', phase:'compose', phases:['compose','render'], phase_index:1 }] };
let startResolve, sceneResolve;
global.Api = {
  paper: {
    videoLookup: async () => ({ ok:true, found:true, running:true, task_id:'video-live',
      createdAt:oldCreated, updatedAt:oldUpdated, model:'m-live' }),
    videoStart: async (body) => { calls.starts.push(body);
      return new Promise((resolve) => { startResolve = resolve; }); },
  },
  motion: {
    status: async () => ({ ok:true, tts_available:true }),
    poll: async () => pollResponse,
    abort: async (taskId) => { calls.aborts.push(taskId); },
    scenes: async () => new Promise((resolve) => { sceneResolve = resolve; }),
    regenScene: async (taskId, sceneId) => { calls.regens.push([taskId, sceneId]);
      return { ok:true, task_id:'regen-1' }; },
  },
};

(0, eval)(fs.readFileSync(BUILT, 'utf8'));
const VideoRuntime = global.TofuTestBundle;

(async () => {
  await VideoRuntime.initVideoTab();
  const reattach = { taskId:_pvideo.taskId, status:_pvideo.status,
    oldStart:_pvideo.genStartedAt <= oldCreated, oldActivity:_pvideo.lastEventAt <= oldUpdated,
    pushTask:push && push.taskId };
  pollResponse = { ok:true, done:true, status:'done', next_cursor:2, events:[],
    model:'m-done', result:{ duration:12 }, artifact_quality:{ degraded:false } };
  const terminalPoll = VideoRuntime.pollVideoOnce();
  await terminalPoll;
  const terminal = { status:_pvideo.status, taskId:_pvideo.taskId,
    cursor:_pvideo.cursor, doneId:_pvideo._doneTaskId, model:_pvideo.artifactModel };

  // The terminal poll started a scene request. Switching papers must fence it.
  _pvideo.paperHash = 'hash-b';
  sceneResolve({ ok:true, scenes:[{scene_id:'old'}] });
  await new Promise((resolve) => setTimeout(resolve, 0));
  const oldScenesFenced = _pvideo.scenes.length === 0 && calls.scenePaints === 0;

  _pvideo.paperHash = 'hash-a'; _pvideo._doneTaskId = 'video-live';
  const regen = VideoRuntime.regenerateVideoScene('scene-2');
  await regen;
  const regenAttached = _pvideo.regenTaskId === 'regen-1' && push.taskId === 'regen-1';

  _pvideo.regenTaskId = ''; _pvideo.regenSceneId = '';
  const pending = VideoRuntime.generateVideo();
  await new Promise((resolve) => setTimeout(resolve, 0));
  _pvideo.paperHash = 'hash-new';
  startResolve({ ok:true, task_id:'stale-video' });
  await pending;
  const staleFenced = _pvideo.taskId !== 'stale-video';

  _pvideo.taskId = 'abort-me';
  await VideoRuntime.abortVideo();
  _pvideo.taskId = 'destroy-me'; _pvideo.regenTaskId = 'regen-old';
  VideoRuntime.destroyVideoRuntime();
  console.log(JSON.stringify({ reattach, terminal, oldScenesFenced,
    regenAttached, regens:calls.regens, staleFenced, aborts:calls.aborts,
    destroyed:_pvideo.taskId === '' && _pvideo.regenTaskId === '' && !_pvideo.pollTimer,
    leakedGlobal:typeof global._initVideoTab === 'function' }));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.skipif(
    not shutil.which('node') or not os.path.isfile(ESBUILD)
    or not os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom')),
    reason='node/jsdom/esbuild dev dependencies required',
)
def test_compiled_video_runtime_owns_tasks_and_fences_scene_races(tmp_path):
    built = tmp_path / 'video-runtime.js'
    compiled = subprocess.run(
        [ESBUILD, SOURCE, '--bundle', '--format=iife', '--platform=browser',
         f'--outfile={built}'], capture_output=True, text=True, timeout=60)
    assert compiled.returncode == 0, compiled.stderr
    process = subprocess.run(
        ['node', '-e', _HARNESS, ROOT, str(built)], capture_output=True,
        text=True, timeout=60)
    assert process.returncode == 0, process.stderr + process.stdout
    result = json.loads(process.stdout.strip().splitlines()[-1])
    assert result['reattach'] == {
        'taskId': 'video-live', 'status': 'generating', 'oldStart': True,
        'oldActivity': True, 'pushTask': 'video-live',
    }
    assert result['terminal'] == {
        'status': 'done', 'taskId': '', 'cursor': 2,
        'doneId': 'video-live', 'model': 'm-done',
    }
    assert result['oldScenesFenced']
    assert result['regenAttached']
    assert result['regens'] == [['video-live', 'scene-2']]
    assert result['staleFenced']
    assert result['aborts'] == ['abort-me']
    assert result['destroyed'] and not result['leakedGlobal']
