"""Inspector reads retain their original pane and cannot revive closed resources."""
import json
import subprocess

import pytest

from tests.frontend.test_settings_save_feedback import ROOT, _require_frontend_bundler

pytestmark = pytest.mark.unit


@pytest.fixture(scope='module')
def inspector_bundle(tmp_path_factory):
    _require_frontend_bundler()
    directory = tmp_path_factory.mktemp('inspector-read-lifetime')
    entry, output = directory / 'entry.ts', directory / 'inspector.cjs'
    exports = {
        'createDiagnosticsMessageReconciler': 'diagnostics-message-reconciliation',
        'createDiagnosticsWireController': 'diagnostics-wire-controller',
        'resolveInspectorView': 'inspector-evidence',
        'createInspectorSession': 'inspector-session',
        'createInspectorFeed': 'inspector-feed',
        'renderInspectorLoading, renderInspectorFailure, renderInspectorBack': 'inspector-pane-view',
        'createInspectorPayloads, inspectorTailMessages': 'inspector-payloads',
        'createInspectorDetails': 'inspector-details',
        'createInspectorInline': 'inspector-inline',
        'createInspectorNavigation': 'inspector-navigation',
        'renderInspectorTrace': 'inspector-trace-view',
    }
    entry.write_text('\n'.join(f'export {{ {name} }} from {json.dumps(str(ROOT / "frontend/src/features" / source))};'
                               for name, source in exports.items()))
    result = subprocess.run(['node', 'scripts/vite_test_bundle.mjs', str(entry), '--bundle',
        '--platform=node', '--format=cjs', f'--outfile={output}'], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return output


HARNESS = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),{JSDOM}=require(ROOT+'/node_modules/jsdom');
const dom=new JSDOM('<!doctype html><body><div id="riDrawer"><div class="ri-main"><div class="debug-panel">'
 +'<div id="debugTitle"></div><div id="debugContent"></div></div></div></div><div id="chat"></div></body>',{url:'http://localhost',pretendToBeVisual:true});
for(const name of ['window','document','HTMLElement','AbortController','AbortSignal'])global[name]=name==='window'?dom.window:dom.window[name];
Object.assign(global,require(BUILT));
const tick=async()=>{for(let n=0;n<4;n++)await new Promise(done=>setImmediate(done));};
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return{promise,resolve,reject};};
const requests=[],timers=new Map(),painted=[],traces=[];let timerId=0,subscriber=null;
window.setTimeout=global.setTimeout=(fn,ms)=>{timers.set(++timerId,{fn,ms});return timerId;};
window.clearTimeout=global.clearTimeout=id=>timers.delete(id);
const fire=async ms=>{const row=[...timers].find(([,value])=>value.ms===ms);assert.ok(row,'scheduled '+ms);timers.delete(row[0]);void row[1].fn();await tick();};
const request=(kind,...args)=>{const d=deferred();requests.push({...d,kind,args});return d.promise;};
const of=kind=>requests.filter(row=>row.kind===kind),content=()=>document.getElementById('debugContent');
let status='running';
global.runtimeScope={nativeVisibility:{isHidden:()=>false},
 ConversationTurnRead:{state:()=>({attemptsById:{attempt:{status}},turnsById:{}})},
 ConversationTurnStore:{ensureRuntimeStore:()=>({subscribe(fn){subscriber=fn;return()=>{subscriber=null;};}})}};
global.DebugShellState={activeConversationId:'conversation',visible:false,requests:{}};
global.Api={tasks:{convWire:async()=>({messages:[],sections:[],turns:[]}),
 getTrace:(...args)=>request('trace',...args),getRequestPayload:(...args)=>request('payload',...args)}};
