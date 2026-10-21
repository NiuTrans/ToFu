"""Behavioral contracts for the typed ConversationSurface production graph."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
BUNDLER = ROOT / "scripts" / "vite_test_bundle.mjs"
ENTRY = ROOT / "frontend/src/conversation/index.ts"


def _ready() -> bool:
    if not shutil.which("node") or not BUNDLER.is_file():
        return False
    return subprocess.run(
        [shutil.which("node"), "-e", "require('jsdom')"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    ).returncode == 0


pytestmark = [pytest.mark.unit, pytest.mark.skipif(
    not _ready(), reason="node + jsdom + Vite test bundler required")]


@pytest.fixture(scope="module")
def conversation_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    built = tmp_path_factory.mktemp("conversation-surface") / "conversation.cjs"
    result = subprocess.run(
        [str(BUNDLER), str(ENTRY), "--bundle", "--format=cjs",
         "--platform=node", f"--outfile={built}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return built


def _run(bundle: Path, source: str) -> dict:
    script = f"""
const harnessDom = new (require('jsdom').JSDOM)('', {{url:'http://localhost/'}});
global.window = harnessDom.window;
const feature = require({json.dumps(str(bundle))});
{source}
"""
    result = subprocess.run(
        [shutil.which("node"), "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_selector_joins_lanes_and_preserves_contract_block_identity(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const turn = (id, lane, ordinal, projection, extra = {}) => ({
  turnId:id, conversationId:'conv-a', laneId:lane, ordinal,
  actor:'assistant', kind:'reply', runId:'', status:'completed',
  currentAttemptId:null, projection, projectionRevision:3, modelContextRevision:2,
  settlement:{outcome:'completed'}, createdAt:1, updatedAt:2, ...extra,
});
const parent = turn('parent', 'main', 1, {
  segments:[
    {type:'thinking', blockId:'thinking:terminal', text:'reason', terminal:true},
    {type:'text', blockId:'text:terminal', text:'answer', translatedText:'答案',
      deliverable:true, terminal:true},
  ],
  images:[{attachmentId:'image-1'}],
  _branchLanes:[{laneId:'lane-b', title:'Alternative'}],
});
const child = turn('child', 'lane-b', 1, {
  segments:[{type:'tool_use', blockId:'tool:call-1', id:'call-1', name:'search',
    input:{q:'x'}, result:{status:'done', content:'hit'}}],
}, {parentTurnId:'parent'});
const legacy = turn('legacy', 'main', 2, {content:'old row', segments:[
  {type:'text', blockId:'text:terminal', text:'old row',
    deliverable:true, terminal:true},
]});
const state = {
  conversationId:'conv-a', conversationRevision:9, transport:'live',
  turnsById:{parent, child, legacy},
  laneOrder:{main:['parent','legacy'], 'lane-b':['child']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{},
  commandPending:{}, liveRoundUsageByTurn:{},
};
const before = JSON.stringify(state);
const compatibility = [];
const vm = feature.selectConversationViewModel(state, {
  onCompatibilityIdentity(turnId, blockId) { compatibility.push([turnId, blockId]); },
});
console.log(JSON.stringify({
  mainIds:vm.mainLane.turns.map(item => item.turnId),
  parentBlocks:vm.mainLane.turns[0].blocks.map(item => [item.kind,item.blockId,item.identitySource]),
  branchTitle:vm.mainLane.turns[0].branches[0].title,
  branchTurn:vm.mainLane.turns[0].branches[0].turns[0].turnId,
  branchBlock:vm.mainLane.turns[0].branches[0].turns[0].blocks[0].blockId,
  compatibility,
  unchanged:before === JSON.stringify(state),
}));
""")
    assert result == {
        "mainIds": ["parent", "legacy"],
        "parentBlocks": [
            ["attachments", "attachments", "contract"],
            ["thinking", "thinking:terminal", "contract"],
            ["text", "text:terminal", "contract"],
        ],
        "branchTitle": "Alternative",
        "branchTurn": "child",
        "branchBlock": "tool:call-1",
        "compatibility": [],
        "unchanged": True,
    }


def test_awaiting_human_round_joins_its_tool_block(conversation_bundle: Path):
    """The interactive ask_human card renders off block.round — the selector
    must join the projection's live round (status + guidance fields) onto the
    tool_use segment's block, or the user gets a label with no question and
    no answer UI (the missing-segment bug's frontend half)."""
    result = _run(conversation_bundle, r"""
const turn = {
  turnId:'turn-hg', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'', status:'running',
  currentAttemptId:'attempt-1', projectionRevision:5,
  settlement:{}, createdAt:1, updatedAt:2,
  projection:{
    segments:[
      {type:'tool_use', blockId:'tool:call-a', id:'call-a', name:'grep_search',
        input:{pattern:'x'}, result:{status:'done', content:'hit'}},
      {type:'tool_use', blockId:'tool:call-hg', id:'call-hg', name:'ask_human',
        input:{question:'Which scope?'}, result:{status:'awaiting_human'}},
    ],
    toolRounds:[
      {roundNum:1, toolCallId:'call-a', toolName:'grep_search', status:'done'},
      {roundNum:2, toolCallId:'call-hg', toolName:'ask_human',
        status:'awaiting_human', guidanceId:'hg_1',
        guidanceQuestion:'Which scope?', guidanceType:'choice',
        guidanceOptions:[{label:'A'},{label:'B'}]},
    ],
  },
};
const state = {
  conversationId:'conv-a', conversationRevision:5, transport:'live',
  turnsById:{'turn-hg':turn}, laneOrder:{main:['turn-hg']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{},
  commandPending:{}, liveRoundUsageByTurn:{},
};
const vm = feature.selectConversationViewModel(state, {});
const blocks = vm.mainLane.turns[0].blocks;
const hg = blocks.find((block) => block.kind === 'tool'
  && block.toolCallId === 'call-hg');
console.log(JSON.stringify({
  kinds:blocks.map((block) => block.kind),
  hgStatus:hg && hg.round && hg.round.status,
  hgGuidanceId:hg && hg.round && hg.round.guidanceId,
  hgQuestion:hg && hg.round && hg.round.guidanceQuestion,
  hgOptions:hg && hg.round && hg.round.guidanceOptions.length,
}));
""")
    assert result == {
        "kinds": ["tool", "tool", "live-status"],
        "hgStatus": "awaiting_human",
        "hgGuidanceId": "hg_1",
        "hgQuestion": "Which scope?",
        "hgOptions": 2,
    }


def test_surface_reuses_turn_and_block_nodes_across_revisions(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const intents = [];
const sourceText1 = {};
const sourceTool = {};
const toolInput = {q:'x'};
const toolResult = {status:'running'};
let actionRenders = 0;
let footerRenders = 0;
const makeVm = (text, includeTool = true) => ({
  conversationId:'conv-a', conversationRevision:text.length, transport:'live',
  orphanLanes:[], queue:[], mainLane:{laneId:'main', parentTurnId:null,
    title:'Conversation', kind:'main', turns:[{
      turnId:'turn-1', laneId:'main', parentTurnId:null, ordinal:1,
      actor:'assistant', role:'assistant', kind:'reply', status:'running',
      attemptId:'attempt-1', projectionRevision:text.length,
      subtreeRevisionKey:String(text.length),
      commandPending:null, finish:null, branches:[], metadata:{translation:{completed:false}},
      actions:[{action:'copy', disabled:false}],
      source:{}, blocks:[
        {kind:'text', blockId:'text:terminal', identitySource:'contract',
          source:sourceText1, markdown:text, deliverable:true, terminal:true,
          resumable:false},
        ...(includeTool ? [{kind:'tool', blockId:'tool:one', identitySource:'contract',
          source:sourceTool, toolCallId:'one', name:'search', input:toolInput,
          result:toolResult}] : []),
      ],
    }]},
});
const surface = feature.createConversationSurface(document.getElementById('chat'), {
  onIntent(intent) { intents.push(intent); },
  renderBlock(node, block) {
    if (block.kind === 'tool') {
      const details = document.createElement('details');
      details.textContent = block.name;
      node.replaceChildren(details);
    } else {
      const button = document.createElement('button');
      button.dataset.conversationAction = 'copy-block';
      button.textContent = block.markdown;
      node.replaceChildren(button);
    }
  },
  renderTurnActions(node, turn) {
    actionRenders += 1;
    const button = document.createElement('button');
    button.dataset.conversationAction = turn.actions[0].action;
    node.replaceChildren(button);
  },
  renderTurnFooter(node) {
    footerRenders += 1;
    node.textContent = 'stable';
  },
});
surface.render(makeVm('a'));
const turn1 = surface.root.querySelector('[data-turn-id="turn-1"]');
const text1 = surface.root.querySelector('[data-block-id="text:terminal"]');
const tool1 = surface.root.querySelector('[data-block-id="tool:one"]');
const action1 = surface.root.querySelector('[data-conversation-part="turn-actions"] button');
action1.focus();
tool1.querySelector('details').open = true;
/* Simulate a stale live-render/asset transition that left duplicate keyed
 * nodes. The next authoritative commit must heal both levels and preserve the
 * original managed nodes rather than orphaning the first copy forever. */
tool1.parentElement.appendChild(tool1.cloneNode(true));
turn1.parentElement.appendChild(turn1.cloneNode(true));
surface.render(makeVm('ab'));
const turn2 = surface.root.querySelector('[data-turn-id="turn-1"]');
const text2 = surface.root.querySelector('[data-block-id="text:terminal"]');
const tool2 = surface.root.querySelector('[data-block-id="tool:one"]');
const action2 = surface.root.querySelector('[data-conversation-part="turn-actions"] button');
text2.querySelector('button').click();
surface.render(makeVm('abc', false));
console.log(JSON.stringify({
  sameTurn:turn1 === turn2,
  sameTextBlock:text1 === text2,
  sameToolBlock:tool1 === tool2,
  sameAction:action1 === action2,
  actionFocusPreserved:document.activeElement === action2,
  actionRenders,
  footerRenders,
  toolExpansionPreserved:tool2.querySelector('details').open,
  text:text2.textContent,
  toolRemoved:!surface.root.querySelector('[data-block-id="tool:one"]'),
  intent:intents[0],
  duplicateTurns:surface.root.querySelectorAll('[data-turn-id="turn-1"]').length,
  duplicateBlocks:surface.root.querySelectorAll('[data-block-id="tool:one"]').length,
}));
""")
    assert result["sameTurn"] is True
    assert result["sameTextBlock"] is True
    assert result["sameToolBlock"] is True
    assert result["sameAction"] is True
    assert result["actionFocusPreserved"] is True
    assert result["actionRenders"] == 1
    assert result["footerRenders"] == 1
    assert result["toolExpansionPreserved"] is True
    assert result["text"] == "abc"
    assert result["toolRemoved"] is True
    assert result["duplicateTurns"] == 1
    assert result["duplicateBlocks"] == 0
    assert result["intent"] == {
        "type": "copy-block",
        "conversationId": "conv-a",
        "turnId": "turn-1",
        "blockId": "text:terminal",
        "laneId": "main",
    }


def test_surface_hands_optimistic_turn_to_durable_identity_in_place(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const vm = (turnId, status, revision) => ({
  conversationId:'conv-a', conversationRevision:revision, transport:'live',
  orphanLanes:[], queue:[], mainLane:{laneId:'main', parentTurnId:null,
    title:'Conversation', kind:'main', turns:[{
      turnId, presentationId:'cmd-stable:output', laneId:'main',
      parentTurnId:null, ordinal:2, actor:'assistant', role:'assistant',
      kind:'reply', status, attemptId:status === 'running' ? 'attempt-1' : null,
      projectionRevision:revision, subtreeRevisionKey:String(revision),
      commandPending:null, finish:null, branches:[], actions:[],
      metadata:{translation:{completed:false}}, source:{}, blocks:[],
    }]},
});
const surface = feature.createConversationSurface(document.getElementById('chat'));
surface.render(vm('transient:outgoing:cmd-stable:assistant', 'pending', 1));
const provisional = surface.root.querySelector(
  '[data-presentation-id="cmd-stable:output"]',
);
surface.render(vm('turn-durable', 'running', 2));
const durable = surface.root.querySelector('[data-turn-id="turn-durable"]');
console.log(JSON.stringify({
  sameNode:provisional === durable,
  presentationId:durable.dataset.presentationId,
  turnId:durable.dataset.turnId,
  count:surface.root.querySelectorAll(
    '[data-presentation-id="cmd-stable:output"]',
  ).length,
}));
surface.dispose();
dom.window.close();
""")
    assert result == {
        "sameNode": True,
        "presentationId": "cmd-stable:output",
        "turnId": "turn-durable",
        "count": 1,
    }


def test_linked_queue_pair_projects_only_user_turn_with_inline_status(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const turn = (turnId, actor, ordinal, currentAttemptId = null) => ({
  turnId, presentationId:'cmd-queue:' + (actor === 'human' ? 'input' : 'output'),
  conversationId:'conv-a', laneId:'main', ordinal, actor,
  kind:actor === 'human' ? 'input' : 'reply', runId:'',
  status:actor === 'human' ? 'completed' : 'pending', currentAttemptId,
  projection:{segments:actor === 'human'
    ? [{type:'text', blockId:'text:terminal', text:'queued text',
        deliverable:true, terminal:true}]
    : []},
  projectionRevision:1, settlement:{}, createdAt:1, updatedAt:1,
});
const input = turn('turn-input', 'human', 1);
const output = turn('turn-output', 'assistant', 2, 'attempt-queue');
const state = {
  conversationId:'conv-a', conversationRevision:3, transport:'live',
  turnsById:{'turn-input':input, 'turn-output':output},
  laneOrder:{main:['turn-input','turn-output']},
  attemptsById:{'attempt-queue':{
    attemptId:'attempt-queue', turnId:'turn-output', attemptNo:1,
    status:'pending', queueBinding:{queueId:'queue-1', state:'pending'},
    createdAt:1, updatedAt:1,
  }},
  queueItems:[{queueId:'queue-1', position:2, kind:'real', priority:1,
    timestamp:1, text:'queued text', inputTurnId:'turn-input',
    outputTurnId:'turn-output', attemptId:'attempt-queue'}],
  pendingEventsByTurn:{}, commandPending:{}, liveRoundUsageByTurn:{},
};
const viewModel = feature.selectConversationViewModel(state);
console.log(JSON.stringify({
  visibleTurnIds:viewModel.mainLane.turns.map(turn => turn.turnId),
  blockKinds:viewModel.mainLane.turns[0].blocks.map(block => block.kind),
  queueId:viewModel.mainLane.turns[0].blocks.at(-1).value.queueId,
  compatibilityQueue:viewModel.queue,
  live:viewModel.mainLane.live,
}));
""")
    assert result == {
        "visibleTurnIds": ["turn-input"],
        "blockKinds": ["text", "queue-status"],
        "queueId": "queue-1",
        "compatibilityQueue": [],
        "live": False,
    }


def test_session_binding_coalesces_store_frames_and_disposes(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const store = feature.createTurnStore('conv-a');
const rendered = [];
let surfaceDisposed = false;
const surface = {
  root:{},
  render(vm) { rendered.push(vm.transport); },
  dispose() { surfaceDisposed = true; },
};
const pending = [];
const scheduler = {
  schedule(render) {
    const item = {render, cancelled:false};
    pending.push(item);
    return () => { item.cancelled = true; };
  },
};
const binding = feature.bindConversationSession(store, surface, {scheduler});
store.dispatch({type:'transport', status:'connecting'});
store.dispatch({type:'transport', status:'live'});
const queuedBeforeFlush = pending.filter(item => !item.cancelled).length;
pending[0].render();
binding.dispose();
store.dispatch({type:'transport', status:'offline'});
console.log(JSON.stringify({rendered, queuedBeforeFlush, surfaceDisposed}));
""")
    assert result == {
        "rendered": ["idle", "live"],
        "queuedBeforeFlush": 1,
        "surfaceDisposed": True,
    }


def test_controller_transfers_dom_ownership_without_replacing_turn_node(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"><div id="legacy-old">old</div></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const scheduled = [];
const committed = [];
const captured = [];
const restored = [];
const makeTurn = (revision, content) => ({
  turnId:'turn-1', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'', status:'completed',
  currentAttemptId:null, projection:{content, segments:[{
    type:'text', blockId:'text:terminal', text:content,
    deliverable:true, terminal:true,
  }]}, projectionRevision:revision,
  settlement:{outcome:'completed'}, createdAt:1, updatedAt:revision,
});
const makeState = (turn) => ({
  conversationId:'conv-a', conversationRevision:turn.projectionRevision,
  transport:'live', turnsById:{'turn-1':turn}, laneOrder:{main:['turn-1']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{},
});
const conversation = {id:'conv-a'};
const controller = feature.createConversationSurfaceController({
  isActive:id => id === 'conv-a',
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  captureScroll() {
    const snapshot = {anchor:'turn-1', offset:captured.length};
    captured.push(snapshot);
    return snapshot;
  },
  restoreScroll(snapshot) { restored.push(snapshot); },
  afterConversationCommit(_conversation, state) {
    committed.push(state.conversationRevision);
  },
});
controller.render(conversation, makeState(makeTurn(1, 'one')));
scheduled.shift()();
const first = document.querySelector('[data-turn-id="turn-1"]');
controller.render(conversation, makeState(makeTurn(2, 'two')));
scheduled.shift()();
const second = document.querySelector('[data-turn-id="turn-1"]');
const beforeDispose = {
  oldRemoved:!document.getElementById('legacy-old'),
  sameNode:first === second,
  canonicalIdentity:second.dataset.turnId,
  text:second.querySelector('[data-block-id="text:terminal"]').textContent,
  committed,
  scrollCaptureCount:captured.length,
  scrollSnapshotsPreserved:restored.every(
    (value, index) => value === captured[index]),
  roots:document.querySelectorAll('[data-conversation-surface]').length,
};
controller.disposeConversation('conv-a');
console.log(JSON.stringify({
  ...beforeDispose,
  rootRemoved:document.querySelectorAll('[data-conversation-surface]').length === 0,
}));
""")
    assert result == {
        "oldRemoved": True,
        "sameNode": True,
        "canonicalIdentity": "turn-1",
        "text": "two",
        "committed": [1, 2],
        "scrollCaptureCount": 2,
        "scrollSnapshotsPreserved": True,
        "roots": 1,
        "rootRemoved": True,
    }


def test_human_turn_is_owned_by_the_typed_surface(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const turn = {
  turnId:'human-1', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'human', kind:'input', runId:'', status:'completed',
  currentAttemptId:null, projection:{content:'**hello**', segments:[
    {type:'text', blockId:'text:terminal', text:'**hello**',
      deliverable:true, terminal:true},
  ]}, projectionRevision:1,
  settlement:{outcome:'completed'}, createdAt:1, updatedAt:1,
};
const state = {
  conversationId:'conv-a', conversationRevision:1, transport:'live',
  turnsById:{'human-1':turn}, laneOrder:{main:['human-1']}, attemptsById:{},
  queueItems:[], pendingEventsByTurn:{}, commandPending:{}, liveRoundUsageByTurn:{},
};

const intents = [];
const scheduled = [];
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },

  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => `<strong>${value.replaceAll('*','')}</strong>`,
    actionLabel:value => value,
  }),
  onIntent:intent => intents.push(intent),
});
controller.render({id:'conv-a'}, state);
scheduled.shift()();
const node = document.querySelector('[data-turn-id="human-1"]');
node.querySelector('[data-conversation-action="copy"]').click();
console.log(JSON.stringify({

  text:node.querySelector('.md-content').textContent,
  isUser:node.classList.contains('user-msg'),
  isFailed:node.classList.contains('turn-failed'),
  intent:intents[0],
}));
""")
    assert result == {

        "text": "hello",
        "isUser": True,
        "isFailed": False,
        "intent": {
            "type": "copy",
            "conversationId": "conv-a",
            "turnId": "human-1",
            "laneId": "main",
        },
    }


def test_simple_assistant_turn_is_native_and_routes_typed_actions(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const turn = {
  turnId:'assistant-1', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'', status:'interrupted',
  currentAttemptId:'attempt-1',
  projection:{segments:[
    {type:'thinking', blockId:'thinking:llm-1', text:'reason', llmRound:1},
    {type:'text', blockId:'text:terminal', text:'**answer**', deliverable:true,
      terminal:true},
  ], content:'**answer**', model:'model-a', timestamp:2, timingTrace:{
    taskId:'task-a', running:false, clockSource:'server-monotonic',
    coverage:'full', totalMs:42000, tEnd:1_700_000_042_000}},
  projectionRevision:2,
  settlement:{outcome:'interrupted', cause:'user_stop', resumeOptions:[
    {operation:'continue', anchor:{segmentId:'text:terminal'}},
  ]}, createdAt:1, updatedAt:2,
};
const state = {
  conversationId:'conv-a', conversationRevision:2, transport:'live',
  turnsById:{'assistant-1':turn}, laneOrder:{main:['assistant-1']}, attemptsById:{
    'attempt-1':{attemptId:'attempt-1', turnId:'assistant-1', taskId:'task-a',
      createdAt:1},
  },
  queueItems:[], pendingEventsByTurn:{}, commandPending:{}, liveRoundUsageByTurn:{},
};

const intents = [];
const scheduled = [];
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => `<strong>${value.replaceAll('*','')}</strong>`,
    actionLabel:value => value,
  }),
  requestInspectorEnabled:() => true,
  onIntent:intent => intents.push(intent),
});
controller.render({id:'conv-a'}, state);
scheduled.shift()();
// Reveal historical content before exercising its controls.
document.querySelector('[data-round-action="expandAll"]')?.click();
const node = document.querySelector('[data-turn-id="assistant-1"]');
node.querySelector('[data-conversation-action="resume"]').click();
node.querySelector('[data-conversation-action="translate"]').click();
node.querySelector('[data-conversation-action="inspect"]').click();
const footerNode = node.querySelector('[data-conversation-part="turn-footer"]');
const timingNode = footerNode.querySelector('.finish-tag.timing');
console.log(JSON.stringify({
  compatible:Boolean(
    feature.selectConversationViewModel(state).mainLane.turns[0]),

  text:node.querySelector('[data-block-id="text:terminal"] .md-content').textContent,
  thinking:node.querySelector('[data-block-id="thinking:llm-1"]').textContent,
  footer:footerNode.textContent.replace(
    timingNode ? timingNode.textContent : '', ''),
  timing:Boolean(timingNode)
    && timingNode.textContent.includes(':')
    && timingNode.textContent.endsWith('· 00:42'),
  failedClass:node.classList.contains('turn-failed'),
  inspectorClass:node.querySelector('[data-conversation-action="inspect"]')
    .classList.contains('ri-anchor'),
  intents,
}));
""")
    assert result == {
        "compatible": True,

        "text": "answer",
        "thinking": "Thinking Processreason",
        "footer": "Interrupted: user_stopmodel-a",
        "timing": True,
        "failedClass": True,
        "inspectorClass": True,
        "intents": [
            {
                "type": "resume",
                "conversationId": "conv-a",
                "turnId": "assistant-1",
                "laneId": "main",
                "operation": "continue",
            },
            {
                "type": "translate",
                "conversationId": "conv-a",
                "turnId": "assistant-1",
                "laneId": "main",
            },
            {
                "type": "inspect",
                "conversationId": "conv-a",
                "turnId": "assistant-1",
                "laneId": "main",
                "operation": "task-a",
            },
        ],
    }


def test_finish_duration_uses_only_matching_complete_server_evidence(conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const trace = {taskId:'task-a', running:false, clockSource:'server-monotonic',
  coverage:'full', totalMs:42000, tEnd:1700000042000};
const cases = [trace, {...trace, coverage:'partial'},
  {...trace, clockSource:'legacy-wall'}, {...trace, clockSource:'mixed'},
  {...trace, taskId:'older-attempt'}, {...trace, running:true},
  {...trace, totalMs:null}, {...trace, totalMs:-1}, {...trace, totalMs:0}, undefined];
console.log(JSON.stringify({
  values:cases.map(value => feature.presentTurnExecutionTiming(value, 9, 'task-a')),
}));
""")
    values = result['values']
    assert values[0] == {'settledAtMs': 1700000042000, 'durationMs': 42000}
    assert all(item['durationMs'] is None for item in values[1:8])
    assert values[4]['settledAtMs'] == values[5]['settledAtMs'] == 9
    assert values[8]['durationMs'] == 0
    assert values[9] == {'settledAtMs': 9, 'durationMs': None}


def test_settled_turn_renders_all_thinking_complete_live_turn_tail_active(
        conversation_bundle: Path):
    """segment.terminal marks the terminal round's accumulator, not 'may still
    grow' — historical (settled) turns must never show an active 'Thinking…'
    block. On a live turn a reasoning block closes the moment narration or a
    tool round lands after it (not one round late, when the NEXT reasoning
    starts); only a reasoning block that is still the activity tail stays
    active."""
    result = _run(conversation_bundle, r"""
const makeTurn = (status, segments, content) => ({
  turnId:'turn-1', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'', status,
  currentAttemptId:null, projection:{segments, content},
  projectionRevision:3, settlement:{outcome:'completed'},
  createdAt:1, updatedAt:2,
});
const makeState = turn => ({
  conversationId:'conv-a', conversationRevision:3, transport:'live',
  turnsById:{'turn-1':turn}, laneOrder:{main:['turn-1']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{},
});
const rounds = [
  {type:'thinking', blockId:'thinking:llm-1', text:'reason one', llmRound:1},
  {type:'tool_use', blockId:'tool:call-1', id:'call-1', name:'search',
    input:{}, result:{status:'done', content:'hit'}, llmRound:1},
  {type:'thinking', blockId:'thinking:llm-2', text:'reason two', llmRound:2},
];
const streamingAnswer = [...rounds,
  {type:'text', blockId:'text:terminal', text:'answer', deliverable:true,
    terminal:true},
];
const thinkingStates = (status, segments, content) =>
  feature.selectConversationViewModel(
    makeState(makeTurn(status, segments, content))).mainLane.turns[0].blocks
  .filter(block => block.kind === 'thinking')
  .map(block => [block.blockId, block.terminal]);
console.log(JSON.stringify({
  settled:thinkingStates('completed', streamingAnswer, 'answer'),
  interrupted:thinkingStates('interrupted', streamingAnswer, 'answer'),
  liveAnswerStreaming:thinkingStates('running', streamingAnswer, 'answer'),
  liveThinkingTail:thinkingStates('running', rounds, ''),
}));
""")
    assert result == {
        "settled": [["thinking:llm-1", True], ["thinking:llm-2", True]],
        "interrupted": [["thinking:llm-1", True], ["thinking:llm-2", True]],
        # The answer streaming after reasoning closes that reasoning
        # immediately — the block flips to "Thinking Process" on time.
        "liveAnswerStreaming": [
            ["thinking:llm-1", True], ["thinking:llm-2", True]],
        # Still the activity tail: genuinely mid-reasoning, stays active.
        "liveThinkingTail": [
            ["thinking:llm-1", True], ["thinking:llm-2", False]],
    }


def test_hidden_gateway_rounds_preserve_explicit_model_round_boundaries(
        conversation_bundle: Path):
    """Internal execute_tools shells are not visible content boundaries.

    Explicit backend rounds remain separate, even when an internal gateway
    is hidden. Round sections now provide the readable disclosure boundary.
    """
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const segments = [];
for (let round = 1; round <= 5; round += 1) {
  segments.push({type:'thinking', blockId:`thinking:llm-${round}`,
    text:`reason ${round}`, llmRound:round});
  if (round < 5) segments.push({type:'tool_use',
    blockId:`tool:gateway-${round}`, id:`gateway-${round}`,
    name:'execute_tools', input:{calls:[]}, llmRound:round,
    result:{status:'done', content:'{"status":"ok"}'}});
}
segments.push({type:'text', blockId:'text:terminal', text:'answer',
  deliverable:true, terminal:true});
const turn = {
  turnId:'turn-1', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'', status:'completed',
  currentAttemptId:null, projection:{segments, content:'answer'},
  projectionRevision:5, settlement:{outcome:'completed'},
  createdAt:1, updatedAt:2,
};
const state = {
  conversationId:'conv-a', conversationRevision:5, transport:'replay',
  turnsById:{'turn-1':turn}, laneOrder:{main:['turn-1']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{},
};
const vm = feature.selectConversationViewModel(state);
const thinking = vm.mainLane.turns[0].blocks.filter(
  block => block.kind === 'thinking');
const surface = feature.createConversationSurface(
  document.getElementById('chat'),
  feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => `<p>${value}</p>`,
  }),
);
surface.render(vm);
for (const process of document.querySelectorAll('.conversation-history-process')) { process.open=true; process.dispatchEvent(new document.defaultView.Event('toggle')); }
// Reveal historical content before exercising its controls.
document.querySelector('[data-round-action="expandAll"]')?.click();
console.log(JSON.stringify({
  count:thinking.length,
  blockId:thinking[0]?.blockId,
  memberBlockIds:thinking[0]?.memberBlockIds,
  markdown:thinking[0]?.markdown,
  disclosureCount:surface.root.querySelectorAll('.thinking-block').length,
  gatewayCount:surface.root.querySelectorAll('[data-block-kind="tool"]').length,
}));
surface.dispose();
dom.window.close();
""")
    assert result == {
        "count": 5,
        "blockId": "thinking:llm-1",
        "memberBlockIds": ["thinking:llm-1"],
        "markdown": "reason 1",
        "disclosureCount": 5,
        "gatewayCount": 0,
    }


@pytest.mark.parametrize("scenario", [
    "next-round", "same-round", "reused-call-across-attempts",
])
def test_childless_programs_keep_gateway_slots_through_surface_lifecycle(
        conversation_bundle: Path, scenario: str):
    """Hidden gateways still own the exact position of their visible parent."""
    result = _run(conversation_bundle, "const scenario = " + json.dumps(scenario) + r""";
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const segments = [], toolRounds = [];
const reused = scenario === 'reused-call-across-attempts';
const firstScope = reused ? {attemptId:'attempt-a'} : {};
const lastScope = reused ? {attemptId:'attempt-b'} : {};
const prose = (type, text, llmRound, scope = {}) => segments.push({
  type, blockId:`${type}:${text}`, text, llmRound, ...scope,
});
const program = (callId, label, scope) => {
  const code = `return "${label}";`;
  const output = {label};
  segments.push({type:'tool_use', blockId:`tool:${label}`, id:callId,
    name:'execute_program', input:{code}, llmRound:11, ...scope,
    result:{status:'done', content:JSON.stringify(output)}});
  toolRounds.push({roundNum:toolRounds.length + 1, llmRound:11, ...scope,
    toolCallId:callId, toolName:'execute_program', toolArgs:{code},
    status:'done', toolContent:output}, {
    roundNum:8800000 + toolRounds.length, llmRound:11, ...scope,
    _programSynthetic:true, _programCallId:callId,
    programBackend:'local_javascript', programSource:'execute_program',
    programCode:code, programResult:output, programStatus:'completed',
    status:'done', childCallIds:[], childToolNames:[],
    programStats:{tool_calls:0, successful_tool_calls:0},
  });
};
prose('thinking', 'thinking11', 11, firstScope);
program('gateway11', 'program11', firstScope);
if (scenario !== 'next-round') {
  prose('text', 'between-programs', 11, lastScope);
  program(reused ? 'gateway11' : 'gateway11b', 'program11b', lastScope);
}
prose('thinking', 'thinking12', 12, lastScope);
prose('text', 'text12', 12, lastScope);
segments.push({type:'tool_use', blockId:'tool:tool12', id:'tool12',
  name:'read_files', input:{path:'sample.txt'}, llmRound:12, ...lastScope,
  result:{status:'done', content:'sample'}});
toolRounds.push({roundNum:12, llmRound:12, ...lastScope,
  toolCallId:'tool12', toolName:'read_files', toolArgs:{path:'sample.txt'},
  status:'done', toolContent:'sample'});
const createSurface = () => feature.createConversationSurface(
  document.getElementById('chat'), feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => `<p>${value}</p>`,
  }));
