"""Trace presentation preserves complete server evidence and separate browser clocks."""
import json
import subprocess

import pytest

from tests.frontend.test_inspector_read_lifecycle import inspector_bundle as inspector_bundle
from tests.frontend.test_settings_save_feedback import ROOT

pytestmark = pytest.mark.unit

HARNESS = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),{JSDOM}=require(ROOT+'/node_modules/jsdom');
const dom=new JSDOM('<!doctype html><body><div id="riDrawer"><div class="ri-main"><div id="debugTitle"></div><div id="debugContent"></div></div></div></body>',{url:'http://localhost',pretendToBeVisual:true});
for(const name of ['window','document','HTMLElement','AbortController','AbortSignal'])global[name]=name==='window'?dom.window:dom.window[name];
Object.assign(global,require(BUILT));
global.DebugShellState={activeConversationId:'conversation',visible:false,requests:{}};
global.runtimeScope={nativeVisibility:{isHidden:()=>false}};
const tick=async()=>{for(let i=0;i<3;i++)await new Promise(done=>setImmediate(done));};
global.t=(key,args)=>key+(args?' '+JSON.stringify(args):'');
global.Icon=()=>'';global.debugLog=()=>{};
global.escapeHtml=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
global.reconcileDebugMessages=(node,messages,options)=>node.replaceChildren(...options.leading);
const span=(id,depth=0,kind='llm')=>({spanId:id,parent:null,name:id,depth,kind,tStart:0,tEnd:1000,status:'done',attrs:{}});
let trace={version:1,taskId:'task',eventsAvailable:true,tStart:0,totalMs:1000,
 summary:{totalMs:1000,llmMs:500,toolMs:200,waitMs:100,compactionMs:0,approvalWaitMs:0,unattributedMs:200,ttftMs:120,overBudget:[]},
 spans:[span('turn',0,'turn'),span('logical',3),span('provider-attempt',4)],gaps:[{tStart:800,tEnd:1000}],
 statusHistory:[],clientObservations:[]};
const content=()=>document.getElementById('debugContent');
if(SCENARIO==='hostile_kind')trace.spans=[span('quoted kind',0,'llm" data-canary="unsafe')];
if(SCENARIO==='prototype_kind')trace.spans=[span('unmapped',0,'__proto__')];
if(SCENARIO==='clip_interval'){trace.tStart=1000;trace.spans=[{...span('partially visible'),tStart:900,tEnd:1400}];trace.gaps=[];}
if(SCENARIO==='zero_status')trace.statusHistory=[{id:'status',phase:'working',detail:'zero-origin phase',attention:'progress',tStart:0,tEnd:500,lastObservedAt:500,count:2}];
if(SCENARIO==='zero_total'){trace.totalMs=0;trace.summary.totalMs=0;trace.spans=[];trace.gaps=[];}
if(SCENARIO==='receipt_only')trace={...trace,source:'attempt-receipts',tStart:undefined,totalMs:undefined,summary:undefined,spans:[],gaps:[],
 statusHistory:[{id:'status',phase:'observed phase',tStart:10,tEnd:30,lastObservedAt:30,count:1,attention:'progress'}],
 clientObservations:[{kind:'terminal_painted',renderMs:25,transportMs:5}]};
if(SCENARIO==='invalid_clock')trace={...trace,tStart:'invalid clock'};
if(SCENARIO==='missing_availability')trace={version:1,taskId:'task'};
if(SCENARIO==='failed')trace=null;
if(SCENARIO==='expired')trace={version:1,taskId:'task',eventsAvailable:false};
if(SCENARIO==='minute_rollover')trace.totalMs=119999;
if(SCENARIO==='notes')trace={...trace,running:true,source:'turn-snapshot',coverage:'partial',coverageReason:'event-read-failed',
 compacted:true,droppedSpans:3,statusDroppedCount:2,clientObservations:[{kind:'phase_painted',renderMs:null,clientDroppedBefore:4},{kind:'transport_recovered',durationMs:60,clientDroppedBefore:7}]};
if(SCENARIO==='sparse_depth')trace.spans=[span('root',0),span('deep',1000000)];
if(SCENARIO==='flags')trace.spans=[{...span('<exact name>',2,'tool'),status:'error',overBudget:true,truncated:true,budgetMs:200,attrs:{query:'<query>',model:'model'}},
 {...span('running',3,'llm'),status:'running',tEnd:null}];
