"""Retained bridge: commands remain in composer; agents use the typed right dock.

Executes authored sections and typed production owners, without generated assets.
No backend/model/network work. Lifecycle budget is specified by the typed owner
suite and this retained composition test checks synchronous switch/disposal.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest
from tests._runtime_sections import ROOT, native_module_path, runtime_section

pytestmark = pytest.mark.unit

HARNESS = r"""
const fs=require('fs'),path=require('path'),assert=require('assert');
const {JSDOM}=require(path.join(process.argv[3],'node_modules','jsdom'));
const dom=new JSDOM('<body><main class="main"><div id="chatInner"><div class="conversation-surface" data-conversation-id="one"><div class="user-msg" data-lane-id="main" data-turn-id="human-one"><div data-conversation-part="turn-context-rail"></div></div></div></div></main><div id="convStatusStrip"><div id="turnNav"></div></div></body>',{url:'http://localhost/'});
global.window=dom.window;global.document=window.document;
Object.defineProperty(document,'hidden',{value:false,configurable:true});
global.runtimeScope={};global.activeConvId='one';
global.getConvById=id=>({id,title:'Task'});global.loadConversation=()=>{};
let dispose;global.retainedCompositionLifecycle={add:fn=>{dispose=fn;}};
let snapshot={swarms:[],backgroundCommands:[]},calls=0;
global.Api={activity:{background:()=>{calls++;return Promise.resolve(snapshot);}}};
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
eval(fs.readFileSync(process.argv[2],'utf8'));
(async()=>{
  runtimeScope.updateBackgroundActivity();await sleep(70);
  const chip=document.getElementById('bgActivityChip');
  assert.equal(chip.parentElement.id,'convStatusStrip');
  assert.equal(chip.nextElementSibling.id,'turnNav');assert.equal(chip.style.display,'none');
  snapshot={swarms:[{swarm_key:'s',conv_id:'one',created_at:1,agents:[
    {id:'a1',role:'coder',status:'running',objective:'Rewrite <b>parser</b>',round:3},
    {id:'a2',role:'reviewer',status:'pending',waitReason:'capacity'}]}],backgroundCommands:[
    {command_id:'c1',conv_id:'one',status:'running',command:'npm run <b>build</b>',elapsed_s:10}]};
  runtimeScope.updateBackgroundActivity();await sleep(70);
  assert.notEqual(chip.style.display,'none');
  assert.equal(chip.textContent,'1 background command running');
  assert.equal(document.querySelectorAll('.agent-dock-active .agent-dock-agent').length,2);
  chip.click();const pop=document.getElementById('bgActivityPopover');
  assert.ok(pop.textContent.includes('Background commands'));assert.equal(pop.querySelectorAll('.bg-act-agent').length,0);
  let command=pop.querySelector('.bg-act-cmd');assert.equal(command.querySelectorAll('b').length,0);
  assert.ok(command.textContent.includes('<b>build</b>'));
  const agent=document.querySelector('.agent-dock-agent');agent.open=true;agent.dispatchEvent(new window.Event('toggle'));await sleep(0);
  assert.ok(agent.textContent.includes('<b>parser</b>'));assert.equal(agent.querySelectorAll('b').length,0);
  assert.ok(agent.textContent.includes('not yet available'));
  const originalAgent=agent;

  // A real summary click is an outside click for the command popover. Restore
  // it explicitly before asserting unchanged command leaf identity.
  if (!document.getElementById('bgActivityPopover')) chip.click();
  command=document.querySelector('.bg-act-cmd');
  snapshot.backgroundCommands[0].elapsed_s=13;runtimeScope.refreshBackgroundActivity();await sleep(280);
  assert.strictEqual(document.querySelector('.agent-dock-agent'),originalAgent);assert.equal(originalAgent.open,true);
  assert.strictEqual(document.querySelector('.bg-act-cmd'),command);assert.equal(command.querySelector('.bg-act-elapsed').textContent,'13s');
  const before=calls;for(let i=0;i<100;i++)runtimeScope.refreshBackgroundActivity();await sleep(280);
  assert.ok(calls-before<=2,'semantic hints coalesce');
  let resolveOld;Api.activity.background=()=>new Promise(resolve=>{resolveOld=resolve;});
  runtimeScope.updateBackgroundActivity();await sleep(280);
  global.activeConvId='two';runtimeScope.updateBackgroundActivity();
  assert.equal(chip.style.display,'none');assert.equal(document.querySelector('.agent-dock'),null);
  assert.equal(document.getElementById('bgActivityPopover'),null);
  resolveOld(snapshot);await sleep(70);assert.equal(document.querySelector('.agent-dock'),null);
  global.activeConvId='one';runtimeScope.updateBackgroundActivity();assert.equal(document.querySelector('.agent-dock').hidden,false);
  dispose();dispose();assert.equal(document.querySelector('.agent-dock'),null);assert.equal(document.getElementById('bgActivityChip'),null);
  console.log('PASS command visibility, agent separation, text safety, keyed folds, switch and disposal');
})().catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>process.exit(process.exitCode||0));
"""


def _source():
    owners = [
        'background-activity-presentation', 'background-activity-controller',
        'agent-sidebar-dock', 'agent-sidebar-dock-projection',
        'conversation/presentation/swarm-messages-projection',
    ]
    return '\n'.join(open(native_module_path(f'.native/{name}.js', ROOT / f'frontend/src/{name}.ts')).read()
                     for name in owners) + '\n' + runtime_section('background-activity.js', scope_prelude=False)


def _run(source, harness_source=HARNESS):
    with tempfile.TemporaryDirectory(prefix='agent-dock-chip-', dir=ROOT) as directory:
        harness = os.path.join(directory, 'harness.js')
        section = os.path.join(directory, 'section.js')
        with open(harness, 'w') as handle:
            handle.write(harness_source)
        with open(section, 'w') as handle:
            handle.write(source)
        return subprocess.run(['node', harness, section, str(ROOT)], capture_output=True, text=True, timeout=30)


@pytest.mark.skipif(not shutil.which('node') or not (ROOT / 'node_modules/jsdom').is_dir(), reason='node/jsdom unavailable')
def test_command_chip_and_agent_dock_retained_composition():
    result = _run(_source())
    assert result.returncode == 0, result.stderr + result.stdout
    assert 'PASS command visibility' in result.stdout


@pytest.mark.skipif(not shutil.which('node') or not (ROOT / 'node_modules/jsdom').is_dir(), reason='node/jsdom unavailable')
def test_separation_assertion_is_load_bearing():
    source = _source()
    marker = 'swarms: [], agentsActive: 0, agentsRunning: 0, agentsQueued: 0'
    assert marker in source
    result = _run(source.replace(marker, 'swarms: summary.swarms', 1))
    assert result.returncode != 0
    assert 'background command running' in result.stderr


LIFECYCLE_HARNESS = r"""
const fs=require('fs'),path=require('path'),assert=require('assert');
const {JSDOM}=require(path.join(process.argv[3],'node_modules','jsdom'));
const dom=new JSDOM('<body><div id="convStatusStrip"><div id="turnNav"></div></div>');
global.window=dom.window;global.document=window.document;
let hidden=false;Object.defineProperty(document,'hidden',{get:()=>hidden});
Object.defineProperty(document,'readyState',{get:()=> 'loading'});
let nativeHidden=false,onNativeVisibility,dispose;
global.runtimeScope={nativeVisibility:{isHidden:()=>nativeHidden,subscribe:fn=>{onNativeVisibility=fn;return()=>{onNativeVisibility=null;};}}};
global.activeConvId='one';global.retainedCompositionLifecycle={add:fn=>{dispose=fn;}};
let serial=0;const timers=new Map();
global.setTimeout=(fn,ms)=>{timers.set(++serial,{fn,ms});return serial;};
global.clearTimeout=id=>timers.delete(id);
const listeners=new Map(),add=document.addEventListener.bind(document),remove=document.removeEventListener.bind(document);
document.addEventListener=(type,fn,opts)=>{if(!listeners.has(type))listeners.set(type,new Set());listeners.get(type).add(fn);add(type,fn,opts);};
document.removeEventListener=(type,fn,opts)=>{listeners.get(type)?.delete(fn);remove(type,fn,opts);};
let calls=0,resolveFlight,fail=false;
const snapshot={swarms:[{swarm_key:'s',conv_id:'one',created_at:90,agents:[{id:'worker',status:'running'}]}],
  backgroundCommands:[{command_id:'c',conv_id:'one',command:'<script>bad</script>',elapsed_s:10}]};