let surface = createSurface();
const snapshots = {};
for (const [phase, transport, status] of [
  ['live', 'live', 'running'], ['terminal', 'live', 'completed'],
  ['replay', 'replay', 'completed'],
]) {
  if (phase === 'replay') { surface.dispose(); surface = createSurface(); }
  const revision = Object.keys(snapshots).length + 1;
  const turn = {
    turnId:'turn-program', conversationId:'conv-a', laneId:'main', ordinal:1,
    actor:'assistant', kind:'reply', runId:'', status,
    currentAttemptId:status === 'running' ? 'attempt-b' : null,
    projection:JSON.parse(JSON.stringify({segments, toolRounds})),
    projectionRevision:revision, createdAt:1, updatedAt:revision,
    settlement:status === 'completed' ? {outcome:'completed'} : {},
  };
  const state = {conversationId:'conv-a', conversationRevision:revision, transport,
    turnsById:{'turn-program':turn}, laneOrder:{main:['turn-program']},
    attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
    liveRoundUsageByTurn:{}};
  const before = JSON.stringify(state);
  const vm = feature.selectConversationViewModel(state);
  surface.render(vm);
  for (const process of surface.root.querySelectorAll('.conversation-history-process')) {
    process.open = true;
    process.dispatchEvent(new dom.window.Event('toggle'));
  }
  surface.root.querySelector('[data-round-action="expandAll"]')?.click();
  const blocks = vm.mainLane.turns[0].blocks.filter(block => block.kind !== 'live-status');
  const label = block => `${block.kind}:${block.kind === 'program'
    ? block.round.programResult.label : block.kind === 'tool'
      ? block.toolCallId : block.markdown}`;
  const labelsById = new Map(blocks.map(block => [block.blockId, label(block)]));
  snapshots[phase] = {
    order:blocks.map(label),
    domOrder:Array.from(surface.root.querySelectorAll('[data-block-kind]'))
      .filter(node => node.dataset.blockKind !== 'live-status')
      .map(node => labelsById.get(node.dataset.blockId)),
    thinkingCount:surface.root.querySelectorAll('.thinking-block').length,
    programCount:surface.root.querySelectorAll('.ptc-card').length,
    uniqueIds:labelsById.size === blocks.length,
    unchanged:before === JSON.stringify(state),
  };
}
surface.dispose();
dom.window.close();
console.log(JSON.stringify(snapshots));
""")
    order = ["thinking:thinking11", "program:program11"]
    if scenario != "next-round":
        order += ["text:between-programs", "program:program11b"]
    order += ["thinking:thinking12", "text:text12", "tool:tool12"]
    expected = {
        "order": order, "domOrder": order, "thinkingCount": 2,
        "programCount": 1 if scenario == "next-round" else 2,
        "uniqueIds": True, "unchanged": True,
    }
    assert result == {phase: expected for phase in ("live", "terminal", "replay")}


def test_thinking_block_collapses_when_streaming_completes(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const classic = feature.createClassicConversationRenderers({
  renderSafeMarkdownHtml:value => `<p>${value}</p>`,
});
const makeViewModel = (revision, terminal) => ({
  conversationId:'conv-a', conversationRevision:revision, transport:'live',
  orphanLanes:[], queue:[], mainLane:{laneId:'main', parentTurnId:null,
    title:'Conversation', kind:'main', turns:[{
      turnId:'turn-1', laneId:'main', parentTurnId:null, ordinal:1,
      actor:'assistant', role:'assistant', kind:'reply',
      status:terminal ? 'completed' : 'running',
      attemptId:'attempt-1', projectionRevision:revision,
      subtreeRevisionKey:String(revision), commandPending:null, finish:null,
      branches:[], actions:[],
      metadata:{translation:{completed:false},
        origin:{initiator:'human'}, orchestration:{runId:'run-1'}},
      source:{createdAt:1, projection:{timestamp:1}},
      blocks:[
        {kind:'thinking', blockId:'thinking:one', identitySource:'contract',
          source:{type:'thinking', blockId:'thinking:one', text:'reason'},
          markdown:'reason', displayMarkdown:'reason', displayMode:'original',
          terminal},
      ],
    }]},
});
const surface = feature.createConversationSurface(
  document.getElementById('chat'), classic,
);
surface.render(makeViewModel(1, false));
const details = surface.root.querySelector(
  '[data-block-id="thinking:one"] .thinking-block');
const openWhileStreaming = details.open;
surface.render(makeViewModel(2, true));
const closedAfterComplete = !details.open
  && details.dataset.state === 'complete';
// A reader-reopened complete block never re-transitions through 'active',
// so later re-renders keep the reader's disclosure choice.
details.open = true;
surface.render(makeViewModel(3, true));
const readerChoiceKept = details.open;
console.log(JSON.stringify({
  openWhileStreaming, closedAfterComplete, readerChoiceKept,
}));
surface.dispose();
dom.window.close();
""")
    assert result == {
        "openWhileStreaming": True,
        "closedAfterComplete": True,
        "readerChoiceKept": True,
    }


def test_waiting_placeholder_yields_to_the_streaming_tail(
        conversation_bundle: Path):
    """The bare default 'waiting' live-status (no phase frame from the
    attempt) must not render underneath a still-streaming thinking/answer
    block — the streaming block IS the live surface. Real phase frames
    (waiting_model with a model label) and non-streaming tails (a running
    tool row) keep their status line."""
    result = _run(conversation_bundle, r"""
const makeTurn = (segments, content) => ({
  turnId:'turn-1', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'', status:'running',
  currentAttemptId:'attempt-1', projection:{segments, content},
  projectionRevision:3, settlement:{},
  createdAt:1, updatedAt:2,
});
const makeState = (turn, livePhase) => ({
  conversationId:'conv-a', conversationRevision:3, transport:'live',
  turnsById:{'turn-1':turn}, laneOrder:{main:['turn-1']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{}, livePhase,
});
const thinkingTail = [
  {type:'thinking', blockId:'thinking:llm-1', text:'reasoning…', llmRound:1},
];
const toolTail = [
  {type:'thinking', blockId:'thinking:llm-1', text:'reasoned', llmRound:1},
  {type:'tool_use', blockId:'tool:call-1', id:'call-1', name:'search',
    input:{}, result:{status:'running', content:''}, llmRound:1},
];
const answerTail = [
  {type:'thinking', blockId:'thinking:llm-1', text:'reasoned', llmRound:1},
  {type:'text', blockId:'text:llm-2', text:'partial answer', llmRound:2},
];
const kinds = (segments, livePhase, content) =>
  feature.selectConversationViewModel(
    makeState(makeTurn(segments, content ?? ''), livePhase))
  .mainLane.turns[0].blocks.map(block => block.kind);
const waitingModelPhase = {
  phase:'waiting_model', detailKey:'stream.phase.waitingForModel',
  detailArgs:{model:'kimi-k3'}, model:'kimi-k3',
};
console.log(JSON.stringify({
  thinkingStreamingNoPhase:kinds(thinkingTail, null),
  thinkingStreamingRealPhase:kinds(thinkingTail, waitingModelPhase),
  toolRunningNoPhase:kinds(toolTail, null),
  answerStreamingNoPhase:kinds(answerTail, null),
}));
""")
    assert result == {
        "thinkingStreamingNoPhase": ["thinking"],
        "thinkingStreamingRealPhase": ["thinking", "live-status"],
        "toolRunningNoPhase": ["thinking", "tool", "live-status"],
        "answerStreamingNoPhase": ["thinking", "text"],
    }


def test_thinking_translation_preview_rides_partial_by_round(
        conversation_bundle: Path):
    """A closed reasoning block's translation streams in through the
    translation activity's partialByRound (keyed by segment blockId) before
    the durable pin (segment.translatedText) lands — and the durable pin
    always wins once present. The preview never becomes translatedMarkdown,
    so toggle semantics keep tracking durable facts only."""
    result = _run(conversation_bundle, r"""
const makeTurn = segments => ({
  turnId:'turn-1', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'', status:'running',
  currentAttemptId:'attempt-1', projection:{segments, content:''},
  projectionRevision:3, settlement:{},
  createdAt:1, updatedAt:2,
});
const makeState = turn => ({
  conversationId:'conv-a', conversationRevision:3, transport:'live',
  turnsById:{'turn-1':turn}, laneOrder:{main:['turn-1']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{},
});
const pick = block => ({
  displayMarkdown:block.displayMarkdown, displayMode:block.displayMode,
  durable:block.translatedMarkdown ?? null,
});
const liveThinking = [
  {type:'thinking', blockId:'thinking:llm-1', text:'reasoning…', llmRound:1},
];
const activity = {status:'pending',
  partialByRound:{'thinking:llm-1':'推理译文'}};
const withActivity = feature.selectConversationViewModel(
  makeState(makeTurn(liveThinking)), {},
  {translationActivityByTurn:new Map([['turn-1', activity]])});
const previewed = pick(withActivity.mainLane.turns[0].blocks[0]);
const durableSegments = [
  {type:'thinking', blockId:'thinking:llm-1', text:'reasoning…', llmRound:1,
    translatedText:'已钉译文'},
];
const pinned = feature.selectConversationViewModel(
  makeState(makeTurn(durableSegments)), {},
  {translationActivityByTurn:new Map([['turn-1', activity]])});
const pinnedPick = pick(pinned.mainLane.turns[0].blocks[0]);
const noActivity = feature.selectConversationViewModel(
  makeState(makeTurn(liveThinking)));
const original = pick(noActivity.mainLane.turns[0].blocks[0]);
console.log(JSON.stringify({previewed, pinnedPick, original}));
""")
    assert result == {
        "previewed": {
            "displayMarkdown": "推理译文",
            "displayMode": "translated",
            "durable": None,
        },
        "pinnedPick": {
            "displayMarkdown": "已钉译文",
            "displayMode": "translated",
            "durable": "已钉译文",
        },
        "original": {
            "displayMarkdown": "reasoning…",
            "displayMode": "original",
            "durable": None,
        },
    }

def test_native_takeover_removes_opaque_children_and_sidecar_ids_are_unique(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const makeTurn = (revision, projection) => ({
  turnId:'turn-1', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'', status:'completed',
  currentAttemptId:null, projection, projectionRevision:revision,
  settlement:{outcome:'completed'}, createdAt:1, updatedAt:revision,
});
const makeState = turn => ({conversationId:'conv-a', conversationRevision:turn.projectionRevision,
  transport:'live', turnsById:{'turn-1':turn}, laneOrder:{main:['turn-1']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{}});
const scheduled = [];
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
  }),
});
const legacy = makeTurn(1, {segments:[{type:'text', blockId:'text:terminal',
  text:'old', terminal:true}], orchestration:{runId:'rich'}});
controller.render({id:'conv-a'}, makeState(legacy));
scheduled.shift()();
const first = document.querySelector('[data-turn-id="turn-1"]');
const native = makeTurn(2, {segments:[{type:'text', blockId:'text:terminal',
  text:'new', terminal:true}], model:'m'});
controller.render({id:'conv-a'}, makeState(native));
scheduled.shift()();
const second = document.querySelector('[data-turn-id="turn-1"]');
const collision = makeTurn(3, {segments:[{type:'text', blockId:'attachments~2',
  text:'body', terminal:true}], images:[{attachmentId:'one'}]});
const ids = feature.selectTurnBlocks(collision).map(block => block.blockId);
const result = {
  sameTurn:first === second,
  opaqueRemoved:!document.getElementById('opaque'),
  nativeText:second.querySelector('.md-content').textContent,
  ids,
  unique:new Set(ids).size === ids.length,
};
controller.dispose();
dom.window.close();
console.log(JSON.stringify(result));
""")
    assert result == {
        "sameTurn": True,
        "opaqueRemoved": True,
        "nativeText": "new",
        "ids": ["attachments", "attachments~2"],
        "unique": True,
    }


def test_tool_segment_joins_rich_round_and_keeps_its_keyed_dom(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const toolSegment = {type:'tool_use', blockId:'tool:call-1', id:'call-1',
  name:'search', input:{q:'typed'}, llmRound:0,
  result:{status:'done', content:'result'}};
const gatewaySegment = {type:'tool_use', blockId:'tool:gateway-1', id:'gateway-1',
  name:'execute_tools', input:{calls:[]}, llmRound:0,
  result:{status:'done', content:'{"status":"ok"}'}};
const richRound = {roundNum:1, llmRound:0, toolCallId:'call-1', toolName:'search',
  toolArgs:{q:'typed'}, status:'done', results:[{title:'Rich result'}]};
const gatewayRound = {roundNum:900, llmRound:0, toolCallId:'gateway-1',
  toolName:'execute_tools', status:'done'};
const textSegment = text => ({type:'text', blockId:'text:terminal', text,
  deliverable:true, terminal:true});
const makeTurn = (revision, text) => ({
  turnId:'turn-tool', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'', status:'completed', currentAttemptId:null,
  projection:{segments:[gatewaySegment, toolSegment, textSegment(text)],
    toolRounds:[gatewayRound, richRound],
    content:text, model:'model-a'}, projectionRevision:revision,
  settlement:{outcome:'completed'}, createdAt:1, updatedAt:revision,
});
const makeState = turn => ({conversationId:'conv-a', conversationRevision:turn.projectionRevision,
  transport:'live', turnsById:{'turn-tool':turn}, laneOrder:{main:['turn-tool']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{}});
const scheduled = [];

let toolRenders = 0;
let joinedTitle = '';
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
    renderToolBlockHtml(block) {
      toolRenders += 1;
      joinedTitle = block.round.results[0].title;
      return `<details open><summary>${block.name}</summary>${joinedTitle}</details>`;
    },
  }),
});
const conversation = {id:'conv-a'};
controller.render(conversation, makeState(makeTurn(1, 'one')));
scheduled.shift()();
for (const process of document.querySelectorAll('.conversation-history-process')) { process.open=true; process.dispatchEvent(new document.defaultView.Event('toggle')); }
// Reveal historical content before exercising its controls.
document.querySelector('[data-round-action="expandAll"]')?.click();
const first = document.querySelector('[data-block-id="tool:call-1"]');
controller.render(conversation, makeState(makeTurn(2, 'two')));
scheduled.shift()();
for (const process of document.querySelectorAll('.conversation-history-process')) { process.open=true; process.dispatchEvent(new document.defaultView.Event('toggle')); }
const second = document.querySelector('[data-block-id="tool:call-1"]');
const payload = {
  native:Boolean(
    feature.selectConversationViewModel(makeState(makeTurn(3, 'three'))).mainLane.turns[0]),

  joinedTitle,
  toolRenders,
  sameNode:first === second,
  wrapperHidden:!document.querySelector('[data-block-id="tool:gateway-1"]'),
  blockIds:feature.selectConversationViewModel(makeState(makeTurn(4, 'four')))
    .mainLane.turns[0].blocks.map(block => block.blockId),
  open:second.querySelector('details').open,
  text:document.querySelector('[data-block-id="text:terminal"]').textContent,
};
controller.dispose();
dom.window.close();
console.log(JSON.stringify(payload));
""")
    assert result == {
        "native": True,

        "joinedTitle": "Rich result",
        "toolRenders": 1,
        "sameNode": True,
        "wrapperHidden": True,
        "blockIds": ["tool:call-1", "text:terminal"],
        "open": True,
        "text": "two",
    }



def test_reused_tool_call_id_joins_each_image_segment_to_its_own_round(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const segment = (blockId, crop) => ({
  type:'tool_use', blockId, id:'reused-image-id', name:'inspect_image',
  input:{crop}, llmRound:0, result:{status:'done', content:'image'},
});
const richRound = (roundNum, crop, uri) => ({
  roundNum, llmRound:0, toolCallId:'reused-image-id', toolName:'inspect_image',
  toolArgs:{crop}, status:'done',
  results:[{imageDataUris:[{uri, filename:`crop-${roundNum}.jpg`}]}],
});
const turn = {
  turnId:'turn-reused-image', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'', status:'completed',
  currentAttemptId:null, projectionRevision:1,
  projection:{
    segments:[
      segment('tool:reused-image-id', [0, 0, 0.5, 0.5]),
      segment('tool:reused-image-id~2', [0.5, 0.5, 1, 1]),
    ],
    toolRounds:[
      richRound(1, [0, 0, 0.5, 0.5], 'data:image/jpeg;base64,FIRST'),
      richRound(2, [0.5, 0.5, 1, 1], 'data:image/jpeg;base64,SECOND'),
    ],
  },
  settlement:{outcome:'completed'}, createdAt:1, updatedAt:1,
};
const blocks = feature.selectTurnBlocks(turn).filter(block => block.kind === 'tool');
console.log(JSON.stringify({
  ids:blocks.map(block => block.blockId),
  crops:blocks.map(block => block.round.toolArgs.crop),
  uris:blocks.map(block => block.round.results[0].imageDataUris[0].uri),
}));
""")
    assert result == {
        "ids": ["tool:reused-image-id", "tool:reused-image-id~2"],
        "crops": [[0, 0, 0.5, 0.5], [0.5, 0.5, 1, 1]],
        "uris": [
            "data:image/jpeg;base64,FIRST",
            "data:image/jpeg;base64,SECOND",
        ],
    }

def test_swarm_rich_panel_preserves_live_nodes_and_reader_disclosure(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const renderers = feature.createClassicConversationRenderers({
  renderSafeMarkdownHtml:value => value,
  renderToolBlockHtml(block) {
    const agents = block.round.agents.map(agent =>
      `<article class="sw-agent" data-agent-id="${agent.id}">`
      + `<button class="sw-a-header">${agent.role}</button>`
      + (block.round.agents.length > 1 ? `<div class="sw-a-tools">read_files</div>` : '')
      + `<details class="sw-a-task"><summary>Task</summary><pre>Instructions</pre></details>`
      + `<span class="sw-a-preview">${agent.preview}</span></article>`
    ).join('');
    return `<div data-prn="1" data-prn-kind="swarm">`
      + `<section class="sw-panel sw-active">${agents}</section></div>`;
  },
});
const makeViewModel = (revision, preview, includeSecond) => ({
  conversationId:'conv-a', conversationRevision:revision, transport:'live',
  orphanLanes:[], queue:[], mainLane:{laneId:'main', parentTurnId:null,
    title:'Conversation', kind:'main', turns:[{
      turnId:'turn-swarm', laneId:'main', parentTurnId:null, ordinal:1,
      actor:'assistant', role:'assistant', kind:'reply', status:'running',
      attemptId:'attempt-1', projectionRevision:revision,
      subtreeRevisionKey:String(revision), commandPending:null, finish:null,
      branches:[], actions:[], metadata:{translation:{completed:false},
        origin:{initiator:'human'}, orchestration:{runId:'run-1'}},
      source:{createdAt:1, projection:{}}, blocks:[{
        kind:'tool', blockId:'tool:swarm', identitySource:'contract',
        source:{type:'tool_use', blockId:'tool:swarm', id:'swarm', name:'swarm'},
        toolCallId:'swarm', name:'swarm', input:{},
        result:{status:'running'}, round:{status:'searching', agents:[
          {id:'agent-a', role:'researcher', preview},
          ...(includeSecond
            ? [{id:'agent-b', role:'analyst', preview:'new agent'}] : []),
        ]},
      }],
    }]},
});
const surface = feature.createConversationSurface(
  document.getElementById('chat'), renderers,
);
surface.render(makeViewModel(1, 'Looking', false));
const toolBlock = surface.root.querySelector('[data-block-id="tool:swarm"]');
const panel = toolBlock.querySelector('.sw-panel');
const agent = panel.querySelector('[data-agent-id="agent-a"]');
const header = agent.querySelector('.sw-a-header');
panel.classList.add('sw-collapsed');
agent.classList.add('sw-a-open');
panel._identityMark = 'panel';
agent._identityMark = 'agent';
header.focus();
const task = agent.querySelector('.sw-a-task');
task.open = true;
task.scrollTop = 20;

surface.render(makeViewModel(2, 'Looking deeper', true));
const nextPanel = toolBlock.querySelector('.sw-panel');
const nextAgent = nextPanel.querySelector('[data-agent-id="agent-a"]');
console.log(JSON.stringify({
  samePanel:panel === nextPanel && nextPanel._identityMark === 'panel',
  sameAgent:agent === nextAgent && nextAgent._identityMark === 'agent',
  sameTask:task === nextAgent.querySelector('.sw-a-task'),
  taskOpen:nextAgent.querySelector('.sw-a-task').open,
  taskScroll:nextAgent.querySelector('.sw-a-task').scrollTop,
  panelCollapsed:nextPanel.classList.contains('sw-collapsed'),
  agentOpen:nextAgent.classList.contains('sw-a-open'),
  focusPreserved:document.activeElement === header,
  preview:nextAgent.querySelector('.sw-a-preview').textContent,
  newAgent:Boolean(nextPanel.querySelector('[data-agent-id="agent-b"]')),
}));
surface.dispose();
dom.window.close();
""")
    assert result == {
        "samePanel": True,
        "sameAgent": True,
        "panelCollapsed": True,
        "sameTask": True,
        "taskOpen": True,
        "taskScroll": 20,
        "agentOpen": True,
        "focusPreserved": True,
        "preview": "Looking deeper",
        "newAgent": True,
    }


def test_childless_toolscript_failure_renders_program_parent_before_diagnostic(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const programRound = {
  roundNum:8800000, llmRound:8, _programSynthetic:true,
  _programCallId:'gateway-1', programBackend:'local_toolscript',
  programSource:'execute_program', programStatus:'error', status:'error',
  programCode:"return {status:'ok' result};",
  programResult:{code:'syntax_error', message:"expected '}', got 'r'", offset:21},
  childCallIds:[], childToolNames:[],
};
const gatewaySegment = {
  type:'tool_use', blockId:'tool:gateway-1', id:'gateway-1',
  name:'execute_tools', input:{program:programRound.programCode}, llmRound:8,
  result:{status:'error', content:'program rejected'},
};
const textSegment = {
  type:'text', blockId:'text:round-8', text:'Checking compacted artifacts',
  llmRound:8, terminal:true,
};
const makeTurn = revision => ({
  turnId:'turn-program', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'', status:'completed',
  currentAttemptId:null, projectionRevision:revision,
  settlement:{outcome:'completed'}, createdAt:1, updatedAt:revision,
  projection:{segments:[textSegment, gatewaySegment], toolRounds:[programRound],
    activityTimeline:{blockId:'activity-timeline', version:1, droppedCount:0,
      entries:[{id:'program-failed', spanId:'program:gateway-1', seq:1,
        occurredAt:10, kind:'tool', status:'failed', severity:'error', count:1,
        summary:'ToolScript failed', toolName:'ToolScript', llmRound:8,
        reasonCode:'syntax_error'}]}},
});
const makeState = turn => ({conversationId:'conv-a', conversationRevision:turn.projectionRevision,
  transport:'live', turnsById:{'turn-program':turn}, laneOrder:{main:['turn-program']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{}});
const scheduled = [];
let programRenders = 0;
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
    renderToolBlockHtml(block) {
      if (block.kind !== 'program') throw new Error('unexpected tool block');
      programRenders += 1;
      return `<section class="program-card"><code>${block.round.programCode}</code>`
        + `<strong>${block.round.programStatus}</strong></section>`;
    },
  }),
});
const conversation = {id:'conv-a'};
controller.render(conversation, makeState(makeTurn(1)));
scheduled.shift()();
for (const process of document.querySelectorAll('.conversation-history-process')) { process.open=true; process.dispatchEvent(new document.defaultView.Event('toggle')); }
const first = document.querySelector('[data-block-id="program:gateway-1"]');
controller.render(conversation, makeState(makeTurn(2)));
scheduled.shift()();
for (const process of document.querySelectorAll('.conversation-history-process')) { process.open=true; process.dispatchEvent(new document.defaultView.Event('toggle')); }
const second = document.querySelector('[data-block-id="program:gateway-1"]');
const blocks = feature.selectTurnBlocks(makeTurn(3));
console.log(JSON.stringify({
  kinds:blocks.map(block => block.kind),
  ids:blocks.map(block => block.blockId),
  code:blocks.find(block => block.kind === 'program').round.programCode,
  status:blocks.find(block => block.kind === 'program').round.programStatus,
  programRenders,
  sameNode:first === second,
  programText:second.textContent,
  wrapperHidden:!document.querySelector('[data-block-id="tool:gateway-1"]'),
  diagnosticAfter:second.nextElementSibling.dataset.blockId,
}));
controller.dispose();
dom.window.close();
""")
    assert result == {
        "kinds": ["text", "program", "activity-event"],
        "ids": [
            "text:round-8", "program:gateway-1",
            "activity:program-failed",
        ],
        "code": "return {status:'ok' result};",
        "status": "error",
        "programRenders": 1,
        "sameNode": True,
        "programText": "return {status:'ok' result};error",
        "wrapperHidden": True,
        "diagnosticAfter": "activity:program-failed",
    }


def test_cloned_snapshots_keep_disclosures_focus_and_inner_nodes_stable(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
let markdownRenders = 0;
let toolRenders = 0;
let headerRenders = 0;
const timestamp = Date.UTC(2026, 7, 26, 15, 31);
const classic = feature.createClassicConversationRenderers({
  renderSafeMarkdownHtml(value) {
    markdownRenders += 1;
    return `<p>${value}</p>`;
  },
  renderToolBlockHtml(block) {
    toolRenders += 1;
    return `<div class="ptool-panel collapsed" data-collapsible="true">`
      + `<button class="ptool-panel-header" aria-expanded="false">Tools</button>`
      + `<div class="ptool-panel-body"><details class="native-tool">`
      + `<summary>${block.name}</summary><div>${block.result.content}</div>`
      + `</details></div></div>`;
  },
  formatTimestamp:() => '2026年8月26日 15:31',
});
const renderers = {
  ...classic,
  renderTurnHeader(node, turn, context) {
    headerRenders += 1;
    classic.renderTurnHeader(node, turn, context);
  },
};
const makeViewModel = (revision, thinking, result) => ({
  conversationId:'conv-a', conversationRevision:revision, transport:'live',
  orphanLanes:[], queue:[], mainLane:{laneId:'main', parentTurnId:null,
    title:'Conversation', kind:'main', turns:[{
      turnId:'turn-1', laneId:'main', parentTurnId:null, ordinal:1,
      actor:'assistant', role:'assistant', kind:'reply', status:'running',
      attemptId:'attempt-1', projectionRevision:revision,
      subtreeRevisionKey:String(revision), commandPending:null, finish:null,
      branches:[], actions:[{action:'copy', disabled:false}],
      metadata:{translation:{completed:false},
        origin:{initiator:'human'}, orchestration:{runId:'run-1'}},
      source:{createdAt:timestamp, projection:{timestamp}},
      blocks:[
        {kind:'thinking', blockId:'thinking:one', identitySource:'contract',
          source:{type:'thinking', blockId:'thinking:one', text:thinking},
          markdown:thinking, displayMarkdown:thinking, displayMode:'original',
          terminal:false},
        {kind:'tool', blockId:'tool:one', identitySource:'contract',
          source:{type:'tool_use', blockId:'tool:one', id:'one', name:'search'},
          toolCallId:'one', name:'search', input:{q:'stable'},
          result:{status:'done', content:result},
          round:{roundNum:1, toolCallId:'one', toolName:'search',
            status:'done', results:[{title:result}]}},
        {kind:'text', blockId:'text:terminal', identitySource:'contract',
          source:{type:'text', blockId:'text:terminal', text:'answer'},
          markdown:'answer', displayMarkdown:'answer', displayMode:'original',
          deliverable:true, terminal:true, resumable:false},
      ],
    }]},
});
const surface = feature.createConversationSurface(
  document.getElementById('chat'), renderers,
);
surface.render(makeViewModel(1, 'reason', 'result one'));
const thinkingBlock = surface.root.querySelector('[data-block-id="thinking:one"]');
const thinkingDetails = thinkingBlock.querySelector('.thinking-block');
const thinkingSummary = thinkingBlock.querySelector('.thinking-header');
const thinkingBody = thinkingBlock.querySelector('.thinking-text');
const toolBlock = surface.root.querySelector('[data-block-id="tool:one"]');
const firstToolDetails = toolBlock.querySelector('details');
const firstToolPanel = toolBlock.querySelector('.ptool-panel');

const firstToolSummary = firstToolDetails.querySelector('summary');
const firstToolContent = firstToolDetails.querySelector('div');
const firstToolText = firstToolContent.firstChild;
const firstToolHeader = firstToolPanel.querySelector('.ptool-panel-header');
thinkingDetails.open = true;
firstToolDetails.open = true;
firstToolPanel.classList.remove('collapsed');
firstToolPanel.querySelector('.ptool-panel-header')
  .setAttribute('aria-expanded', 'true');
thinkingSummary.focus();

// A full JSON snapshot recreates every object while preserving semantics.
surface.render(JSON.parse(JSON.stringify(makeViewModel(2, 'reason', 'result one'))));
const clonedThinkingDetails = thinkingBlock.querySelector('.thinking-block');
const clonedThinkingBody = thinkingBlock.querySelector('.thinking-text');
const clonedToolDetails = toolBlock.querySelector('details');

// A real thinking delta updates only its markdown body.
surface.render(makeViewModel(3, 'reason grows', 'result one'));
const updatedThinkingDetails = thinkingBlock.querySelector('.thinking-block');
const updatedThinkingBody = thinkingBlock.querySelector('.thinking-text');
const thinkingFocusPreserved = document.activeElement === thinkingSummary;

// A rich-tool delta must update the output IN retained nodes, not freeze the
// old HTML or replace a reader's disclosure, focused control, or scroll box.
clonedToolDetails.scrollTop = 19;
clonedToolDetails.querySelector('summary').focus();
surface.render(makeViewModel(4, 'reason grows', 'result two'));
const updatedToolDetails = toolBlock.querySelector('details');
const updatedToolPanel = toolBlock.querySelector('.ptool-panel');
const time = surface.root.querySelector('.message-time');
console.log(JSON.stringify({
  thinkingDetailsStable:thinkingDetails === clonedThinkingDetails
    && thinkingDetails === updatedThinkingDetails,
  thinkingBodyStable:thinkingBody === clonedThinkingBody
    && thinkingBody === updatedThinkingBody,
  thinkingOpen:updatedThinkingDetails.open,
  focusPreserved:thinkingFocusPreserved,
  thinkingText:updatedThinkingBody.textContent,
  clonedToolNodeStable:firstToolDetails === clonedToolDetails,
  changedToolNodesStable:firstToolDetails === updatedToolDetails
    && firstToolPanel === updatedToolPanel
    && firstToolSummary === updatedToolDetails.querySelector('summary')
    && firstToolContent === updatedToolDetails.querySelector('div')
    && firstToolText === firstToolContent.firstChild
    && firstToolHeader === updatedToolPanel.querySelector('.ptool-panel-header'),
  changedToolContent:firstToolContent.textContent,
  changedToolStillOpen:updatedToolDetails.open,
  changedToolFocusPreserved:document.activeElement === firstToolSummary,
  changedToolScrollPreserved:updatedToolDetails.scrollTop,
  changedToolPanelExpanded:!updatedToolPanel.classList.contains('collapsed')
    && updatedToolPanel.querySelector('.ptool-panel-header')
      .getAttribute('aria-expanded') === 'true',
  markdownRenders,
  toolRenders,
  headerRenders,
  timeTag:time.tagName,
  timeIsShort:/^\d{2}:\d{2}$/.test(time.textContent),
  timeTitle:time.title,
  timeHasMachineValue:Boolean(time.dateTime),
}));
surface.dispose();
dom.window.close();
""")
    assert result == {
        "thinkingDetailsStable": True,
        "thinkingBodyStable": True,
        "thinkingOpen": True,
        "focusPreserved": True,
        "thinkingText": "reason grows",
        "clonedToolNodeStable": True,
        "changedToolNodesStable": True,
        "changedToolContent": "result two",
        "changedToolStillOpen": True,
        "changedToolFocusPreserved": True,
        "changedToolScrollPreserved": 19,
        "changedToolPanelExpanded": True,
        "markdownRenders": 3,
        "toolRenders": 2,
        "headerRenders": 1,
        "timeTag": "TIME",
        "timeIsShort": True,
        "timeTitle": "2026年8月26日 15:31",
        "timeHasMachineValue": True,
    }