const before=JSON.stringify(trace);
global.Api={tasks:{convWire:async()=>({messages:[],sections:[],turns:[]}),getTrace:async()=>trace}};
require('node:vm').runInThisContext(fs.readFileSync(SOURCE,'utf8'));
async function main(){
 openRequestInspector();await tick();await _riOpenTrace('task');
 assert.equal(JSON.stringify(trace),before,'presentation does not rewrite server evidence');
 assert.ok(content().querySelector('.ri-feed-back button'),'all trace states retain back navigation');
 const text=content().textContent;
 if(SCENARIO==='deep_spans'){
  assert.equal(content().querySelectorAll('.tr-bar:not(.tr-k-gap)').length,3,'every retained provider-attempt span is visible');
  assert.ok([...content().querySelectorAll('.tr-bar')].some(bar=>bar.title.includes('provider-attempt')));
 }else if(SCENARIO==='hostile_kind'){
  assert.equal(content().querySelector('[data-canary]'),null,'kind is data, never HTML attribute syntax');
  assert.match(text,/quoted kind/);
 }else if(SCENARIO==='prototype_kind'){
  assert.match(content().querySelector('.tr-bar').title,/__proto__/,'unknown kinds do not consult inherited object properties');
 }else if(SCENARIO==='clip_interval'){
  const bar=content().querySelector('.tr-bar');assert.equal(parseFloat(bar.style.left),0);assert.equal(parseFloat(bar.style.width),40);
  assert.match(bar.title,/500ms/,'clipping presentation geometry keeps the original measured interval');
 }else if(SCENARIO==='zero_status'){
  const note=[...content().querySelectorAll('.tr-note')].find(node=>node.textContent.includes('ri.traceStatusHistory'));
  assert.match(note.textContent,/zero-origin phase 500ms/,'zero is a valid clock origin');
  assert.match(note.textContent,/ri.traceStatusRepeated/);
 }else if(SCENARIO==='zero_total'){
  assert.deepEqual([...content().querySelectorAll('.tr-axis span')].map(node=>node.textContent),['0','0ms','0ms']);
 }else if(SCENARIO==='receipt_only'){
  assert.match(text,/ri.traceReceiptsOnly/);assert.match(text,/observed phase/);assert.match(text,/ri.traceTerminalPainted/);
  assert.equal(content().querySelectorAll('.tr-flame').length,0,'browser/status receipts cannot invent a server axis');
 }else if(SCENARIO==='invalid_clock'){
  assert.equal(content().querySelectorAll('.tr-flame').length,0);assert.match(text,/clock-unavailable/);
 }else if(SCENARIO==='missing_availability'||SCENARIO==='failed')assert.match(text,/ri.loadFailed/);
 else if(SCENARIO==='expired'){assert.match(text,/ri.expiredHint/);assert.equal(content().querySelector('.tr-flame'),null);}
 else if(SCENARIO==='minute_rollover')assert.match(content().querySelector('.tr-c-total').textContent,/2m00s/);
 else if(SCENARIO==='notes'){
  for(const value of ['ri.traceLive','ri.traceDurable','event-read-failed','ri.traceCompacted {"n":5}','ri.traceClientDropped {"n":7}','ri.tracePaintRestored','ri.traceTransportRecovered'])assert.ok(text.includes(value),value);
 }else if(SCENARIO==='sparse_depth'){
  assert.equal(content().querySelectorAll('.tr-bar:not(.tr-k-gap)').length,2);
  assert.ok(content().querySelectorAll('.tr-row').length<=4,'sparse depths do not allocate empty rows');
 }else if(SCENARIO==='flags'){
  const bar=content().querySelector('.tr-err');assert.ok(bar.classList.contains('tr-over')&&bar.classList.contains('tr-trunc'));
  assert.match(bar.title,/<query>/);assert.match(bar.title,/ri.trTipOverBudget/);assert.ok(content().querySelector('.tr-live'));
 }else throw new Error('unknown scenario '+SCENARIO);
}
main().then(()=>console.log('Inspector trace completed')).catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>{closeRequestInspector();dom.window.close();});
'''


@pytest.mark.parametrize('scenario', [
    'deep_spans', 'hostile_kind', 'zero_status', 'zero_total', 'receipt_only',
    'invalid_clock', 'missing_availability', 'failed', 'expired',
    'minute_rollover', 'notes', 'sparse_depth', 'flags',
    'prototype_kind', 'clip_interval',
])
def test_inspector_trace_evidence(inspector_bundle, scenario):
    bindings = {'ROOT': str(ROOT), 'BUILT': str(inspector_bundle), 'SCENARIO': scenario,
                'SOURCE': str(ROOT / 'frontend/src/runtime/sections/core/request_inspector.js')}
    script = '\n'.join(f'const {key}={json.dumps(value)};' for key, value in bindings.items()) + HARNESS
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    assert 'Inspector trace completed' in result.stdout, 'Node exited before trace completion'
