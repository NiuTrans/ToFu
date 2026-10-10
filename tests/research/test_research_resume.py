"""Auto-research console resumes an in-flight server job after navigation.

Leaving the Evolution research page tears down only client resources; the job
keeps running under the owner. Re-entering must re-attach through
``GET /api/v1/tasks?kind=research`` instead of stranding the researcher on
the landing page until the run lands in the durable archive.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RESEARCH_RUNTIME_TS = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'evolution', 'research',
    'controllers', 'runtime.ts')
RESEARCH_PAGE_TS = os.path.join(
    ROOT, 'frontend', 'src', 'features', 'evolution', 'research', 'page.ts')
ESBUILD = os.path.join(ROOT, 'scripts', 'vite_test_bundle.mjs')


@pytest.mark.skipif(not shutil.which('node') or not os.path.isdir(
    os.path.join(ROOT, 'node_modules', 'jsdom')) or not os.path.isfile(ESBUILD),
    reason='node/jsdom/vite test bundler dev-deps not installed')
def test_resume_reattaches_running_job_and_hydrates_finished_one():
    harness = r"""
const fs = require('fs'), path = require('path');
const ROOT = process.argv[1];
const {JSDOM} = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<body><div id="researchViewer"></div></body>',
                      {url:'http://localhost/'});
global.window=global; global.document=dom.window.document;
global.t=(k)=>k;
const DIRECTION='mechanistic interpretability of small models';
const subscribed=[], unsubscribed=[], listCalls=[], lookups=[];
global.pushSubscribe=(channel,taskId)=>subscribed.push([channel,taskId]);
global.pushUnsubscribe=(channel,taskId)=>unsubscribed.push([channel,taskId]);
let scenario='running';
global.Api={tasks:{
  list:async(kind,status)=>{
    listCalls.push([kind,status]);
    const row={id:'task-resume-1',kind:'research',status:'running',
      created_at:1,meta:{direction:DIRECTION,lang:'zh'}};
    return {ok:true,tasks:(scenario==='none'||status!=='running')?[]:[row]};
  },
  get:async(id)=>({
    ok:true,
    status:scenario==='done'?'done':'running',
    createdAt:Date.now(),updatedAt:Date.now(),
    meta:{direction:DIRECTION,lang:'zh'},
    result:scenario==='done'?{accepted:[{}],rejected:[],corpus_size:2,
      gate_reached:'rubric'}:{},
    events:scenario==='done'?[]:[
      {seq:1,type:'stage_started',stage:'harvest'},
      {seq:2,type:'tool_start',roundNum:0,toolName:'web_search',
        toolCallId:'c1',query:'q'}],
  }),
  events:async(id,cursor)=>({ok:true}),
  abort:async()=>({ok:true}),
}, research:{lookup:async(direction,lang)=>{
  lookups.push([direction,lang]);
  return {found:true,accepted:[{}],rejected:[],gate_reached:'rubric'};
}}};
(0,eval)(fs.readFileSync(process.argv[2],'utf8'));
// Runtime state written by the module lives in the featureRegistry overrides,
// not on globalThis; read the stream through the registry.
const REG=globalThis[Object.keys(globalThis).find(
  (k)=>k.startsWith('TofuNativeTest_'))].featureRegistry;
(async()=>{
  const out={};
  // A) in-flight job: resume re-attaches push + replays the trajectory.
  out.a={resumed:await _resumeInflightResearch()};
  const a=REG._researchStream;
  out.a.direction=a&&a.direction; out.a.taskId=a&&a.taskId;
  out.a.status=a&&a.status; out.a.phase=a&&a.phase;
  out.a.rounds=a?a.toolRounds.length:0;
  out.a.subscribed=subscribed.slice();
  _destroyResearchRuntime();
  // B) job finished while away: resume projects done + hydrates the archive.
  scenario='done';
  out.b={resumed:await _resumeInflightResearch()};
  const b=REG._researchStream;
  await new Promise((resolve)=>setTimeout(resolve,20));
  out.b.status=b&&b.status; out.b.accepted=b&&b.accepted;
  out.b.subscribed=subscribed.slice();
  out.b.lookups=lookups.slice();
  _destroyResearchRuntime();
  // C) nothing in flight: stay on the landing, no half-restored stream.
  scenario='none';
  out.c={resumed:await _resumeInflightResearch(),stream:!!REG._researchStream,
    listCalls:listCalls.slice()};
  console.log(JSON.stringify(out));
})();
"""
    built = native_module_path('research/controllers/runtime.js', RESEARCH_RUNTIME_TS)
    proc = subprocess.run(['node', '-e', harness, ROOT, str(built)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout.strip().splitlines()[-1])

    assert got['a']['resumed'] is True
    assert got['a']['direction'] == (
        'mechanistic interpretability of small models')
    assert got['a']['taskId'] == 'task-resume-1'
    assert got['a']['status'] == 'running'
    assert got['a']['phase'] == 'harvest'
    assert got['a']['rounds'] == 1
    assert got['a']['subscribed'] == [['research', 'task-resume-1']]

    assert got['b']['resumed'] is True
    assert got['b']['status'] == 'done'
    assert got['b']['accepted'] == 1
    # A finished job must not hold a push subscription; it re-opens the
    # durable archive instead.
    assert got['b']['subscribed'] == [['research', 'task-resume-1']]
    assert got['b']['lookups'] == [[
        'mechanistic interpretability of small models', 'zh']]

    assert got['c']['resumed'] is False
    assert got['c']['stream'] is False
    assert got['c']['listCalls'][-2:] == [
        ['research', 'running'], ['research', 'pending']]


def test_research_page_mount_resumes_instead_of_stranding_on_landing():
    source = open(RESEARCH_PAGE_TS, encoding='utf-8').read()
    mount = source.split('export function mountResearchPage', 1)[1]
    assert '_resumeInflightResearch' in mount, (
        'mounting the Evolution research page with no local stream must '
        're-attach to an in-flight server job; otherwise any navigation away '
        'loses the live console and the user lands on the initial page again')
    runtime = open(RESEARCH_RUNTIME_TS, encoding='utf-8').read()
    assert '_resumeInflightResearch = resumeInflightResearch' in runtime