def test_authoritative_tool_cards_stay_single_layer(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const classic = feature.createClassicConversationRenderers({
  renderSafeMarkdownHtml:value => value,
  renderToolBlockHtml:block => '<details class="ptool-result-block" '
    + 'data-tool-result-authority="toolContent"><summary>History</summary>'
    + '<pre>' + block.round.toolContent + '</pre></details>',
});
const makeTurn = (revision, firstResult) => ({
  turnId:'turn-tools', conversationId:'conv-a', laneId:'main', parentTurnId:null,
  ordinal:1, actor:'assistant', kind:'reply', runId:'', status:'completed',
  currentAttemptId:null, projectionRevision:revision,
  settlement:{outcome:'completed'}, createdAt:1, updatedAt:revision,
  projection:{
    segments:[
      {type:'tool_use', blockId:'tool:reused', id:'reused', name:'history',
        input:{before:4}, result:{status:'done', content:'STALE_SEGMENT_ONE'}},
      {type:'tool_use', blockId:'tool:reused~2', id:'reused', name:'history',
        input:{before:8}, result:{status:'done', content:'STALE_SEGMENT_TWO'}},
    ],
    toolRounds:[
      {roundNum:1, toolCallId:'reused', toolName:'history', status:'done',
        toolContent:firstResult, results:[{summary:'UNRELATED_RICH_METADATA'}]},
      {roundNum:2, toolCallId:'reused', toolName:'history', status:'done',
        toolContent:'BACKEND_TRUTH_TWO', results:[{summary:'UNRELATED_RICH_METADATA'}]},
    ],
  },
});
const state = turn => ({
  conversationId:'conv-a', conversationRevision:turn.projectionRevision,
  transport:'live', turnsById:{'turn-tools':turn}, laneOrder:{main:['turn-tools']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{},
});
const surface = feature.createConversationSurface(
  document.getElementById('chat'), classic,
);
surface.render(feature.selectConversationViewModel(
  state(makeTurn(1, 'BACKEND_TRUTH_ONE')),
));
const firstBlock = surface.root.querySelector('[data-block-id="tool:reused"]');
const secondBlock = surface.root.querySelector('[data-block-id="tool:reused~2"]');
const firstDetails = firstBlock.querySelector('details');
const secondDetails = secondBlock.querySelector('details');
const initialFirstText = firstDetails.querySelector('pre').textContent;
const initialSecondText = secondDetails.querySelector('pre').textContent;

surface.render(feature.selectConversationViewModel(
  state(makeTurn(2, 'BACKEND_TRUTH_ONE_UPDATED')),
));
const updatedDetails = firstBlock.querySelector('details');
console.log(JSON.stringify({
  sources:[firstDetails.dataset.toolResultAuthority,
    secondDetails.dataset.toolResultAuthority],
  firstText:initialFirstText,
  secondText:initialSecondText,
  detailsPerCard:[firstBlock.querySelectorAll('details').length,
    secondBlock.querySelectorAll('details').length],
  summaries:[firstDetails.querySelector('summary').textContent,
    secondDetails.querySelector('summary').textContent],
  updatedText:updatedDetails.querySelector('pre').textContent,
  nestedBackendDisclosure:!!surface.root.querySelector(
    '.conversation-tool__authoritative-result'),
}));
surface.dispose();
dom.window.close();
""")
    assert result == {
        "sources": ["toolContent", "toolContent"],
        "firstText": "BACKEND_TRUTH_ONE",
        "secondText": "BACKEND_TRUTH_TWO",
        "detailsPerCard": [1, 1],
        "summaries": ["History", "History"],
        "updatedText": "BACKEND_TRUTH_ONE_UPDATED",
        "nestedBackendDisclosure": False,
    }


def test_activity_events_anchor_inline_at_their_call_and_round(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const toolSegment = {type:'tool_use', blockId:'tool:call-1', id:'call-1',
  name:'search', input:{q:'typed'}, llmRound:0,
  result:{status:'failed', content:'tool timed out'}};
const textSegment = {type:'text', blockId:'text:terminal', text:'answer',
  deliverable:true, terminal:true, llmRound:1};
const richRound = {roundNum:1, llmRound:0, toolCallId:'call-1', toolName:'search',
  toolArgs:{q:'typed'}, status:'failed',
  result:{status:'failed', content:'tool timed out'}};
const entries = [
  {id:'tool-fail', spanId:'tool:call-1', parentSpanId:'model:1', seq:1,
    occurredAt:1005, startedAt:1000, endedAt:1005, durationMs:5,
    kind:'tool', status:'failed', severity:'error', count:1,
    summary:'search failed', detail:'tool timed out', reasonCode:'timeout',
    toolName:'search', toolCallId:'call-1', llmRound:0},
  {id:'schema-1', spanId:'schema:write_file', parentSpanId:'model:1', seq:2,
    occurredAt:1010, kind:'tool', status:'skipped', severity:'warning', count:1,
    summary:'⚠️ write_file schema rejected', detail:'required description is missing',
    toolName:'write_file', reasonCode:'invalid_schema', action:'omitted',
    llmRound:0},
  {id:'tool-1', spanId:'tool:call-1:ok', parentSpanId:'model:1', seq:3,
    occurredAt:1015, kind:'tool', status:'succeeded', severity:'info', count:1,
    summary:'search completed', toolName:'search', toolCallId:'call-1',
    llmRound:0},
  {id:'gateway-shell', spanId:'tool:gateway-1', parentSpanId:'model:1', seq:3,
    occurredAt:1016, kind:'tool', status:'failed', severity:'error', count:1,
    summary:'execute_tools failed', toolName:'execute_tools',
    toolCallId:'gateway-1', llmRound:0},
  {id:'model-1', spanId:'model:2', seq:4, occurredAt:1020, startedAt:1000,
    endedAt:1020, durationMs:20, kind:'model', status:'failed', severity:'error',
    count:1, summary:'Kimi K3 request failed', model:'kimi-k3',
    providerId:'example-corp', keyHint:'2208********5409', statusCode:400,
    llmRound:1},
  {id:'switch-1', spanId:'fallback:1', seq:5, occurredAt:1030,
    kind:'model', status:'switched', severity:'warning', count:1,
    summary:'Switched from Kimi K3 to GLM 5.3', fromModel:'kimi-k3',
    toModel:'glm-5.3', llmRound:1},
];
const turn = {
  turnId:'turn-activity', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'', status:'completed', currentAttemptId:null,
  projection:{
    segments:[toolSegment, textSegment],
    toolRounds:[richRound], content:'answer', model:'glm-5.3',
    fallbackFrom:'kimi-k3', fallbackModel:'glm-5.3', fallbackReason:'provider failure',
    activityTimeline:{blockId:'activity-timeline', version:1, droppedCount:5, entries},
  },
  projectionRevision:1, settlement:{outcome:'completed'}, createdAt:1, updatedAt:2,
};
const state = {conversationId:'conv-a', conversationRevision:1, transport:'live',
  turnsById:{'turn-activity':turn}, laneOrder:{main:['turn-activity']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{}};
const vmTurn = feature.selectConversationViewModel(state).mainLane.turns[0];
let richToolRenders = 0;
const scheduled = [];
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
    formatTimestamp:() => '12:00:00',
    localizedText:(key, fallback) => key === 'stream.retryReason.waitingBackoff'
      ? '等待模型（错误退避中，非限流）' : fallback,
    renderToolBlockHtml(block) {
      richToolRenders += 1;
      return `<details class="native-tool"><summary>${block.name}</summary>`
        + `${block.round.result.content}</details>`;
    },
  }),
});
controller.render({id:'conv-a'}, state);
scheduled.shift()();
for (const process of document.querySelectorAll('.conversation-history-process')) { process.open=true; process.dispatchEvent(new document.defaultView.Event('toggle')); }
const firstFailureRow = document.querySelector('[data-block-id="activity:model-1"]');
const nextState = JSON.parse(JSON.stringify(state));
nextState.conversationRevision = 2;
nextState.turnsById['turn-activity'].projectionRevision = 2;
nextState.turnsById['turn-activity'].updatedAt = 3;
nextState.turnsById['turn-activity'].projection.activityTimeline.entries.push({
  id:'retry-1', spanId:'status:retry', seq:6, occurredAt:1040,
  kind:'status', status:'waiting', severity:'warning', count:2,
  summary:'Retrying Kimi K3…', summaryKey:'stream.phase.retryReason',
  summaryArgs:{reason:'Waiting for model (retry backoff)',
    reasonKey:'stream.retryReason.waitingBackoff', model:'kimi-k3', attempt:20},
  model:'kimi-k3',
  reasonCode:'stream.retryReason.waitingBackoff', llmRound:1,
});
controller.render({id:'conv-a'}, nextState);
scheduled.shift()();
for (const process of document.querySelectorAll('.conversation-history-process')) { process.open=true; process.dispatchEvent(new document.defaultView.Event('toggle')); }
const block = id => document.querySelector(`[data-block-id="${id}"]`);
const payload = {
  blockIds:vmTurn.blocks.map(block => block.blockId),
  domOrder:[...document.querySelectorAll('[data-block-id]')]
    .map(node => node.dataset.blockId),
  fallbackInTimeline:vmTurn.metadata.fallbackInTimeline,
  toolBlocks:document.querySelectorAll('[data-block-id="tool:call-1"]').length,
  errorRowUnderCall:block('tool:call-1').nextElementSibling.dataset.blockId,
  schemaAfterErrorRow:block('activity:tool-fail').nextElementSibling.dataset.blockId,
  failureAfterRoundText:block('activity:model-1').previousElementSibling.dataset.blockId,
  switchAfterFailure:block('activity:switch-1').previousElementSibling.dataset.blockId,
  retryAfterSwitch:block('activity:retry-1').previousElementSibling.dataset.blockId,
  timelineBlocks:document.querySelectorAll('[data-block-id="activity-timeline"]').length,
  fallbackTags:document.querySelectorAll('.fallback-tag').length,
  infoRowsHidden:!document.querySelector('[data-activity-id="tool-1"]'),
  gatewayShellHidden:!document.querySelector('[data-activity-id="gateway-shell"]'),
  failureRowStable:firstFailureRow === block('activity:model-1'),
  schemaVisible:document.body.textContent.includes('required description is missing'),
  schemaSummary:block('activity:schema-1')
    .querySelector('.activity-event__summary').textContent,
  failureFacts:block('activity:model-1')
    .querySelector('.activity-event__facts').textContent,
  activityMarkerSvgs:document.querySelectorAll('.activity-event__marker svg').length,
  switchVisible:document.body.textContent.includes('Switched from Kimi K3 to GLM 5.3'),
  retryVisible:document.body.textContent.includes('Retrying Kimi K3'),
  retryFacts:block('activity:retry-1')
    .querySelector('.activity-event__facts').textContent,
  richToolRenders,
  richToolText:block('tool:call-1').textContent,
  droppedRowAbsent:!block('activity:dropped'),
};
controller.dispose();
dom.window.close();
console.log(JSON.stringify(payload));
""")
    assert result == {
        "blockIds": [
            "tool:call-1",
            "activity:tool-fail",
            "activity:schema-1",
            "activity:gateway-shell",
            "text:terminal",
            "activity:model-1",
            "activity:switch-1",
        ],
        "domOrder": [
            "text:terminal",
            "tool:call-1",
            "activity:tool-fail",
            "activity:schema-1",
            "activity:gateway-shell",
            "activity:model-1",
            "activity:switch-1",
            "activity:retry-1",
        ],
        "fallbackInTimeline": True,
        "toolBlocks": 1,
        "errorRowUnderCall": "activity:tool-fail",
        "schemaAfterErrorRow": "activity:schema-1",
        "failureAfterRoundText": "activity:gateway-shell",
        "switchAfterFailure": "activity:model-1",
        "retryAfterSwitch": "activity:switch-1",
        "timelineBlocks": 0,
        "fallbackTags": 0,
        "infoRowsHidden": True,
        # A missing gateway payload cannot erase the only failure evidence.
        "gatewayShellHidden": False,
        "failureRowStable": True,
        "schemaVisible": True,
        "schemaSummary": "write_file schema rejected",
        "failureFacts": "kimi-k3 · example-corp · 2208********5409 · HTTP 400",
        "activityMarkerSvgs": 6,
        "switchVisible": True,
        "retryVisible": True,
        "retryFacts": "kimi-k3",
        "droppedRowAbsent": True,
        # Appending an unrelated activity row must reuse the stable rich-tool
        # block instead of paying its renderer a second time.
        "richToolRenders": 1,
        "richToolText": "searchtool timed out",
    }


def test_compaction_receipt_is_the_only_info_event_rendered_as_a_turn_block(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const entries = [{
  id:'compact-1', spanId:'compaction:archive-a', seq:2, occurredAt:240251,
  startedAt:240000, endedAt:240251, durationMs:251, kind:'status',
  status:'succeeded', severity:'info', count:1,
  summary:'Context compacted', summaryKey:'activity.compaction.succeeded',
  phase:'compacting', reasonCode:'context_compaction', archiveId:'archive-a',
  trigger:'working_set', tokenCountKind:'estimated',
  tokensBefore:180000, tokensAfter:42000, messagesBefore:42, messagesAfter:11,
  reductionPercent:77, model:'kimi-k3', llmRound:0,
}, {
  id:'tool-info', spanId:'tool:call-a', seq:1, occurredAt:1010,
  kind:'tool', status:'succeeded', severity:'info', count:1,
  summary:'search completed', toolName:'search', toolCallId:'call-a', llmRound:0,
}];
const turn = {
  turnId:'turn-compact-activity', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'', status:'completed', currentAttemptId:null,
  projection:{content:'answer', segments:[{type:'text', blockId:'text:terminal',
    text:'answer', deliverable:true, terminal:true, llmRound:0}], toolRounds:[],
    activityTimeline:{blockId:'activity-timeline', version:1, entries},
    timingTrace:{version:1, taskId:'compact-task', spans:[{
      kind:'compaction', status:'done', tStart:1000, tEnd:240253,
    }]}},
  projectionRevision:1, settlement:{outcome:'completed'}, createdAt:1, updatedAt:2,
};
const state = {conversationId:'conv-a', conversationRevision:1, transport:'live',
  turnsById:{'turn-compact-activity':turn}, laneOrder:{main:['turn-compact-activity']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{}};
const scheduled = [];
const intents = [];
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
  }),
  onIntent:intent => intents.push(intent),
});
controller.render({id:'conv-a'}, state);
scheduled.shift()();
for (const process of document.querySelectorAll('.conversation-history-process')) { process.open=true; process.dispatchEvent(new document.defaultView.Event('toggle')); }
const vmTurn = feature.selectConversationViewModel(state).mainLane.turns[0];
const row = document.querySelector('[data-activity-id="compact-1"]');
const before = row.querySelector('.compaction-token-stage.is-before');
const after = row.querySelector('.compaction-token-stage.is-after');
row.querySelector('[data-conversation-action="open-compaction"]').click();
const span = turn.projection.timingTrace.spans[0];
const uncertainDurations = [
  [span, {...span}],
  [{...span, status:'running'}],
  [{...span, tEnd:200000}],
  Array(2049).fill(span),
].map(spans => {
  const candidate = {...turn, projection:{...turn.projection,
    timingTrace:{...turn.projection.timingTrace, spans}}};
  return feature.selectConversationViewModel({...state,
    turnsById:{'turn-compact-activity':candidate}}).mainLane.turns[0]
    .blocks.find(block => block.kind === 'activity-event').value.durationMs;
});
console.log(JSON.stringify({
  blockKinds:vmTurn.blocks.map(block => block.kind),
  infoToolHidden:!document.querySelector('[data-activity-id="tool-info"]'),
  compactionClass:row.classList.contains('activity-event--compaction'),
  durationMs:vmTurn.blocks.find(block => block.kind === 'activity-event').value.durationMs,
  storedDurationMs:entries[0].durationMs,
  uncertainDurations,
  title:row.querySelector('.activity-event__summary').textContent,
  beforeCount:before.dataset.tokenCount,
  beforeText:before.querySelector('.compaction-token-value').textContent,
  afterCount:after.dataset.tokenCount,
  afterText:after.querySelector('.compaction-token-value').textContent,
  saved:row.querySelector('.compaction-token-saved').textContent,
  snapshotAction:intents.map(intent => [intent.type, intent.operation || '']),
}));
controller.dispose();
dom.window.close();
""")
    assert result == {
        "blockKinds": ["activity-event", "text"],
        "infoToolHidden": True,
        "compactionClass": True,
        "durationMs": 239253,
        "storedDurationMs": 251,
        "uncertainDurations": [251, 251, 251, 251],
        "title": "Context compacted",
        "beforeCount": "180000",
        "beforeText": "≈180,000",
        "afterCount": "42000",
        "afterText": "≈42,000",
        "saved": "77% less",
        "snapshotAction": [["open-compaction", "archive-a"]],
    }


def test_activity_tool_rejection_row_yields_to_the_inline_tool_rejection(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const toolSegment = {type:'tool_use', blockId:'tool:call-b', id:'call-b',
  name:'search_tool_artifact', input:{query:'consoleErrors'}, llmRound:0,
  result:{status:'rejected', content:'ERROR: Tool call was NOT executed.'}};
const textSegment = {type:'text', blockId:'text:terminal', text:'answer',
  deliverable:true, terminal:true, llmRound:1};
const rejectedRound = {roundNum:1, llmRound:0, toolCallId:'call-b',
  toolName:'search_tool_artifact', toolArgs:{query:'consoleErrors'},
  status:'rejected',
  rejection:{kind:'tool_contract_invalid', tool:'search_tool_artifact',
    reason:'ERROR: Tool call was NOT executed.'},
  result:{status:'rejected', content:'ERROR: Tool call was NOT executed.'}};
const entries = [
  {id:'blocked-1', spanId:'tool:call-b', parentSpanId:'model:1', seq:1,
    occurredAt:1005, startedAt:1000, endedAt:1005, durationMs:5,
    kind:'tool', status:'skipped', severity:'warning', count:1,
    summary:'search_tool_artifact blocked',
    summaryKey:'activity.tool.blocked',
    summaryArgs:{tool:'search_tool_artifact'},
    detail:'ERROR: Tool call was NOT executed.',
    reasonCode:'tool_contract_invalid',
    toolName:'search_tool_artifact', toolCallId:'call-b', llmRound:0},
  {id:'unavail-1', spanId:'tool:call-c', parentSpanId:'model:1', seq:2,
    occurredAt:1010, kind:'tool', status:'skipped', severity:'warning', count:1,
    summary:'legacy_tool unavailable', summaryKey:'activity.tool.unavailable',
    summaryArgs:{tool:'legacy_tool'}, detail:'not in this turn toolset',
    reasonCode:'tool_not_available', toolName:'legacy_tool',
    toolCallId:'call-c', llmRound:1},
];
const turn = {
  turnId:'turn-reject', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'', status:'completed', currentAttemptId:null,
  projection:{segments:[toolSegment, textSegment], toolRounds:[rejectedRound],
    content:'answer',
    activityTimeline:{blockId:'activity-timeline', version:1, entries}},
  projectionRevision:1, settlement:{outcome:'completed'}, createdAt:1, updatedAt:2,
};
const state = {conversationId:'conv-a', conversationRevision:1, transport:'live',
  turnsById:{'turn-reject':turn}, laneOrder:{main:['turn-reject']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{}};
const scheduled = [];
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
    renderToolBlockHtml:block => '<details class="native-tool"><summary>'
      + block.name + '</summary></details>',
  }),
});
controller.render({id:'conv-a'}, state);
scheduled.shift()();
for (const process of document.querySelectorAll('.conversation-history-process')) { process.open=true; process.dispatchEvent(new document.defaultView.Event('toggle')); }
const vmTurn = feature.selectConversationViewModel(state).mainLane.turns[0];
console.log(JSON.stringify({
  blockIds:vmTurn.blocks.map(block => block.blockId),
  blockedRowGone:!document.querySelector('[data-activity-id="blocked-1"]'),
  orphanRowSummary:document.querySelector('[data-activity-id="unavail-1"]')
    .querySelector('.activity-event__summary').textContent,
}));
controller.dispose();
dom.window.close();
""")
    # The blocked row duplicates the rejected tool row's badge + full reason,
    # so only the orphan unavailable row (no matching round) survives.
    # Its missing explicit call ID fails closed at the chronology window's
    # neutral start, rather than guessing from a coincident model round.
    assert result == {
        "blockIds": ["tool:call-b", "activity:unavail-1", "text:terminal"],
        "blockedRowGone": True,
        "orphanRowSummary": "legacy_tool unavailable",
    }


def test_activity_tool_failure_row_yields_to_the_inline_tool_error(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const toolSegment = {type:'tool_use', blockId:'tool:call-e', id:'call-e',
  name:'mcp__llm__experiment_stop', input:{mis:'359059'}, llmRound:0,
  result:{status:'error', content:'MCP Error: confirm=true required'}};
const textSegment = {type:'text', blockId:'text:terminal', text:'answer',
  deliverable:true, terminal:true, llmRound:2};
const errorRound = {roundNum:1, llmRound:0, toolCallId:'call-e',
  toolName:'mcp__llm__experiment_stop', status:'error',
  toolContent:'MCP Error: confirm=true required',
  result:{status:'error', content:'MCP Error: confirm=true required'}};
const gatewayContent = JSON.stringify({
  contractVersion:'tofu.tool-result/v2', status:'ok', items:[{
    status:'error', results:[], errors:[{
      code:'missing_required_arguments', name:'read_tool_artifact',
      message:'Missing required arguments: artifact_ref',
      retry_hint:'Provide artifact_ref and retry.',
    }],
  }],
});
const gatewayRound = {roundNum:2, llmRound:1, toolCallId:'gw-1',
  toolName:'execute_tools', status:'error', toolContent:gatewayContent};
const entries = [
  {id:'failed-1', spanId:'tool:call-e', parentSpanId:'model:1', seq:1,
    occurredAt:1005, startedAt:1000, endedAt:1005, durationMs:5,
    kind:'tool', status:'failed', severity:'error', count:1,
    summary:'mcp__llm__experiment_stop failed',
    summaryKey:'activity.tool.failed',
    summaryArgs:{tool:'mcp__llm__experiment_stop'},
    detail:'MCP Error: {"ok": false … <log policy omitted 628 chars> …',
    reasonCode:'error',
    toolName:'mcp__llm__experiment_stop', toolCallId:'call-e', llmRound:0},
  {id:'failed-orphan', spanId:'tool:call-x', parentSpanId:'model:1', seq:2,
    occurredAt:1010, kind:'tool', status:'failed', severity:'error', count:1,
    summary:'orphan_probe failed', summaryKey:'activity.tool.failed',
    summaryArgs:{tool:'orphan_probe'}, detail:'trimmed round lost',
    reasonCode:'error', toolName:'orphan_probe',
    toolCallId:'call-x', llmRound:1},
  {id:'gw-1', spanId:'tool:gw-1', parentSpanId:'model:1', seq:3,
    occurredAt:1016, kind:'tool', status:'failed', severity:'error', count:1,
    summary:'execute_tools failed', summaryKey:'activity.tool.failed',
    summaryArgs:{tool:'execute_tools'},
    toolName:'execute_tools', toolCallId:'gw-1', llmRound:1},
];
const turn = {
  turnId:'turn-fail', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'', status:'completed', currentAttemptId:null,
  projection:{segments:[toolSegment, textSegment],
    toolRounds:[errorRound, gatewayRound],
    content:'answer',
    activityTimeline:{blockId:'activity-timeline', version:1, entries}},
  projectionRevision:1, settlement:{outcome:'completed'}, createdAt:1, updatedAt:2,
};
const state = {conversationId:'conv-a', conversationRevision:1, transport:'live',
  turnsById:{'turn-fail':turn}, laneOrder:{main:['turn-fail']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{}};
const scheduled = [];
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
    renderToolBlockHtml:block => '<details class="native-tool"><summary>'
      + block.name + '</summary></details>',
  }),
});
controller.render({id:'conv-a'}, state);
scheduled.shift()();
for (const process of document.querySelectorAll('.conversation-history-process')) { process.open=true; process.dispatchEvent(new document.defaultView.Event('toggle')); }
const vmTurn = feature.selectConversationViewModel(state).mainLane.turns[0];
console.log(JSON.stringify({
  blockIds:vmTurn.blocks.map(block => block.blockId),
  orphanRowSummary:document.querySelector('[data-activity-id="failed-orphan"]')
    .querySelector('.activity-event__summary').textContent,
}));
controller.dispose();
dom.window.close();
""")
    # The failed row duplicates the error tool row's badge + full content, so
    # it drops; the orphan failed row (round trimmed away) and the execute_
    # tools gateway row (replays as child validation) both survive.
    block_ids = result["blockIds"]
    assert "activity:failed-1" not in block_ids
    assert "activity:failed-orphan" in block_ids
    assert "activity:gw-1:validation-0" in block_ids
    assert result["orphanRowSummary"] == "orphan_probe failed"


def test_legacy_gateway_shell_replays_as_actionable_child_validation(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const gatewayContent = JSON.stringify({
  contractVersion:'tofu.tool-result/v2', status:'ok', items:[{
    status:'error', results:[], errors:[{
      code:'missing_required_arguments', name:'read_tool_artifact',
      message:'Missing required arguments: artifact_ref',
      retry_hint:'Provide artifact_ref and retry.',
    }],
  }],
});
const turn = {
  turnId:'turn-legacy-gateway', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'', status:'completed', currentAttemptId:null,
  projection:{
    segments:[{type:'text', blockId:'text:terminal', text:'answer',
      deliverable:true, terminal:true}],
    toolRounds:[{roundNum:2, llmRound:0, toolCallId:'gateway-1',
      toolName:'execute_tools', status:'error', toolContent:gatewayContent}],
    activityTimeline:{blockId:'activity-timeline', version:1, entries:[{
      id:'legacy-gateway', spanId:'tool:gateway-1', seq:3, occurredAt:1016,
      kind:'tool', status:'failed', severity:'error', count:1,
      summary:'execute_tools failed', toolName:'execute_tools',
      toolCallId:'gateway-1', llmRound:0,
    }]},
  },
  projectionRevision:1, settlement:{outcome:'completed'}, createdAt:1, updatedAt:2,
};
const state = {conversationId:'conv-a', conversationRevision:1, transport:'replay',
  turnsById:{'turn-legacy-gateway':turn}, laneOrder:{main:['turn-legacy-gateway']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{}};
const blocks = feature.selectConversationViewModel(state).mainLane.turns[0].blocks;
const activities = blocks.filter(block => block.kind === 'activity-event');
console.log(JSON.stringify({
  count:activities.length,
  blockId:activities[0]?.blockId,
  toolName:activities[0]?.value.toolName,
  status:activities[0]?.value.status,
  severity:activities[0]?.value.severity,
  summaryKey:activities[0]?.value.summaryKey,
  reasonCode:activities[0]?.value.reasonCode,
  detail:activities[0]?.value.detail,
  shellVisible:activities.some(block => block.value.toolName === 'execute_tools'),
}));
""")
    assert result == {
        "count": 1,
        "blockId": "activity:legacy-gateway:validation-0",
        "toolName": "read_tool_artifact",
        "status": "skipped",
        "severity": "warning",
        "summaryKey": "activity.tool.validationRejected",
        "reasonCode": "missing_required_arguments",
        "detail": (
            "Missing required arguments: artifact_ref "
            "Next: Provide artifact_ref and retry."
        ),
        "shellVisible": False,
    }


def test_terminal_error_uses_complete_settlement_and_preserves_user_collapse(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const error = {
  kind:'tool_not_available', severity:'warning', retryable:false,
  message:'Model called a tool that is not available this turn',
  hint:'How to fix: ' + 'h'.repeat(900) + ' HINT_TAIL',
  detail:'code_exec was not dispatched ' + 'd'.repeat(500) + ' DETAIL_TAIL',
  model:'kimi-k3', context:'tool-dispatch', source:'orchestrator',
  raw:'missing tool: code_exec', titleKey:'err.k.tool_not_available.title',
  hintKey:'err.k.tool_not_available.hint',
};
const turn = {
  turnId:'turn-terminal-error', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'', status:'failed', currentAttemptId:null,
  projection:{content:'partial answer', model:'kimi-k3', segments:[{
    type:'text', blockId:'text:terminal', text:'partial answer',
    deliverable:true, terminal:true,
  }], activityTimeline:{blockId:'activity-timeline', version:1, entries:[{
    id:'terminal', spanId:'error:terminal', seq:4, occurredAt:1004,
    kind:'error', status:'failed', severity:'error', count:1,
    summary:'Turn failed', summaryKey:'activity.error.failed',
    detail:"{'kind':'tool_not_available' … <log policy omitted 903 chars> …",
    reasonCode:'tool_not_available', model:'kimi-k3',
  }]}},
  projectionRevision:1,
  settlement:{outcome:'failed', cause:'generation_error', error},
  createdAt:1, updatedAt:2,
};
const state = {conversationId:'conv-a', conversationRevision:1, transport:'live',
  turnsById:{'turn-terminal-error':turn}, laneOrder:{main:['turn-terminal-error']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{}};
const scheduled = [];
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
    localizedText:(_key, fallback) => fallback,
  }),
});
controller.render({id:'conv-a'}, state);
scheduled.shift()();
const blockSelector = '[data-block-id="activity:terminal"]';
const firstBlock = document.querySelector(blockSelector);
const firstDisclosure = firstBlock.querySelector('details.activity-event--terminal-error');
const firstEnvelope = firstDisclosure.querySelector('.activity-event__terminal-envelope')
  .textContent;
const defaultOpen = firstDisclosure.open;
firstDisclosure.open = false;

const nextState = JSON.parse(JSON.stringify(state));
nextState.conversationRevision = 2;
nextState.turnsById['turn-terminal-error'].projectionRevision = 2;
nextState.turnsById['turn-terminal-error'].updatedAt = 3;
nextState.turnsById['turn-terminal-error'].settlement.error.detail += ' UPDATED_TAIL';
controller.render({id:'conv-a'}, nextState);
scheduled.shift()();
const updatedBlock = document.querySelector(blockSelector);
const updatedDisclosure = updatedBlock.querySelector(
  'details.activity-event--terminal-error');
const updatedEnvelope = updatedDisclosure.querySelector(
  '.activity-event__terminal-envelope').textContent;
const payload = {
  defaultOpen,
  completeHint:firstEnvelope.includes('HINT_TAIL'),
  completeDetail:firstEnvelope.includes('DETAIL_TAIL'),
  compactCopyHidden:!firstEnvelope.includes('log policy omitted'),
  collapsePreserved:updatedDisclosure.open === false,
  updatedDetailVisible:updatedEnvelope.includes('UPDATED_TAIL'),
  blockStable:firstBlock === updatedBlock,
  summaryText:updatedDisclosure.querySelector('.activity-event__summary').textContent,
};
controller.dispose();
dom.window.close();
console.log(JSON.stringify(payload));
""")
    assert result == {
        "defaultOpen": True,
        "completeHint": True,
        "completeDetail": True,
        "compactCopyHidden": True,
        "collapsePreserved": True,
        "updatedDetailVisible": True,
        "blockStable": True,
        "summaryText": "Turn failed",
    }


def test_activity_rows_from_a_failed_attempt_stay_above_the_resume_tools(
        conversation_bundle: Path):
    """Continue/resume restarts the model round counter at 0, so a failed
    attempt's round-19 error row must not sink below the resume's fresh
    round-0/1 tool blocks: the durable timeline's tool rows bound the round
    scan to the content that existed when the error was recorded."""
    result = _run(conversation_bundle, r"""
const toolSegment = (callId, llmRound) => ({
  type:'tool_use', blockId:`tool:${callId}`, id:callId, name:'read_files',
  input:{path:'x'}, llmRound, result:{status:'done', content:'ok'},
});
const toolRow = (id, callId, seq) => ({
  id, spanId:`tool:${callId}`, seq, occurredAt:seq, kind:'tool',
  status:'succeeded', severity:'info', count:1, summary:'read_files',
  toolName:'read_files', toolCallId:callId,
});
const entries = [
  toolRow('tool-old', 'call-old', 1),
  {id:'err-1', spanId:'model:19', seq:2, occurredAt:2, kind:'model',
    status:'failed', severity:'error', count:1, llmRound:19,
    summary:'kimi-k3 request failed', model:'kimi-k3'},
  {id:'err-2', spanId:'status:retry', seq:3, occurredAt:3, kind:'status',
    status:'waiting', severity:'warning', count:1, llmRound:19,
    summary:'retrying kimi-k3', model:'kimi-k3'},
  toolRow('tool-new-1', 'call-new-1', 4),
  toolRow('tool-new-2', 'call-new-2', 5),
];
const turn = {
  turnId:'turn-resume', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'', status:'completed',
  currentAttemptId:null,
  projection:{
    segments:[
      toolSegment('call-old', 18),
      toolSegment('call-new-1', 0),
      toolSegment('call-new-2', 1),
      {type:'text', blockId:'text:terminal', text:'done', deliverable:true,
        terminal:true, llmRound:2},
    ],
    content:'done',
    activityTimeline:{blockId:'activity-timeline', version:1, entries},
  },
  projectionRevision:1, settlement:{outcome:'completed'}, createdAt:1,
  updatedAt:2,
};
const state = {conversationId:'conv-a', conversationRevision:1,
  transport:'live', turnsById:{'turn-resume':turn},
  laneOrder:{main:['turn-resume']}, attemptsById:{}, queueItems:[],
  pendingEventsByTurn:{}, commandPending:{}, liveRoundUsageByTurn:{}};
const vmTurn = feature.selectConversationViewModel(state).mainLane.turns[0];
console.log(JSON.stringify({
  blockIds:vmTurn.blocks.map(block => block.blockId),
}));
""")
    assert result == {
        "blockIds": [
            "tool:call-old",
            "activity:err-1",
            "activity:err-2",
            "tool:call-new-1",
            "tool:call-new-2",
            "text:terminal",
        ],
    }


def test_typed_attachments_render_natively_and_emit_indexed_intents(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const images = [
  {attachmentId:'image-1', preview:'/media/image.png', sizeKB:12, caption:'Chart'},
  {attachmentId:'image-bad', preview:'javascript:alert(1)', sizeKB:1},
];
const videos = [{video_id:'video-1', name:'Demo', video_url:'/media/video.mp4',
  poster:'/media/poster.jpg', duration_s:65, frame_count:4, transcript:'hello'}];
const pdfTexts = [{name:'report.pdf', text:'body', textLength:4, pages:2}];
const convRefs = [{id:'conv-ref', title:'Prior discussion'}];
const replyQuotes = ['quoted\ntext'];
const makeTurn = (revision, text) => ({
  turnId:'human-files', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'human', kind:'input', runId:'', status:'completed', currentAttemptId:null,
  projection:{segments:[{type:'text', blockId:'text:terminal', text,
    deliverable:true, terminal:true}], content:text, images, videos, pdfTexts,
    convRefs, replyQuotes}, projectionRevision:revision,
  settlement:{outcome:'completed'}, createdAt:1, updatedAt:revision,
});
const makeState = turn => ({conversationId:'conv-a', conversationRevision:turn.projectionRevision,
  transport:'live', turnsById:{'human-files':turn}, laneOrder:{main:['human-files']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{}});
const scheduled = [];
const intents = [];

const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
    resolveMediaUrl:value => value.startsWith('/') ? `/base${value}` : value,
    localizedText:(_key, fallback) => fallback,
  }),
  onIntent:intent => intents.push(intent),
});
const conversation = {id:'conv-a'};
controller.render(conversation, makeState(makeTurn(1, 'one')));
scheduled.shift()();
const attachment1 = document.querySelector('[data-block-id="attachments"]');
const imageButton1 = attachment1.querySelector('[data-conversation-action="preview-image"]');
imageButton1.click();
attachment1.querySelector('[data-conversation-action="open-video"]').click();
attachment1.querySelector('[data-conversation-action="preview-document"]').click();
controller.render(conversation, makeState(makeTurn(2, 'two')));
scheduled.shift()();
const attachment2 = document.querySelector('[data-block-id="attachments"]');
const imageButton2 = attachment2.querySelector('[data-conversation-action="preview-image"]');
const vm = feature.selectConversationViewModel(makeState(makeTurn(3, 'three')));
const payload = {
  native:Boolean(vm.mainLane.turns[0]),
  firstBlock:vm.mainLane.turns[0].blocks[0].kind,

  sameAttachment:attachment1 === attachment2,
  sameImageButton:imageButton1 === imageButton2,
  imageSrc:imageButton2.querySelector('img').getAttribute('src'),
  unsafeImageIsPlaceholder:attachment2.querySelectorAll('.msg-img-thumb.placeholder').length,
  videoMeta:attachment2.querySelector('.msg-video-meta').textContent,
  quote:attachment2.querySelector('.reply-quote-badge-name').textContent,
  reference:Array.from(attachment2.querySelectorAll('.reply-quote-badge-name'))[1].textContent,
  intents,
};
controller.dispose();
dom.window.close();
console.log(JSON.stringify(payload));
""")
    assert result == {
        "native": True,
        "firstBlock": "attachments",

        "sameAttachment": True,
        "sameImageButton": True,
        "imageSrc": "/base/media/image.png",
        "unsafeImageIsPlaceholder": 1,
        "videoMeta": "01:05 · 4 frames · transcript",
        "quote": "quoted text",
        "reference": "Prior discussion",
        "intents": [
            {
                "type": "preview-image",
                "conversationId": "conv-a",
                "turnId": "human-files",
                "blockId": "attachments",
                "laneId": "main",
                "operation": "0",
            },
            {
                "type": "open-video",
                "conversationId": "conv-a",
                "turnId": "human-files",
                "blockId": "attachments",
                "laneId": "main",
                "operation": "0",
            },
            {
                "type": "preview-document",
                "conversationId": "conv-a",
                "turnId": "human-files",
                "blockId": "attachments",
                "laneId": "main",
                "operation": "0",
            },
        ],
    }


