"""Inline evidence keeps its exact tool address and original mounted view."""
import json
import subprocess

import pytest

from tests.frontend.test_inspector_read_lifecycle import inspector_bundle as inspector_bundle  # noqa: F401
from tests.frontend.test_settings_save_feedback import ROOT

pytestmark = pytest.mark.unit

HARNESS = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),{JSDOM}=require(ROOT+'/node_modules/jsdom');
const dom=new JSDOM('<!doctype html><body><div id="riDrawer"><div class="ri-main"><div class="debug-panel"><div id="debugTitle"></div><div id="debugContent"></div></div></div></div><div id="chat"></div></body>',{url:'http://localhost',pretendToBeVisual:true});
for(const name of ['window','document','HTMLElement','AbortController','AbortSignal'])global[name]=name==='window'?dom.window:dom.window[name];
Object.assign(global,require(BUILT));
const tick=async()=>{for(let n=0;n<4;n++)await new Promise(done=>setImmediate(done));};
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return{promise,resolve,reject};};
const calls=[],painted=[],expanded=[];
global.runtimeScope={nativeVisibility:{isHidden:()=>false}};
global.DebugShellState={activeConversationId:'conversation',visible:false,requests:{}};
global.t=key=>key;global.Icon=()=>'<svg></svg>';global.escapeHtml=value=>String(value??'');global.debugLog=()=>{};
global.reconcileDebugMessages=(node,messages,options)=>node.replaceChildren(...options.leading);
global.renderDebugBlocksInto=(node,messages)=>{painted.push(messages);node.replaceChildren(...messages.map(message=>{const block=document.createElement('div');block.className='debug-msg-block';block.textContent=message.content;return block;}));};
global._debugMsgChars=message=>String(message.content||'').length;
global._debugOpenBlock=node=>{expanded.push(node);node.classList.add('open');};
global.showMessagesInDebug=messages=>{document.getElementById('debugContent').textContent=messages.map(row=>row.content).join('|');};
const payload=(label='evidence',extra={})=>({messages:[{role:'system',content:'history'},{role:'tool',content:label}],foldPrefix:1,...extra});
let read=async()=>payload();
global.Api={tasks:{convWire:async()=>({messages:[],sections:[],turns:[]}),getRequestPayload:(...args)=>{calls.push(args);return read(...args);}}};
require('node:vm').runInThisContext(fs.readFileSync(SOURCE,'utf8'));
function slot(task='task',round=1,turn='phase-a'){
 const node=document.createElement('div');node.dataset.prn=String(round);const anchor=document.createElement('button');
 anchor.dataset.riState=task+':'+round;anchor.dataset.riTurn=turn;anchor.textContent='Inspect';node.append(anchor);document.getElementById('chat').append(node);return{node,anchor};
}
const panel=()=>document.querySelector('.ri-state-panel');
const start=(task,round,anchor)=>{const operation=openToolDebugPanel(task,round,anchor);operation.catch(()=>{});return operation;};
async function main(){
 if(SCENARIO==='quoted_address'){
  const task='task"#agent:quoted';const target=slot(task);await start(task,1);await tick();
  assert.equal(target.node.nextElementSibling,panel());assert.equal(calls[0][0],task);
 }else if(SCENARIO==='lookup_turn'){
  slot('task',1,'worker:exact');await start('task',1);await tick();assert.equal(calls[0][2],'worker:exact','lookup must retain the matched turn');
 }else if(SCENARIO==='detached_anchor'){
  const retired=slot();retired.node.remove();const current=slot();await start('task',1,retired.anchor);await tick();
  assert.ok(panel()?.isConnected);assert.equal(current.node.nextElementSibling,panel());
 }else if(SCENARIO==='ambiguous_lookup'){
  slot('task',1,'planner');slot('task',1,'worker');await start('task',1);await tick();
  assert.equal(panel(),null,'ambiguous phase address must use canonical detached detail, not the first DOM row');assert.ok(document.querySelector('#debugContent .ri-feed-back'));
 }else if(SCENARIO==='mismatched_anchor'){
  const wrong=slot('other'),right=slot();await start('task',1,wrong.anchor);await tick();assert.equal(right.node.nextElementSibling,panel());
 }else if(SCENARIO==='positional_anchor'){
  const target=slot();target.anchor.removeAttribute('data-ri-state');target.anchor.dataset.riTurn='worker:explicit';
  await start('task',2,target.anchor);await tick();assert.equal(target.node.nextElementSibling,panel());assert.equal(calls[0][2],'worker:explicit');assert.equal(calls[0][1],2);
 }else if(SCENARIO==='exact_detached_turn'){
  const retired=slot('task',1,'worker');retired.node.remove();slot('task',1,'planner');const right=slot('task',1,'worker');
  await start('task',1,retired.anchor);await tick();assert.equal(right.node.nextElementSibling,panel());assert.equal(calls[0][2],'worker');
 }else if(SCENARIO==='same_round_tools'){
  const first=slot(),second=slot();await start('task',1,first.anchor);await tick();await start('task',1,second.anchor);await tick();
  assert.equal(panel(),null,'same request has one shared inline view even across different tool entries');assert.equal(calls.length,1);
 }else if(SCENARIO==='close_focus'){
  const target=slot();await start('task',1,target.anchor);await tick();const close=panel().querySelector('.ri-state-panel-close');close.focus();close.click();
  assert.equal(panel(),null);assert.equal(document.activeElement,target.anchor,'close restores focus to the producing tool entry');
 }else if(SCENARIO==='retry_feedback'){
  const target=slot(),held=deferred();let reads=0;read=()=>++reads===1?Promise.reject(new Error('temporary')):held.promise;
  await start('task',1,target.anchor);await tick();panel().querySelector('.ri-retry').click();await tick();
  assert.match(panel().textContent,/ri.loading/);assert.equal(panel().querySelector('.ri-retry'),null);
  held.resolve(payload('recovered'));await tick();assert.equal(painted.at(-1)[0].content,'recovered');
 }else if(SCENARIO==='retired_retry'){
  const target=slot(),held=deferred();let reads=0;read=()=>++reads===1?Promise.reject(new Error('temporary')):held.promise;
  await start('task',1,target.anchor);await tick();const retry=panel().querySelector('.ri-retry');retry.click();await tick();retry.click();await tick();
  assert.equal(reads,2,'retired retry control cannot start another read');panel().querySelector('.ri-state-panel-close').click();retry.click();assert.equal(reads,2);
  held.resolve(payload('retired'));await tick();assert.equal(painted.length,0);
 }else if(SCENARIO==='retired_close'){
  const first=slot(),second=slot('another');await start('task',1,first.anchor);await tick();const close=panel().querySelector('.ri-state-panel-close');
  await start('another',1,second.anchor);await tick();close.click();assert.equal(panel().dataset.riTask,'another');
 }else if(SCENARIO==='empty_evidence'){
  const target=slot();read=async()=>null;await start('task',1,target.anchor);await tick();
  assert.match(panel().textContent,/ri.stateEmpty/);assert.equal(panel().getAttribute('aria-busy'),'false');assert.equal(painted.length,0);
 }else if(SCENARIO==='late_read_error'){
  const first=slot(),second=slot('another'),held=deferred();read=task=>task==='task'?held.promise:Promise.resolve(payload('current'));
  const old=start('task',1,first.anchor);await tick();await start('another',1,second.anchor);await tick();held.reject(new Error('old failure'));await old;await tick();
  assert.equal(panel().dataset.riTask,'another');assert.equal(panel().querySelector('.ri-retry'),null);assert.deepEqual(painted.map(rows=>rows[0].content),['current']);
 }else if(SCENARIO==='reopen_race'){
  const target=slot(),old=deferred(),current=deferred();let reads=0;read=()=>++reads===1?old.promise:current.promise;
  const first=start('task',1,target.anchor);await tick();panel().querySelector('.ri-state-panel-close').click();
  const second=start('task',1,target.anchor);await tick();current.resolve(payload('current'));await second;await tick();old.resolve(payload('old'));await first;await tick();
  assert.deepEqual(painted.map(rows=>rows[0].content),['current']);
 }else if(SCENARIO==='closed_before_increment'){
  const target=slot('task',2),held=deferred();read=()=>held.promise;const operation=start('task',2,target.anchor);await tick();panel().querySelector('.ri-state-panel-close').click();
  held.resolve(payload('retired',{foldPrefix:undefined}));await operation;await tick();assert.equal(calls.length,1);assert.equal(painted.length,0);
 }else if(SCENARIO==='late_increment'){
  const target=slot('task',2),held=deferred();read=(_task,round)=>round===2?Promise.resolve(payload('retired',{foldPrefix:undefined})):held.promise;
  const operation=start('task',2,target.anchor);await tick();assert.equal(calls.length,2);panel().querySelector('.ri-state-panel-close').click();held.resolve(payload('previous'));await operation;await tick();
  assert.equal(panel(),null);assert.equal(painted.length,0);
 }else if(SCENARIO==='diff_failure_tail'){
  const target=slot('task',2);read=(_task,round)=>round===2?Promise.resolve(payload('result',{foldPrefix:undefined,messages:[{role:'system',content:'history'},{role:'assistant',content:'call',tool_calls:[{id:'tool'}]},{role:'tool',content:'result'}]})):Promise.reject(new Error('optional base unavailable'));
  await start('task',2,target.anchor);await tick();assert.deepEqual(painted[0].map(row=>row.content),['call','result']);
 }else if(SCENARIO==='small_expand'||SCENARIO==='count_limit'||SCENARIO==='bytes_limit'){
  const target=slot(),count=SCENARIO==='count_limit'?7:6,length=SCENARIO==='bytes_limit'?307201:2;
  read=async()=>payload('',{messages:[{role:'system',content:'history'},...Array.from({length:count},(_,i)=>({role:'tool',content:i?'ok':'x'.repeat(length)}))]});
  await start('task',1,target.anchor);await tick();assert.equal(painted[0].length,count);assert.equal(expanded.length,SCENARIO==='small_expand'?6:0);
 }else if(SCENARIO==='folded_increment'){
  const target=slot('task',3);await start('task',3,target.anchor);await tick();assert.equal(calls.length,1);assert.deepEqual(painted[0].map(row=>row.content),['evidence']);
 }else if(SCENARIO==='request_fallback'){
  const target=slot();read=async(_task,_round,_turn,kind)=>{if(kind==='state')throw Object.assign(new Error('missing'),{status:404});return payload('request',{foldPrefix:undefined});};
  await start('task',1,target.anchor);await tick();assert.equal(panel().dataset.riKind,'request');assert.deepEqual(painted[0].map(row=>row.content),['history','request']);
 }else throw new Error('Unknown scenario '+SCENARIO);
}
main().then(()=>console.log('Inspector inline completed')).catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>{closeRequestInspector();dom.window.close();});
'''


@pytest.mark.parametrize('scenario', [
    'quoted_address', 'lookup_turn', 'detached_anchor', 'ambiguous_lookup',
    'mismatched_anchor', 'positional_anchor', 'exact_detached_turn', 'same_round_tools', 'close_focus',
    'retry_feedback', 'retired_retry', 'retired_close', 'empty_evidence', 'late_read_error', 'reopen_race',
    'closed_before_increment', 'late_increment', 'diff_failure_tail',
    'small_expand', 'count_limit', 'bytes_limit', 'folded_increment', 'request_fallback',
])
def test_inspector_inline_evidence(inspector_bundle, scenario):
    setup = 'const ROOT=%s, BUILT=%s, SOURCE=%s, SCENARIO=%s;\n' % tuple(map(json.dumps, [
        str(ROOT), str(inspector_bundle), str(ROOT / 'frontend/src/runtime/sections/core/request_inspector.js'), scenario]))
    result = subprocess.run(['node', '-e', setup + HARNESS], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Inspector inline completed' in result.stdout, 'Node exited before inline completion'
