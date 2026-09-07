"""Compiled Podcast task owner: reattach clocks, replay and teardown."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit
ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), '..'))
SOURCE = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'paper', 'podcast-runtime.ts')
ESBUILD = os.path.join(ROOT, 'scripts', 'vite_test_bundle.mjs')


_HARNESS = r"""
const fs = require('fs'), path = require('path');
const ROOT = process.argv[1], BUILT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="paperPodcastContent"></div>' +
  '<select id="podcastModeSel"><option value="full">full</option></select>' +
  '<select id="podcastLangSel"><option value="en">en</option></select>' +
  '<input id="podcastVoiceInp" value="voice-x"></body>', { url:'http://localhost/' });
global.window = global;
global.document = dom.window.document;
global.localStorage = dom.window.localStorage;
global._paperHash = 'hash-a';
global._PODCAST_POLL_MS = 999999;
global._PC_POLL_FAIL_LIMIT = 5;
global._podcast = { paperHash:'', mode:'short', lang:'zh', voice:'', model:'m1',
  artifactModel:'', taskId:'', cursor:0, pollTimer:null, pollBusy:false,
  status:'idle', data:null, errorText:'', progress:{done:0,total:0},
  pollFails:0, phases:[], phaseIndex:0, currentPhase:'', scriptStep:'',
  scriptChars:0, scriptSegments:0, scriptCharTarget:0, genStartedAt:0,
  lastEventAt:0, tickTimer:null, sleepTimerId:0, sleepDeadline:0,
  _segFirstTick:0, etaSec:0 };
const calls = { renders:0, progress:0, detach:0, aborts:[], starts:[] };
global._pcRender = () => { calls.renders++; };
global._pcRenderProgress = () => { calls.progress++; };
global._pcRenderActivity = () => {};
global._pcT = (_key, fallback) => fallback;
global._pcEsc = (value) => String(value);
global._pcSeedOptions = () => {};
global._pcPersistOptions = () => {};
global._pmSeedModel = () => {};
global._pmAdoptModel = (_kind, model) => { _podcast.artifactModel = model; };
global.paperDetachPush = () => { calls.detach++; };
let push = null;
global.paperAttachPush = (_state, taskId, options) => { push = { taskId, options }; };
global.paperIngestEvent = (state, event, consume) => consume(state, event);
global.taskReplayIngestPage = (state, page, consume, cursor) => {
  let changed = false;
  for (const event of page.events || []) changed = Boolean(consume(state, event)) || changed;
  return { accepted:(page.events || []).length, nextCursor:page.cursor ?? cursor,
    changed };
};
const oldCreated = Date.now() - 120000, oldUpdated = Date.now() - 40000;
let pollResponse = { ok:true, done:false, cursor:1,
  events:[{ type:'phase_started', phase:'script', phases:['script','audio'], phase_index:1 }] };
let startResolve;
global.Api = { paper: {
  podcastStatus: async () => ({ ok:true, tts_available:true, default_voice:'alloy' }),
  podcastLookup: async () => ({ ok:true, found:true, running:true, task_id:'task-live',
    createdAt:oldCreated, updatedAt:oldUpdated, model:'m-live' }),
  podcastPoll: async () => pollResponse,
  podcastStart: async (body) => { calls.starts.push(body);
    return new Promise((resolve) => { startResolve = resolve; }); },
  podcastAbort: async (taskId) => { calls.aborts.push(taskId); },
}};

(0, eval)(fs.readFileSync(BUILT, 'utf8'));
const PodcastRuntime = global.TofuTestBundle;

(async () => {
  await PodcastRuntime.initPodcastTab();
  const reattach = { taskId:_podcast.taskId, status:_podcast.status,
    oldStart:_podcast.genStartedAt <= oldCreated, oldActivity:_podcast.lastEventAt <= oldUpdated,
    pushTask:push && push.taskId };
  pollResponse = { ok:true, done:true, status:'done', cursor:2, events:[],
    scriptOnly:false, model:'m-done', script:{segments:[]} };
  await PodcastRuntime.pollPodcastOnce();
  const terminal = { status:_podcast.status, taskId:_podcast.taskId,
    cursor:_podcast.cursor, model:_podcast.artifactModel };

  _podcast.paperHash = 'hash-a';
  const pending = PodcastRuntime.generatePodcast();
  await new Promise((resolve) => setTimeout(resolve, 0));
  _podcast.paperHash = 'hash-b';
  startResolve({ ok:true, task_id:'stale-task' });
  await pending;
  const staleFenced = _podcast.taskId !== 'stale-task';

  _podcast.taskId = 'abort-me';
  await PodcastRuntime.abortPodcast();
  _podcast.taskId = 'destroy-me';
  PodcastRuntime.destroyPodcastRuntime();
  console.log(JSON.stringify({ reattach, terminal, staleFenced,
    aborts:calls.aborts, destroyed:_podcast.taskId === '' && !_podcast.pollTimer,
    leakedGlobal:typeof global._initPodcastTab === 'function' }));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""


@pytest.mark.skipif(
    not shutil.which('node') or not os.path.isfile(ESBUILD)
    or not os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom')),
    reason='node/jsdom/esbuild dev dependencies required',
)
def test_compiled_podcast_runtime_owns_task_state_and_teardown(tmp_path):
    built = tmp_path / 'podcast-runtime.js'
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
        'taskId': 'task-live', 'status': 'generating', 'oldStart': True,
        'oldActivity': True, 'pushTask': 'task-live',
    }
    assert result['terminal'] == {
        'status': 'done', 'taskId': '', 'cursor': 2, 'model': 'm-done',
    }
    assert result['staleFenced']
    assert result['aborts'] == ['abort-me']
    assert result['destroyed'] and not result['leakedGlobal']