def test_document_badge_glyph_follows_file_extension(conversation_bundle: Path):
    """Document attachment badges render the file extension (PDF/DOCX/…) as
    the glyph instead of a blanket DOC; extensionless names keep the DOC
    fallback. Mirrors the knowledge panel glyph cap."""
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const pdfTexts = [
  {name:'report.pdf', text:'a', textLength:1, pages:1},
  {name:'notes.docx', text:'b', textLength:1, pages:2},
  {name:'LICENSE', text:'c', textLength:1},
];
const turn = {
  turnId:'doc-glyph', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'human', kind:'input', runId:'', status:'completed', currentAttemptId:null,
  projection:{segments:[{type:'text', blockId:'text:terminal', text:'x',
    deliverable:true, terminal:true}], content:'x', pdfTexts},
  projectionRevision:1, settlement:{outcome:'completed'}, createdAt:1, updatedAt:1,
};
const state = {conversationId:'conv-a', conversationRevision:1, transport:'live',
  turnsById:{'doc-glyph':turn}, laneOrder:{main:['doc-glyph']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{}};
const scheduled = [];
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
    localizedText:(_key, fallback) => fallback,
  }),
  onIntent() {},
});
controller.render({id:'conv-a'}, state);
scheduled.shift()();
const payload = {
  glyphs:Array.from(document.querySelectorAll('.pdf-attach-icon'))
    .map(node => node.textContent),
};
controller.dispose();
dom.window.close();
console.log(JSON.stringify(payload));
""")
    assert result == {"glyphs": ["PDF", "DOCX", "DOC"]}

def test_translation_display_mode_is_local_and_does_not_mutate_turn_facts(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const segment = {type:'text', blockId:'text:terminal', text:'Original answer',
  translatedText:'翻译答案', deliverable:true, terminal:true};
const turn = {
  turnId:'translated-turn', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'', status:'completed', currentAttemptId:null,
  projection:{segments:[segment], content:'Original answer',
    translatedContent:'翻译答案', _translateDone:true, model:'model-a'},
  projectionRevision:2, settlement:{outcome:'completed'}, createdAt:1, updatedAt:2,
};
const state = {conversationId:'conv-a', conversationRevision:2, transport:'live',
  turnsById:{'translated-turn':turn}, laneOrder:{main:['translated-turn']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{}};
const before = JSON.stringify(state);
const scheduled = [];
const forwarded = [];

const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
    actionLabel:value => value,
    localizedText:(_key, fallback) => fallback,
  }),
  onIntent:intent => forwarded.push(intent),
});
controller.render({id:'conv-a'}, state);
scheduled.shift()();
const node = document.querySelector('[data-turn-id="translated-turn"]');
const primary = () => node.querySelector('[data-block-id="text:terminal"] > .md-content').textContent;
const action = () => node.querySelector('[data-conversation-action="translate"]');
const copy = () => node.querySelector('[data-conversation-action="copy"]');
const initial = primary();
node.querySelector('.bilingual-block').open = true;
action().click();
const original = primary();
const alternativeStayedOpen = node.querySelector('.bilingual-block').open;
copy().click();
action().click();
const translated = primary();
copy().click();
const vm = feature.selectConversationViewModel(state);
const payload = {
  native:Boolean(vm.mainLane.turns[0]),

  initial,
  original,
  translated,
  alternativeStayedOpen,
  forwarded:forwarded.map(intent => [intent.type, intent.operation]),
  stateUnchanged:before === JSON.stringify(state),
  serverProjectionHasNoUiMode:!Object.hasOwn(turn.projection, '_showingTranslation'),
};
controller.dispose();
dom.window.close();
console.log(JSON.stringify(payload));
""")
    assert result == {
        "native": True,

        "initial": "翻译答案",
        "original": "Original answer",
        "translated": "翻译答案",
        "alternativeStayedOpen": True,
        "forwarded": [
            ["copy", "copy-original"],
            ["copy", "copy-translated"],
        ],
        "stateUnchanged": True,
        "serverProjectionHasNoUiMode": True,
    }


def test_translation_alternative_only_hangs_off_the_deliverable_block(
        conversation_bundle: Path):
    """Mid-turn narration/thinking render their translation inline and never
    carry the per-block 原文/译文 collapsible — that alternative belongs to the
    deliverable answer alone (per-block toggles mid-turn are timing-dependent
    noise). Regression pin for the deliverable guard in
    renderTranslationAlternative."""
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const segments = [
  {type:'thinking', blockId:'thinking:llm-0', text:'Reasoning in English',
    translatedText:'中文推理', llmRound:0},
  {type:'text', blockId:'text:llm-0', text:'Narration in English',
    translatedText:'中文叙述', llmRound:0, deliverable:false},
  {type:'tool_use', blockId:'tool:call-1', id:'call-1', name:'read_files',
    input:{}, llmRound:0, result:{content:'x', status:'done'}},
  {type:'text', blockId:'text:terminal', text:'Final answer',
    translatedText:'最终答案', deliverable:true, terminal:true},
];
const turn = {
  turnId:'mixed-turn', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'', status:'completed', currentAttemptId:null,
  projection:{segments, content:'Final answer',
    translatedContent:'最终答案', _translateDone:true, model:'model-a'},
  projectionRevision:2, settlement:{outcome:'completed'}, createdAt:1, updatedAt:2,
};
const state = {conversationId:'conv-a', conversationRevision:2, transport:'live',
  turnsById:{'mixed-turn':turn}, laneOrder:{main:['mixed-turn']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{}};
const scheduled = [];
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
    actionLabel:value => value,
    localizedText:(_key, fallback) => fallback,
  }),
});
controller.render({id:'conv-a'}, state);
scheduled.shift()();
for (const process of document.querySelectorAll('.conversation-history-process')) { process.open=true; process.dispatchEvent(new document.defaultView.Event('toggle')); }
const block = (id) => document.querySelector(`[data-block-id="${id}"]`);
const payload = {
  narrationText:block('text:llm-0').querySelector('.md-content').textContent,
  narrationHasAlternative:Boolean(block('text:llm-0').querySelector('.bilingual-block')),
  thinkingHasAlternative:Boolean(block('thinking:llm-0').querySelector('.bilingual-block')),
  deliverableText:block('text:terminal').querySelector('.md-content').textContent,
  deliverableHasAlternative:Boolean(block('text:terminal').querySelector('.bilingual-block')),
  deliverableAlternativeLabel:block('text:terminal')
    .querySelector('.bilingual-block .bilingual-label')?.textContent || '',
};
controller.dispose();
dom.window.close();
console.log(JSON.stringify(payload));
""")
    assert result == {
        "narrationText": "中文叙述",
        "narrationHasAlternative": False,
        "thinkingHasAlternative": False,
        "deliverableText": "最终答案",
        "deliverableHasAlternative": True,
        "deliverableAlternativeLabel": "Original",
    }


def test_branch_lane_is_keyed_collapsible_and_routes_stable_lane_intents(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const makeTurn = (id, laneId, ordinal, actor, status, text, extra = {}) => ({
  turnId:id, conversationId:'conv-a', laneId, ordinal, actor,
  kind:actor === 'human' ? 'input' : 'reply', runId:'', status,
  currentAttemptId:status === 'running' ? `attempt-${id}` : null,
  projection:{segments:[{type:'text', blockId:'text:terminal', text,
    deliverable:true, terminal:true}], content:text, ...(extra.projection || {})},
  projectionRevision:1, settlement:status === 'running' ? {} : {outcome:'completed'},
  createdAt:ordinal, updatedAt:ordinal, ...(extra.parentTurnId
    ? {parentTurnId:extra.parentTurnId} : {}),
});
const parent = makeTurn('parent', 'main', 1, 'assistant', 'completed', 'Main', {
  projection:{_branchLanes:[{laneId:'lane-b', title:'Alternative', icon:'B'}]},
});
const childUser = makeTurn('branch-user', 'lane-b', 1, 'human', 'completed', 'Question', {
  parentTurnId:'parent',
});
const childAssistant = makeTurn('branch-assistant', 'lane-b', 2, 'assistant',
  'running', 'Partial', {parentTurnId:'parent'});
const state = {conversationId:'conv-a', conversationRevision:3, transport:'live',
  turnsById:{parent, 'branch-user':childUser, 'branch-assistant':childAssistant},
  laneOrder:{main:['parent'], 'lane-b':['branch-user','branch-assistant']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{}};
const before = JSON.stringify(state);
const scheduled = [];
const forwarded = [];

const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
    actionLabel:value => value,
    localizedText:(_key, fallback, values) => values?.n == null
      ? fallback : `${values.n} user turns`,
  }),
  onIntent:intent => forwarded.push(intent),
});
controller.render({id:'conv-a'}, state);
scheduled.shift()();
const parentNode = document.querySelector('[data-turn-id="parent"]');
const lane = document.querySelector('[data-lane-id="lane-b"]');
const turns = lane.querySelector(':scope > [data-conversation-part="lane-turns"]');
const initiallyHidden = turns.hidden;
lane.querySelector('[data-conversation-action="toggle-branch"]').click();
const visibleAfterOpen = !turns.hidden;
const childNode = lane.querySelector('[data-turn-id="branch-assistant"]');
const childCopy = childNode.querySelector('[data-conversation-action="copy"]');
childCopy.click();
lane.querySelector('[data-conversation-action="stop-branch"]').click();
lane.querySelector('[data-conversation-action="toggle-branch"]').click();
const hiddenAfterCollapse = turns.hidden;
lane.querySelector('[data-conversation-action="delete-branch"]').click();
const vm = feature.selectConversationViewModel(state);
const payload = {
  native:Boolean(vm.mainLane.turns[0]),

  initiallyHidden,
  visibleAfterOpen,
  hiddenAfterCollapse,
  sameParent:parentNode === document.querySelector('[data-turn-id="parent"]'),
  childActions:Array.from(childNode.querySelectorAll('[data-conversation-action]'))
    .map(node => node.dataset.conversationAction),
  forwarded:forwarded.map(intent => [intent.type, intent.turnId, intent.laneId,
    intent.operation || null]),
  stateUnchanged:before === JSON.stringify(state),
};
controller.dispose();
dom.window.close();
console.log(JSON.stringify(payload));
""")
    assert result == {
        "native": True,

        "initiallyHidden": True,
        "visibleAfterOpen": True,
        "hiddenAfterCollapse": True,
        "sameParent": True,
        "childActions": ["copy-block", "copy"],
        "forwarded": [
            ["toggle-branch", "parent", "lane-b", "open"],
            ["copy", "branch-assistant", "lane-b", None],
            ["stop-branch", "parent", "lane-b", None],
            ["toggle-branch", "parent", "lane-b", "close"],
            ["delete-branch", "parent", "lane-b", None],
        ],
        "stateUnchanged": True,
    }


def test_autopilot_run_notice_is_a_stable_plain_text_turn_block(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const makeTurn = (turnId, ordinal, actor, content, projection = {}) => ({
  turnId, conversationId:'conv-a', laneId:'main', ordinal, actor,
  kind:actor === 'human' ? 'input' : 'reply', runId:'', status:'completed',
  currentAttemptId:null, projection:{segments:[{type:'text',
    blockId:'text:terminal', text:content, deliverable:true, terminal:true}],
    content, ...projection}, projectionRevision:1,
  settlement:{outcome:'completed'}, createdAt:ordinal, updatedAt:ordinal,
});
const turns = [
  makeTurn('human-before', 1, 'human', 'Start'),
  makeTurn('vu-run', 2, 'virtual_user', 'Question', {_autopilotRunId:'run-a'}),
  makeTurn('assistant-tail', 3, 'assistant', 'Tail answer'),
  makeTurn('human-boundary', 4, 'human', 'I took over'),
  makeTurn('after-boundary', 5, 'assistant', 'Later answer'),
];
const state = {conversationId:'conv-a', conversationRevision:5, transport:'live',
  turnsById:Object.fromEntries(turns.map(turn => [turn.turnId, turn])),
  laneOrder:{main:turns.map(turn => turn.turnId)}, attemptsById:{}, queueItems:[],
  pendingEventsByTurn:{}, commandPending:{}, liveRoundUsageByTurn:{}};
const record = {status:'concluded', reason:'yielded_to_human', unsent:true,
  content:'<b>draft only</b>'};
const summaries = {'run-a':record};
const before = JSON.stringify({state, summaries});
const vm = feature.selectConversationViewModel(state, {}, {autopilotSummaries:summaries});
const blocksByTurn = Object.fromEntries(vm.mainLane.turns.map(turn => [
  turn.turnId, turn.blocks.filter(block => block.kind === 'autopilot-run-notice'),
]));
const scheduled = [];
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
    localizedText:(key, fallback) => key === 'autopilot.endedYielded'
      ? 'localized-yielded' : fallback,
  }),
});
const conversation = {id:'conv-a', autopilotSummaries:summaries};
controller.render(conversation, state);
scheduled.shift()();
const tail = document.querySelector('[data-turn-id="assistant-tail"]');
const block = tail.querySelector('[data-block-kind="autopilot-run-notice"]');
const noticeBefore = block.querySelector('.ap-run-notice');
const blockIdBefore = block.dataset.blockId;
const plainTextBefore = block.querySelector('.ap-run-notice-text').textContent;
record.reason = 'stuck';
record.content = '<i>new draft</i>';
controller.render(conversation, state);
scheduled.shift()();
const blockAfter = tail.querySelector('[data-block-kind="autopilot-run-notice"]');
const payload = {
  anchoredOnlyAtTail:blocksByTurn['assistant-tail'].length === 1
    && blocksByTurn['vu-run'].length === 0
    && blocksByTurn['human-boundary'].length === 0
    && blocksByTurn['after-boundary'].length === 0,
  blockId:blockIdBefore,
  sameBlockNode:block === blockAfter,
  localizedLabel:noticeBefore.querySelector('.ap-run-notice-label').textContent,
  plainTextBefore,
  markupWasNotParsed:!noticeBefore.querySelector('b') && !blockAfter.querySelector('i'),
  updatedReason:blockAfter.querySelector('.ap-run-notice').dataset.apReason,
  updatedText:blockAfter.querySelector('.ap-run-notice-text').textContent,
  stateUnchanged:before === JSON.stringify({state, summaries:{'run-a':{
    status:'concluded', reason:'yielded_to_human', unsent:true,
    content:'<b>draft only</b>'}}}),
};
controller.dispose();
dom.window.close();
console.log(JSON.stringify(payload));
""")
    assert result == {
        "anchoredOnlyAtTail": True,
        "blockId": "assistant-tail:autopilot-run-notice:run-a",
        "sameBlockNode": True,
        "localizedLabel": "localized-yielded",
        "plainTextBefore": "<b>draft only</b>",
        "markupWasNotParsed": True,
        "updatedReason": "stuck",
        "updatedText": "<i>new draft</i>",
        "stateUnchanged": True,
    }


def test_branch_composer_session_owns_only_stable_local_identity(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const session = feature.createBranchComposerSession();
const input = {conversationId:'conv-a', parentTurnId:'turn-parent',
  laneId:'lane-b', title:'Alternative'};
const opened = session.open(input);
input.laneId = 'positional-index-would-drift';
const current = session.current();
const wrongConversation = session.isActive('conv-b');
const rightConversation = session.isActive('conv-a');
const closed = session.close();
console.log(JSON.stringify({
  opened,
  current,
  copied:current !== input,
  wrongConversation,
  rightConversation,
  closed,
  empty:session.current() === null && !session.isActive(),
}));
""")
    assert result == {
        "opened": {
            "conversationId": "conv-a",
            "parentTurnId": "turn-parent",
            "laneId": "lane-b",
            "title": "Alternative",
        },
        "current": {
            "conversationId": "conv-a",
            "parentTurnId": "turn-parent",
            "laneId": "lane-b",
            "title": "Alternative",
        },
        "copied": True,
        "wrongConversation": False,
        "rightConversation": True,
        "closed": {
            "conversationId": "conv-a",
            "parentTurnId": "turn-parent",
            "laneId": "lane-b",
            "title": "Alternative",
        },
        "empty": True,
    }


def test_durable_live_phase_reconciles_inside_running_turn(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const turn = {turnId:'turn-live', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'', status:'running',
  currentAttemptId:'attempt-a', projection:{segments:[], content:''},
  projectionRevision:1, settlement:{}, createdAt:1, updatedAt:1};
const makeState = (livePhase, status = 'running') => ({conversationId:'conv-a',
  conversationRevision:1, transport:'live',
  turnsById:{'turn-live':{...turn, status,
    currentAttemptId:status === 'running' ? 'attempt-a' : null,
    settlement:status === 'running' ? {} : {outcome:'completed'}}},
  laneOrder:{main:['turn-live']}, attemptsById:{}, queueItems:[],
  pendingEventsByTurn:{}, commandPending:{}, liveRoundUsageByTurn:{}, livePhase});
const scheduled = [];
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
    localizedText:(key, fallback, values) => key === 'stream.phase.retryRateLimited'
      ? `rate limited ${values.model} ${values.attempt}`
      : key === 'stream.phase.executorQueuedWithMetrics'
        ? `queue ${values.position}; ${values.active}/${values.capacity}; ${values.waitSeconds}s`
        : fallback,
  }),
});
const conversation = {id:'conv-a'};
const firstState = makeState({phase:'retrying',
  detailKey:'stream.phase.retryRateLimited', detailArgs:{model:'m1', attempt:2},
  attempt:2});
const before = JSON.stringify(firstState);
controller.render(conversation, firstState);
scheduled.shift()();
const first = document.querySelector('[data-block-id="live-status"]');
const firstText = first.querySelector('.stream-phase-text').textContent;
const secondState = makeState({phase:'tool_exec', detail:'Running search',
  tools:['search']});
controller.render(conversation, secondState);
scheduled.shift()();
const second = document.querySelector('[data-block-id="live-status"]');
const secondText = second.querySelector('.stream-phase-text').textContent;
const queueState = makeState({phase:'executor_queued',
  detailKey:'stream.phase.executorQueuedWithMetrics',
  detailArgs:{position:2, queued:3, active:4, capacity:4, waitSeconds:31},
  queuePosition:2, queued:3, active:4, capacity:4, waitSeconds:31}, 'pending');
controller.render(conversation, queueState);
scheduled.shift()();
const queued = document.querySelector('[data-block-id="live-status"]');
const queuedText = queued.querySelector('.stream-phase-text').textContent;
controller.render(conversation, makeState(null, 'completed'));
scheduled.shift()();
const payload = {
  stableNode:first === second && second === queued,
  firstText,
  secondText,
  queuedText,
  removedAtSettlement:!document.querySelector('[data-block-id="live-status"]'),
  stateUnchanged:before === JSON.stringify(firstState),
};
controller.dispose();
dom.window.close();
console.log(JSON.stringify(payload));
""")
    assert result == {
        "stableNode": True,
        "firstText": "rate limited m1 2",
        "secondText": "Running search",
        "queuedText": "queue 2; 4/4; 31s",
        "removedAtSettlement": True,
        "stateUnchanged": True,
    }


def test_send_preparation_does_not_steal_status_from_active_assistant(
        conversation_bundle: Path):
    """Submitting while an assistant attempt is still active composes two
    transient rows after that attempt: the optimistic human echo and a send
    preparation status. The preparation row owns its explicit status, but it
    must not displace the conversation-level live phase from the durable
    assistant and leave that assistant as a header-only ghost."""
    result = _run(conversation_bundle, r"""
const active = {
  turnId:'turn-active', conversationId:'conv-a', laneId:'main', parentTurnId:null,
  ordinal:1, actor:'assistant', kind:'reply', runId:'task-a', status:'running',
  currentAttemptId:'attempt-a', projection:{content:'', segments:[]},
  projectionRevision:1, settlement:{}, createdAt:1, updatedAt:1,
};
const durable = {conversationId:'conv-a', conversationRevision:1,
  transport:'live', turnsById:{'turn-active':active},
  laneOrder:{main:['turn-active']}, attemptsById:{'attempt-a':{
    attemptId:'attempt-a', turnId:'turn-active', status:'running',
    taskId:'task-a', createdAt:1, updatedAt:1}},
  queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{},
  livePhase:{phase:'tool_exec', detail:'Running search', tools:['search']},
};
const overlay = feature.createTransientTurnOverlay();
overlay.upsert(feature.createOptimisticUserTurn({
  conversationId:'conv-a', commandId:'cmd-next', text:'next question', timestamp:2,
}));
overlay.upsert(feature.createTransientStatusTurn({
  conversationId:'conv-a', turnId:'transient:send-preparation',
  phase:'translating', label:'Translating…', timestamp:3,
}));
const vm = feature.selectConversationViewModel(overlay.compose(durable));
const visible = vm.mainLane.turns.map(turn => ({
  turnId:turn.turnId,
  role:turn.role,
  blocks:turn.blocks.map(block => [
    block.kind,
    block.kind === 'live-status' ? block.value.detail || block.value.label : '',
  ]),
}));
console.log(JSON.stringify({
  visible,
  headerOnly:visible.filter(turn => turn.role === 'assistant'
    && turn.blocks.length === 0).map(turn => turn.turnId),
}));
""")
    assert result == {
        "visible": [
            {
                "turnId": "turn-active",
                "role": "assistant",
                "blocks": [["live-status", "Running search"]],
            },
            {
                "turnId": "transient:outgoing:cmd-next",
                "role": "user",
                "blocks": [["text", ""]],
            },
            {
                "turnId": "transient:send-preparation",
                "role": "assistant",
                "blocks": [["live-status", "Translating…"]],
            },
        ],
        "headerOnly": [],
    }


def test_cold_snapshot_requires_executor_evidence_before_reporting_queue(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const baseTurn = {turnId:'turn-live', conversationId:'conv-a', laneId:'main',
  ordinal:1, actor:'assistant', kind:'reply', runId:'',
  currentAttemptId:'attempt-a', projection:{segments:[], content:''},
  projectionRevision:1, settlement:{}, createdAt:1, updatedAt:1};
const makeState = (status, taskId) => ({conversationId:'conv-a',
  conversationRevision:1, transport:'snapshot',
  turnsById:{'turn-live':{...baseTurn, status}},
  laneOrder:{main:['turn-live']},
  attemptsById:{'attempt-a':{attemptId:'attempt-a', turnId:'turn-live',
    status, taskId, createdAt:1, updatedAt:1}},
  queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{}, livePhase:null});
const scheduled = [];
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
    localizedText:key => key,
  }),
});
const conversation = {id:'conv-a'};
const renderText = state => {
  controller.render(conversation, state);
  scheduled.shift()();
  return document.querySelector(
    '[data-block-id="live-status"] .stream-phase-text').textContent;
};
const preparing = renderText(makeState('pending', ''));
const queued = renderText(makeState('pending', 'task-a'));
const running = renderText(makeState('running', 'task-a'));
const queueEvidence = {phase:'executor_queued',
  detailKey:'stream.phase.executorQueuedWithMetrics',
  detailArgs:{position:1, active:4, capacity:4, waitSeconds:20}};
const provedQueue = renderText({...makeState('pending', 'task-a'), livePhase:queueEvidence});
const staleQueue = renderText({...makeState('running', 'task-a'), livePhase:queueEvidence});
const explicit = renderText({...makeState('running', 'task-a'), livePhase:{
  phase:'working', detailKey:'stream.phase.workerStarting'}});
controller.dispose();
dom.window.close();
console.log(JSON.stringify({preparing, queued, running, provedQueue, staleQueue, explicit}));
""")
    assert result == {
        "preparing": "stream.phase.executorPreparing",
        "queued": "stream.phase.executorPreparing",
        "running": "stream.phase.waitingWorkerStatus",
        "provedQueue": "stream.phase.executorQueuedWithMetrics",
        "staleQueue": "stream.phase.waitingWorkerStatus",
        "explicit": "stream.phase.workerStarting",
    }


def test_push_withheld_wedge_replaces_the_waiting_placeholder(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const turn = {turnId:'turn-live', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'', status:'running',
  currentAttemptId:'attempt-a', projection:{segments:[], content:''},
  projectionRevision:1, settlement:{}, createdAt:1, updatedAt:1};
const makeState = (livePhase, pushWithheld) => ({conversationId:'conv-a',
  conversationRevision:1, transport:'live', pushWithheld,
  turnsById:{'turn-live':turn},
  laneOrder:{main:['turn-live']}, attemptsById:{}, queueItems:[],
  pendingEventsByTurn:{}, commandPending:{}, liveRoundUsageByTurn:{}, livePhase});
const scheduled = [];
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
    localizedText:(key, fallback) => key === 'stream.phase.storageWedged'
      ? 'WEDGE-LABEL' : fallback,
  }),
});
const conversation = {id:'conv-a'};
controller.render(conversation, makeState(null, true));
scheduled.shift()();
const wedged = document.querySelector('[data-block-id="live-status"]');
const wedgedText = wedged.querySelector('.stream-phase-text').textContent;
const wedgedClass = wedged.querySelector('.stream-phase').className;
controller.render(conversation, makeState(null, false));
scheduled.shift()();
const cleared = document.querySelector('[data-block-id="live-status"]');
const clearedText = cleared.querySelector('.stream-phase-text').textContent;
const clearedClass = cleared.querySelector('.stream-phase').className;
/* A stale livePhase on record must not outrank the wedge: while pushes are
 * withheld no newer frame can arrive, so any recorded phase is history. */
controller.render(conversation, makeState(
  {phase:'tool_exec', detail:'Running search', tools:['search']}, true));
scheduled.shift()();
const stalePhase = document.querySelector(
  '[data-block-id="live-status"] .stream-phase-text').textContent;
/* Reducer fold: authoritative snapshots ship the key both ways; the action
 * folds heartbeat-carried flips; delta snapshots omit the key entirely. */
let state = feature.createTurnState('conv-a');
state = feature.reduceTurnState(state, {type:'snapshot',
  snapshot:{conversationRevision:1, pushWithheld:true}});
const foldedTrue = state.pushWithheld;
state = feature.reduceTurnState(state, {type:'push_withheld', pushWithheld:false});
const foldedAction = state.pushWithheld;
state = feature.reduceTurnState(state, {type:'snapshot',
  snapshot:{conversationRevision:2, pushWithheld:false}});
const foldedFalse = state.pushWithheld;
state = feature.reduceTurnState(state, {type:'push_withheld', pushWithheld:true});
state = feature.reduceTurnState(state, {type:'snapshot',
  snapshot:{conversationRevision:3}});
const deltaUntouched = state.pushWithheld;
controller.dispose();
dom.window.close();
console.log(JSON.stringify({
  wedgedText, wedgedClass, clearedText, clearedClass, stalePhase,
  foldedTrue, foldedAction, foldedFalse, deltaUntouched,
}));
""")
    assert result == {
        "wedgedText": "WEDGE-LABEL",
        "wedgedClass": "stream-phase stream-phase-wedged",
        "clearedText": "Waiting for an execution status update…",
        "clearedClass": "stream-phase",
        "stalePhase": "WEDGE-LABEL",
        "foldedTrue": True,
        "foldedAction": False,
        "foldedFalse": False,
        "deltaUntouched": True,
    }


def test_queue_items_are_native_keyed_workflow_state_not_fake_messages(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const makeState = text => ({conversationId:'conv-a', conversationRevision:1,
  transport:'live', turnsById:{}, laneOrder:{main:[]}, attemptsById:{},
  queueItems:[
    {queueId:'queue-real', position:2, kind:'real', priority:1, timestamp:1,
      text, hasImages:true, isPeerMessage:true, isPeerHuman:false, fromConv:'conv-peer'},
    {queueId:'queue-autopilot', position:3, kind:'autopilot', priority:90,
      timestamp:1, text:'sentinel'},
  ], pendingEventsByTurn:{}, commandPending:{}, liveRoundUsageByTurn:{}});
const scheduled = [];
const intents = [];
let compatibilityQueueCalls = 0;
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
    localizedText:(_key, fallback) => fallback,
  }),
  onIntent:intent => intents.push(intent),
  renderQueueDocument() { compatibilityQueueCalls += 1; return '<div>wrong</div>'; },
});
const conversation = {id:'conv-a'};
const before = JSON.stringify(makeState('first'));
controller.render(conversation, makeState('first'));
scheduled.shift()();
const first = document.querySelector('[data-queue-id="queue-real"]');
first.querySelector('[data-conversation-action="remove-queue"]').click();
controller.render(conversation, makeState('updated'));
scheduled.shift()();
const second = document.querySelector('[data-queue-id="queue-real"]');
const payload = {
  compatibilityQueueCalls,
  sameNode:first === second,
  text:second.querySelector('.conversation-queue-item__body').firstChild.textContent,
  source:second.querySelector('.queue-item-src').textContent,
  queueNodes:document.querySelectorAll('[data-queue-id]').length,
  autopilotInTranscript:!!document.querySelector('[data-queue-id="queue-autopilot"]'),
  intent:intents[0],
  stateUnchanged:before === JSON.stringify(makeState('first')),
};
controller.dispose();
dom.window.close();
console.log(JSON.stringify(payload));
""")
    assert result == {
        "compatibilityQueueCalls": 0,
        "sameNode": True,
        "text": "updated",
        "source": "from conv-peer",
        "queueNodes": 1,
        "autopilotInTranscript": False,
        "intent": {
            "type": "remove-queue",
            "conversationId": "conv-a",
            "queueId": "queue-real",
        },
        "stateUnchanged": True,
    }


def test_injection_blocks_use_backend_identity_and_anchor_before_consuming_round(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const injection = {blockId:'injection:user-steer:round-2', round:2, count:1,
  previews:[{text:'focus on the failing test'}]};
const roundZero = {type:'text', blockId:'text:llm-0', text:'I will inspect it.',
  llmRound:0, deliverable:false, terminal:false};
const tool = {type:'tool_use', blockId:'tool:call-2', id:'call-2', name:'read_file',
  input:{path:'a.py'}, result:{status:'done', content:'body'}, llmRound:1};
const terminal = text => ({type:'text', blockId:'text:terminal', text,
  deliverable:true, terminal:true});
const makeTurn = (revision, text) => ({
  turnId:'turn-inject', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'', status:'completed', currentAttemptId:null,
  projection:{segments:[roundZero, tool, terminal(text)], content:text,
    _userSteerInjects:[injection]},
  projectionRevision:revision, settlement:{outcome:'completed'}, createdAt:1,
  updatedAt:revision,
});
const makeState = turn => ({conversationId:'conv-a',
  conversationRevision:turn.projectionRevision, transport:'live',
  turnsById:{'turn-inject':turn}, laneOrder:{main:['turn-inject']}, attemptsById:{},
  queueItems:[], pendingEventsByTurn:{}, commandPending:{}, liveRoundUsageByTurn:{}});
const firstState = makeState(makeTurn(1, 'first answer'));
const secondState = makeState(makeTurn(2, 'revised answer'));
const scheduled = [];

let injectionRenders = 0;
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
    renderToolBlockHtml:() => '<div class="native-tool">tool</div>',
    renderInjectionBlockHtml:() => {
      injectionRenders += 1;
      return '<details open class="native-injection"><summary>Injected</summary></details>';
    },
  }),
});
const conversation = {id:'conv-a'};
controller.render(conversation, firstState);
scheduled.shift()();
for (const process of document.querySelectorAll('.conversation-history-process')) { process.open=true; process.dispatchEvent(new document.defaultView.Event('toggle')); }
const firstVm = feature.selectConversationViewModel(firstState);
const blockOrder = firstVm.mainLane.turns[0].blocks.map(block => block.blockId);
const firstNode = document.querySelector(
  '[data-block-id="injection:user-steer:round-2"]');
firstNode.querySelector('details').open = false;
controller.render(conversation, secondState);
scheduled.shift()();
for (const process of document.querySelectorAll('.conversation-history-process')) { process.open=true; process.dispatchEvent(new document.defaultView.Event('toggle')); }
const secondNode = document.querySelector(
  '[data-block-id="injection:user-steer:round-2"]');
