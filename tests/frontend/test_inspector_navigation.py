"""Inspector navigation follows Turn identity and owns only its visible resources."""
import json
import subprocess

import pytest

from tests.frontend.test_inspector_read_lifecycle import inspector_bundle as inspector_bundle
from tests.frontend.test_settings_save_feedback import ROOT

pytestmark = pytest.mark.unit

HARNESS = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),{JSDOM}=require(ROOT+'/node_modules/jsdom');
const dom=new JSDOM('<!doctype html><body><div id="riDrawer"><div id="riTurnNav" hidden>'
 +'<button id="riTurnPrev"></button><button id="riTurnPos"><span id="riTurnReadout"></span></button>'
 +'<button id="riTurnNext"></button></div><div id="riTurnJump" hidden></div>'
 +'<div class="ri-main"><div id="debugTitle"></div><div id="debugContent"></div></div></div>'
 +'<button id="outside">outside</button></body>',{url:'http://localhost',pretendToBeVisual:true});
for(const name of ['window','document','HTMLElement','AbortController','AbortSignal'])global[name]=name==='window'?dom.window:dom.window[name];
Object.assign(global,require(BUILT));
const tick=async()=>{for(let n=0;n<4;n++)await new Promise(done=>setImmediate(done));};
const timers=new Map();let timerId=0,scrollTarget='',orderedReads=0;
window.setTimeout=global.setTimeout=(fn,ms)=>{timers.set(++timerId,{fn,ms});return timerId;};
window.clearTimeout=global.clearTimeout=id=>timers.delete(id);
const fire=async ms=>{const row=[...timers].find(([,value])=>value.ms===ms);assert.ok(row,'scheduled '+ms);timers.delete(row[0]);row[1].fn();await tick();};
const el=id=>document.getElementById(id),scroller=()=>document.querySelector('.ri-main');
let ids=['one','two','three'];
const wire=()=>({messages:ids.map(id=>({role:'assistant',content:id})),
 sections:ids.map((id,i)=>({type:'turn',msgIndex:i,turnId:id,taskId:'task-'+id,createdAt:i+1})),
 turns:ids.map((id,i)=>({turnId:id,taskId:'task-'+id,preview:'preview '+id,createdAt:i+1}))});
global.DebugShellState={activeConversationId:'conversation',visible:false,requests:{}};
global.runtimeScope={nativeVisibility:{isHidden:()=>false},ConversationTurnRead:{state:()=>null,
 ordered:()=>{orderedReads++;return ids.map(turnId=>({turnId,actor:'assistant'}));}}};
