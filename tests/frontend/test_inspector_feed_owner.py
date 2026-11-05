"""The real Inspector entry keeps feed reads separate from selected evidence."""
import json
import subprocess

import pytest

from tests.frontend.test_inspector_read_lifecycle import inspector_bundle as inspector_bundle  # noqa: F401
from tests.frontend.test_settings_save_feedback import ROOT

pytestmark = pytest.mark.unit

HARNESS = r'''
const assert=require('node:assert/strict'),fs=require('node:fs'),{JSDOM}=require(ROOT+'/node_modules/jsdom');
const dom=new JSDOM('<!doctype html><body><div id="riDrawer"><div class="ri-main"><div class="debug-panel">'
 +'<div id="debugTitle"></div><div id="debugContent"></div></div></div></div></body>',{url:'http://localhost'});
for(const key of ['window','document','HTMLElement','AbortController','AbortSignal'])global[key]=key==='window'?dom.window:dom.window[key];
Object.assign(global,require(BUILT));
const tick=async()=>{for(let n=0;n<4;n++)await new Promise(done=>setImmediate(done));};
const requests=[];let timerId=0;
window.setTimeout=()=>++timerId;window.clearTimeout=()=>{};
function request(kind,...args){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});requests.push({kind,args,resolve,reject});return promise;}
const of=kind=>requests.filter(row=>row.kind===kind),content=()=>document.getElementById('debugContent');
global.runtimeScope={ConversationTurnRead:{ordered:()=>[{turnId:'turn',actor:'assistant'}],state:()=>({})}};
global.DebugShellState={activeConversationId:'conversation',visible:false,requests:{}};
global.Api={tasks:{convWire:(...args)=>request('wire',...args),getTrace:(...args)=>request('trace',...args),getRequestPayload:(...args)=>request('payload',...args)}};
global.t=(key,values)=>key+(values?' '+JSON.stringify(values):'');global.Icon=()=>'';global.debugLog=()=>{};
global.escapeHtml=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const renderer=createDiagnosticsMessageReconciler({identity:row=>row.content,summarize:row=>row.content,
 create(row){const node=document.createElement('article');node.className='debug-msg-block';node.textContent=row.content;return node;},open:node=>node.classList.add('open')});
global.reconcileDebugMessages=(...args)=>renderer.render(...args);
global.renderDebugBlocksInto=(node,messages)=>renderer.render(node,messages,{reuse:false});
global.showMessagesInDebug=(messages,label,checkpoints,conversation,tools)=>{renderer.render(content(),messages,{reuse:false});content()._rawMessages=messages;content()._rawTools=tools;};
document.addEventListener('click',event=>{if(event.target.closest('[data-tofu-action="riRefreshTasks()"]'))void riRefreshTasks();});
require('node:vm').runInThisContext(fs.readFileSync(SOURCE,'utf8'));
renderInspectorTrace=(node,title,doc)=>{node.textContent=doc?.id||'trace unavailable';title.textContent='trace';};
const wire=(text='feed')=>({messages:[{role:'user',content:text}],sections:[{type:'turn',msgIndex:0,turnId:'turn',taskId:'task',status:'done'}],turns:[{turnId:'turn',taskId:'task',status:'done'}]});
async function open(value){openRequestInspector();await tick();if(value!==undefined){of('wire').at(-1).resolve(value);await tick();}}
async function detail(kind='trace'){
 const operation=kind==='trace'?_riOpenTrace('task'):openToolDebugPanel('task',1);await tick();
 of(kind==='trace'?'trace':'payload').at(-1).resolve(kind==='trace'?{id:'selected evidence'}:{messages:[{role:'tool',content:'selected evidence'}],tools:[{name:'detail tool'}]});await operation;
 assert.match(content().textContent,/selected evidence/);
}
async function main(){
 if(SCENARIO==='late_failure_trace'||SCENARIO==='late_failure_detached'){
  await open();await detail(SCENARIO.endsWith('trace')?'trace':'detached');
  of('wire')[0].resolve(null);await tick();
  assert.match(content().textContent,/selected evidence/,'failed feed read cannot overwrite selected evidence');
  content().querySelector('.ri-feed-back button').click();assert.match(content().textContent,/ri.loadFailed/,'returning to feed preserves its failure state');
 }else if(SCENARIO==='refresh_while_trace'){
  await open();await detail();const refreshing=riRefreshTasks();await tick();
  assert.match(content().textContent,/selected evidence/,'manual feed refresh cannot paint its loading notice over trace');
  of('wire')[0].resolve(wire('latest feed'));await refreshing;
  assert.match(content().textContent,/selected evidence/);content().querySelector('.ri-feed-back button').click();assert.match(content().textContent,/latest feed/);
 }else if(SCENARIO==='back_during_initial_read'){
  await open();await detail();content().querySelector('.ri-feed-back button').click();
  assert.match(content().textContent,/ri.loading/,'an unsettled initial feed read is loading, not empty');
  of('wire')[0].resolve(wire());await tick();assert.match(content().textContent,/feed/);
 }else if(SCENARIO==='loading_clears_copy_evidence'){
  content()._rawMessages=[{content:'previous conversation'}];content()._rawTools=[{name:'previous tools'}];
  await open();assert.equal(content()._rawMessages,null,'loading must not keep copyable previous conversation evidence');assert.equal(content()._rawTools,null);
  of('wire')[0].resolve(wire());await tick();
 }else if(SCENARIO==='detail_loading_clears_tools'){
  await open(wire());await detail('detached');assert.ok(content()._rawTools.length);
  const next=_riOpenTrace('next');await tick();assert.equal(content()._rawTools,null,'loading another evidence pane releases previous tools');
  of('trace').at(-1).resolve({id:'next'});await next;
 }else if(SCENARIO==='retry_loading_and_retirement'){
  await open(null);const retry=content().querySelector('.ri-retry');retry.click();await tick();
  assert.equal(of('wire').length,2);assert.match(content().textContent,/ri.loading/);
  retry.click();await tick();assert.equal(of('wire').length,2,'a replaced retry cannot initiate another intent');
  of('wire')[1].resolve(wire('recovered'));await tick();retry.click();await tick();
  assert.equal(of('wire').length,2);assert.match(content().textContent,/recovered/);
 }else if(SCENARIO==='closed_retry'){
  await open(null);const retry=content().querySelector('.ri-retry');closeRequestInspector();retry.click();await tick();
  assert.equal(of('wire').length,1,'closed retry is inert');
 }else if(SCENARIO==='conversation_retry'){
  await open(null);const retry=content().querySelector('.ri-retry');DebugShellState.activeConversationId='other';_riOnConvSwitch('other');await tick();
  assert.deepEqual(of('wire').map(row=>row.args[0]),['conversation','other']);retry.click();await tick();
  assert.equal(of('wire').length,2,'retired retry cannot reload a different opening');
  of('wire')[1].resolve(wire('other conversation'));await tick();assert.match(content().textContent,/other conversation/);
 }else if(SCENARIO==='retired_divider_actions'){
  let jumps=0;global._findRenderedNativeTurnNode=()=>({scrollIntoView(){jumps++;}});
  await open(wire());const trace=content().querySelector('.ri-feed-trace'),jump=content().querySelector('.ri-turn-chip');
  const refresh=riRefreshTasks();await tick();of('wire')[1].resolve(wire('updated'));await refresh;
  trace.click();jump.click();await tick();assert.equal(of('trace').length,0);assert.equal(jumps,0);
  content().querySelector('.ri-turn-chip').click();assert.equal(jumps,1,'current turn action still targets chat');
  content().querySelector('.ri-feed-trace').click();await tick();assert.equal(of('trace').length,1);
  of('trace')[0].resolve({id:'current trace'});await tick();assert.match(content().textContent,/current trace/);
 }else if(SCENARIO==='partial_last_good'){
  await open(wire('complete evidence'));const refresh=riRefreshTasks();await tick();
  of('wire')[1].resolve({...wire('incomplete replacement'),readError:true,eventReadBounds:[{reason:'bounded'}]});await refresh;
  assert.match(content().textContent,/complete evidence/);assert.doesNotMatch(content().textContent,/incomplete replacement|ri.expired/);
  assert.ok(content().querySelector('.ri-warn-line'));assert.match(content().textContent,/ri.eventWindowPartial/);
  content().querySelector('.ri-retry').click();await tick();of('wire')[2].resolve(wire('recovered'));await tick();
  assert.equal(content().querySelector('.ri-warn-line'),null);assert.doesNotMatch(content().textContent,/ri.eventWindowPartial/);
 }else if(SCENARIO==='partial_initial'){
  await open({...wire('available evidence'),readError:true,eventReadBounds:[{}],olderTurnsDropped:3});
  assert.match(content().textContent,/available evidence/);assert.match(content().textContent,/ri.feedTruncated/);
  assert.match(content().textContent,/ri.eventWindowPartial/);assert.ok(content().querySelector('.ri-retry'));
 }else if(SCENARIO==='section_order_and_safety'){
  const value=wire();value.messages.push({role:'tool',content:'second'});
  value.sections=[{type:'agent',msgIndex:0,agentId:'<img src=x onerror=bad>'},{type:'turn',msgIndex:0,turnId:'quoted"turn',taskId:'task',status:'unknown<svg>',preview:'<b>literal</b>'},
    {type:'rebuild',msgIndex:1,kept:1,dropped:2},{type:'agent',msgIndex:99,agentId:'tail'}];
  await open(value);assert.deepEqual([...content().children].map(node=>node.className),['ri-feed-agent','ri-feed-turn','debug-msg-block','ri-feed-rebuild','debug-msg-block','ri-feed-agent']);
  assert.equal(content().querySelectorAll('img,svg,b').length,0);assert.match(content().textContent,/<b>literal<\/b>/);
  assert.equal(content().querySelector('.ri-feed-turn').dataset.turnId,'quoted"turn');
  assert.equal(content().querySelector('.ri-tone-neutral').textContent,'unknown<svg>');assert.deepEqual(content()._rawMessages,value.messages);
 }else if(SCENARIO==='message_and_scroll_continuity'){
  const scroller=document.querySelector('.ri-main');let height=600,nextHeight=600;
  Object.defineProperties(scroller,{scrollHeight:{get:()=>height},clientHeight:{get:()=>100}});
  const reconcile=global.reconcileDebugMessages;global.reconcileDebugMessages=(...args)=>{reconcile(...args);height=nextHeight;};
  await open(wire());const message=content().querySelector('.debug-msg-block');message.classList.add('open');scroller.scrollTop=120;
  const changed=wire();changed.messages.push({role:'tool',content:'appended'});let refresh=riRefreshTasks();await tick();of('wire')[1].resolve(changed);await refresh;
  assert.equal(content().querySelector('.debug-msg-block'),message);assert.ok(message.classList.contains('open'));assert.equal(scroller.scrollTop,120);
  scroller.scrollTop=500;nextHeight=900;refresh=riRefreshTasks();await tick();of('wire')[2].resolve(changed);await refresh;assert.equal(scroller.scrollTop,900,'bottom-pinned readers follow appended evidence');
 }else if(SCENARIO==='reopen_failure_race'){
  await open();closeRequestInspector();await open();of('wire')[1].resolve(wire('current opening'));await tick();
  of('wire')[0].reject(new Error('retired failure'));await tick();assert.match(content().textContent,/current opening/);assert.equal(content().querySelector('.ri-retry'),null);
 }else throw new Error('Unknown scenario '+SCENARIO);
 closeRequestInspector();dom.window.close();console.log('Feed scenario completed');
}
main().catch(error=>{console.error(error);process.exitCode=1;dom.window.close();});
'''


@pytest.mark.parametrize('scenario', [
    'late_failure_trace', 'late_failure_detached', 'refresh_while_trace',
    'back_during_initial_read', 'loading_clears_copy_evidence', 'detail_loading_clears_tools',
    'retry_loading_and_retirement', 'closed_retry', 'conversation_retry', 'retired_divider_actions',
    'partial_last_good', 'partial_initial', 'section_order_and_safety', 'message_and_scroll_continuity', 'reopen_failure_race',
])
def test_inspector_feed_ownership(inspector_bundle, scenario):
    values = {'ROOT': str(ROOT), 'BUILT': str(inspector_bundle), 'SCENARIO': scenario,
              'SOURCE': str(ROOT / 'frontend/src/runtime/sections/core/request_inspector.js')}
    script = '\n'.join(f'const {key}={json.dumps(value)};' for key, value in values.items()) + HARNESS
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    assert 'Feed scenario completed' in result.stdout, 'Node exited before the scenario settled'