console.log(JSON.stringify({
  native:Boolean(firstVm.mainLane.turns[0]),
  blockOrder,
  identity:firstVm.mainLane.turns[0].blocks.find(block => block.kind === 'injections')
    .identitySource,

  injectionRenders,
  sameNode:firstNode === secondNode,
  stayedClosed:!secondNode.querySelector('details').open,
  revisedText:document.querySelector('[data-block-id="text:terminal"] .md-content')
    .textContent,
}));
controller.dispose();
dom.window.close();
""")
    assert result == {
        "native": True,
        "blockOrder": [
            "text:llm-0",
            "injection:user-steer:round-2",
            "tool:call-2",
            "text:terminal",
        ],
        "identity": "contract",

        "injectionRenders": 1,
        "sameNode": True,
        "stayedClosed": True,
        "revisedText": "revised answer",
    }


def test_terminal_footer_port_keeps_rich_metadata_on_the_native_turn_node(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const makeTurn = (revision, tokens) => ({turnId:'turn-rich-footer',
  conversationId:'conv-a', laneId:'main', ordinal:1, actor:'assistant', kind:'reply',
  runId:'task-a', status:'completed', currentAttemptId:null,
  projection:{segments:[{type:'text', blockId:'text:terminal', text:'done',
    deliverable:true, terminal:true}], content:'done', model:'actual-model',
    providerId:'provider-a', usage:{input_tokens:tokens, output_tokens:2}},
  projectionRevision:revision, settlement:{outcome:'completed'}, createdAt:1,
  updatedAt:revision});
const state = turn => ({conversationId:'conv-a',
  conversationRevision:turn.projectionRevision, transport:'live',
  turnsById:{'turn-rich-footer':turn}, laneOrder:{main:['turn-rich-footer']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{}});
const scheduled = [];

let footerRenders = 0;
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
    renderTurnFooterHtml:turn => {
      footerRenders += 1;
      return `<span class="rich-footer">${turn.metadata.model} · ${
        turn.metadata.usage.input_tokens}</span>`;
    },
  }),
});
const conversation = {id:'conv-a'};
controller.render(conversation, state(makeTurn(1, 10)));
scheduled.shift()();
const first = document.querySelector('[data-conversation-part="turn-footer"]');
controller.render(conversation, state(makeTurn(2, 20)));
scheduled.shift()();
const second = document.querySelector('[data-conversation-part="turn-footer"]');
console.log(JSON.stringify({

  footerRenders,
  sameNode:first === second,
  footer:second.textContent,
}));
controller.dispose();
dom.window.close();
""")
    assert result == {

        "footerRenders": 2,
        "sameNode": True,
        "footer": "actual-model · 20",
    }


def test_stray_flat_turn_footer_is_collapsed_not_duplicated(
        conversation_bundle: Path):
    """A flat-schema swap can leave a second `turn-footer` beside the managed
    one (the telemetry strip then renders twice). The adoption loop must keep
    exactly one."""
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const makeTurn = (revision) => ({turnId:'turn-a',
  conversationId:'conv-a', laneId:'main', ordinal:1, actor:'assistant', kind:'reply',
  runId:'task-a', status:'completed', currentAttemptId:null,
  projection:{segments:[{type:'text', blockId:'text:t', text:'done',
    deliverable:true, terminal:true}], content:'done', model:'m',
    providerId:'p', usage:{input_tokens:1, output_tokens:2}},
  projectionRevision:revision, settlement:{outcome:'completed'}, createdAt:1,
  updatedAt:revision});
const state = turn => ({conversationId:'conv-a',
  conversationRevision:turn.projectionRevision, transport:'live',
  turnsById:{'turn-a':turn}, laneOrder:{main:['turn-a']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{}});
const scheduled = [];
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
    renderTurnFooterHtml:() => '<span class="rich-footer">telemetry</span>',
  }),
});
const conversation = {id:'conv-a'};
controller.render(conversation, state(makeTurn(1)));
scheduled.shift()();
const turnNode = document.querySelector('[data-turn-id="turn-a"]');
// Simulate the short-lived flat schema: a footer as a DIRECT child of the
// turn node (not nested inside turn-content), created after the nested one.
const stray = document.createElement('footer');
stray.dataset.conversationPart = 'turn-footer';
stray.innerHTML = '<span class="rich-footer">telemetry</span>';
turnNode.appendChild(stray);
const before = turnNode.querySelectorAll(
  '[data-conversation-part="turn-footer"]').length;
controller.render(conversation, state(makeTurn(2)));
scheduled.shift()();
const after = document.querySelectorAll(
  '[data-turn-id="turn-a"] [data-conversation-part="turn-footer"]').length;
console.log(JSON.stringify({before, after}));
controller.dispose();
dom.window.close();
""")
    assert result == {"before": 2, "after": 1}


def test_orchestration_is_header_metadata_not_an_opaque_content_block(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const makeTurn = (id, ordinal, actor, text, orchestration) => ({turnId:id,
  conversationId:'conv-a', laneId:'main', ordinal, actor, kind:`endpoint_${actor}`,
  runId:'run-a', status:'completed', currentAttemptId:null,
  projection:{segments:[{type:'text', blockId:'text:terminal', text,
    deliverable:true, terminal:true}], content:text, orchestration},
  projectionRevision:1, settlement:{outcome:'completed'}, createdAt:ordinal,
  updatedAt:ordinal});
const planner = makeTurn('planner', 1, 'planner', 'Plan body', {iteration:1});
const critic = makeTurn('critic', 2, 'critic', 'Please revise', {
  iteration:2, approved:false, nextPhase:'planner', stuck:false});
const state = {conversationId:'conv-a', conversationRevision:2, transport:'live',
  turnsById:{planner, critic}, laneOrder:{main:['planner','critic']}, attemptsById:{},
  queueItems:[], pendingEventsByTurn:{}, commandPending:{}, liveRoundUsageByTurn:{}};
const scheduled = [];

const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
    localizedText:(_key, fallback) => fallback,
  }),
});
controller.render({id:'conv-a'}, state);
scheduled.shift()();
const vm = feature.selectConversationViewModel(state);
const payload = {

  native:vm.mainLane.turns.map(() => true),
  blockKinds:vm.mainLane.turns.map(turn => turn.blocks.map(block => block.kind)),
  roles:Array.from(document.querySelectorAll('.message-role')).map(node => node.textContent),
  badges:Array.from(document.querySelectorAll('.ep-verdict-badge'))
    .map(node => [node.classList[1], node.textContent]),
};
controller.dispose();
dom.window.close();
console.log(JSON.stringify(payload));
""")
    assert result == {

        "native": [True, True],
        "blockKinds": [["text"], ["text"]],
        "roles": ["Planner", "Critic"],
        "badges": [
            ["ep-verdict-planner", "Plan"],
            ["ep-verdict-replan", "Replan"],
        ],
    }


def test_provenance_is_one_keyed_block_and_actionable_legacy_rows_fail_closed(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const provenance = {blockId:'provenance',
  memoryPrefetch:{phase:'done', selected:1},
  preferencesApplied:{chars:20, items:['focused tests']},
  relatedConversations:{count:2, items:[]},
  preferencesLearned:[{kind:'added', summary:'Prefer stable IDs'}]};
const makeTurn = (revision, text, value = provenance) => ({turnId:'turn-prov',
  conversationId:'conv-a', laneId:'main', ordinal:1, actor:'assistant', kind:'reply',
  runId:'task-a', status:'completed', currentAttemptId:null,
  projection:{segments:[{type:'text', blockId:'text:terminal', text,
    deliverable:true, terminal:true}], content:text, provenance:value},
  projectionRevision:revision, settlement:{outcome:'completed'}, createdAt:1,
  updatedAt:revision});
const state = turn => ({conversationId:'conv-a',
  conversationRevision:turn.projectionRevision, transport:'live',
  turnsById:{'turn-prov':turn}, laneOrder:{main:['turn-prov']}, attemptsById:{},
  queueItems:[], pendingEventsByTurn:{}, commandPending:{}, liveRoundUsageByTurn:{}});
const scheduled = [];

let provenanceRenders = 0;
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
    renderProvenanceBlockHtml:() => {
      provenanceRenders += 1;
      return '<details open class="native-provenance"><summary>Context</summary></details>';
    },
  }),
});
const conversation = {id:'conv-a'};
const firstState = state(makeTurn(1, 'first'));
controller.render(conversation, firstState);
scheduled.shift()();
const vm = feature.selectConversationViewModel(firstState);
const first = document.querySelector('[data-block-id="provenance"]');
first.querySelector('details').open = false;
controller.render(conversation, state(makeTurn(2, 'second')));
scheduled.shift()();
const second = document.querySelector('[data-block-id="provenance"]');
const actionable = makeTurn(3, 'third', {...provenance,
  preferencesLearned:[{kind:'pending', summary:'Ask first', pending:true}]});
console.log(JSON.stringify({
  native:Boolean(vm.mainLane.turns[0]),
  blockOrder:vm.mainLane.turns[0].blocks.map(block => [block.kind, block.blockId]),

  provenanceRenders,
  sameNode:first === second,
  stayedClosed:!second.querySelector('details').open,
  actionableNative:Boolean(
    feature.selectConversationViewModel(state(actionable)).mainLane.turns[0]),
}));
controller.dispose();
dom.window.close();
""")
    assert result == {
        "native": True,
        "blockOrder": [["provenance", "provenance"], ["text", "text:terminal"]],

        "provenanceRenders": 1,
        "sameNode": True,
        "stayedClosed": True,
        "actionableNative": True,
    }


def test_rolled_back_tail_stays_visible_above_the_resumed_lane(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const rolledBack = [{blockId:'rolled-back', attemptId:'attempt-1', at:7,
  thinking:'old **reasoning**', content:'old draft answer'}];
const makeTurn = (revision, text, rolled = rolledBack) => ({
  turnId:'turn-rb', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'task-rb', status:'completed',
  currentAttemptId:'attempt-2', projectionRevision:revision,
  projection:{
    segments:[
      {type:'tool_use', blockId:'tool:1', id:'call-1', name:'run_command',
        input:{}, result:{}},
      {type:'thinking', blockId:'thinking:terminal', text:'new reasoning',
        terminal:true},
      {type:'text', blockId:'text:terminal', text, deliverable:true,
        terminal:true},
    ],
    content:text, thinking:'new reasoning', rolledBack:rolled,
  },
  settlement:{outcome:'completed'}, createdAt:1, updatedAt:revision});
const state = turn => ({conversationId:'conv-a',
  conversationRevision:turn.projectionRevision, transport:'live',
  turnsById:{'turn-rb':turn}, laneOrder:{main:['turn-rb']}, attemptsById:{},
  queueItems:[], pendingEventsByTurn:{}, commandPending:{}, liveRoundUsageByTurn:{}});
const scheduled = [];
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
  }),
});
const conversation = {id:'conv-a'};
controller.render(conversation, state(makeTurn(1, 'first')));
scheduled.shift()();
for (const process of document.querySelectorAll('.conversation-history-process')) { process.open=true; process.dispatchEvent(new document.defaultView.Event('toggle')); }
const vm = feature.selectConversationViewModel(state(makeTurn(1, 'first')));
const firstBlock = document.querySelector('[data-block-kind="rolled-back"]');
const firstThinking = firstBlock.querySelector('.thinking-block.thinking-prior');
const firstContent = firstBlock.querySelector('.thinking-block.content-prior');
controller.render(conversation, state(makeTurn(2, 'second')));
scheduled.shift()();
for (const process of document.querySelectorAll('.conversation-history-process')) { process.open=true; process.dispatchEvent(new document.defaultView.Event('toggle')); }
const secondBlock = document.querySelector('[data-block-kind="rolled-back"]');
const emptyVm = feature.selectConversationViewModel(state(makeTurn(3, 'third',
  [{blockId:'rolled-back', thinking:'  ', content:''}])));
console.log(JSON.stringify({
  blockOrder:vm.mainLane.turns[0].blocks.map(block => [block.kind, block.blockId]),
  thinkingText:firstThinking.querySelector('.thinking-text').textContent,
  contentText:firstContent.querySelector('.thinking-text').textContent,
  collapsed:!firstThinking.open && !firstContent.open,
  labels:[...firstBlock.querySelectorAll('.thinking-label')]
    .map(el => el.textContent),
  sameNode:firstBlock === secondBlock,
  emptyFiltered:!emptyVm.mainLane.turns[0].blocks
    .some(block => block.kind === 'rolled-back'),
}));
controller.dispose();
dom.window.close();
""")
    assert result == {
        "blockOrder": [
            ["tool", "tool:1"],
            ["rolled-back", "rolled-back"],
            ["thinking", "thinking:terminal"],
            ["text", "text:terminal"],
        ],
        "thinkingText": "old **reasoning**",
        "contentText": "old draft answer",
        "collapsed": True,
        "labels": [
            "Earlier Thinking (rolled back)",
            "Interrupted Draft (rolled back)",
        ],
        "sameNode": True,
        "emptyFiltered": True,
    }

def test_file_changes_use_stable_turn_intents_and_keep_their_keyed_dom(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const makeTurn = (revision, state) => ({
  turnId:'turn-files', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'task-files', status:'completed',
  currentAttemptId:'attempt-files', projectionRevision:revision,
  projection:{
    content:'done',
    segments:[{type:'text', blockId:'text:terminal', text:'done',
      deliverable:true, terminal:true}],
    modifiedFiles:1,
    modifiedFileList:[{path:'src/app.ts', action:'modified'}],
    fileChanges:{blockId:'file-changes', taskId:'task-files', count:1, state,
      files:[{path:'src/app.ts', action:'modified'}]},
  },
  settlement:{outcome:'completed'}, createdAt:1, updatedAt:revision,
});
const makeState = turn => ({conversationId:'conv-a',
  conversationRevision:turn.projectionRevision, transport:'live',
  turnsById:{'turn-files':turn}, laneOrder:{main:['turn-files']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{}});
const scheduled = [];
const intents = [];

const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
  }),
  onIntent:intent => intents.push(intent),
});
const conversation = {id:'conv-a'};
const firstState = makeState(makeTurn(1, 'applied'));
controller.render(conversation, firstState);
scheduled.shift()();
const vm = feature.selectConversationViewModel(firstState);
const first = document.querySelector('[data-block-id="file-changes"]');
first.querySelector('details').open = false;
const undoAction = first.querySelector('[data-conversation-action="undo-turn-files"]');
const actionInSummary = undoAction.closest('summary') === first.querySelector('summary');
undoAction.click();
const closedAfterAction = !first.querySelector('details').open;
controller.render(conversation, makeState(makeTurn(2, 'undone')));
scheduled.shift()();
const second = document.querySelector('[data-block-id="file-changes"]');
second.querySelector('[data-conversation-action="redo-turn-files"]').click();
const remoteTurn = makeTurn(3, 'applied');
remoteTurn.projection.fileChanges.undoAvailable = false;
remoteTurn.projection.fileChanges.count = 512;
controller.render(conversation, makeState(remoteTurn));
scheduled.shift()();
const remoteUndoHidden = !document.querySelector('[data-conversation-action="undo-turn-files"]');
const truncatedTurn = structuredClone(remoteTurn);
truncatedTurn.projectionRevision = 4;
truncatedTurn.projection.fileChanges.truncated = true;
controller.render(conversation, makeState(truncatedTurn));
scheduled.shift()();
const truncatedLabel = document.querySelector('[data-block-id="file-changes"] summary').textContent.includes('512+');
console.log(JSON.stringify({
  remoteUndoHidden, truncatedLabel,
  native:Boolean(vm.mainLane.turns[0]),
  blockKinds:vm.mainLane.turns[0].blocks.map(block => block.kind),
  actionInSummary,
  closedAfterAction,
  sameNode:first === second,
  stayedClosed:!second.querySelector('details').open,
  actions:intents.map(intent => ({type:intent.type, turnId:intent.turnId,
    blockId:intent.blockId})),
  oldUndoAll:document.querySelector('[data-conversation-action="undo-all-files"]') !== null,
}));
controller.dispose();
dom.window.close();
""")
    assert result == {
        "native": True,
        "blockKinds": ["text", "file-changes"],
        "remoteUndoHidden": True,
        "truncatedLabel": True,
        "actionInSummary": True,
        "closedAfterAction": True,
        "sameNode": True,
        "stayedClosed": True,
        "actions": [
            {
                "type": "undo-turn-files",
                "turnId": "turn-files",
                "blockId": "file-changes",
            },
            {
                "type": "redo-turn-files",
                "turnId": "turn-files",
                "blockId": "file-changes",
            },
        ],
        "oldUndoAll": False,
    }


def test_origin_context_and_compaction_are_native_typed_blocks(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const turn = (turnId, ordinal, actor, projection) => ({
  turnId, conversationId:'conv-a', laneId:'main', ordinal, actor,
  kind:actor === 'human' ? 'input' : 'reply', runId:'', status:'completed',
  currentAttemptId:null, projection, projectionRevision:1,
  settlement:{outcome:'completed'}, createdAt:1, updatedAt:1,
});
const brain = turn('turn-brain', 1, 'human', {
  content:'work', segments:[{type:'text', blockId:'text:terminal', text:'work',
    deliverable:true, terminal:true}],
  origin:{blockId:'origin', initiator:'brain', boardTaskId:'epic-a', brain:{
    epicId:'epic-a', epicTitle:'Typed architecture', originatorConv:'conv-source',
    originatorTitle:'Source conversation', route:'creator', method:'posted',
    answered:true,
  }},
  contextSnapshot:{blockId:'turn-context', snapshot:{model:'model-a', depth:'high',
    tools:[{label:'Code'}], roots:[{path:'/workspace/project', short:'project'}]}},
});
const compacted = turn('turn-compact', 2, 'assistant', {
  content:'## Context compacted\n\n**Durable summary**',
  segments:[{type:'text', blockId:'text:terminal',
    text:'## Context compacted\n\n**Durable summary**',
    deliverable:true, terminal:true}],
  compaction:{blockId:'compaction', archiveId:'archive-a',
    tokensBefore:9000, tokensAfter:1200, reductionPercent:87},
});
const state = {conversationId:'conv-a', conversationRevision:2, transport:'live',
  turnsById:{'turn-brain':brain, 'turn-compact':compacted},
  laneOrder:{main:['turn-brain','turn-compact']}, attemptsById:{}, queueItems:[],
  pendingEventsByTurn:{}, commandPending:{}, liveRoundUsageByTurn:{}};
const scheduled = [];
const intents = [];
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => `<strong>${value.replaceAll('*','')}</strong>`,
  }),
  onIntent:intent => intents.push(intent),
});
controller.render({id:'conv-a'}, state);
scheduled.shift()();
const brainNode = document.querySelector('[data-turn-id="turn-brain"]');
const compactNode = document.querySelector('[data-turn-id="turn-compact"]');
brainNode.querySelector('[data-conversation-action="open-project-brain"]').click();
compactNode.querySelector('[data-conversation-action="open-compaction"]').click();
console.log(JSON.stringify({
  blocks:[...document.querySelectorAll('[data-block-id]')].map(node => node.dataset.blockId),
  brainTitle:brainNode.querySelector('.bdc-epic-title').textContent,
  role:brainNode.querySelector('.message-role').textContent,
  contextFold:brainNode.querySelector('.tctx-fold').textContent,
  contextFoldTitle:brainNode.querySelector('.tctx-fold').getAttribute('title'),
  contextRail:brainNode.querySelector('.turn-ctx').textContent,
  avatarIsDirect:brainNode.querySelector(':scope > .message-avatar') !== null,
  contentIsDirect:brainNode.querySelector(':scope > .message-content') !== null,
  railIsDirect:brainNode.querySelector(':scope > .turn-ctx') !== null,
  foldLivesInContent:brainNode.querySelector(
    ':scope > .message-content .tctx-fold') !== null,
  railAbsentFromContent:brainNode.querySelector(
    ':scope > .message-content .turn-ctx') === null,
  exactTurnShell:[...brainNode.children].map(
    node => node.dataset.conversationPart),
  emptyHumanFooterHidden:brainNode.querySelector(
    '[data-conversation-part="turn-footer"]')?.hidden,
  compactSummary:compactNode.querySelector('.compact-card-body').textContent,
  terminalTextRemoved:!compactNode.querySelector('[data-block-id="text:terminal"]'),
  intents:intents.map(intent => [intent.type, intent.operation || '']),
  compatibilityExports:[
    typeof feature.createLegacyConversationSurfaceController,
    typeof feature.canRenderSimpleConversationTurnNatively,
  ],
}));
""")
    assert result == {
        "blocks": ["origin", "turn-context", "text:terminal", "compaction"],
        "brainTitle": "Typed architecture",
        "role": "Project Brain",
        "contextFold": "model-a · high · 1 tools · 1 workspaces",
        "contextFoldTitle": "model-a · high · 1 tools · 1 workspaces",
        "contextRail": "model-ahighToolsCodeWorkspaceproject",
        "avatarIsDirect": True,
        "contentIsDirect": True,
        "railIsDirect": True,
        "foldLivesInContent": True,
        "railAbsentFromContent": True,
        "exactTurnShell": [
            "turn-avatar", "turn-content", "turn-context-rail",
        ],
        "emptyHumanFooterHidden": True,
        "compactSummary": "Durable summaryView pre-compaction snapshot",
        "terminalTextRemoved": True,
        "intents": [
            ["open-project-brain", ""],
            ["open-compaction", "archive-a"],
        ],
        "compatibilityExports": ["undefined", "undefined"],
    }


def test_context_fold_line_rides_the_localized_text_port(
        conversation_bundle: Path):
    """The fold line (and its truncation tooltip) is user-facing copy: the
    counts must come through ``ports.localizedText`` so a zh UI never shows
    '5 tools · 1 ws' — full words, localized, and the title carries the
    same complete line for the ellipsized state."""
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const zh = {
  'turnCtx.toolsLabel':'工具',
  'turnCtx.workspaceLabel':'工作区',
  'turnCtx.toolCount':'{count} 个工具',
  'turnCtx.workspaceCount':'{count} 个工作区',
};
const human = {
  turnId:'turn-ctx', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'human', kind:'input', runId:'', status:'completed',
  currentAttemptId:null, projectionRevision:1,
  projection:{
    content:'work',
    segments:[{type:'text', blockId:'text:terminal', text:'work',
      deliverable:true, terminal:true}],
    contextSnapshot:{blockId:'turn-context', snapshot:{
      model:'model-a', depth:'high',
      tools:[{label:'Code'}, {label:'Browser'}, {label:'Memory'},
        {label:'Fetch'}, {label:'Search'}],
      roots:[{path:'/workspace/project', short:'project'}]}},
  },
  settlement:{outcome:'completed'}, createdAt:1, updatedAt:1,
};
const state = {conversationId:'conv-a', conversationRevision:1,
  transport:'live', turnsById:{'turn-ctx':human},
  laneOrder:{main:['turn-ctx']}, attemptsById:{}, queueItems:[],
  pendingEventsByTurn:{}, commandPending:{}, liveRoundUsageByTurn:{}};
const scheduled = [];
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => `<strong>${value}</strong>`,
    localizedText:(key, fallback, values) => {
      let s = Object.prototype.hasOwnProperty.call(zh, key) ? zh[key] : fallback;
      if (values) {
        for (const [name, val] of Object.entries(values)) {
          s = s.replaceAll('{' + name + '}', String(val));
        }
      }
      return s;
    },
  }),
  onIntent:() => {},
});
controller.render({id:'conv-a'}, state);
scheduled.shift()();
const node = document.querySelector('[data-turn-id="turn-ctx"]');
console.log(JSON.stringify({
  fold:node.querySelector('.tctx-fold').textContent,
  foldTitle:node.querySelector('.tctx-fold').getAttribute('title'),
  rail:node.querySelector('.turn-ctx').textContent,
}));
""")
    assert result == {
        "fold": "model-a · high · 5 个工具 · 1 个工作区",
        "foldTitle": "model-a · high · 5 个工具 · 1 个工作区",
        "rail": "model-ahigh工具CodeBrowserMemoryFetchSearch工作区project",
    }


def test_image_generation_is_a_native_typed_block(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const turn = {
  turnId:'image-turn', conversationId:'conv-a', laneId:'main', parentTurnId:null,
  ordinal:0, actor:'assistant', kind:'image_generation_result', runId:'',
  status:'completed', currentAttemptId:null, projectionRevision:1,
  settlement:{outcome:'completed'}, createdAt:1, updatedAt:2,
  projection:{content:'caption', segments:[{type:'text', blockId:'text:terminal',
    text:'caption', terminal:true, deliverable:true}], imageGeneration:{
      blockId:'image-generation', mode:'batch', status:'completed', results:[
        {ok:true, prompt:'lighthouse', model:'image-a', imageUrl:'/generated/a.png',
          elapsedSeconds:2.5},
        {ok:false, prompt:'lighthouse', model:'image-b', error:'rate limited',
          errorType:'rate_limited'},
      ],
    }},
};
const state = {conversationId:'conv-a', conversationRevision:1, transport:'live',
  turnsById:{'image-turn':turn}, laneOrder:{main:['image-turn']}, attemptsById:{},
  queueItems:[], pendingEventsByTurn:{}, commandPending:{}, liveRoundUsageByTurn:{}};
const scheduled = [];
const intents = [];
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
  }),
  onIntent:intent => intents.push([intent.type, intent.operation]),
});
controller.render({id:'conv-a'}, state);
scheduled.shift()();
document.querySelector('[data-conversation-action="preview-generated-image"]').click();
document.querySelector('[data-conversation-action="retry-image-generation"]').click();
console.log(JSON.stringify({
  blocks:[...document.querySelectorAll('[data-block-id]')].map(node => node.dataset.blockId),
  image:document.querySelector('.ig-result-card img')?.getAttribute('src'),
  error:document.querySelector('.ig-error-text')?.textContent,
  intents,
}));
""")
    assert result == {
        "blocks": ["text:terminal", "image-generation"],
        "image": "/generated/a.png",
        "error": "rate limited",
        "intents": [
            ["preview-generated-image", "0"],
            ["retry-image-generation", "1"],
        ],
    }


def test_transient_turn_overlay_never_mutates_durable_state(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const durable = {
  turnId:'turn-a', conversationId:'conv-a', laneId:'main', parentTurnId:null,
  ordinal:0, actor:'assistant', kind:'reply', runId:'', status:'completed',
  currentAttemptId:null, projection:{content:'durable', segments:[]},
  projectionRevision:1, settlement:{outcome:'completed'}, createdAt:1, updatedAt:1,
};
const state = {conversationId:'conv-a', conversationRevision:1, transport:'live',
  turnsById:{'turn-a':durable}, laneOrder:{main:['turn-a']}, attemptsById:{},
  queueItems:[], pendingEventsByTurn:{}, commandPending:{}, liveRoundUsageByTurn:{}};
const overlay = feature.createTransientTurnOverlay();
overlay.upsert({...durable, status:'running', projectionRevision:2,
  projection:{content:'local running', segments:[]}});
const composed = overlay.compose(state);
const removed = overlay.remove('conv-a', 'turn-a');
console.log(JSON.stringify({
  base:state.turnsById['turn-a'].projection.content,
  visible:composed.turnsById['turn-a'].projection.content,
  order:composed.laneOrder.main,
  changed:composed !== state,
  removed,
  restored:overlay.compose(state) === state,
}));
""")
    assert result == {
        "base": "durable",
        "visible": "local running",
        "order": ["turn-a"],
        "changed": True,
        "removed": True,
        "restored": True,
    }


def test_optimistic_user_turn_mirrors_the_authoritative_human_turn(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const turn = feature.createOptimisticUserTurn({
  conversationId:'conv-a', commandId:'cmd-echo-1', text:'echo me', timestamp:10,
  images:[{preview:'/p.png'}],
  pdfTexts:[{name:'a.pdf'}],
  videos:[{name:'v.mp4'}],
  replyQuotes:['quoted line'],
  convRefs:[{id:'conv-b', title:'Other'}],
  contextSnapshot:{composeContextSummary:{text:'ctx note'}},
});
const overlay = feature.createTransientTurnOverlay();
overlay.upsert(turn);
const state = {conversationId:'conv-a', conversationRevision:1, transport:'live',
  turnsById:{}, laneOrder:{main:[]}, attemptsById:{}, queueItems:[],
  pendingEventsByTurn:{}, commandPending:{}, liveRoundUsageByTurn:{}};
const vm = feature.selectConversationViewModel(overlay.compose(state));
const echo = vm.mainLane.turns[0];
console.log(JSON.stringify({
  idHelper: feature.optimisticUserTurnId('cmd-echo-1'),
  turnId: echo.turnId,
  role: echo.role,
  kind: echo.kind,
  status: echo.status,
  live: vm.mainLane.live,
  blocks: echo.blocks.map((block) => [block.kind, block.blockId]),
  markdown: echo.blocks[echo.blocks.length - 1].markdown,
  images: echo.blocks[1].images?.length,
  removed: overlay.remove('conv-a', turn.turnId),
  restored: overlay.compose(state) === state,
}));
""")
    assert result == {
        "idHelper": "transient:outgoing:cmd-echo-1",
        "turnId": "transient:outgoing:cmd-echo-1",
        "role": "user",
        "kind": "input",
        "status": "completed",
        "live": False,
        "blocks": [
            ["context", "turn-context"],
            ["attachments", "attachments"],
            ["text", "text:terminal"],
        ],
        "markdown": "echo me",
        "images": 1,
        "removed": True,
        "restored": True,
    }


def test_autopilot_vu_frames_reduce_to_one_keyed_transient_turn(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
let turn = feature.createAutopilotVuTransientTurn({
  conversationId:'conv-a', vuMsgId:'vu-1', timestamp:10,
});
const initial = turn;
turn = feature.reduceAutopilotVuTransientTurn(turn, {
  type:'autopilot_vu_event', vuMsgId:'vu-1', inner:{
    type:'delta', thinking:'reason ', content:'hello\n[VU: TASK_DONE]',
  },
}, 11);
turn = feature.reduceAutopilotVuTransientTurn(turn, {
  type:'autopilot_vu_event', vuMsgId:'vu-1', inner:{
    type:'tool_start', roundNum:1, toolCallId:'call-1', toolName:'search',
    toolArgs:{q:'typed turns'}, query:'typed turns',
  },
}, 12);
const toolBlockBefore = turn.projection.segments.find(s => s.type === 'tool_use');
turn = feature.reduceAutopilotVuTransientTurn(turn, {
  type:'autopilot_vu_event', vuMsgId:'vu-1', inner:{
    type:'tool_result', roundNum:1, toolCallId:'call-1', results:[{title:'hit'}],
  },
}, 13);
const vm = feature.selectConversationViewModel({
  conversationId:'conv-a', conversationRevision:0, transport:'live',
  turnsById:{[turn.turnId]:turn}, laneOrder:{main:[turn.turnId]},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{},
});
const terminal = feature.settleAutopilotVuTransientTurn(
  turn, 'conv-a', 'vu-1', {
    _turnId:'turn-vu-durable', content:'backend final', thinking:'backend reason',
    segments:[{type:'text', blockId:'text:terminal', text:'backend final',
      deliverable:true, terminal:true}], timestamp:14,
  }, 14,
);
console.log(JSON.stringify({
  id:turn.turnId,
  actor:turn.actor,
  initialUnchanged:initial.projection.content === ''
    && initial.projectionRevision === 1,
  masked:turn.projection.content,
  blockIds:turn.projection.segments.map(segment => segment.blockId),
  toolStable:toolBlockBefore.blockId === turn.projection.segments
    .find(segment => segment.type === 'tool_use').blockId,
  toolStatus:turn.projection.segments
    .find(segment => segment.type === 'tool_use').result.status,
  viewBlocks:vm.mainLane.turns[0].blocks.map(block => block.kind),
  actionCount:vm.mainLane.turns[0].actions.length,
  finalId:terminal.turnId,
  finalStatus:terminal.status,
  finalContent:terminal.projection.content,
  finalBlocks:terminal.projection.segments.map(segment => segment.blockId),
  transientCleared:terminal.transientPresentation == null,
}));
""")
    assert result == {
        "id": "transient:autopilot-vu:vu-1",
        "actor": "virtual_user",
        "initialUnchanged": True,
        "masked": "hello",
        "blockIds": [
            "thinking:autopilot-live",
            "tool:call-1",
            "text:autopilot-live",
        ],
        "toolStable": True,
        "toolStatus": "done",
        "viewBlocks": ["thinking", "tool", "text", "live-status"],
        "actionCount": 0,
        "finalId": "turn-vu-durable",
        "finalStatus": "completed",
        "finalContent": "backend final",
        "finalBlocks": ["text:terminal"],
        "transientCleared": True,
    }


def test_autopilot_vu_live_status_reuses_surface_nodes(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
let turn = feature.createAutopilotVuTransientTurn({
  conversationId:'conv-a', vuMsgId:'vu-1', timestamp:1,
});
const overlay = feature.createTransientTurnOverlay();
const durable = {conversationId:'conv-a', conversationRevision:1, transport:'live',
  turnsById:{}, laneOrder:{main:[]}, attemptsById:{}, queueItems:[],
  pendingEventsByTurn:{}, commandPending:{}, liveRoundUsageByTurn:{}};
const scheduled = [];
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
    localizedText:(key, fallback, values) => key === 'stream.phase.chars'
      ? String(values.n) + ' chars' : fallback,
  }),
});
overlay.upsert(turn);
controller.render({id:'conv-a'}, overlay.compose(durable));
scheduled.shift()();
const turnNode = document.querySelector('[data-turn-id]');
const statusNode = document.querySelector('[data-block-id="live-status"]');
const initialBlocks = [...turnNode.querySelectorAll('[data-block-id]')]
  .map(node => node.dataset.blockId);
turn = feature.reduceAutopilotVuTransientTurn(turn, {
  type:'autopilot_vu_event', vuMsgId:'vu-1',
  inner:{type:'delta', thinking:'abc'},
}, 2);
overlay.upsert(turn);
controller.render({id:'conv-a'}, overlay.compose(durable));
scheduled.shift()();
console.log(JSON.stringify({
  sameTurn:turnNode === document.querySelector('[data-turn-id]'),
  sameStatus:statusNode === document.querySelector('[data-block-id="live-status"]'),
  initialBlocks,
  role:turnNode.querySelector('.message-role').textContent,
  status:statusNode.textContent,
  visibleRunningBlocks:[...turnNode.querySelectorAll('[data-block-id]')]
    .map(node => node.dataset.blockId),
  emptyRunningFooterHidden:turnNode.querySelector(
    '[data-conversation-part="turn-footer"]')?.hidden,
  durableCount:Object.keys(durable.turnsById).length,
  durableOrder:durable.laneOrder.main.length,
}));
controller.dispose();
dom.window.close();
""")
    assert result == {
        "sameTurn": True,
        "sameStatus": True,
        "initialBlocks": ["live-status"],
        "role": "Autopilot",
        "status": "Thinking…3 chars...",
        "visibleRunningBlocks": ["thinking:autopilot-live", "live-status"],
        "emptyRunningFooterHidden": True,
        "durableCount": 0,
        "durableOrder": 0,
    }


