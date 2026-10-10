"""Atomic delivery changes are reduced once, not hidden by renderer heuristics."""
from pathlib import Path

import pytest

from tests._jsdom import run_harness
from tests._runtime_sections import native_module_path

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_background_receipt_reducer_view_model_and_queue_agree():
    targets = [native_module_path(f'.native/bg-delivery-{name}.js', ROOT / source)
               for name, source in (
                   ('state', 'frontend/src/core/turn-state.ts'),
                   ('vm', 'frontend/src/conversation/presentation/conversation-view-model.ts'),
                   ('render', 'frontend/src/conversation/ui/classic-conversation-renderers.ts'))]
    run_harness(target_js=str(targets[0]), extra_targets=list(map(str, targets[1:])),
                label='background delivery state', expect_pass=9, body_js=r"""
const { setup } = require(process.env.JSDOM_HARNESS);
const { check, report } = setup({root: process.argv[3],
  targets: [process.argv[2], process.argv[4], process.argv[5]]});
const completion = {commandId:'bg_1',status:'completed',command:'pytest',resultPreview:'ok',resultTruncated:false};
const queue = id => ({queueId:id,position:id==='q-bg'?1:2,kind:'workflow_step',priority:1,
  timestamp:1,text:'result',backgroundCommand:completion});
const turn = {turnId:'t1',conversationId:'c1',laneId:'main',ordinal:1,actor:'assistant',kind:'reply',
  runId:'',status:'running',currentAttemptId:'a1',projectionRevision:1,settlement:{},
  createdAt:1,updatedAt:1,projection:{content:'',segments:[],toolRounds:[]}};
const snapshot = {conversationRevision:1,authoritativeFull:true,turns:[turn],
  attempts:[{attemptId:'a1',turnId:'t1',status:'running',lastSeq:1}],queueItems:[queue('q-bg'),queue('q-other')]};
const receipt = {blockId:'injection:bg:q-bg',round:3,count:1,
  previews:[{queueId:'q-bg',commandId:'bg_1',text:'ok',backgroundCommand:completion}]};
const event = {conversationId:'c1',turnId:'t1',attemptId:'a1',seq:2,projectionRevision:2,
  type:'projection_updated',payload:{projectionPatch:{version:1,baseRevision:1,targetRevision:2,
    operations:[{op:'set',path:['_bgCommandInjects'],value:[receipt]}]},removedQueueIds:['q-bg']}};
let state = reduceTurnState(createTurnState('c1'),{type:'snapshot',snapshot});
const vmOf = state => selectConversationViewModel(state);
check('completion_alone_is_not_delivery', state.queueItems.length===2 && vmOf(state).queue.length===2);
const accepted = event;
const stale = reduceTurnState(state,{type:'event',event:{...accepted,attemptId:'old-attempt'}});
check('stale_attempt_cannot_remove_queue',stale.queueItems.length===2);
const duplicate = reduceTurnState(state,{type:'event',event:{...accepted,seq:1}});
check('duplicate_sequence_cannot_remove_queue',duplicate.queueItems.length===2);
let gaps=0;
const gap = reduceTurnState(state,{type:'event',event:{...event,payload:{...event.payload,
  projectionPatch:{baseRevision:8,targetRevision:2,operations:[]}}}}, {onProjectionGap:()=>gaps++});
check('bad_patch_cannot_remove_queue',gaps===1 && gap.queueItems.length===2);
state = reduceTurnState(state,{type:'event',event:accepted});
const vm = vmOf(state);
check('one_fold_adds_receipt_and_removes_exact_queue',state.queueItems.length===1
  && state.queueItems[0].queueId==='q-other' && vm.queue.length===1
  && state.queueItems[0].position===1 && vm.queue[0].position===1
  && vm.mainLane.turns[0].blocks.some(b=>b.kind==='injections' && b.channel==='background-command'));
const renderer = createClassicConversationRenderers({});
const host = document.createElement('div');
for (const item of vm.queue) {const node=document.createElement('div');renderer.renderQueueItem(node,item);host.append(node);}
check('rendered_queue_keeps_unrelated_same_command',host.children.length===1
  && host.textContent.includes('delivery unconfirmed'));
const replay = reduceTurnState(state,{type:'event',event:accepted});
check('accepted_replay_is_idempotent',replay.queueItems.length===1
  && replay.turnsById.t1.projection._bgCommandInjects.length===1);
const reload = reduceTurnState(createTurnState('c1'),{type:'snapshot',snapshot:{...snapshot,
  conversationRevision:2,turns:[state.turnsById.t1],attempts:[state.attemptsById.a1],
  queueItems:[{...queue('q-other'),position:1}]}});
check('reload_matches_live_semantics',JSON.stringify(vmOf(reload).queue)===JSON.stringify(vm.queue)
  && reload.turnsById.t1.projection._bgCommandInjects[0].previews[0].queueId==='q-bg');
const store = createTurnStore('c1');
store.dispatch({type:'snapshot',snapshot});
const paints=[];
store.subscribe(s=>paints.push({queue:s.queueItems.length,receipts:s.turnsById.t1.projection._bgCommandInjects?.length||0}));
store.dispatch({type:'event',event:accepted});
check('subscriber_never_sees_half_transition',paints.length===1 && paints[0].queue===1 && paints[0].receipts===1);
report();
""")
