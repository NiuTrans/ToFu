"""Background snapshots update sidebar status without reopening a settled Turn."""
import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import ROOT, native_module_path, runtime_section

pytestmark = pytest.mark.unit

_HARNESS = r"""
const assert = require('node:assert/strict');
const { setup } = require(process.env.JSDOM_HARNESS);
const timers = new Map();
const disposers = [];
let timerId = 0, requests = 0, searchRequests = 0, failure = false;
let snapshot = {swarms: [], backgroundCommands: []};
const turns = new Map();
const rows = ['one', 'two', 'idle'].map(id => ({id, title:'Session '+id,
  _serverTurnCount:1, updatedAt:Date.now(), lastMsgRole:'assistant',
  lastFinishReason:'stop', lastMsgHasOutput:true}));
const locale = require(process.argv[6]);
const { window, document, check, report } = setup({
  root: process.argv[3],
  html:'<body><div id="convList"></div><div id="sidebarSearchStats"></div>'+
    '<div id="convStatusStrip"><div id="turnNav"></div></div></body>',
  targets:[process.argv[2],process.argv[4],process.argv[5]],
  globals:{
    runtimeScope:{ConversationTurnRead:{state:c=>({turnsById:turns.get(c.id)||{}}),
      ordered:c=>Object.values(turns.get(c.id)||{})}},
    retainedCompositionLifecycle:{add:fn=>disposers.push(fn),listen:()=>{}},
    activeConvId:'one', conversations:rows, sidebarSearchQuery:'',
    _conversationDisplayTitle:(title,fallback)=>title||fallback,
    conversationTimestampLabels:()=>({date:'Today',time:'10:00'}),
    presentConversationRateLimit:()=>null,
    getConvById:id=>rows.find(row=>row.id===id),
    setTimeout:(fn,ms)=>{timers.set(++timerId,{fn,ms});return timerId;},
    clearTimeout:id=>timers.delete(id),
    t:(key,vars)=>(locale[key]||key).replace(/\{(\w+)\}/g,(match,name)=>vars?.[name]??match),
    Api:{activity:{background:()=>{requests++;return failure?Promise.reject(Error('offline')):Promise.resolve(snapshot);}},
      conversations:{searchPage:async()=>{searchRequests++;return {ok:true,items:[{
        id:'deep',conversation:{id:'deep',title:'Deep search hit',lastMsgRole:'assistant',
          lastFinishReason:'stop',lastMsgHasOutput:true},matchField:'title'}],nextCursor:null,coverage:'ready'};}}},
  },
});
Object.defineProperty(document,'hidden',{value:false,configurable:true});
const flush = async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
const poll = async()=>{
  assert.equal(timers.size,1,'only one activity poll is scheduled: '+JSON.stringify([...timers.values()].map(timer=>({ms:timer.ms,fn:String(timer.fn).slice(0,160)}))));
  const [id,timer]=timers.entries().next().value;
  timers.delete(id);timer.fn();await flush();
};
const select = id=>document.querySelector(`.conv-item[data-conv-id="${id}"]`);
const status = id=>select(id).querySelector('.conv-status-background');
const update = async data=>{snapshot=data;runtimeScope.updateBackgroundActivity();await poll();};
(async()=>{
  renderConversationList();
  const one = select('one'), title = one.querySelector('.conv-title'), action=one.querySelector('.conv-rename');
  action.focus();
  for (const [id,timer] of timers) if (timer.ms===0) { timers.delete(id);timer.fn(); }
  await update({swarms:[{conv_id:'one',swarm_key:'s',agents:[
    {id:'a',status:'running'},{id:'b',status:'pending',waitReason:'capacity'}]}],backgroundCommands:[
    {conv_id:'two',command_id:'c',status:'running',command:'echo <script>unsafe</script>'}]});
  assert.ok(status('one'),'settled parent with live children remains visibly active in sidebar');
  assert.ok(status('two'),'inactive conversation with a detached command is also active');
  assert.equal(status('idle'),null,'activity does not leak to unrelated conversation');
  assert.equal(status('one').textContent,locale['sidebar.backgroundRunningTag'].replace('{count}','2'));
  assert.ok(status('one').title.includes(locale['bgActivity.chip.agentQueuedOne'].replace('{count}','1')));
  assert.ok(one.querySelector('.conv-background-dot'));
  assert.equal(convIsBusy(rows[0]),false,'background work does not keep composer/Turn busy');
  assert.equal(select('one'),one,'snapshot patches existing row');
  assert.equal(one.querySelector('.conv-title'),title);
  assert.equal(document.activeElement,action,'status-only update preserves focus');
  assert.equal(document.querySelectorAll('script').length,0);
  const badge=status('one');
  await poll();
  assert.equal(status('one'),badge,'unchanged snapshots do not churn status nodes');
  assert.equal([...timers.values()][0].ms,3000);
  // Navigation/new chat affect only the composer chip, never owner-wide polling.
  global.activeConvId=window.activeConvId=null;
  runtimeScope.updateBackgroundActivity();await poll();
  assert.equal(document.getElementById('bgActivityChip').style.display,'none');
  assert.ok(status('one'));
  assert.equal([...timers.values()][0].ms,3000,'background-only conversations keep active cadence');
  failure=true;await poll();
  assert.ok(status('one'),'failed fetch does not falsely settle work');
  failure=false;
  snapshot.swarms[0].agents[0].status='pending';
  await update(snapshot);
  assert.equal(status('one').textContent,locale['sidebar.backgroundWaitingTag'].replace('{count}','2'));
  assert.ok(one.querySelector('.conv-waiting-dot.conv-background-dot'),'queued-only is not presented as running');
  renderConversationList();
  assert.equal(select('one'),one,'status hash includes background without forcing a rebuild');
  snapshot.swarms[0].agents.push({id:'ended',status:'failed'}, {id:'unknown'});
  await update(snapshot);
  assert.equal(status('one').textContent,locale['sidebar.backgroundWaitingTag'].replace('{count}','2'),
    'failed/completed/unknown agents are not active work');
  // Retained sessions may contain a full terminal roster; existence is not liveness.
  const liveSnapshot=snapshot;
  await update({swarms:[{conv_id:'one',swarm_key:'settled',agents:[
    {id:'done',status:'completed'},{id:'failed',status:'failed'},
    {id:'cancelled',status:'cancelled'},{id:'aborted',status:'aborted'},
    {id:'unknown'}]}],backgroundCommands:[]});
  assert.equal(status('one'),null,'all-terminal retained swarm does not keep sidebar active');
  assert.equal(one.querySelector('.conv-background-dot'),null);
  assert.equal([...timers.values()][0].ms,10000,'terminal roster uses idle polling cadence');
  await update(liveSnapshot);
  // A foreground turn still owns Answering; errors must remain inspectable.
  turns.set('one',{t:{actor:'assistant',status:'running',projection:{}}});
  renderConversationList();
  assert.equal(one.querySelector('.conv-status-streaming').textContent,locale['sidebar.answering']);
  turns.set('one',{t:{actor:'assistant',status:'failed',projection:{error:'failed'}}});
  renderConversationList();
  assert.ok(one.querySelector('.conv-error-dot'));
  assert.ok(one.querySelector('.conv-status-error'));
  assert.ok(status('one'),'a settled error does not hide ongoing children');
  turns.clear();
  await update({swarms:[],backgroundCommands:[]});
  assert.equal(status('one'),null);
  assert.equal(status('two'),null);
  assert.equal(one.querySelector('.conv-background-dot'),null);
  assert.equal([...timers.values()][0].ms,10000,'fully idle snapshot backs off');
  // A row first materialized after the poll reads the latest snapshot index.
  await update({swarms:[],backgroundCommands:[{conv_id:'late',command_id:'late-command',status:'running'}]});
  assert.equal(select('late'),null);
  rows.push({id:'late',title:'Late row',_serverTurnCount:1,updatedAt:Date.now(),
    lastMsgRole:'assistant',lastFinishReason:'stop',lastMsgHasOutput:true});
  renderConversationList();
  assert.ok(status('late'),'newly mounted rows read current background status');
  await update({swarms:[],backgroundCommands:[]});
  // Deep server search hits need status updates without joining the catalog or refetching search.
  global.sidebarSearchQuery=window.sidebarSearchQuery='deep';
  renderConversationList();
  const searchTimer=[...timers.entries()].find(([,timer])=>timer.ms===180);
  assert.ok(searchTimer);timers.delete(searchTimer[0]);searchTimer[1].fn();await flush();
  const deep=select('deep');assert.ok(deep);
  assert.equal(searchRequests,1);
  await update({swarms:[],backgroundCommands:[{conv_id:'deep',command_id:'d',status:'running'}]});
  assert.ok(status('deep'));
  assert.equal(select('deep'),deep,'search rows are patched without replacing their snippets/actions');
  assert.equal(searchRequests,1,'activity does not trigger search requests');
  assert.equal(rows.some(row=>row.id==='deep'),false,'background display does not hydrate catalog or Turns');
  await update({swarms:[],backgroundCommands:[]});
  assert.equal(status('deep'),null);
  disposers.reverse().forEach(fn=>fn());
  assert.equal(timers.size,0,'teardown releases poll');
  check('sidebar lifecycle, focus, foreground precedence, search, and polling budget',requests>0);
  report();
})().catch(error=>{console.error(error);process.exitCode=1;});
"""


def test_background_activity_updates_sidebar_without_foreground_turn(tmp_path):
    retained = tmp_path / 'sidebar-background.js'
    retained.write_text(runtime_section('ui/conversation_list.js', scope_prelude=False)
                        + '\n' + runtime_section('background-activity.js', scope_prelude=False))
    run_harness(
        target_js=native_module_path('.native/sidebar-background-presentation.js',
                                     ROOT / 'frontend/src/background-activity-presentation.ts'),
        extra_targets=[
            native_module_path('.native/sidebar-background-search.js',
                               ROOT / 'frontend/src/sidebar-search-controller.ts'),
            str(retained),
            str(ROOT / 'frontend/src/i18n/locales/en.json'),
        ],
        body_js=_HARNESS,
        min_pass=1,
        label='background activity sidebar',
    )