def test_plan_documents_select_and_render_without_duplicate_tagged_prose(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const plan = {
  turnId:'plan-turn', conversationId:'conv-a', laneId:'main', parentTurnId:null,
  ordinal:1, actor:'assistant', kind:'reply', runId:'', status:'completed',
  currentAttemptId:null, projectionRevision:7,
  settlement:{outcome:'completed'}, createdAt:1, updatedAt:1,
  projection:{
    content:'Ready.\n\n<proposed_plan>\n## Steps\n- change parser\n</proposed_plan>\n\nPostscript.',
    translatedContent:'准备。\n\n<proposed_plan>\n## 步骤\n- 修改解析器\n</proposed_plan>\n\n后记。',
    segments:[{type:'text', blockId:'text:terminal',
      text:'Ready.\n\n<proposed_plan>\n## Steps\n- change parser\n</proposed_plan>\n\nPostscript.',
      terminal:true, deliverable:true}],
    proposedPlan:{blockId:'proposed-plan', planId:'plan-a', revision:1,
      format:'markdown', text:'## Steps\n- change parser'},
  },
};
const execution = {
  turnId:'execution-input', conversationId:'conv-a', laneId:'main',
  parentTurnId:'plan-turn', ordinal:2, actor:'human', kind:'plan_execution',
  runId:'', status:'completed', currentAttemptId:null, projectionRevision:1,
  settlement:{outcome:'completed'}, createdAt:2, updatedAt:2,
  projection:{content:'', segments:[{type:'text', blockId:'text:terminal',
    text:'', terminal:true, deliverable:true}], planExecution:{
      blockId:'plan-execution', planId:'plan-a', sourceTurnId:'plan-turn',
      sourceProjectionRevision:7, contextMode:'fresh',
      planText:'## Steps\n- change parser',
  }},
};
const state = {conversationId:'conv-a', conversationRevision:1, transport:'live',
  turnsById:{'plan-turn':plan}, laneOrder:{main:['plan-turn']}, attemptsById:{},
  queueItems:[], pendingEventsByTurn:{}, commandPending:{}, liveRoundUsageByTurn:{}};
const vm = feature.selectConversationViewModel(state);
const partialVm = feature.selectConversationViewModel(state, {}, {
  translationActivityByTurn:new Map([['plan-turn', {
    status:'pending',
    partial:'准备。\n\n<proposed_plan>\n## 步骤\n- 正在修改',
  }]]),
});
const untranslatedPlan = {...plan, projection:{...plan.projection}};
delete untranslatedPlan.projection.translatedContent;
const untranslatedState = {...state, turnsById:{'plan-turn':untranslatedPlan}};
const emptyEnvelopeVm = feature.selectConversationViewModel(untranslatedState, {}, {
  translationActivityByTurn:new Map([['plan-turn', {
    status:'pending', partial:'<proposed_plan>',
  }]]),
});
const droppedEnvelopeState = {...state, turnsById:{'plan-turn':{
  ...plan, projection:{...plan.projection,
    translatedContent:'准备。\n\n## 步骤\n- 修改解析器\n\n后记。'},
}}};
const droppedEnvelopeVm = feature.selectConversationViewModel(droppedEnvelopeState);
const scheduled = [];
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
  }),
});
controller.render({id:'conv-a'}, state);
scheduled.shift()();
const planCard = document.querySelector('[data-block-id="proposed-plan"]');
const planTurnNode = document.querySelector('[data-turn-id="plan-turn"]');
const planDecisionNode = planTurnNode.querySelector(
  '[data-conversation-part="turn-plan-decision"]',
);
const withExecution = {...state, conversationRevision:2,
  turnsById:{'plan-turn':plan, 'execution-input':execution},
  laneOrder:{main:['plan-turn','execution-input']}};
const executionVm = feature.selectConversationViewModel(withExecution);
const planWithBranch = {...plan, projection:{...plan.projection,
  _branchLanes:[{laneId:'branch-a', title:'Alternative'}]}};
const branchPlan = {...plan, turnId:'branch-plan', laneId:'branch-a',
  parentTurnId:'plan-turn', projectionRevision:3, projection:{...plan.projection,
    proposedPlan:{...plan.projection.proposedPlan, planId:'plan-branch'}}};
const branchState = {...state, conversationRevision:3,
  turnsById:{'plan-turn':planWithBranch, 'branch-plan':branchPlan},
  laneOrder:{main:['plan-turn'], 'branch-a':['branch-plan']}};
const expandedBranchVm = feature.selectConversationViewModel(
  branchState, {}, {expandedBranchLaneId:'branch-a'},
);
const collapsedBranchVm = feature.selectConversationViewModel(branchState);
console.log(JSON.stringify({
  blockKinds:vm.mainLane.turns[0].blocks.map(block => block.kind),
  prose:vm.mainLane.turns[0].blocks.find(block => block.kind === 'text')?.displayMarkdown,
  translatedPlan:vm.mainLane.turns[0].blocks.find(
    block => block.kind === 'proposed-plan')?.displayMarkdown,
  partialPlan:partialVm.mainLane.turns[0].blocks.find(
    block => block.kind === 'proposed-plan')?.displayMarkdown,
  partialStreaming:partialVm.mainLane.turns[0].blocks.find(
    block => block.kind === 'proposed-plan')?.translationStreaming,
  partialPending:partialVm.mainLane.turns[0].metadata.translation.pending,
  emptyEnvelopePlan:emptyEnvelopeVm.mainLane.turns[0].blocks.find(
    block => block.kind === 'proposed-plan')?.displayMarkdown,
  emptyEnvelopeStreaming:emptyEnvelopeVm.mainLane.turns[0].blocks.find(
    block => block.kind === 'proposed-plan')?.translationStreaming,
  droppedEnvelopeProse:droppedEnvelopeVm.mainLane.turns[0].blocks.find(
    block => block.kind === 'text')?.displayMarkdown,
  droppedEnvelopePlan:droppedEnvelopeVm.mainLane.turns[0].blocks.find(
    block => block.kind === 'proposed-plan')?.displayMarkdown,
  decision:vm.planDecision,
  cardText:planCard.textContent,
  cardTitleI18n:planCard.querySelector('.plan-card-title')?.dataset.i18n,
  taggedTextVisible:document.getElementById('chat').textContent.includes('<proposed_plan>'),
  executionBlocks:executionVm.mainLane.turns[1].blocks.map(block => block.kind),
  decisionAfterExecution:executionVm.planDecision,
  expandedBranchDecision:expandedBranchVm.planDecision,
  collapsedBranchDecision:collapsedBranchVm.planDecision,
  decisionInsideSourceTurn:planDecisionNode?.closest('[data-turn-id]') === planTurnNode,
  decisionDirectlyAfterBlocks:planDecisionNode?.previousElementSibling
    ?.dataset.conversationPart === 'turn-blocks-later'
    && planDecisionNode.previousElementSibling.hidden
    && planDecisionNode.previousElementSibling.previousElementSibling
      ?.dataset.conversationPart === 'turn-blocks',
  decisionBeforeTurnActions:planDecisionNode?.nextElementSibling
    ?.dataset.conversationPart === 'turn-actions',
}));
controller.dispose();
dom.window.close();
""")
    assert result["blockKinds"] == ["text", "proposed-plan"]
    assert result["prose"] == "准备。\n\n后记。"
    assert result["translatedPlan"] == "## 步骤\n- 修改解析器"
    assert result["partialPlan"] == "## 步骤\n- 正在修改"
    assert result["partialStreaming"] is True
    assert result["partialPending"] is True
    assert result["emptyEnvelopePlan"] == "## Steps\n- change parser"
    assert result["emptyEnvelopeStreaming"] is False
    assert result["droppedEnvelopeProse"] == "Ready.\n\nPostscript."
    assert result["droppedEnvelopePlan"] == "## Steps\n- change parser"
    assert result["decision"] == {
        "sourceTurnId": "plan-turn",
        "sourceProjectionRevision": 7,
        "planId": "plan-a",
        "pending": False,
    }
    assert "Proposed Plan" in result["cardText"]
    assert "修改解析器" in result["cardText"]
    assert result["cardTitleI18n"] == "plan.cardTitle"
    assert result["taggedTextVisible"] is False
    assert result["executionBlocks"] == ["plan-execution"]
    assert result["decisionAfterExecution"] is None
    assert result["expandedBranchDecision"]["sourceTurnId"] == "branch-plan"
    assert result["expandedBranchDecision"]["planId"] == "plan-branch"
    assert result["collapsedBranchDecision"]["sourceTurnId"] == "plan-turn"
    assert result["decisionInsideSourceTurn"] is True
    assert result["decisionDirectlyAfterBlocks"] is True
    assert result["decisionBeforeTurnActions"] is True


def test_plan_decision_bar_keeps_composer_and_submits_explicit_context(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<section id="host"><article data-turn-id="turn-a"><div data-conversation-part="turn-blocks"></div><aside id="plan-mount" data-conversation-part="turn-plan-decision"></aside></article><div class="input-box"><textarea id="input"></textarea></div></section>');
global.Element = dom.window.Element;
const document = dom.window.document;
let release;
const executionGate = new Promise(resolve => { release = resolve; });
const executions = [];
const errors = [];
const bar = feature.createPlanDecisionBar({
  copy:() => ({title:'Plan ready', description:'Choose',
    continueDiscussion:'Continue', executeCurrent:'Current', executeFresh:'Fresh',
    executing:'Starting', freshHint:'History remains'}),
  onContinueDiscussion:() => document.getElementById('input').focus(),
  async onExecute(_conversationId, decision, mode) {
    executions.push([decision.planId, mode]);
    if (mode === 'fresh') throw new Error('Fresh failed');
    await executionGate;
  },
  onError:error => errors.push(error.message),
});
const decision = {sourceTurnId:'turn-a', sourceProjectionRevision:4,
  planId:'plan-a', pending:false};
(async () => {
  const composer = document.querySelector('.input-box');
  const mount = document.getElementById('plan-mount');
  bar.activateConversation('conv-a');
  bar.render(mount, 'conv-a', decision);
  const ariaLive = document.querySelector('.plan-decision-bar')
    .getAttribute('aria-live');
  const freshAriaLabel = document.querySelector(
    '[data-plan-decision-action="fresh"]',
  ).getAttribute('aria-label');
  const liveI18nBound = [
    mount.dataset.i18nAriaLabel,
    mount.querySelector('.plan-decision-title')?.dataset.i18n,
    mount.querySelector('.plan-decision-description')?.dataset.i18n,
    mount.querySelector('[data-plan-decision-action="continue"]')?.dataset.i18n,
    mount.querySelector('[data-plan-decision-action="current"]')?.dataset.i18n,
    mount.querySelector('[data-plan-decision-action="fresh"]')?.dataset.i18n,
    mount.querySelector('[data-plan-decision-action="fresh"]')?.dataset.i18nTitle,
    mount.querySelector('[data-plan-decision-action="fresh"]')?.dataset.i18nAriaLabel,
  ].join('|');
  document.querySelector('[data-plan-decision-action="continue"]').click();
  const focused = document.activeElement?.id;
  document.querySelector('[data-plan-decision-action="current"]').click();
  const disabledDuringSubmit = [...document.querySelectorAll('.plan-decision-button')]
    .every(button => button.disabled);
  const ariaBusyDuringSubmit = document.querySelector('.plan-decision-bar')
    .getAttribute('aria-busy');
  const composerStillEnabled = !document.getElementById('input').disabled;
  const mountedInSourceTurn = document.querySelector('.plan-decision-bar') === mount
    && mount.closest('[data-turn-id]')?.dataset.turnId === decision.sourceTurnId;
  release();
  await Promise.resolve();
  await Promise.resolve();
  const enabledAfterSubmit = [...document.querySelectorAll('.plan-decision-button')]
    .every(button => !button.disabled);
  document.querySelector('[data-plan-decision-action="fresh"]').click();
  await Promise.resolve();
  await Promise.resolve();
  const ariaBusyAfterSubmit = document.querySelector('.plan-decision-bar')
    .getAttribute('aria-busy');
  bar.activateConversation(null);
  console.log(JSON.stringify({
    focused, ariaLive, freshAriaLabel, liveI18nBound,
    disabledDuringSubmit, ariaBusyDuringSubmit, ariaBusyAfterSubmit,
    composerStillEnabled, mountedInSourceTurn, enabledAfterSubmit, executions, errors,
    composerPreserved:document.querySelector('.input-box') === composer,
    barCleared:mount.hidden && mount.childElementCount === 0,
  }));
  bar.dispose();
  dom.window.close();
})().catch(error => { console.error(error); process.exitCode = 1; });
""")
    assert result == {
        "focused": "input",
        "ariaLive": "polite",
        "freshAriaLabel": "Fresh. History remains",
        "liveI18nBound": (
            "plan.readyTitle|plan.readyTitle|plan.readyDescription|"
            "plan.continueDiscussion|plan.executeCurrent|plan.executeFresh|"
            "plan.freshHint|plan.executeFreshAria"
        ),
        "disabledDuringSubmit": True,
        "ariaBusyDuringSubmit": "true",
        "ariaBusyAfterSubmit": "false",
        "composerStillEnabled": True,
        "mountedInSourceTurn": True,
        "enabledAfterSubmit": True,
        "executions": [["plan-a", "current"], ["plan-a", "fresh"]],
        "errors": ["Fresh failed"],
        "composerPreserved": True,
        "barCleared": True,
    }


def test_plan_decision_bar_is_conversation_bound_across_async_settlement(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<section><aside id="mount-a"></aside><aside id="mount-b"></aside></section>');
global.Element = dom.window.Element;
const document = dom.window.document;
const executions = [];
const releases = {};
const bar = feature.createPlanDecisionBar({
  copy:() => ({title:'Ready', description:'Choose', continueDiscussion:'Edit',
    executeCurrent:'Current', executeFresh:'Fresh', executing:'Starting',
    freshHint:'History remains'}),
  onExecute(conversationId, decision, mode) {
    executions.push([conversationId, decision.planId, mode]);
    return new Promise(resolve => { releases[conversationId] = resolve; });
  },
});
const decisionA = {sourceTurnId:'turn-a', sourceProjectionRevision:1,
  planId:'plan-a', pending:false};
const decisionB = {sourceTurnId:'turn-b', sourceProjectionRevision:2,
  planId:'plan-b', pending:false};
(async () => {
  const mountA = document.getElementById('mount-a');
  const mountB = document.getElementById('mount-b');
  bar.activateConversation('conv-a');
  bar.render(mountA, 'conv-a', decisionA);
  const rootA = mountA;
  rootA.querySelector('[data-plan-decision-action="current"]').click();

  bar.activateConversation('conv-b');
  const clearedOnSwitch = mountA.hidden && mountA.childElementCount === 0;
  bar.render(mountA, 'conv-a', decisionA);
  const inactiveCommitIgnored = mountA.hidden && mountA.childElementCount === 0;
  bar.render(mountB, 'conv-b', decisionB);
  const rootB = mountB;
  rootB.querySelector('[data-plan-decision-action="fresh"]').click();
  const secondLocked = [...rootB.querySelectorAll('button')]
    .every(button => button.disabled);

  releases['conv-a']();
  await Promise.resolve();
  await Promise.resolve();
  const staleSettlementKeptSecondLocked = [...rootB.querySelectorAll('button')]
    .every(button => button.disabled);
  const ownerStayedBound = rootB.dataset.conversationId === 'conv-b'
    && rootB.dataset.planId === 'plan-b';

  releases['conv-b']();
  await Promise.resolve();
  await Promise.resolve();
  const secondUnlocked = [...rootB.querySelectorAll('button')]
    .every(button => !button.disabled);
  bar.activateConversation(null);
  console.log(JSON.stringify({
    clearedOnSwitch, inactiveCommitIgnored, secondLocked,
    staleSettlementKeptSecondLocked, ownerStayedBound, secondUnlocked,
    executions, clearedOnNewChat:mountB.hidden && mountB.childElementCount === 0,
  }));
  bar.dispose();
  dom.window.close();
})().catch(error => { console.error(error); process.exitCode = 1; });
""")
    assert result == {
        "clearedOnSwitch": True,
        "inactiveCommitIgnored": True,
        "secondLocked": True,
        "staleSettlementKeptSecondLocked": True,
        "ownerStayedBound": True,
        "secondUnlocked": True,
        "executions": [
            ["conv-a", "plan-a", "current"],
            ["conv-b", "plan-b", "fresh"],
        ],
        "clearedOnNewChat": True,
    }


def test_surface_windows_300_turns_and_roundtrips_without_truncating_state(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const turns = Array.from({length:300}, (_, index) => ({
  turnId:`turn-${index}`, conversationId:'conv-window', laneId:'main',
  ordinal:index, actor:'assistant', kind:'reply', status:'completed',
  currentAttemptId:null, projectionRevision:1,
  projection:{content:`turn ${index}`, segments:[{
    type:'text', blockId:'text:terminal', text:`turn ${index}`,
    deliverable:true, terminal:true,
  }]}, settlement:{outcome:'completed'}, createdAt:index, updatedAt:index,
}));
const state = {
  conversationId:'conv-window', conversationRevision:300, transport:'live',
  turnsById:Object.fromEntries(turns.map(turn => [turn.turnId, turn])),
  laneOrder:{main:turns.map(turn => turn.turnId)}, attemptsById:{},
  queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{},
};
const before = JSON.stringify(state);
const viewModel = feature.selectConversationViewModel(state);
let blockRenders = 0;
const surface = feature.createConversationSurface(
  document.getElementById('chat'),
  {renderBlock(node, block) { blockRenders += 1; node.textContent = block.markdown; }},
);
surface.render(viewModel);
const visited = new Set();
let maxTurnNodes = 0;
const visibleIds = () => Array.from(
  surface.root.querySelectorAll('[data-turn-id]'),
).map(node => node.dataset.turnId);
const observe = () => {
  const ids = visibleIds();
  maxTurnNodes = Math.max(maxTurnNodes, ids.length);
  ids.forEach(id => visited.add(id));
  return ids;
};
const initial = observe();
const earlier = surface.root.querySelector(
  '[data-conversation-window-action="earlier"]',
);
let earlierMoves = 0;
while (!earlier.disabled && earlierMoves < 20) {
  earlier.click();
  earlierMoves += 1;
  observe();
}
const top = visibleIds();
const later = surface.root.querySelector(
  '[data-conversation-window-action="later"]',
);
let laterMoves = 0;
while (!later.disabled && laterMoves < 20) {
  later.click();
  laterMoves += 1;
  observe();
}
const finalIds = visibleIds();
const finalWindow = surface.windowState;
const output = {
  initialFirst:initial[0], initialLast:initial.at(-1),
  topFirst:top[0], topLast:top.at(-1),
  finalFirst:finalIds[0], finalLast:finalIds.at(-1),
  earlierMoves, laterMoves, visited:visited.size, maxTurnNodes,
  blockRenders, finalWindow,
  durableStateUnchanged:before === JSON.stringify(state),
};
surface.dispose();
dom.window.close();
console.log(JSON.stringify(output));
""")

    assert result == {
        "initialFirst": "turn-220",
        "initialLast": "turn-299",
        "topFirst": "turn-0",
        "topLast": "turn-79",
        "finalFirst": "turn-220",
        "finalLast": "turn-299",
        "earlierMoves": 11,
        "laterMoves": 11,
        "visited": 300,
        "maxTurnNodes": 80,
        "blockRenders": 520,
        "finalWindow": {
            "start": 220,
            "end": 300,
            "total": 300,
            "maxTurns": 80,
            "batchSize": 20,
        },
        "durableStateUnchanged": True,
    }


def test_surface_tail_window_keeps_retained_turns_in_place(
        conversation_bundle: Path):
    """Advancing a full window must not move its 79 retained text subtrees."""
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const turns = Array.from({length:81}, (_, index) => ({
  turnId:`turn-${index}`, conversationId:'conv-tail', laneId:'main',
  ordinal:index, actor:index % 2 ? 'virtual_user' : 'assistant',
  kind:index % 2 ? 'autopilot_virtual_user' : 'flow_node', status:'completed',
  currentAttemptId:null, projectionRevision:1,
  projection:{content:`turn ${index}`, segments:[{
    type:'text', blockId:'text:terminal', text:`turn ${index}`,
    deliverable:true, terminal:true,
  }]}, settlement:{outcome:'completed'}, createdAt:index, updatedAt:index,
}));
const store = feature.createTurnStore('conv-tail');
let blockRenders = 0;
const surface = feature.createConversationSurface(
  document.getElementById('chat'),
  {renderBlock(node, block) { blockRenders += 1; node.textContent = block.markdown; }},
);
store.dispatch({type:'snapshot', snapshot:{conversationRevision:80,
  turns:turns.slice(0, 80), authoritativeFull:true}});
surface.render(feature.selectConversationViewModel(store.getState()));
const lane = surface.root.querySelector('[data-conversation-part="lane-turns"]');
const retained = Array.from(lane.children).slice(1);
const observer = new dom.window.MutationObserver(() => {});
observer.observe(lane, {childList:true});
store.dispatch({type:'snapshot', snapshot:{conversationRevision:81,
  turns:turns.slice(80), authoritativeFull:false}});
surface.render(feature.selectConversationViewModel(store.getState()));
const records = observer.takeRecords();
const result = {
  removed:records.flatMap(record => Array.from(record.removedNodes))
    .map(node => node.dataset.turnId),
  added:records.flatMap(record => Array.from(record.addedNodes))
    .map(node => node.dataset.turnId),
  retainedInPlace:retained.every((node, index) => lane.children[index] === node),
  mounted:lane.childElementCount, blockRenders,
  durableTurns:Object.keys(store.getState().turnsById).length,
};
observer.disconnect(); surface.dispose(); dom.window.close();
console.log(JSON.stringify(result));
""")
    assert result == {
        "removed": ["turn-0"], "added": ["turn-80"],
        "retainedInPlace": True, "mounted": 80, "blockRenders": 81,
        "durableTurns": 81,
    }


def test_surface_reveal_turn_mounts_history_and_cancels_pending_follow(conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="viewport"><div id="chat"></div></main>');
global.Element = dom.window.Element;
const doc = dom.window.document;
const viewport = doc.getElementById('viewport');
const frames = [];
const anchor = feature.createConversationViewportPort(viewport, {
  scheduleAfterLayout(callback) {
    const frame = {callback, cancelled:false}; frames.push(frame);
    return () => { frame.cancelled = true; };
  },
});
const state = feature.createTurnState('conv-a');
const append = i => {
  const id = 'turn-' + i;
  state.laneOrder.main.push(id);
  state.turnsById[id] = {turnId:id, conversationId:'conv-a', laneId:'main', ordinal:i,
    actor:'human', kind:'input', status:'completed', projection:{content:'Message '+i},
    projectionRevision:1, createdAt:i, updatedAt:i};
};
state.laneOrder.main = [];
for (let i = 0; i < 303; i++) append(i);
const surface = feature.createConversationSurface(doc.getElementById('chat'), {
  scrollAnchor:anchor, onIntent() {},
});
surface.render(feature.selectConversationViewModel(state));
const mounted = id => Boolean(doc.querySelector('[data-turn-id="'+id+'"]'));
const pendingFollow = frames.at(-1);
const initial = {old:mounted('turn-0'), tail:mounted('turn-302')};
const revealed = surface.revealTurn('turn-0');
const after = {old:mounted('turn-0'), tail:mounted('turn-302'),
  following:anchor.following, cancelled:pendingFollow.cancelled,
  count:surface.root.querySelectorAll('[data-turn-id]').length};
append(303);
surface.render(feature.selectConversationViewModel(state));
const retained = mounted('turn-0');
const missing = surface.revealTurn('not-a-turn');
surface.followLatest();
const latest = {old:mounted('turn-0'), tail:mounted('turn-303'), following:anchor.following,
  count:surface.root.querySelectorAll('[data-turn-id]').length};
surface.revealTurn('turn-302');
const mountedJumpSuspends = !anchor.following;
surface.dispose(); anchor.dispose(); dom.window.close();
console.log(JSON.stringify({initial, revealed, after, retained, missing, latest, mountedJumpSuspends}));
""")
    assert result == {
        "initial": {"old": False, "tail": True}, "revealed": True,
        "after": {"old": True, "tail": False, "following": False, "cancelled": True, "count": 80},
        "retained": True, "missing": False,
        "latest": {"old": False, "tail": True, "following": True, "count": 80},
        "mountedJumpSuspends": True,
    }


def test_controller_reveal_flushes_pending_state_and_rejects_inactive_conversation(conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const doc = dom.window.document;
const state = feature.createTurnState('conv-a');
state.laneOrder.main = ['one'];
state.turnsById.one = {turnId:'one', conversationId:'conv-a', laneId:'main', ordinal:0,
  actor:'human', kind:'input', status:'completed', projection:{content:'First'},
  projectionRevision:1, createdAt:1, updatedAt:1};
let active = 'conv-a'; let cancelled = 0;
const controller = feature.createConversationSurfaceController({
  isActive:id => id === active, getContainer:() => doc.getElementById('chat'),
  schedule() { return () => { cancelled++; }; }, onIntent() {},
});
controller.render({id:'conv-a'}, state);
const before = doc.querySelector('[data-turn-id="one"]') !== null;
const revealed = controller.revealTurn('conv-a','one');
const mounted = doc.querySelector('[data-turn-id="one"]') !== null;
active = 'conv-b';
const inactive = controller.revealTurn('conv-a','one');
controller.dispose();
const disposed = controller.revealTurn('conv-b','one');
dom.window.close();
console.log(JSON.stringify({before,revealed,mounted,inactive,disposed,cancelled}));
""")
    assert result == {"before": False, "revealed": True, "mounted": True,
                      "inactive": False, "disposed": False, "cancelled": 1}


def test_surface_windows_blocks_within_oversized_turn(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const makeTurn = (conversationId, turnId, blockCount) => ({
  turnId, conversationId, laneId:'main',
  ordinal:0, actor:'assistant', kind:'reply', status:'failed',
  currentAttemptId:null, projectionRevision:1,
  projection:{content:`block ${blockCount - 1}`,
    segments:Array.from({length:blockCount}, (_, i) => ({
      type:'text', blockId:`${turnId}:block-${i}`, text:`block ${i}`,
      deliverable:i === blockCount - 1, terminal:i === blockCount - 1,
    }))},
  settlement:{outcome:'failed'}, createdAt:0, updatedAt:0,
});
const makeVm = (conversationId, turns) => feature.selectConversationViewModel({
  conversationId, conversationRevision:1, transport:'live',
  turnsById:Object.fromEntries(turns.map(turn => [turn.turnId, turn])),
  laneOrder:{main:turns.map(turn => turn.turnId)}, attemptsById:{},
  queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{},
});
let blockRenders = 0;
const surface = feature.createConversationSurface(
  document.getElementById('chat'),
  {renderBlock(node, block) { blockRenders += 1; node.textContent = block.markdown; },
   windowing:{blockLabel:(n) => `earlier:${n}`}},
);
const MAX = feature.CONVERSATION_BLOCK_WINDOW_MAX_BLOCKS;
const BATCH = feature.CONVERSATION_BLOCK_WINDOW_BATCH_BLOCKS;
const blockCount = () => surface.root.querySelectorAll('[data-block-id]').length;
const firstBlockId = () => surface.root.querySelector('[data-block-id]')?.dataset.blockId;
const earlierButton = () => surface.root.querySelector(
  '[data-conversation-block-window-action="earlier"]',
);
const earlierNav = () => surface.root.querySelector(
  '[data-conversation-part="turn-blocks-earlier"]',
);
const hugeVm = makeVm('conv-blocks', [makeTurn('conv-blocks', 'turn-huge', 450)]);
surface.render(hugeVm);
const visited = new Set();
const observe = () => surface.root.querySelectorAll('[data-block-id]')
  .forEach(node => visited.add(node.dataset.blockId));
observe();
const contentParts = () => Array.from(
  surface.root.querySelector('.conversation-turn-content').children,
).map(node => node.dataset.conversationPart).filter(Boolean).slice(0, 3);
const output = {
  max:MAX, batch:BATCH,
  initialBlocks:blockCount(), initialFirst:firstBlockId(),
  initialRenders:blockRenders,
  navVisible:!earlierNav().hidden,
  buttonLabel:earlierButton().textContent,
  contentOrder:contentParts(),
  clicks:[],
};
for (let i = 0; i < 4; i += 1) {
  const button = earlierButton();
  if (!button || earlierNav().hidden) break;
  button.click();
  output.clicks.push({blocks:blockCount(), first:firstBlockId(),
    hidden:earlierNav().hidden, label:earlierButton().textContent});

  observe();
}
output.expandedBlocks = blockCount();
surface.render(makeVm('conv-blocks', [makeTurn('conv-blocks', 'turn-huge', 450)]));
output.expandedKeptAfterRerender = blockCount();

output.vmHistory = hugeVm.mainLane.turns[0].blocks.length;
output.earlierVisited = visited.size;
const laterButton = () => surface.root.querySelector(
  '[data-conversation-block-window-action="later"]');
for (let i = 0; i < 4 && !laterButton().disabled; i++) {
  laterButton().click(); observe();
}
output.laterFirst = firstBlockId();
output.laterBlocks = blockCount();
output.laterDisabled = laterButton().disabled;
earlierButton().click();
surface.followLatest();
output.latestFirst = firstBlockId();
output.latestBlocks = blockCount();
surface.render(makeVm('conv-blocks-2', [
  makeTurn('conv-blocks-2', 'turn-huge-2', 450),
  makeTurn('conv-blocks-2', 'turn-small', 100),
]));
output.resetBlocksFirstTurn = blockCount();
const smallTurn = surface.root.querySelector('[data-turn-id="turn-small"]');
output.smallTurnBlocks = smallTurn.querySelectorAll('[data-block-id]').length;
output.smallTurnNavHidden = smallTurn.querySelector(
  '[data-conversation-part="turn-blocks-earlier"]',
).hidden;
surface.dispose();
dom.window.close();
console.log(JSON.stringify(output));
""")

    assert result["max"] == 200
    assert result["batch"] == 100
    assert result["initialBlocks"] == 200
    assert result["initialFirst"] == "turn-huge:block-250"
    assert result["initialRenders"] == 200
    assert result["navVisible"] is True
    assert result["buttonLabel"] == "earlier:250"
    assert result["contentOrder"] == [
        "turn-header", "turn-blocks-earlier", "turn-blocks",
    ]
    assert result["clicks"] == [
        {"blocks": 200, "first": "turn-huge:block-150",
         "hidden": False, "label": "earlier:150"},
        {"blocks": 200, "first": "turn-huge:block-50",
         "hidden": False, "label": "earlier:50"},
        {"blocks": 200, "first": "turn-huge:block-0",
         "hidden": True, "label": "earlier:0"},
    ]
    assert result["expandedBlocks"] == 200
    assert result["expandedKeptAfterRerender"] == 200

    assert result["vmHistory"] == result["earlierVisited"] == 450
    assert result["laterFirst"] == result["latestFirst"] == "turn-huge:block-250"
    assert result["laterBlocks"] == result["latestBlocks"] == 200
    assert result["laterDisabled"] is True
    assert result["resetBlocksFirstTurn"] == 300
    assert result["smallTurnBlocks"] == 100
    assert result["smallTurnNavHidden"] is True