global.Api={tasks:{convWire:async()=>wire(),getTrace:async()=>null}};
global.t=(key,params)=>key+(params?.n?':'+params.n:'');global.Icon=()=>'';
global.escapeHtml=value=>String(value??'');global.debugLog=()=>{};
global.reconcileDebugMessages=(node,messages,options)=>{
 node.replaceChildren(...options.leading,...[...(options.beforeMessage?.values()||[])].flat());
 for(const [i,divider] of [...node.querySelectorAll('.ri-feed-turn')].entries()){
  Object.defineProperty(divider,'offsetTop',{get:()=>i*100});
  divider.getBoundingClientRect=()=>({top:50+i*100-scroller().scrollTop});
  divider.scrollIntoView=()=>{scrollTarget=divider.dataset.turnId;};
 }
};
Object.defineProperty(scroller(),'clientHeight',{value:500});
scroller().getBoundingClientRect=()=>({top:50,height:500});
require('node:vm').runInThisContext(fs.readFileSync(SOURCE,'utf8'));
const open=async()=>{openRequestInspector();await tick();};
const refresh=async()=>{riRefreshTasks();await tick();};
const rows=()=>[...el('riTurnJump').querySelectorAll('.ri-turn-jump-row')];
async function main(){
 await open();
 if(SCENARIO==='prepend_identity'){
  el('riTurnPrev').click();assert.equal(scrollTarget,'two');ids.unshift('older');await refresh();
  assert.equal(el('riTurnReadout').textContent,'ri.turnChip:3','history insertion preserves the selected Turn');
  el('riTurnNext').click();assert.equal(scrollTarget,'three');
 }else if(SCENARIO==='reopen_newest'){
  el('riTurnPrev').click();el('riTurnPrev').click();closeRequestInspector();await open();
  assert.equal(el('riTurnReadout').textContent,'ri.turnChip:3','a new opening starts on the newest Turn');
 }else if(SCENARIO==='scroll_close'){
  scroller().dispatchEvent(new window.Event('scroll'));assert.equal([...timers.values()].filter(t=>t.ms===120).length,1);
  closeRequestInspector();assert.equal(timers.size,0,'closing cancels pending navigation and polling');
  scroller().dispatchEvent(new window.Event('scroll'));assert.equal(timers.size,0,'closed navigation has no scroll listener');
 }else if(SCENARIO==='flash_close'){
  el('riTurnPrev').click();const divider=el('debugContent').querySelector('[data-turn-id="two"]');
  assert.equal(divider.classList.contains('ri-flash'),true);closeRequestInspector();
  assert.equal(timers.size,0,'closing cancels highlight retention');assert.equal(divider.classList.contains('ri-flash'),false);
 }else if(SCENARIO==='refresh_focus'){
  el('riTurnPos').click();const row=rows()[1];row.focus();await refresh();
  assert.equal(document.activeElement,row,'an unchanged jump-list row keeps keyboard focus');
  assert.equal(rows()[1],row);row.click();assert.equal(scrollTarget,'two');
 }else if(SCENARIO==='keyboard_row'){
  el('riTurnPos').click();const row=rows()[1];row.focus();row.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
  assert.equal(scrollTarget,'two','the keyboard activates the same feed jump as a pointer');
 }else if(SCENARIO==='scroll_work'){
  ids=Array.from({length:120},(_,i)=>'turn-'+i);await refresh();let queries=0;
  const content=el('debugContent'),query=content.querySelectorAll.bind(content);
  content.querySelectorAll=(...args)=>{queries++;return query(...args);};
  scroller().scrollTop=5100;scroller().dispatchEvent(new window.Event('scroll'));await fire(120);
  assert.equal(el('riTurnReadout').textContent,'ri.turnChip:53');
  console.log(JSON.stringify({turns:ids.length,dividerQueriesPerScroll:queries}));
  assert.ok(queries<=1,'one scroll must not query the full divider list for every Turn');
 }else if(SCENARIO==='ordinal_work'){
  orderedReads=0;await refresh();console.log(JSON.stringify({orderedReads}));
  assert.equal(orderedReads,1,'one canonical ordinal projection serves both the bar and all feed dividers');
 }else if(SCENARIO==='trace_suspends'){
  el('riTurnPos').click();scroller().dispatchEvent(new window.Event('scroll'));
  el('debugContent').querySelector('.ri-feed-trace').click();await tick();
  assert.equal(el('riTurnNav').hidden,true,'a detail pane cannot offer feed-only navigation');
  assert.equal(el('riTurnJump').hidden,true);assert.equal([...timers.values()].some(t=>t.ms===120),false);
  el('debugContent').querySelector('.ri-feed-back button').click();assert.equal(el('riTurnNav').hidden,false);
 }else if(SCENARIO==='removed_focus'){
  el('riTurnPrev').click();el('riTurnPos').click();rows()[1].focus();ids=['one','three'];await refresh();
  assert.equal(document.activeElement,rows()[0],'removal returns focus to the surviving selected Turn');
  assert.equal(rows()[0].getAttribute('aria-current'),'true');
 }else if(SCENARIO==='replaced_scroller'){
  const previous=scroller();previous.dispatchEvent(new window.Event('scroll'));
  previous.replaceWith(previous.cloneNode(true));await refresh();
  assert.equal([...timers.values()].some(t=>t.ms===120),false,'replacing the feed retires its old scheduled work');
  previous.dispatchEvent(new window.Event('scroll'));assert.equal([...timers.values()].some(t=>t.ms===120),false);
  scroller().dispatchEvent(new window.Event('scroll'));assert.equal([...timers.values()].filter(t=>t.ms===120).length,1);
 }else if(SCENARIO==='repeat_highlight'){
  await openRequestInspectorForTask('task-two');await openRequestInspectorForTask('task-two');
  assert.equal([...timers.values()].filter(t=>t.ms===1600).length,1,'repeated highlighting retains one scheduled cleanup per divider');
  await fire(1600);assert.equal(el('debugContent').querySelector('.ri-flash'),null);
 }else if(SCENARIO==='chip_keyboard'){
  const jumps=[];global._findRenderedNativeTurnNode=id=>({scrollIntoView:()=>jumps.push(id)});
  el('riTurnPos').click();const chip=rows()[1].querySelector('button');
  chip.dispatchEvent(new window.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));chip.click();
  assert.deepEqual(jumps,['two']);assert.equal(scrollTarget,'','chip navigation does not also activate its feed row');
 }else if(SCENARIO==='anchor_pending'){
  closeRequestInspector();let resolve;Api.tasks.convWire=()=>new Promise(done=>{resolve=done;});
  await openRequestInspectorForTask('task-two');resolve(wire());await tick();assert.equal(scrollTarget,'two');
 }else if(SCENARIO==='anchor_missing'){
  await openRequestInspectorForTask('out-of-page');assert.equal(scrollTarget,'three');
 }else throw new Error('unknown scenario '+SCENARIO);
}
main().then(()=>console.log('Inspector navigation completed')).catch(error=>{console.error(error);process.exitCode=1;}).finally(()=>{closeRequestInspector();dom.window.close();});
'''


@pytest.mark.parametrize('scenario', [
    'prepend_identity', 'reopen_newest', 'scroll_close', 'flash_close',
    'refresh_focus', 'keyboard_row', 'scroll_work', 'ordinal_work',
    'trace_suspends', 'anchor_pending', 'anchor_missing',
    'removed_focus', 'replaced_scroller', 'repeat_highlight', 'chip_keyboard',
])
def test_inspector_navigation(inspector_bundle, scenario):
    bindings = {'ROOT': str(ROOT), 'BUILT': str(inspector_bundle), 'SCENARIO': scenario,
                'SOURCE': str(ROOT / 'frontend/src/runtime/sections/core/request_inspector.js')}
    script = '\n'.join(f'const {key}={json.dumps(value)};' for key, value in bindings.items()) + HARNESS
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    assert 'Inspector navigation completed' in result.stdout, 'Node exited before the scenario settled'