global.Api={activity:{background:()=>{calls++;if(fail)throw Error('sync failure');return new Promise(resolve=>{resolveFlight=resolve;});}}};
const flush=async()=>{for(let i=0;i<8;i++)await Promise.resolve();};
const tick=async()=>{assert.equal(timers.size,1);const[id,timer]=timers.entries().next().value;timers.delete(id);timer.fn();await flush();};
const settle=async()=>{resolveFlight(snapshot);await flush();};
eval(fs.readFileSync(process.argv[2],'utf8'));
(async()=>{
  runtimeScope.updateBackgroundActivity();document.dispatchEvent(new window.Event('DOMContentLoaded'));
  assert.equal([...timers.values()][0].ms,0);await tick();
  for(let i=0;i<100;i++)runtimeScope.updateBackgroundActivity();
  assert.equal(calls,1);assert.equal(timers.size,0);await settle();
  assert.equal([...timers.values()][0].ms,250);await tick();await settle();assert.equal([...timers.values()][0].ms,3000);
  const chip=document.getElementById('bgActivityChip');chip.click();
  const pop=document.getElementById('bgActivityPopover'),cmd=pop.querySelector('.bg-act-cmd');
  assert.equal(pop.querySelector('.bg-act-swarm'),null);assert.equal(cmd.querySelectorAll('script').length,0);
  runtimeScope.refreshBackgroundActivity();await tick();await settle();assert.strictEqual(document.getElementById('bgActivityPopover'),pop);
  snapshot.backgroundCommands[0].elapsed_s=13;await tick();await settle();
  assert.strictEqual(pop.querySelector('.bg-act-cmd'),cmd);assert.equal(cmd.querySelector('.bg-act-elapsed').textContent,'13s');
  nativeHidden=true;onNativeVisibility();assert.equal(timers.size,0);runtimeScope.refreshBackgroundActivity();assert.equal(timers.size,0);
  nativeHidden=false;onNativeVisibility();assert.equal([...timers.values()][0].ms,0);await tick();await settle();
  hidden=true;document.dispatchEvent(new window.Event('visibilitychange'));assert.equal(timers.size,0);
  runtimeScope.updateBackgroundActivity();assert.equal(timers.size,0);
  hidden=false;document.dispatchEvent(new window.Event('visibilitychange'));assert.equal([...timers.values()][0].ms,0);await tick();
  hidden=true;document.dispatchEvent(new window.Event('visibilitychange'));await settle();assert.equal(timers.size,0);
  hidden=false;document.dispatchEvent(new window.Event('visibilitychange'));fail=true;await tick();assert.equal([...timers.values()][0].ms,10000);
  for(let i=0;i<100;i++)runtimeScope.refreshBackgroundActivity();assert.equal([...timers.values()][0].ms,10000);
  fail=false;await tick();dispose();dispose();assert.equal(onNativeVisibility,null);await settle();
  runtimeScope.updateBackgroundActivity();document.dispatchEvent(new window.Event('visibilitychange'));document.dispatchEvent(new window.Event('DOMContentLoaded'));
  assert.equal(timers.size,0);assert.equal(document.getElementById('bgActivityChip'),null);assert.equal(document.getElementById('bgActivityPopover'),null);
  for(const type of ['click','visibilitychange','DOMContentLoaded'])assert.equal(listeners.get(type)?.size||0,0);
  console.log('PASS lifecycle, elapsed leaves, single-flight and hidden budgets');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""


@pytest.mark.skipif(not shutil.which('node') or not (ROOT / 'node_modules/jsdom').is_dir(), reason='node/jsdom unavailable')
def test_background_activity_elapsed_and_lifecycle_budget():
    result = _run(_source(), LIFECYCLE_HARNESS)
    assert result.returncode == 0, result.stderr + result.stdout
    assert 'PASS lifecycle' in result.stdout