def test_surface_requests_remote_history_and_reveals_prepended_batch(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const turn = ordinal => ({
  turnId:`turn-${ordinal}`, conversationId:'conv-remote-window', laneId:'main',
  ordinal, actor:'assistant', kind:'reply', status:'completed',
  currentAttemptId:null, projectionRevision:1,
  projection:{content:`turn ${ordinal}`}, settlement:{outcome:'completed'},
  createdAt:ordinal, updatedAt:ordinal,
});
const state = turns => ({
  conversationId:'conv-remote-window', conversationRevision:300, transport:'live',
  turnsById:Object.fromEntries(turns.map(item => [item.turnId, item])),
  laneOrder:{main:turns.map(item => item.turnId)}, attemptsById:{},
  historyByLane:{main:{nextBeforeOrdinal:turns[0].ordinal,
    hasMore:true, totalTurns:300}},
  queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{},
});
const loaded = Array.from({length:96}, (_, index) => turn(index + 100));
const intents = [];
const surface = feature.createConversationSurface(
  document.getElementById('chat'), {onIntent(intent) { intents.push(intent); }},
);
surface.render(feature.selectConversationViewModel(state(loaded)));
const earlier = surface.root.querySelector(
  '[data-conversation-window-action="earlier"]',
);
earlier.click();
const localFirst = surface.root.querySelector('[data-turn-id]').dataset.turnId;
earlier.click();
const requested = intents[0];
const prepended = Array.from({length:64}, (_, index) => turn(index + 36));
surface.render(feature.selectConversationViewModel(state([...prepended, ...loaded])));
const visible = Array.from(surface.root.querySelectorAll('[data-turn-id]'))
  .map(node => node.dataset.turnId);
const output = {
  localFirst,
  requested,
  revealedFirst:visible[0],
  revealedLast:visible.at(-1),
  domTurns:visible.length,
  windowState:surface.windowState,
};
surface.dispose(); dom.window.close();
console.log(JSON.stringify(output));
""")

    assert result == {
        "localFirst": "turn-100",
        "requested": {
            "type": "load-earlier-turns",
            "conversationId": "conv-remote-window",
            "laneId": "main",
            "beforeOrdinal": 100,
            "limit": 64,
        },
        "revealedFirst": "turn-80",
        "revealedLast": "turn-159",
        "domTurns": 80,
        "windowState": {
            "start": 44,
            "end": 124,
            "total": 160,
            "maxTurns": 80,
            "batchSize": 20,
        },
    }


def test_surface_window_budget_clamps_hostile_options(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="large"></main><main id="small"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const turn = (index, laneId = 'main') => ({
  turnId:`${laneId}-turn-${index}`, laneId, parentTurnId:null, ordinal:index,
  actor:'assistant', role:'assistant', kind:'reply', status:'completed',
  attemptId:null, projectionRevision:1, commandPending:null, finish:null,
  actions:[], branches:[], blocks:[],
  metadata:{translation:{pending:false, completed:false}}, source:{projection:{}},
});
const mainTurns = Array.from({length:300}, (_, index) => turn(index));
const branchTurns = Array.from(
  {length:300}, (_, index) => turn(index, 'branch-a'),
);
mainTurns.at(-1).branches = [{
  laneId:'branch-a', parentTurnId:mainTurns.at(-1).turnId,
  title:'Branch', kind:'branch', expanded:true, live:false,
  humanTurnCount:0, turns:branchTurns,
}];
const vm = {
  conversationId:'conv-budget', conversationRevision:1, transport:'live',
  mainLane:{laneId:'main', parentTurnId:null, title:'Conversation', kind:'main',
    expanded:true, live:false, humanTurnCount:0,
    turns:mainTurns},
  orphanLanes:[], queue:[], planDecision:null,
};
const large = feature.createConversationSurface(document.getElementById('large'), {
  windowing:{maxTurns:Number.MAX_SAFE_INTEGER, batchSize:Number.MAX_SAFE_INTEGER},
});
large.render(vm);
let maxNodes = large.root.querySelectorAll('[data-turn-id]').length;
const roundtripLane = laneId => {
  const visited = new Set();
  const observe = () => {
    const nodes = large.root.querySelectorAll(
      `[data-turn-id][data-lane-id="${laneId}"]`,
    );
    nodes.forEach(node => visited.add(node.dataset.turnId));
    maxNodes = Math.max(
      maxNodes, large.root.querySelectorAll('[data-turn-id]').length,
    );
  };
  observe();
  const earlier = large.root.querySelector(
    `[data-conversation-window-action="earlier"][data-lane-id="${laneId}"]`,
  );
  let guard = 0;
  while (earlier && !earlier.disabled && guard++ < 30) {
    earlier.click(); observe();
  }
  const later = large.root.querySelector(
    `[data-conversation-window-action="later"][data-lane-id="${laneId}"]`,
  );
  guard = 0;
  while (later && !later.disabled && guard++ < 30) {
    later.click(); observe();
  }
  return visited.size;
};
// The branch owner is attached to the main tail, so traverse it before moving
// the main lane away from that durable parent Turn.
const branchVisited = roundtripLane('branch-a');
const mainVisited = roundtripLane('main');
const small = feature.createConversationSurface(document.getElementById('small'), {
  windowing:{maxTurns:7, batchSize:-5},
});
small.render({
  ...vm,
  conversationId:'conv-small',
  mainLane:{
    ...vm.mainLane,
    turns:mainTurns.map(item => ({...item, branches:[]})),
  },
});
const output = {
  largeNodes:large.root.querySelectorAll('[data-turn-id]').length,
  largeWindow:large.windowState,
  maxNodes, mainVisited, branchVisited,
  smallNodes:small.root.querySelectorAll('[data-turn-id]').length,
  smallWindow:small.windowState,
};
large.dispose(); small.dispose(); dom.window.close();
console.log(JSON.stringify(output));
""")

    assert result == {
        "largeNodes": 80,
        "largeWindow": {
            "start": 260,
            "end": 300,
            "total": 300,
            "maxTurns": 40,
            "batchSize": 20,
        },
        "maxNodes": 80,
        "mainVisited": 300,
        "branchVisited": 300,
        "smallNodes": 7,
        "smallWindow": {
            "start": 293,
            "end": 300,
            "total": 300,
            "maxTurns": 7,
            "batchSize": 7,
        },
    }


def test_surface_viewport_suspends_follow_preserves_anchor_and_repins_real_height(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<div id="viewport"><main id="chat"></main></div>');
global.Element = dom.window.Element;
const document = dom.window.document;
const viewport = document.getElementById('viewport');
let scrollHeight = 1000;
const clientHeight = 200;
let scrollTop = 0;
Object.defineProperty(viewport, 'clientHeight', {get:() => clientHeight});
Object.defineProperty(viewport, 'scrollHeight', {get:() => scrollHeight});
Object.defineProperty(viewport, 'scrollTop', {
  get:() => scrollTop,
  set:value => {
    scrollTop = Math.max(0, Math.min(
      Number(value) || 0, scrollHeight - clientHeight,
    ));
  },
});
viewport.getBoundingClientRect = () => ({top:0, bottom:200});
const scheduled = [];
const port = feature.createConversationViewportPort(viewport, {
  scheduleAfterLayout(callback) {
    const task = {callback, cancelled:false}; scheduled.push(task);
    return () => { task.cancelled = true; };
  },
});
const flushLayout = () => {
  for (const task of scheduled.splice(0)) if (!task.cancelled) task.callback();
};
const turn = (index, text = `turn ${index}`) => ({
  turnId:`turn-${index}`, laneId:'main', parentTurnId:null, ordinal:index,
  actor:'assistant', role:'assistant', kind:'reply', status:'running',
  attemptId:'attempt', projectionRevision:text.length, commandPending:null,
  finish:null, actions:[], branches:[],
  blocks:[{kind:'text', blockId:'text:terminal', identitySource:'contract',
    source:{text}, markdown:text, deliverable:true, terminal:true, resumable:false}],
  metadata:{translation:{pending:false, completed:false}}, source:{projection:{}},
});
const vm = text => ({
  conversationId:'conv-scroll', conversationRevision:text.length, transport:'live',
  mainLane:{laneId:'main', parentTurnId:null, title:'Conversation', kind:'main',
    expanded:true, live:true, humanTurnCount:0,
    turns:Array.from({length:10}, (_, index) => turn(index, index ? `turn ${index}` : text))},
  orphanLanes:[], queue:[], planDecision:null,
});
let layoutShift = 0;
const surface = feature.createConversationSurface(document.getElementById('chat'), {
  scrollAnchor:port,
  renderBlock(node, block) {
    node.textContent = block.markdown;
    if (block.markdown === 'stream update') layoutShift = 120;
  },
});
surface.render(vm('initial'));
for (const node of surface.root.querySelectorAll('[data-turn-id]')) {
  const index = Number(node.dataset.turnId.split('-')[1]);
  node.getBoundingClientRect = () => ({
    top:index * 100 + layoutShift - viewport.scrollTop,
    bottom:index * 100 + 80 + layoutShift - viewport.scrollTop,
  });
}
flushLayout();
const initialBottom = viewport.scrollTop;
viewport.scrollTop = 400;
viewport.dispatchEvent(new dom.window.WheelEvent('wheel', {deltaY:-20}));
const suspended = !port.following;
surface.render(vm('stream update'));
const anchorPreservedTop = viewport.scrollTop;
const stayedAwayFromBottom = viewport.scrollTop < scrollHeight;
surface.followLatest();
const immediateHeight = viewport.scrollTop;
scrollHeight = 1600;
flushLayout();
const finalHeight = viewport.scrollTop;
const resumed = port.following;
const output = {
  initialBottom, suspended, anchorPreservedTop, stayedAwayFromBottom,
  immediateHeight, finalHeight, resumed,
};
surface.dispose(); port.dispose(); dom.window.close();
console.log(JSON.stringify(output));
""")

    assert result == {
        "initialBottom": 800,
        "suspended": True,
        "anchorPreservedTop": 520,
        "stayedAwayFromBottom": True,
        "immediateHeight": 800,
        "finalHeight": 1400,
        "resumed": True,
    }


def test_inline_turn_editor_mounts_in_place_and_survives_turn_node_rebuild(
        conversation_bundle: Path):
    """The edit affordance swaps the turn's rendered blocks for an in-place
    textarea session instead of the detached prompt. Regression pins: the
    turn node carries data-inline-editing while blocks hide, Escape cancels
    without submitting, Ctrl+Enter submits the draft, a rebuilt turn node
    re-adopts the same host with its draft, an empty draft keeps save
    disabled, and a windowed-out turn returns null for the modal fallback."""
    result = _run(conversation_bundle, r"""
(async () => {
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"><div id="chatInner"></div></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const root = document.getElementById('chatInner');
const buildTurn = () => {
  const article = document.createElement('article');
  article.dataset.turnId = 'turn-1';
  article.className = 'conversation-turn message user-msg';
  const content = document.createElement('div');
  content.dataset.conversationPart = 'turn-content';
  const blocks = document.createElement('div');
  blocks.dataset.conversationPart = 'turn-blocks';
  blocks.className = 'conversation-turn-blocks message-body';
  const actions = document.createElement('div');
  actions.dataset.conversationPart = 'turn-actions';
  content.append(blocks, actions);
  article.appendChild(content);
  return article;
};
root.appendChild(buildTurn());
const submissions = [];
let cancels = 0;
const options = {
  conversationId:'conv-a', turnId:'turn-1', text:'hello draft', canResend:true,
  findTurnNode:(id) => root.querySelector(`[data-turn-id="${id}"]`),
  translate:(key) => key,
  onSubmit:async ({text, resend}) => { submissions.push([text, resend]); return true; },
  onCancel:() => { cancels += 1; },
};
const missingReturnsNull = feature.openTurnInlineEditor(
  {...options, turnId:'turn-absent'}) === null;
const session = feature.openTurnInlineEditor(options);
const editor = root.querySelector('.turn-inline-editor');
const textarea = editor.querySelector('textarea');
const buttons = [...editor.querySelectorAll('.turn-inline-editor-btn')]
  .map(b => [b.dataset.i18n, b.hidden, b.disabled]);
const attrWhileEditing = root.querySelector(
  '[data-turn-id="turn-1"]').dataset.inlineEditing || '';
textarea.value = 'hello draft edited';
textarea.dispatchEvent(new dom.window.Event('input', {bubbles:true}));
textarea.dispatchEvent(new dom.window.KeyboardEvent('keydown', {key:'Escape', bubbles:true}));
const afterCancel = {
  submissions: submissions.length, cancels,
  editorGone: !root.querySelector('.turn-inline-editor'),
  attrCleared: !root.querySelector('[data-turn-id="turn-1"]').dataset.inlineEditing,
  hostDetached: !editor.isConnected,
};
feature.openTurnInlineEditor(options);
const editor2 = root.querySelector('.turn-inline-editor');
editor2.querySelector('textarea').value = 'second draft';
editor2.querySelector('textarea').dispatchEvent(
  new dom.window.KeyboardEvent('keydown', {key:'Enter', ctrlKey:true, bubbles:true}));
await new Promise(resolve => setTimeout(resolve, 0));
const afterSubmit = {
  submissions: submissions.slice(),
  editorGone: !root.querySelector('.turn-inline-editor'),
};
const session3 = feature.openTurnInlineEditor(options);
const host3 = root.querySelector('.turn-inline-editor');
host3.querySelector('textarea').value = 'draft survives remount';
root.querySelector('[data-turn-id="turn-1"]').remove();
root.appendChild(buildTurn());
feature.reconcileTurnInlineEditors();
const remounted = root.querySelector('.turn-inline-editor');
const afterRemount = {
  sameHost: remounted === host3,
  draftKept: remounted ? remounted.querySelector('textarea').value : '',
  attrSet: root.querySelector('[data-turn-id="turn-1"]').dataset.inlineEditing === 'true',
  placedAfterBlocks: remounted
    ? remounted.previousElementSibling.dataset.conversationPart === 'turn-blocks' : false,
};
session3.close();
const session4 = feature.openTurnInlineEditor({...options, text:''});
const emptySaveDisabled = root.querySelector(
  '.turn-inline-editor-btn--save').disabled;
session4.close();
console.log(JSON.stringify({
  missingReturnsNull, opened: Boolean(session), attrWhileEditing, buttons,
  afterCancel, afterSubmit, afterRemount, emptySaveDisabled,
  cancelsFinal: cancels,
}));
})();
""")

    assert result == {
        "missingReturnsNull": True,
        "opened": True,
        "attrWhileEditing": "true",
        "buttons": [
            ["editMsg.cancel", False, False],
            ["editMsg.resend", False, False],
            ["editMsg.save", False, False],
        ],
        "afterCancel": {
            "submissions": 0, "cancels": 1, "editorGone": True,
            "attrCleared": True, "hostDetached": True,
        },
        "afterSubmit": {
            "submissions": [["second draft", False]],
            "editorGone": True,
        },
        "afterRemount": {
            "sameHost": True,
            "draftKept": "draft survives remount",
            "attrSet": True,
            "placedAfterBlocks": True,
        },
        "emptySaveDisabled": True,
        "cancelsFinal": 3,
    }


def test_inline_turn_editor_keeps_focus_and_caret_through_surface_commits(
        conversation_bundle: Path):
    """Regression for the mid-typing caret loss: an authoritative surface
    commit (SSE sync, streaming deltas of other turns) re-normalizes the
    turn's content part order. That normalization must skip the unmanaged
    editor host instead of pushing it after the footer — moving a focused
    host blurs the textarea. A genuine turn-node rebuild must still remount
    the host and restore the caret where the user was typing, not collapse
    it to the draft end."""
    result = _run(conversation_bundle, r"""
(async () => {
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"><div id="chatInner"></div></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const root = document.getElementById('chatInner');
const turn = (id, text, actor) => ({
  turnId:id, laneId:'main', parentTurnId:null, ordinal:Number(id.split('-')[1]),
  actor, role:actor === 'human' ? 'user' : 'assistant',
  kind:actor === 'human' ? 'input' : 'reply', status:'completed',
  attemptId:`attempt-${id}`, projectionRevision:text.length, commandPending:null,
  finish:null, actions:[], branches:[],
  blocks:[{kind:'text', blockId:`text:${id}`, identitySource:'contract',
    source:{text}, markdown:text, deliverable:true, terminal:true, resumable:false}],
  metadata:{translation:{pending:false, completed:false}}, source:{projection:{}},
});
const vm = streaming => ({
  conversationId:'conv-edit', conversationRevision:streaming.length, transport:'live',
  mainLane:{laneId:'main', parentTurnId:null, title:'Conversation', kind:'main',
    expanded:true, live:true, humanTurnCount:1,
    turns:[turn('turn-1','edit me please','human'),
      turn('turn-2', streaming, 'assistant')]},
  orphanLanes:[], queue:[], planDecision:null,
});
const surface = feature.createConversationSurface(root, {});
surface.render(vm('streaming chunk one'));
const findTurnNode = id => root.querySelector(`[data-turn-id="${id}"]`);
const session = feature.openTurnInlineEditor({
  conversationId:'conv-edit', turnId:'turn-1', text:'edit me please',
  canResend:false, allowEmpty:false, findTurnNode,
  onSubmit:() => true,
});
const host = root.querySelector('.turn-inline-editor');
const textarea = host.querySelector('textarea');
textarea.focus();
textarea.value = 'edit me please - plus more';
textarea.setSelectionRange(8, 8);
textarea.dispatchEvent(new dom.window.Event('input', {bubbles:true}));
textarea.dispatchEvent(new dom.window.Event('select', {bubbles:true}));
/* A background commit: another turn streams more text. */
surface.render(vm('streaming chunk one, now longer'));
const drifted = host.previousElementSibling?.dataset?.conversationPart || '';
feature.reconcileTurnInlineEditors();
const afterCommit = {
  drifted,
  pinnedAfterBlocks: host.previousElementSibling?.dataset?.conversationPart || '',
  draft: textarea.value,
  selection:[textarea.selectionStart, textarea.selectionEnd],
  focused: document.activeElement === textarea,
};
/* A genuine rebuild: the turn node is replaced outright. */
findTurnNode('turn-1').remove();
surface.render(vm('streaming chunk one, longer still'));
feature.reconcileTurnInlineEditors();
const afterRemount = {
  remounted: host.isConnected,
  placedAfterBlocks: host.previousElementSibling?.dataset?.conversationPart || '',
  draft: textarea.value,
  selection:[textarea.selectionStart, textarea.selectionEnd],
  focused: document.activeElement === textarea,
};
session.close();
surface.dispose();
dom.window.close();
console.log(JSON.stringify({afterCommit, afterRemount}));
})();
""")

    assert result == {
        "afterCommit": {
            "drifted": "turn-blocks",
            "pinnedAfterBlocks": "turn-blocks",
            "draft": "edit me please - plus more",
            "selection": [8, 8],
            "focused": True,
        },
        "afterRemount": {
            "remounted": True,
            "placedAfterBlocks": "turn-blocks",
            "draft": "edit me please - plus more",
            "selection": [8, 8],
            "focused": True,
        },
    }


def test_inline_turn_editor_paste_attaches_images_with_chip_lifecycle(
        conversation_bundle: Path):
    """Pasting an image into the edit session attaches it to the TURN (not
    the composer draft). Regression pins: text-only paste keeps the default
    caret insertion, image paste is claimed (preventDefault like the
    composer), an optimistic chip shows while the caller compresses/uploads,
    save stays disabled while any chip is processing (its projection payload
    does not exist yet), an attached image satisfies the non-empty rule for
    an otherwise empty draft, submit carries the caller-provided payloads,
    a null resolution drops the chip, and removing a chip mid-flight
    discards the late result."""
    result = _run(conversation_bundle, r"""
(async () => {
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"><div id="chatInner"></div></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const root = document.getElementById('chatInner');
const article = document.createElement('article');
article.dataset.turnId = 'turn-1';
const content = document.createElement('div');
content.dataset.conversationPart = 'turn-content';
const blocks = document.createElement('div');
blocks.dataset.conversationPart = 'turn-blocks';
content.appendChild(blocks);
article.appendChild(content);
root.appendChild(article);
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const pasteEvent = (items) => {
  const event = new dom.window.Event('paste', {bubbles:true, cancelable:true});
  Object.defineProperty(event, 'clipboardData', {value:{items}});
  return event;
};
const imageItem = () => ({
  type:'image/png',
  getAsFile:() => new dom.window.File(['png-bytes'], 'pasted.png', {type:'image/png'}),
});
const submissions = [];
let cancels = 0;
let attachCalls = 0;
const base = {
  conversationId:'conv-a', turnId:'turn-1', text:'',
  findTurnNode:(id) => root.querySelector(`[data-turn-id="${id}"]`),
  translate:(key) => key,
  onSubmit:async ({text, resend, images}) => {
    submissions.push({text, resend, images}); return true; },
  onCancel:() => { cancels += 1; },
};
const saveButton = () => root.querySelector('.turn-inline-editor-btn--save');
const chips = () => root.querySelectorAll('.turn-inline-editor-chip');
/* Scenario A/B: text paste flows through; image paste is claimed and the
 * resolved payload reaches onSubmit. Empty text + attached image = saveable. */
feature.openTurnInlineEditor({...base,
  onImageAttach:async (file) => {
    attachCalls += 1;
    return {payload:{base64:'AAA', mediaType:file.type, url:'/api/images/a.png'},
      preview:'data:image/png;base64,AAA'};
  },
});
const textarea = () => root.querySelector('.turn-inline-editor-input');
const textPaste = pasteEvent([{type:'text/plain', getAsFile:() => null}]);
textarea().dispatchEvent(textPaste);
const textPasteDefaultAllowed = !textPaste.defaultPrevented;
const imagePaste = pasteEvent([imageItem()]);
textarea().dispatchEvent(imagePaste);
const imagePasteClaimed = imagePaste.defaultPrevented;
const chipShownImmediately = chips().length === 1;
const chipProcessingInitially = chips()[0]?.dataset.processing === 'true';
await tick();
const saveEnabledWithImageAfterAttach = !saveButton().disabled;
saveButton().click();
await tick();
const editorClosedAfterSave = !root.querySelector('.turn-inline-editor');
/* Scenario C: an unresolved upload keeps save disabled. */
let resolveAttach;
const gate = new Promise(resolve => { resolveAttach = resolve; });
feature.openTurnInlineEditor({...base, onImageAttach:() => { attachCalls += 1; return gate; }});
textarea().dispatchEvent(pasteEvent([imageItem()]));
await tick();
const saveDisabledWhileProcessing = saveButton().disabled;
const chipMarkedProcessing = chips()[0]?.dataset.processing === 'true';
resolveAttach({payload:{base64:'BBB'}, preview:''});
await tick();
const saveEnabledAfterResolve = !saveButton().disabled;
root.querySelector('.turn-inline-editor-btn--cancel').click();
/* Scenario D: a null resolution drops the chip and re-blocks an empty save. */
feature.openTurnInlineEditor({...base, onImageAttach:async () => { attachCalls += 1; return null; }});
textarea().dispatchEvent(pasteEvent([imageItem()]));
await tick();
const chipsAfterFailedAttach = chips().length;
const saveDisabledAfterChipDropped = saveButton().disabled;
root.querySelector('.turn-inline-editor-btn--cancel').click();
/* Scenario E: removing a chip mid-flight discards the late result. */
let resolveLate;
const lateGate = new Promise(resolve => { resolveLate = resolve; });
feature.openTurnInlineEditor({...base, onImageAttach:() => { attachCalls += 1; return lateGate; }});
textarea().dispatchEvent(pasteEvent([imageItem()]));
await tick();
root.querySelector('.turn-inline-editor-chip-remove').click();
resolveLate({payload:{base64:'CCC'}, preview:''});
await tick();
const lateResolveKeptZeroChips = chips().length === 0;
console.log(JSON.stringify({
  textPasteDefaultAllowed, imagePasteClaimed, chipShownImmediately,
  chipProcessingInitially, saveEnabledWithImageAfterAttach,
  submissions, editorClosedAfterSave,
  saveDisabledWhileProcessing, chipMarkedProcessing, saveEnabledAfterResolve,
  chipsAfterFailedAttach, saveDisabledAfterChipDropped,
  lateResolveKeptZeroChips, attachCalls, cancels,
}));
})();
""")

    assert result == {
        "textPasteDefaultAllowed": True,
        "imagePasteClaimed": True,
        "chipShownImmediately": True,
        "chipProcessingInitially": True,
        "saveEnabledWithImageAfterAttach": True,
        "submissions": [{
            "text": "",
            "resend": False,
            "images": [{"base64": "AAA", "mediaType": "image/png",
                        "url": "/api/images/a.png"}],
        }],
        "editorClosedAfterSave": True,
        "saveDisabledWhileProcessing": True,
        "chipMarkedProcessing": True,
        "saveEnabledAfterResolve": True,
        "chipsAfterFailedAttach": 0,
        "saveDisabledAfterChipDropped": True,
        "lateResolveKeptZeroChips": True,
        "attachCalls": 4,
        "cancels": 2,
    }


def test_inline_editor_input_neutralizes_global_composer_textarea_leak() -> None:
    """Root-cause pin for the truncated/no-scroll editor draft: the bare
    composer rule `textarea{flex:1;max-height:200px;…}` in
    02-messages-composer.css leaks onto the inline editor input. The editor
    rule must keep its explicit counters — without them flex-basis:0%
    ignores the JS-fitted pixel height and the 200px clamp sits below the
    editor's 45vh fit cap, so long drafts render clipped with no scrollbar."""
    source = (ROOT / "frontend/src/styles/application"
              / "23-reading-editor.css").read_text(encoding="utf-8")
    block = source.split(
        ".conversation-surface .turn-inline-editor-input {", 1)[1].split("\n}", 1)[0]
    assert "flex: none;" in block
    assert "max-height: none;" in block


def test_file_changes_details_scroll_inside_the_open_clamp() -> None:
    """Root-cause pin for the unscrollable 146-file list: the collapsed base
    rule `.fc-details{max-height:0;overflow:hidden;…}` keeps overflow:hidden
    for the collapse animation, and the [open] lift only raised max-height to
    400px — so long lists rendered clipped at ~18 rows with no scrollbar. The
    open rule must re-enable vertical scrolling inside the clamp."""
    source = (ROOT / "frontend/src/styles/application"
              / "23-reading-evidence.css").read_text(encoding="utf-8")
    block = source.split(
        ".conversation-surface .conversation-file-changes[open] .fc-details {",
        1)[1].split("\n}", 1)[0]
    assert "max-height: 400px;" in block
    assert "overflow-y: auto;" in block


def test_controller_keeps_legacy_scroll_when_viewport_is_unavailable_at_mount(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main><div id="late-viewport"></div>');
global.Element = dom.window.Element;
const document = dom.window.document;
const scheduled = [];
const captured = [];
const restored = [];
let viewportAvailable = false;
let viewportLookups = 0;
let legacyFollowCalls = 0;
const turn = revision => ({
  turnId:'turn-1', conversationId:'conv-scroll-fallback', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', status:'completed', currentAttemptId:null,
  projectionRevision:revision, createdAt:1, updatedAt:revision,
  projection:{content:`revision ${revision}`, segments:[{
    type:'text', blockId:'text:terminal', text:`revision ${revision}`,
    deliverable:true, terminal:true,
  }]}, settlement:{outcome:'completed'},
});
const state = revision => {
  const item = turn(revision);
  return {
    conversationId:'conv-scroll-fallback', conversationRevision:revision,
    transport:'live', turnsById:{'turn-1':item}, laneOrder:{main:['turn-1']},
    attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
    liveRoundUsageByTurn:{},
  };
};
const controller = feature.createConversationSurfaceController({
  isActive:id => id === 'conv-scroll-fallback',
  getContainer:() => document.getElementById('chat'),
  getScrollViewport() {
    viewportLookups += 1;
    return viewportAvailable ? document.getElementById('late-viewport') : null;
  },
  schedule(render) { scheduled.push(render); return () => {}; },
  captureScroll() {
    const snapshot = {ordinal:captured.length + 1};
    captured.push(snapshot);
    return snapshot;
  },
  restoreScroll(snapshot) { restored.push(snapshot); },
  followLatest() { legacyFollowCalls += 1; },
});
const conversation = {id:'conv-scroll-fallback'};
controller.render(conversation, state(1));
scheduled.shift()();
viewportAvailable = true;
controller.render(conversation, state(2));
scheduled.shift()();
controller.followLatest();
console.log(JSON.stringify({
  viewportLookups,
  captures:captured.length,
  restores:restored.length,
  snapshotsPreserved:restored.every((item, index) => item === captured[index]),
  legacyFollowCalls,
  text:document.querySelector('[data-block-id="text:terminal"]').textContent,
}));
controller.dispose();
dom.window.close();
""")

    assert result == {
        "viewportLookups": 1,
        "captures": 2,
        "restores": 2,
        "snapshotsPreserved": True,
        "legacyFollowCalls": 1,
        "text": "revision 2",
    }


def test_human_text_block_requests_raw_html_escape(conversation_bundle: Path):
    """Human-authored input is plain text: the classic renderer must ask the
    markdown port to neutralize raw '<tag>' HTML for actor === 'human' turns
    only, so a typed '<MyComponent>' renders literally instead of being
    swallowed as an unknown element. Assistant turns keep the raw-HTML
    contract (no options argument) — model output may legitimately rely on
    inline HTML. Regression pin for the renderTextBlock threading."""
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const turn = (id, ordinal, actor, kind, text) => ({
  turnId:id, conversationId:'conv-a', laneId:'main', ordinal,
  actor, kind, runId:'', status:'completed', currentAttemptId:null,
  projection:{ segments:[{type:'text', blockId:'text:terminal', text,
    deliverable:true, terminal:true}], content:text },
  projectionRevision:1, settlement:{outcome:'completed'},
  createdAt:1, updatedAt:2,
});
const state = {
  conversationId:'conv-a', conversationRevision:1, transport:'replay',
  turnsById:{
    'human-turn':turn('human-turn', 1, 'human', 'input',
      'look at <MyComponent> now'),
    'assistant-turn':turn('assistant-turn', 2, 'assistant', 'reply',
      'use <b>bold</b> here'),
  },
  laneOrder:{main:['human-turn', 'assistant-turn']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{},
  commandPending:{}, liveRoundUsageByTurn:{},
};
const vm = feature.selectConversationViewModel(state);
const calls = [];
const surface = feature.createConversationSurface(
  document.getElementById('chat'),
  feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:(value, options) => {
      calls.push({ value, escapeRawHtml:Boolean(options?.escapeRawHtml),
        optionsPresent:options !== undefined });
      return '<p>' + value + '</p>';
    },
  }),
);
surface.render(vm);
console.log(JSON.stringify(calls));
surface.dispose();
dom.window.close();
""")
    assert result == [
        {"value": "look at <MyComponent> now", "escapeRawHtml": True,
         "optionsPresent": True},
        # Streaming-capable text renderers receive an options object; only
        # human-authored input opts into raw-HTML escaping.
        {"value": "use <b>bold</b> here", "escapeRawHtml": False,
         "optionsPresent": True},
    ]


def test_footer_rerenders_when_cost_signature_lands(conversation_bundle: Path):
    """Historical settled turns (projection written before the server-side
    cost fold) have no authoritative ``projection.cost``; the footer falls
    back to the client ``calcCostCny`` micro-batch. That fill mutates no Turn
    fact, so unless the signature rides presentation state into the footer
    compare, the re-render requested by the batch is diffed away and the cost
    tag never appears (2026-08-29, mtd9ci53zq3xfm)."""
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const projection = {
  usage:{prompt_tokens:1000, completion_tokens:500},
  model:'gpt-4o',
  segments:[{type:'text', blockId:'text:terminal', text:'answer',
    deliverable:true, terminal:true}],
};
const state = {
  conversationId:'conv-a', conversationRevision:5, transport:'live',
  turnsById:{'turn-1':{
    turnId:'turn-1', conversationId:'conv-a', laneId:'main', ordinal:1,
    actor:'assistant', kind:'reply', runId:'', status:'completed',
    currentAttemptId:null, projection, projectionRevision:3,
    settlement:{outcome:'completed'}, createdAt:1, updatedAt:2}},
  laneOrder:{main:['turn-1']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{},
  commandPending:{}, liveRoundUsageByTurn:{},
};
let footerRenders = 0;
const surface = feature.createConversationSurface(
  document.getElementById('chat'), {
    onIntent() {},
    renderBlock(node, block) { node.textContent = block.markdown || ''; },
    renderTurnFooter(node) { footerRenders += 1; node.textContent = 'footer'; },
  });
const presentation = (signature) => ({
  costSignatureByTurnId: signature
    ? new Map([['turn-1', signature]]) : new Map(),
});
const vmEmpty = feature.selectConversationViewModel(state, {}, presentation(''));
surface.render(vmEmpty);
const vmLanded = feature.selectConversationViewModel(
  state, {}, presentation('0.0234'));
const landedSignature = vmLanded.mainLane.turns[0].metadata.costSignature;
surface.render(vmLanded);
const afterFill = footerRenders;
surface.render(feature.selectConversationViewModel(
  state, {}, presentation('0.0234')));
const afterSame = footerRenders;
/* An authoritative projection cost needs no presentation signature: the
 * projectionRevision footer compare already covers it. */
const stampedState = JSON.parse(JSON.stringify(state));
stampedState.turnsById['turn-1'].projection = {...projection, cost:{costCny:0.02}};
const vmStamped = feature.selectConversationViewModel(
  stampedState, {}, presentation('0.0234'));
console.log(JSON.stringify({
  emptySignature: vmEmpty.mainLane.turns[0].metadata.costSignature ?? null,
  landedSignature,
  afterFill,
  afterSame,
  stampedSignature: vmStamped.mainLane.turns[0].metadata.costSignature ?? null,
}));
surface.dispose();
dom.window.close();
""")
    assert result["emptySignature"] is None
    assert result["landedSignature"] == "0.0234"
    assert result["afterFill"] == 2
    assert result["afterSame"] == 2
    assert result["stampedSignature"] is None


def test_text_block_copy_button_addresses_exactly_that_block(
        conversation_bundle: Path):
    """Mid-turn prose (deliverable=False text segments) had no copy path: the
    turn-level Copy only holds projection.content (the final answer). Every
    text block now carries a hover-revealed copy button whose intent is
    scoped by blockId, and the controller rewrites it with the live
    translation display mode exactly like the turn-level copy."""
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const segments = [
  {type:'thinking', blockId:'thinking:llm-1', text:'reason', llmRound:1},
  {type:'text', blockId:'text:llm-1', text:'Middle prose',
    translatedText:'中间段落', llmRound:1},
  {type:'text', blockId:'text:terminal', text:'Original answer',
    translatedText:'翻译答案', deliverable:true, terminal:true},
];
const turn = {
  turnId:'copy-block-turn', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', runId:'', status:'completed',
  currentAttemptId:null,
  projection:{segments, content:'Original answer',
    translatedContent:'翻译答案', _translateDone:true, model:'model-a'},
  projectionRevision:2, settlement:{outcome:'completed'},
  createdAt:1, updatedAt:2,
};
const state = {conversationId:'conv-a', conversationRevision:2,
  transport:'live',
  turnsById:{'copy-block-turn':turn}, laneOrder:{main:['copy-block-turn']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{}};
const scheduled = [];
const forwarded = [];
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
    actionLabel:value => value,
    localizedText:(_key, fallback) => fallback,
  }),
  onIntent:intent => forwarded.push(intent),
});
controller.render({id:'conv-a'}, state);
scheduled.shift()();
for (const process of document.querySelectorAll('.conversation-history-process')) { process.open=true; process.dispatchEvent(new document.defaultView.Event('toggle')); }
// Reveal historical content before exercising its controls.
document.querySelector('[data-round-action="expandAll"]')?.click();
const node = document.querySelector('[data-turn-id="copy-block-turn"]');
const textBlocks = () => Array.from(
  node.querySelectorAll('.conversation-block--text'));
const copyButtons = () => textBlocks().map(block =>
  block.querySelectorAll('button[data-conversation-action="copy-block"]')
    .length);
const thinkingCopies = node.querySelectorAll(
  '.conversation-block--thinking button[data-conversation-action="copy-block"]'
).length;
const middleCopy = () => node.querySelector('[data-block-id="text:llm-1"]')
  .querySelector('button[data-conversation-action="copy-block"]');
const aria = middleCopy().getAttribute('aria-label');
middleCopy().click();
node.querySelector('[data-conversation-action="translate"]').click();
middleCopy().click();
const afterTranslate = forwarded.map(intent =>
  [intent.type, intent.blockId, intent.operation]);
controller.render({id:'conv-a'}, state, {force:true});
scheduled.shift()();
for (const process of document.querySelectorAll('.conversation-history-process')) { process.open=true; process.dispatchEvent(new document.defaultView.Event('toggle')); }
const countsAfterRerender = copyButtons();
const payload = {
  textBlockCount:textBlocks().length,
  copyButtons:copyButtons(),
  thinkingCopies,
  aria,
  forwarded:afterTranslate,
  countsAfterRerender,
};
controller.dispose();
dom.window.close();
console.log(JSON.stringify(payload));
""")
    assert result == {
        "textBlockCount": 2,
        "copyButtons": [1, 1],
        "thinkingCopies": 0,
        "aria": "Copy",
        "forwarded": [
            ["copy-block", "text:llm-1", "copy-translated"],
            ["copy-block", "text:llm-1", "copy-original"],
        ],
        "countsAfterRerender": [1, 1],
    }


def test_human_text_block_has_no_per_block_copy_button(
        conversation_bundle: Path) -> None:
    """Human bubbles are exempt from the per-block copy affordance: their
    single text block IS the turn content, so the action-bar Copy covers it
    and the floating button only overlaps the message text. Assistant blocks
    keep their own copy button (mid-turn prose has no turn-level path)."""
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const turn = (id, actor, ordinal) => ({
  turnId:id, conversationId:'conv-a', laneId:'main', ordinal,
  actor, kind: actor === 'human' ? 'input' : 'reply', runId:'',
  status:'completed', currentAttemptId:null,
  projection:{segments:[
    {type:'text', blockId:`text:${id}`, text:'body',
      deliverable:true, terminal:true},
  ], content:'body'},
  projectionRevision:1, settlement:{outcome:'completed'},
  createdAt:1, updatedAt:2,
});
const human = turn('human-1', 'human', 1);
const assistant = turn('assistant-1', 'assistant', 2);
const state = {conversationId:'conv-a', conversationRevision:2,
  transport:'live',
  turnsById:{'human-1':human, 'assistant-1':assistant},
  laneOrder:{main:['human-1','assistant-1']},
  attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
  liveRoundUsageByTurn:{}};
const scheduled = [];
const controller = feature.createConversationSurfaceController({
  isActive:() => true,
  getContainer:() => document.getElementById('chat'),
  schedule(render) { scheduled.push(render); return () => {}; },
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value => value,
    actionLabel:value => value,
    localizedText:(_key, fallback) => fallback,
  }),
  onIntent:() => {},
});
controller.render({id:'conv-a'}, state);
scheduled.shift()();
const copies = id => document.querySelector(`[data-turn-id="${id}"]`)
  .querySelectorAll('button[data-conversation-action="copy-block"]').length;