global.t=key=>key;global.Icon=()=>'';global.escapeHtml=value=>String(value??'');global.debugLog=()=>{};
global.reconcileDebugMessages=(node,messages,options)=>node.replaceChildren(...options.leading);
global.renderDebugBlocksInto=(node,messages)=>{node.textContent=messages.map(row=>row.content).join('|');};
global.showMessagesInDebug=messages=>{painted.push(messages);content().textContent=messages.map(row=>row.content).join('|');};
require('node:vm').runInThisContext(fs.readFileSync(SOURCE,'utf8'));
renderInspectorTrace=(node,title,doc)=>{traces.push(doc);node.textContent=doc?.id||'trace unavailable';};
const open=async()=>{openRequestInspector();await tick();};
const payload=content=>({messages:[{role:'tool',content}],label:content});
async function main(){
 if(SCENARIO==='pane_dom_release'){
  // Measure the native owner separately: JSDOM's selector engine retains its
  // last context even without product code (gc-fixture-diagnostic reproduction).
  assert.equal(typeof global.gc,'function');let node=document.createElement('div');
  document.getElementById('chat').appendChild(node);const reference=new WeakRef(node);
  const owner=createInspectorSession({conversation:()=> 'conversation',content:()=>node,hidden:()=>false,live:()=>false,
   load:async()=>{},fingerprint:()=>'',subscribe:()=>null,failed:error=>{throw error;}});
  owner.open('conversation');const pane=owner.selectPane('trace'),held=deferred();
  const operation=held.promise.then(()=>pane.isCurrent());node.remove();node=null;owner.close();
  for(let i=0;i<8;i++){await tick();global.gc();}
  assert.equal(reference.deref(),undefined,'retired pending detail releases its removed content node');
  held.resolve();assert.equal(await operation,false);
 }else if(SCENARIO==='payload_error_policy'){
  const failure=Object.assign(new Error('storage failed'),{status:503});Api.tasks.getRequestPayload=async()=>{throw failure;};
  await assert.rejects(_riPayloads.round('task',1,'turn'),error=>error===failure);
  const kinds=[];Api.tasks.getRequestPayload=async(task,round,turn,kind)=>{kinds.push(kind);if(kind==='state')throw Object.assign(new Error('absent'),{status:404});return payload('request');};
  const view=await _riPayloads.round('task',1,'turn');assert.equal(view.kind,'request');assert.deepEqual(kinds,['state',undefined]);
 }else if(SCENARIO==='payload_live_snapshot_wins'){
  await open();const old=_riPayloads.read('task',1,'turn','state');await tick();
  DebugShellState.requests.task={states:[{...payload('live'),roundNum:1,turn:'turn'}],rounds:{}};
  assert.equal((await _riPayloads.read('task',1,'turn','state')).messages[0].content,'live');
  of('payload')[0].resolve(payload('old'));await old;delete DebugShellState.requests.task;
  assert.equal((await _riPayloads.read('task',1,'turn','state')).messages[0].content,'live');
 }else if(SCENARIO==='cache_fifo_bound'){
  const reads=[];Api.tasks.getRequestPayload=async(task)=>{reads.push(task);return payload(task);};
  for(let i=0;i<41;i++)await _riPayloads.read('task'+i,1,'turn','state');
  await _riPayloads.read('task1',1,'turn','state');assert.equal(reads.length,41);
  await _riPayloads.read('task0',1,'turn','state');assert.equal(reads.length,42,'existing 40-entry FIFO retention remains in force');
 }else if(SCENARIO==='trace_node_replaced'){
  await open();const operation=_riOpenTrace('task');await tick();const newer=document.createElement('div');newer.id='debugContent';newer.textContent='replacement';
  content().replaceWith(newer);of('trace')[0].resolve({id:'old'});await operation;assert.equal(newer.textContent,'replacement');
 }else if(SCENARIO==='poll_hidden'){
  await open();runtimeScope.nativeVisibility.isHidden=()=>true;Api.tasks.convWire=(...args)=>request('wire',...args);
  await fire(15000);assert.equal(of('wire').length,0);assert.equal([...timers.values()].filter(row=>row.ms===15000).length,1);
 }else if(SCENARIO==='poll_reopen'){
  await open();Api.tasks.convWire=(...args)=>request('wire',...args);await fire(15000);closeRequestInspector();
  Api.tasks.convWire=async()=>({messages:[],sections:[],turns:[]});await open();const scheduled=[...timers.keys()];
  of('wire')[0].resolve({messages:[],sections:[],turns:[]});await tick();assert.deepEqual([...timers.keys()],scheduled,'old completion cannot replace the new opening poll');
 }else if(SCENARIO==='detached_loading_back'){
  await open();const operation=openToolDebugPanel('task',1);await tick();
  const back=content().querySelector('.ri-feed-back button');assert.ok(back,'a pending detail retains navigation to the feed');
  back.click();of('payload')[0].resolve(payload('late'));await operation;assert.match(content().textContent,/ri.empty/);
 }else if(SCENARIO==='trace_repeat'||SCENARIO==='trace_reopen'){
  await open();const one=_riOpenTrace('same-task');await tick();
  if(SCENARIO==='trace_reopen'){closeRequestInspector();await open();}
  const two=_riOpenTrace('same-task');await tick();
  of('trace')[1].resolve({id:'new'});await two;of('trace')[0].resolve({id:'old'});await one;
  assert.equal(content().textContent.includes('old'),false,'an older trace cannot replace the later view of the same task');
  assert.deepEqual(traces.map(row=>row.id),['new']);
 }else if(SCENARIO==='trace_closed'){
  Api.tasks.getTrace=async()=>{requests.push({kind:'trace'});return null;};
  await _riOpenTrace('task');assert.equal(of('trace').length,0,'a closed owner starts no trace read');
 }else if(SCENARIO==='trace_late_error'){
  await open();const one=_riOpenTrace('task');let error;const observed=one.catch(value=>{error=value;});
  await tick();closeRequestInspector();of('trace')[0].reject(new Error('retired read'));await observed;
  assert.equal(error,undefined,'a retired read failure cannot escape an event callback');assert.deepEqual(traces,[]);
 }else if(SCENARIO==='detached_order'||SCENARIO==='detached_reopen'){
  await open();const one=openToolDebugPanel('first',1);await tick();
  if(SCENARIO==='detached_reopen'){closeRequestInspector();await open();}
  const two=openToolDebugPanel('second',1);await tick();
  of('payload')[1].resolve(payload('new'));await two;of('payload')[0].resolve(payload('old'));await one;
  assert.deepEqual(painted.map(rows=>rows[0].content),['new'],'only the current detached target may publish');
 }else if(SCENARIO==='detached_then_trace'||SCENARIO==='detached_error_after_trace'){
  await open();const one=openToolDebugPanel('round-task',1);await tick();const two=_riOpenTrace('trace-task');await tick();
  of('trace')[0].resolve({id:'trace-current'});await two;
  if(SCENARIO==='detached_then_trace')of('payload')[0].resolve(payload('old-round'));
  else of('payload')[0].reject(new Error('old round failed'));
  await one;assert.match(content().textContent,/trace-current/,'round completion cannot replace the selected trace');
 }else if(SCENARIO==='detached_back'){
  await open();const initial=openToolDebugPanel('initial',1);await tick();of('payload')[0].resolve(payload('initial'));await initial;
  const pending=openToolDebugPanel('later',1);await tick();
  content().querySelector('.ri-feed-back button').click();of('payload')[1].resolve(payload('late'));await pending;
  assert.match(content().textContent,/ri.empty/,'returning to the feed retires a pending detail');
 }else if(SCENARIO==='poll_close'||SCENARIO==='store_close'){
  await open();Api.tasks.convWire=(...args)=>request('wire',...args);
  if(SCENARIO==='poll_close')await fire(15000);
  else {status='done';subscriber();await fire(800);}
  closeRequestInspector();of('wire')[0].resolve({messages:[],sections:[],turns:[]});await tick();
  assert.equal(timers.size,0,'a completed retired read cannot schedule another poll');assert.equal(subscriber,null);
 }else if(SCENARIO==='payload_late_cache'){
  await open();const row={taskId:'task',live:true,status:'running'};Api.tasks.convWire=async()=>({messages:[],sections:[],turns:[row]});await riRefreshTasks();
  const one=_riPayloads.read('task',1,'turn','state'),two=_riPayloads.read('task',1,'turn','state');await tick();
  assert.equal(of('payload').length,2);
  row.live=false;row.status='done';await riRefreshTasks();
  of('payload')[1].resolve(payload('new'));await two;of('payload')[0].resolve(payload('old'));await one;
  const cached=await _riPayloads.read('task',1,'turn','state');assert.equal(cached.messages[0].content,'new','older live read cannot poison the settled cache');
 }else if(SCENARIO==='fold_cache_parity'){
  await open();Api.tasks.getRequestPayload=async(task,round,turn,kind,fold)=>({messages:[{role:'user',content:'base'},...round===2?[{role:'tool',content:'new'}]:[]]});
  await _riPayloads.read('task',2,'turn','state');const view=await _riPayloads.round('task',2,'turn');
  const scoped=await _riPayloads.increment('task',2,view.kind,view.payload.messages,'turn');
  assert.deepEqual(scoped,[{role:'tool',content:'new'}],'a full cached payload has an existing exact-diff fallback');
 }else if(SCENARIO==='inline_closed'){
  const slot=document.createElement('div');slot.dataset.prn='1';document.getElementById('chat').appendChild(slot);
  const anchor=document.createElement('button');anchor.dataset.riState='task:1';anchor.dataset.riTurn='turn';slot.append(anchor);
  const operation=openToolDebugPanel('task',1,anchor);await tick();document.querySelector('.ri-state-panel-close').click();
  of('payload')[0].resolve(payload('late'));await operation;assert.equal(document.querySelector('.ri-state-panel'),null);
 }else if(SCENARIO==='live_snapshot'){
  DebugShellState.requests.task={rounds:{'turn|1':payload('request')},states:[{...payload('state'),roundNum:1,turn:'turn'}]};
  assert.equal((await _riPayloads.read('task',1,'turn','state')).messages[0].content,'state');
  assert.equal((await _riPayloads.read('task',1,'turn','request')).messages[0].content,'request');assert.equal(requests.length,0);
 }else throw new Error('Unknown scenario '+SCENARIO);
 closeRequestInspector();dom.window.close();console.log('Inspector scenario completed');
}
main().catch(error=>{console.error(error);process.exitCode=1;dom.window.close();});
'''


@pytest.mark.parametrize('scenario', [
    'trace_repeat', 'trace_reopen', 'trace_closed', 'trace_late_error',
    'detached_order', 'detached_reopen', 'detached_then_trace', 'detached_error_after_trace',
    'detached_back', 'poll_close', 'store_close', 'payload_late_cache',
    'fold_cache_parity', 'inline_closed', 'live_snapshot',
    'payload_live_snapshot_wins', 'cache_fifo_bound', 'trace_node_replaced',
    'poll_hidden', 'poll_reopen', 'detached_loading_back',
    'payload_error_policy', 'pane_dom_release',
])
def test_inspector_read_lifetime(inspector_bundle, scenario):
    bindings = {'ROOT': str(ROOT), 'BUILT': str(inspector_bundle), 'SCENARIO': scenario,
                'SOURCE': str(ROOT / 'frontend/src/runtime/sections/core/request_inspector.js')}
    script = '\n'.join(f'const {key}={json.dumps(value)};' for key, value in bindings.items()) + HARNESS
    result = subprocess.run(['node', '--expose-gc', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    assert 'Inspector scenario completed' in result.stdout, 'Node exited with the scenario still unsettled'