const before = {human:copies('human-1'), assistant:copies('assistant-1')};
controller.render({id:'conv-a'}, state, {force:true});
scheduled.shift()();
const after = {human:copies('human-1'), assistant:copies('assistant-1')};
controller.dispose();
dom.window.close();
console.log(JSON.stringify({before, after}));
""")
    assert result == {
        "before": {"human": 0, "assistant": 1},
        "after": {"human": 0, "assistant": 1},
    }


def test_text_block_copy_button_is_hover_revealed() -> None:
    """The per-block copy affordance must stay a hover/focus disclosure (the
    same contract as the turn action shelf), not permanent chrome on every
    paragraph block; touch layouts (max-width:720px) reveal it instead."""
    source = (ROOT / "frontend/src/styles/application"
              / "23-reading-actions.css").read_text(encoding="utf-8")
    rule = source.split(
        ".conversation-surface .conversation-block-copy {", 1)[1].split(
        "\n}", 1)[0]
    assert "position: absolute;" in rule
    assert "opacity: 0;" in rule
    assert (".conversation-surface .conversation-block--text:hover"
            " .conversation-block-copy," in source)
    assert ("focus-within .conversation-block-copy" in source)


def _note_segments(count: int) -> str:
    """JS for `count` adjacent context-injection system_note segments."""
    rows = ",".join(
        "{type:'system_note', blockId:'system-note:turn-1:context-injection-%d',"
        " noteKind:'context-injection', action:'prompt', reason:'reason_%d',"
        " text:'body-%d', llmRound:0}" % (i, i, i)
        for i in range(count)
    )
    return "[" + rows + "]"


def test_system_note_group_preserves_user_open_state_when_growing(
        conversation_bundle: Path):
    """Unified disclosure semantic: a default-collapsed disclosure's open
    state is user intent. When a live-growing system-note-group gains a member
    mid-turn, its sameBlock arm returns false (items.length changed) and the
    block re-renders via replaceChildren — without the preserveDetailsOpen
    read-back the reader's open state would snap back to collapsed. Pin the
    invariant: a manually-OPENED group stays open across the growth commit,
    and a group the reader never touched stays collapsed."""
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
global.Element = dom.window.Element;
const document = dom.window.document;
const classic = feature.createClassicConversationRenderers({
  renderSafeMarkdownHtml:value => `<p>${value}</p>`,
  localizedText:(key, fallback) => fallback,
});
const surface = feature.createConversationSurface(
  document.getElementById('chat'), classic);
const makeVm = (noteCount, revision) => {
  const segments = NOTE_SEGS.slice(0, noteCount);
  const turn = {
    turnId:'turn-1', conversationId:'conv-a', laneId:'main', ordinal:1,
    actor:'assistant', kind:'reply', runId:'', status:'running',
    currentAttemptId:null, projection:{segments}, projectionRevision:revision,
    settlement:{outcome:'running'}, createdAt:1, updatedAt:2,
  };
  return feature.selectConversationViewModel({
    conversationId:'conv-a', conversationRevision:revision, transport:'live',
    turnsById:{'turn-1':turn}, laneOrder:{main:['turn-1']},
    attemptsById:{}, queueItems:[], pendingEventsByTurn:{}, commandPending:{},
    liveRoundUsageByTurn:{},
  });
};
const group = () => surface.root.querySelector('.sw-note-group');

// 1) Two-note group renders collapsed by default.
surface.render(makeVm(2, 1));
const initiallyCollapsed = group() && group().open === false
  && group().querySelectorAll('.sw-note-group-item').length === 2;

// 2) The reader opens the group, then a third injection lands mid-turn.
group().open = true;
surface.render(makeVm(3, 2));
const stayedOpenAfterGrowth = group() && group().open === true
  && group().querySelectorAll('.sw-note-group-item').length === 3;

// 3) The reader then collapses the (grown) group; a further commit that
//    re-renders the block must preserve THAT choice too — open intent is
//    sticky in both directions, never reset to the default.
group().open = false;
surface.render(makeVm(3, 3));
const userCollapsePersists = group() && group().open === false;

surface.dispose();
dom.window.close();
console.log(JSON.stringify({
  initiallyCollapsed, stayedOpenAfterGrowth, userCollapsePersists,
}));
""".replace("NOTE_SEGS", _note_segments(3)))
    assert result == {
        "initiallyCollapsed": True,
        "stayedOpenAfterGrowth": True,
        "userCollapsePersists": True,
    }


@pytest.mark.parametrize("summary_key,status", [
    ("activity.tool.failed", "error"),
    ("activity.tool.blocked", "rejected"),
    ("activity.tool.unavailable", "rejected"),
])
def test_activity_dedup_requires_the_same_rendered_occurrence(
        conversation_bundle: Path, summary_key: str, status: str):
    result = _run(conversation_bundle, "const key=" + json.dumps(summary_key)
                  + ";const status=" + json.dumps(status) + r"""
const diagnostic = (taskId, attemptId, ordinal=1) => ({
  id:'diagnostic', spanId:'span', seq:4, occurredAt:4, kind:'tool',
  status:status==='error'?'failed':'skipped', severity:'error', count:1,
  summary:'kept failure', summaryKey:key, toolName:'read', toolCallId:'reused',
  taskId, attemptId, anchorToolRoundNum:ordinal,
});
const round = {roundNum:1, llmRound:0, toolCallId:'reused', toolName:'read',
  status, taskId:'new-task', attemptId:'new-attempt',
  ...(status==='rejected'?{rejection:{reason:'not run'}}:{})};
const segment = {type:'tool_use', blockId:'new-tool', id:'reused', name:'read',
  input:{}, result:{status}, roundNum:1, llmRound:0,
  taskId:'new-task', attemptId:'new-attempt'};
const count = (entry, segments=[segment], rounds=[round]) => {
  const turn={turnId:'t', conversationId:'c', laneId:'main', ordinal:1,
    actor:'assistant', kind:'reply', runId:'', status:'completed',
    currentAttemptId:null, projectionRevision:1, createdAt:1, updatedAt:2,
    settlement:{outcome:'completed'}, projection:{segments,toolRounds:rounds,
      activityTimeline:{blockId:'activity',version:1,entries:[entry]}}};
  return feature.selectTurnBlocks(turn).filter(b=>b.kind==='activity-event').length;
};
console.log(JSON.stringify({
  conflicting:count(diagnostic('old-task','old-attempt')),
  current:count(diagnostic('new-task','new-attempt')),
  trimmed:count(diagnostic('new-task','new-attempt'), []),
  missingOrdinal:count(diagnostic('new-task','new-attempt',2)),
  ambiguous:count(diagnostic(undefined,undefined),[
    {...segment,blockId:'old-tool',taskId:'old-task',attemptId:'old-attempt'},segment],
    [{...round,taskId:'old-task',attemptId:'old-attempt'},round]),
  stalePair:count(diagnostic('new-task','new-attempt'),[segment],
    [{...round,taskId:'old-task',attemptId:'old-attempt'}]),
  childAnchoredAtParent:count({...diagnostic('new-task','new-attempt'),toolCallId:'child'}),
}));
""")
    assert result == {"conflicting": 1, "current": 0, "trimmed": 1,
                      "missingOrdinal": 1, "ambiguous": 1, "stalePair": 1,
                      "childAnchoredAtParent": 1}


def test_gateway_validation_replay_does_not_borrow_a_reused_call_id(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const gateway = (ordinal, scope, message) => ({roundNum:ordinal, llmRound:0,
  toolCallId:'gateway', toolName:'execute_tools', status:'error',
  taskId:scope, attemptId:scope, toolContent:JSON.stringify({
    errors:[{name:'read_files',code:'invalid',message}]})});
const old=gateway(1,'old','old failure'), current=gateway(1,'new','new failure');
const read = (entryExtra, rounds) => {
  const entry={id:'gateway-diagnostic',spanId:'span',seq:4,occurredAt:4,kind:'tool',
    status:'failed',severity:'error',count:1,summary:'gateway failed',
    summaryKey:'activity.tool.failed',toolName:'execute_tools',toolCallId:'gateway',
    ...entryExtra};
  const turn={turnId:'t',conversationId:'c',laneId:'main',ordinal:1,actor:'assistant',
    kind:'reply',runId:'',status:'completed',currentAttemptId:null,projectionRevision:1,
    createdAt:1,updatedAt:2,settlement:{outcome:'completed'},projection:{segments:[],
      toolRounds:rounds,activityTimeline:{blockId:'activity',version:1,entries:[entry]}}};
  return feature.selectTurnBlocks(turn).filter(b=>b.kind==='activity-event')
    .map(b=>({tool:b.value.toolName,detail:b.value.detail??null}));
};
console.log(JSON.stringify({
  scoped:read({taskId:'new',attemptId:'new',anchorToolRoundNum:1},[old,current]),
  ambiguous:read({},[old,current]),
  trimmed:read({taskId:'new',attemptId:'new',anchorToolRoundNum:1},[old]),
  noPayload:read({},[{...current,toolContent:''}]),
}));
""")
    retained = [{"tool": "execute_tools", "detail": None}]
    assert result == {"scoped": [{"tool": "read_files", "detail": "new failure"}],
                      "ambiguous": retained, "trimmed": retained, "noPayload": retained}


def test_surface_first_paint_scales_with_viewport_and_keeps_history(conversation_bundle):
    result = _run(conversation_bundle, r"""
const {JSDOM}=require('jsdom');const dom=new JSDOM('<main id="viewport"><div id="chat"></div></main>');
global.Element=dom.window.Element;const document=dom.window.document;const viewport=document.getElementById('viewport');
let height=400;Object.defineProperty(viewport,'clientHeight',{get:()=>height});
const store=feature.createTurnStore('viewport-budget');
const turns=Array.from({length:100},(_,i)=>({turnId:'t-'+i,presentationId:'t-'+i,
  conversationId:'viewport-budget',laneId:'main',ordinal:i+1,actor:'human',kind:'text',
  status:'completed',currentAttemptId:null,projectionRevision:1,createdAt:1,updatedAt:1,
  settlement:{outcome:'completed'},projection:{segments:i===99
    ?Array.from({length:160},(_,j)=>({type:'text',blockId:'block-'+j,text:'History '+j,terminal:j===159,deliverable:j===159}))
    :[{type:'text',blockId:'text-'+i,text:'Turn '+i}]}}));
store.dispatch({type:'snapshot',snapshot:{conversationRevision:1,turns,authoritativeFull:true}});
const surface=feature.createConversationSurface(document.getElementById('chat'),{scrollViewport:viewport,
 renderBlock(node,block){node.textContent=block.markdown;}});
const paint=()=>surface.render(feature.selectConversationViewModel(store.getState()));paint();
const count=()=>({turns:surface.root.querySelectorAll('[data-turn-id]').length,
 blocks:surface.root.querySelector('[data-turn-id="t-99"]')?.querySelectorAll('[data-block-id]').length??0});
const small=count();const historyPreserved=Object.keys(store.getState().turnsById).length===100;
surface.showEarlier();const earlier=!!surface.root.querySelector('[data-turn-id="t-89"]');
surface.followLatest();height=1600;paint();const large=count();
const firstIndex=()=>Number(surface.root.querySelector('[data-turn-id="t-99"] [data-block-id]').dataset.blockId.slice(6));
const tailStart=firstIndex();
surface.root.querySelector('[data-turn-id="t-99"] [data-conversation-block-window-action="earlier"]').click();
const shifted=count().blocks===large.blocks
 && firstIndex()<tailStart
 && !surface.root.querySelector('[data-turn-id="t-99"] [data-block-id="block-159"]');
surface.followLatest();
const latestReset=count().blocks===large.blocks
 && !!surface.root.querySelector('[data-turn-id="t-99"] [data-block-id="block-159"]');
surface.dispose();dom.window.close();
console.log(JSON.stringify({small,large,historyPreserved,earlier,shifted,latestReset}));
""")
    assert result['small']['turns'] == 8
    assert result['large']['turns'] == 20
    assert result['small']['blocks'] < result['large']['blocks'] < 160
    assert result['historyPreserved'] and result['earlier']
    assert result['shifted'] and result['latestReset']


def test_deferred_terminal_projection_recovers_before_patch_and_accepts_full_authority(conversation_bundle):
    result = _run(conversation_bundle, r"""
const lazy={turnId:'t',conversationId:'c',laneId:'main',ordinal:1,actor:'assistant',kind:'reply',
  status:'completed',currentAttemptId:'a',projectionRevision:3,
  projection:{content:'answer',toolRounds:[{toolCallId:'call',status:'done',
    _snapshotToolResult:{preview:'preview'}}]}};
let state=feature.reduceTurnState(feature.createTurnState('c'),{type:'snapshot',snapshot:{
  conversationRevision:1,turns:[lazy],attempts:[{attemptId:'a',turnId:'t',status:'completed'}]}});
let gaps=0;
const patch={version:1,baseRevision:3,targetRevision:4,operations:[]};
const event={turnId:'t',attemptId:'a',seq:1,projectionRevision:4,type:'status_changed',
  payload:{projectionPatch:patch,turnState:{turnId:'t',status:'pending'}}};
const blockedEvent=feature.reduceTurnState(state,{type:'event',event},{onProjectionGap:()=>gaps++});
const blockedDelta=feature.reduceTurnState(state,{type:'snapshot',snapshot:{conversationRevision:2,
  turnPatches:[{turnId:'t',baseProjectionRevision:3,targetProjectionRevision:4,
    projectionPatch:patch,updatedAt:2}]}},{onProjectionGap:()=>gaps++});
const full={...lazy,status:'pending',currentAttemptId:'a',projectionRevision:4,
  projection:{content:'answer',toolRounds:[{toolCallId:'call',status:'done',toolContent:'complete original output'}]}};
state=feature.reduceTurnState(blockedEvent,{type:'snapshot',snapshot:{conversationRevision:2,
  turns:[full],attempts:[{attemptId:'a',turnId:'t',status:'pending'}]}});
const resumed=feature.reduceTurnState(state,{type:'event',event:{...event,seq:2,projectionRevision:5,
  payload:{projectionPatch:{version:1,baseRevision:4,targetRevision:5,
    operations:[{op:'set',path:['content'],value:'continued answer'}]}}}},
  {onProjectionGap:()=>gaps++});
console.log(JSON.stringify({gaps,eventRevision:blockedEvent.turnsById.t.projectionRevision,
  eventStatus:blockedEvent.turnsById.t.status,deltaRevision:blockedDelta.turnsById.t.projectionRevision,
  resumedRevision:resumed.turnsById.t.projectionRevision,content:resumed.turnsById.t.projection.content,
  output:resumed.turnsById.t.projection.toolRounds[0].toolContent,
  sourceReadonly:state.turnsById.t.projection.content==='answer'}));
""")
    assert result == {'gaps': 2, 'eventRevision': 3, 'eventStatus': 'completed', 'deltaRevision': 3,
                      'resumedRevision': 5, 'content': 'continued answer',
                      'output': 'complete original output', 'sourceReadonly': True}



def test_large_closed_reasoning_defers_latest_body_and_retires_listener(conversation_bundle):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<main id="chat"></main>');
const document = dom.window.document;
global.Element = dom.window.Element;
let parses = 0;
const classic = feature.createClassicConversationRenderers({
  renderSafeMarkdownHtml(text) {
    parses++;
    const p=document.createElement('p'); p.textContent=text; return p.outerHTML;
  },
});
const node=document.createElement('div'); document.getElementById('chat').append(node);
const makeBlock = (text, terminal=true) => ({
  kind:'thinking', blockId:'reason', identitySource:'contract',
  markdown:'original '.repeat(1500), displayMarkdown:text,
  displayMode:'translated', terminal,
  source:{type:'thinking', blockId:'reason', text:'canonical evidence'},
});
const turn={actor:'assistant'};
const render = b => classic.renderBlock(node,b,{turn});
const original=makeBlock('old translation '.repeat(1500));
render(original);
const details=node.querySelector('details');
const body=node.querySelector('.thinking-text');
const initiallyDeferred=parses===0 && !details.open && body.childNodes.length===0;
const latest=makeBlock('latest <script>literal</script> '.repeat(1500));
const before=JSON.stringify(latest);
render(latest);
details.open=true; details.dispatchEvent(new dom.window.Event('toggle'));
const latestVisible=body.textContent===latest.displayMarkdown && !body.querySelector('script');
const parsedOnce=parses===1;
details.open=false; details.dispatchEvent(new dom.window.Event('toggle'));
render(makeBlock(latest.displayMarkdown));
details.open=true; details.dispatchEvent(new dom.window.Event('toggle'));
const reused=parses===1 && node.querySelector('details')===details;
// An update while closed must replace the deferred callback, including a
// switch from translated text back to the full original reasoning.
details.open=false; details.dispatchEvent(new dom.window.Event('toggle'));
const next={...latest,displayMarkdown:latest.markdown,displayMode:'original'};
render(next);
const staleRemoved=body.childNodes.length===0 && parses===1;
details.open=true; details.dispatchEvent(new dom.window.Event('toggle'));
const originalRestored=body.textContent===next.markdown && parses===2;
classic.disposeBlock(node);
details.open=false;details.dispatchEvent(new dom.window.Event('toggle'));
details.open=true;details.dispatchEvent(new dom.window.Event('toggle'));
const retired=parses===2;
console.log(JSON.stringify({initiallyDeferred,latestVisible,parsedOnce,reused,
  staleRemoved,originalRestored,retired,sourceUnchanged:JSON.stringify(latest)===before}));
dom.window.close();
""")
    assert all(result.values()), result


def test_large_streaming_reasoning_keeps_live_body_and_defers_terminal_highlight(conversation_bundle):
    result = _run(conversation_bundle, r"""
const { JSDOM } = require('jsdom');
const dom=new JSDOM('<main></main>');const document=dom.window.document;
const calls=[];
const classic=feature.createClassicConversationRenderers({
 renderSafeMarkdownHtml(text,options) {calls.push(options?.streaming===true);return '<p>live body</p>';},
});
const node=document.createElement('div');document.body.append(node);
const block={kind:'thinking',blockId:'reason',identitySource:'contract',
 markdown:'reason '.repeat(2000),displayMode:'original',terminal:false,
 source:{type:'thinking',blockId:'reason'}};
classic.renderBlock(node,block,{turn:{actor:'assistant'}});
const details=node.querySelector('details');
const liveVisible=details.open && node.querySelector('.thinking-text').textContent==='live body';
classic.renderBlock(node,{...block,terminal:true},{turn:{actor:'assistant'}});
const terminalDeferred=!details.open && calls.length===1;
details.open=true;details.dispatchEvent(new dom.window.Event('toggle'));
const finalHighlight=calls.length===2 && calls[0] && !calls[1];
console.log(JSON.stringify({liveVisible,terminalDeferred,finalHighlight}));
classic.disposeBlock(node);dom.window.close();
""")
    assert all(result.values()), result


def test_task_time_header_repaints_without_mutating_turn_authority(conversation_bundle):
    result = _run(conversation_bundle, r"""
const {JSDOM}=require('jsdom');
const dom=new JSDOM('<main id="root"></main>');
global.document=dom.window.document;
const turn={turnId:'turn',conversationId:'conv',laneId:'main',ordinal:1,actor:'assistant',
  kind:'reply',runId:'run',status:'completed',projectionRevision:1,
  currentAttemptId:'attempt',projection:{content:'answer',timestamp:1234},createdAt:1234};
const state={conversationId:'conv',conversationRevision:1,transport:'live',turnsById:{turn},
  laneOrder:{main:['turn']},attemptsById:{attempt:{attemptId:'attempt',turnId:'turn',taskId:'task',createdAt:1234}},
  queueItems:[],pendingEventsByTurn:{},commandPending:{},liveRoundUsageByTurn:{}};
const original=JSON.stringify(state);
const map=new Map();
const surface=feature.createConversationSurface(document.querySelector('#root'));
surface.render(feature.selectConversationViewModel(state,{}, {taskTimePresentationsByAttempt:map}));
const record={id:'a'.repeat(64),runId:'run',stepId:'show',conversationId:'conv',attemptId:'attempt',turnId:'turn',
  output:{startedAt:1.234,timezone:'UTC',text:'任务开始时间：1970-01-01T00:00:01.234+00:00 [UTC] <img>'}};
map.set('attempt',[record]);
surface.render(feature.selectConversationViewModel(state,{}, {taskTimePresentationsByAttempt:map}));
const node=document.querySelector('[data-automation-presentation-id]');
const visible=node?.textContent; const html=node?.querySelector('img');
map.set('another-attempt',[{...record,attemptId:'another-attempt'}]);map.delete('attempt');
surface.render(feature.selectConversationViewModel(state,{}, {taskTimePresentationsByAttempt:map}));
console.log(JSON.stringify({visible,html:!!html,stale:!!document.querySelector('[data-automation-presentation-id]'),
  unchanged:JSON.stringify(state)===original}));
surface.dispose();
""")
    assert result=={'visible':'任务开始时间：1970-01-01T00:00:01.234+00:00 [UTC] <img>',
                    'html':False,'stale':False,'unchanged':True}



def test_translation_snapshot_refreshes_renderer_with_unchanged_model_revision(conversation_bundle):
    result = _run(conversation_bundle, r"""
const {JSDOM} = require('jsdom');
const dom = new JSDOM('<div id="chat"></div>');
global.window=dom.window; global.document=dom.window.document; global.Element=dom.window.Element;
const turn={turnId:'translated',conversationId:'translation-conv',laneId:'main',ordinal:1,
  actor:'assistant',kind:'reply',runId:'',status:'completed',currentAttemptId:null,
  projection:{content:'Original answer',segments:[{type:'text',blockId:'text:terminal',
    text:'Original answer',deliverable:true,terminal:true}]},
  projectionRevision:1,modelContextRevision:1,settlement:{outcome:'completed'},createdAt:1,updatedAt:1};
const store=feature.createTurnStore('translation-conv');
const scheduled=[];
const controller=feature.createConversationSurfaceController({
  isActive:()=>true,getContainer:()=>document.getElementById('chat'),
  schedule(render){scheduled.push(render);return ()=>{};},
  nativeRenderers:feature.createClassicConversationRenderers({
    renderSafeMarkdownHtml:value=>value,actionLabel:value=>value,
    localizedText:(_key,fallback)=>fallback}),onIntent() {}});
function show(snapshot){store.dispatch({type:'snapshot',snapshot});
  controller.render({id:'translation-conv'},store.getState()); scheduled.shift()();}
show({conversationRevision:1,turns:[turn]});
const original=document.querySelector('[data-turn-id="translated"]');
const overlay={...turn,projectionRevision:2,modelContextRevision:1,updatedAt:2,
  projection:{...turn.projection,translatedContent:'后台译文',_showingTranslation:true,
    segments:[{...turn.projection.segments[0],translatedText:'后台译文'}]}};
show({conversationRevision:2,turns:[overlay]});
console.log(JSON.stringify({sameTurnNode:original===document.querySelector('[data-turn-id="translated"]'),
  rendered:document.getElementById('chat').textContent.includes('后台译文'),
  sourcePreserved:store.getState().turnsById.translated.projection.content==='Original answer',
  modelRevision:store.getState().turnsById.translated.modelContextRevision,
  projectionRevision:store.getState().turnsById.translated.projectionRevision}));
controller.dispose();
""")
    assert result == {'sameTurnNode': True, 'rendered': True, 'sourcePreserved': True,
                      'modelRevision': 1, 'projectionRevision': 2}


@pytest.mark.parametrize("settlement", ["resolve", "reject"])
def test_surface_history_loading_single_flight_retry_and_disposal(
        conversation_bundle: Path, settlement: str):
    result = _run(conversation_bundle, "const settlement=" + json.dumps(settlement) + ";" + r"""
(async () => {
const {JSDOM}=require('jsdom');
const dom=new JSDOM('<main id="chat"></main>');
global.Element=dom.window.Element;const document=dom.window.document;
const state=feature.createTurnState('async-history');
state.laneOrder.main=['t'];
state.turnsById.t={turnId:'t',conversationId:'async-history',laneId:'main',ordinal:100,
 actor:'human',kind:'input',status:'completed',projectionRevision:1,
 projection:{content:'loaded'},createdAt:1,updatedAt:1};
state.historyByLane={main:{hasMore:true,nextBeforeOrdinal:100,totalTurns:100}};
const flights=[];const intents=[];
const surface=feature.createConversationSurface(document.getElementById('chat'),{
 onIntent(intent){intents.push(intent);return new Promise((resolve,reject)=>flights.push({resolve,reject}));}
});
surface.render(feature.selectConversationViewModel(state));
const boundary=()=>surface.root.querySelector('[data-conversation-part="turn-window-earlier"]');
const snapshot=()=>({state:boundary().dataset.historyState,
 disabled:boundary().querySelector('button').disabled,
 status:boundary().querySelector('[role="status"]').textContent});
surface.showEarlier();const loading=snapshot();
surface.showEarlier();boundary().querySelector('button').click();
const singleFlight=intents.length===1;
flights[0][settlement](settlement==='reject'?new Error('offline'):undefined);
await Promise.resolve();await Promise.resolve();
const settled=snapshot();
surface.showEarlier();const retried=intents.length===2 && snapshot().state==='loading';
surface.dispose();flights[1][settlement](settlement==='reject'?new Error('late'):undefined);
await Promise.resolve();await Promise.resolve();
const disposed=document.getElementById('chat').children.length===0;
dom.window.close();console.log(JSON.stringify({loading,singleFlight,settled,retried,disposed,
 cursors:intents.map(i=>i.beforeOrdinal)}));
})().catch(error=>{console.error(error);process.exitCode=1;});
""")
    assert result["loading"]["state"] == "loading"
    assert result["loading"]["disabled"] is True
    assert "Loading" in result["loading"]["status"]
    assert result["singleFlight"] is True
    assert result["settled"]["state"] == ("failed" if settlement == "reject" else "idle")
    assert result["settled"]["disabled"] is False
    if settlement == "reject":
        assert "retry" in result["settled"]["status"]
    assert result["retried"] and result["disposed"]
    assert result["cursors"] == [100, 100]


def test_surface_same_round_history_is_bounded_and_stream_does_not_steal_reading(
        conversation_bundle: Path):
    result = _run(conversation_bundle, r"""
const {JSDOM}=require('jsdom');const dom=new JSDOM('<main id="chat"></main>');
global.Element=dom.window.Element;const document=dom.window.document;
const store=feature.createTurnStore('same-round');
const turn=count=>({turnId:'t',conversationId:'same-round',laneId:'main',ordinal:1,
 actor:'assistant',kind:'reply',status:'running',currentAttemptId:'a',projectionRevision:count,
 createdAt:1,updatedAt:count,projection:{segments:Array.from({length:count},(_,i)=>({
 type:'text',blockId:'b-'+i,text:'block '+i,roundNum:1,llmRound:0}))}});
const update=count=>store.dispatch({type:'snapshot',snapshot:{conversationRevision:count,
 turns:[turn(count)],authoritativeFull:true}});
const surface=feature.createConversationSurface(document.getElementById('chat'),{
 windowing:{maxBlocksPerTurn:20,blockBatchSize:10},renderBlock(node,b){node.textContent=b.markdown;}});
const paint=()=>surface.render(feature.selectConversationViewModel(store.getState()));
const ids=()=>Array.from(surface.root.querySelectorAll('[data-block-id]')).map(n=>n.dataset.blockId);
const earlier=()=>surface.root.querySelector('[data-conversation-block-window-action="earlier"]');
const later=()=>surface.root.querySelector('[data-conversation-block-window-action="later"]');
update(125);paint();const initial=ids();const seen=new Set(initial);let bounded=true;let monotonic=true;
let last=105;let guard=0;
while(!earlier().disabled && guard++<20){earlier().click();const current=ids();
 const start=Number(current[0].slice(2));monotonic&&=start<last;last=start;
 bounded&&=current.length<=20;current.forEach(id=>seen.add(id));}
const reading=ids();update(130);paint();const streamPreserved=JSON.stringify(ids())===JSON.stringify(reading);
guard=0;while(!later().disabled && guard++<20){later().click();bounded&&=ids().length<=20;}
const reachedLatest=ids().includes('b-129');earlier().click();surface.followLatest();
const reset=ids().includes('b-129') && ids().length===20;
const durable=store.getState().turnsById.t.projection.segments.length;
surface.dispose();dom.window.close();console.log(JSON.stringify({initial:initial[0],seen:seen.size,
 bounded,monotonic,streamPreserved,reachedLatest,reset,durable}));
""")
    assert result == {"initial": "b-105", "seen": 125, "bounded": True,
                      "monotonic": True, "streamPreserved": True,
                      "reachedLatest": True, "reset": True, "durable": 130}


def test_remote_prepend_retains_visible_anchor_with_small_turn_budget(conversation_bundle):
    result = _run(conversation_bundle, r"""
const {JSDOM}=require('jsdom');const dom=new JSDOM('<main id="chat"></main>');
global.Element=dom.window.Element;const document=dom.window.document;
const turn=i=>({turnId:'t-'+i,conversationId:'small-prepend',laneId:'main',ordinal:i,
 actor:'assistant',kind:'reply',status:'completed',projectionRevision:1,
 projection:{content:'Turn '+i},createdAt:i,updatedAt:i});
const state=turns=>({...feature.createTurnState('small-prepend'),
 turnsById:Object.fromEntries(turns.map(t=>[t.turnId,t])),laneOrder:{main:turns.map(t=>t.turnId)},
 historyByLane:{main:{hasMore:true,nextBeforeOrdinal:turns[0].ordinal,totalTurns:200}}});
const loaded=Array.from({length:8},(_,i)=>turn(i+100));
const surface=feature.createConversationSurface(document.getElementById('chat'),{
 windowing:{maxTurns:8,batchSize:20},onIntent(){}});
surface.render(feature.selectConversationViewModel(state(loaded)));
const anchor=surface.root.querySelector('[data-turn-id="t-100"]');
surface.showEarlier();
const older=Array.from({length:64},(_,i)=>turn(i+36));
surface.render(feature.selectConversationViewModel(state([...older,...loaded])));
const visible=[...surface.root.querySelectorAll('[data-turn-id]')];
const output={same:surface.root.querySelector('[data-turn-id="t-100"]')===anchor,
 count:visible.length,first:visible[0].dataset.turnId,last:visible.at(-1).dataset.turnId};
surface.dispose();dom.window.close();console.log(JSON.stringify(output));
""")
    assert result == {"same": True, "count": 8, "first": "t-96", "last": "t-103"}
