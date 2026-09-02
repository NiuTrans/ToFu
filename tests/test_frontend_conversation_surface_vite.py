"""Behavioral contracts for the typed ConversationSurface production graph."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
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
  currentAttemptId:null, projection, projectionRevision:3,
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
    assert result["intent"] == {
        "type": "copy-block",
        "conversationId": "conv-a",
        "turnId": "turn-1",
        "blockId": "text:terminal",
        "laneId": "main",
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
  ], content:'**answer**', model:'model-a', timestamp:2},
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
const node = document.querySelector('[data-turn-id="assistant-1"]');
node.querySelector('[data-conversation-action="resume"]').click();
node.querySelector('[data-conversation-action="translate"]').click();
node.querySelector('[data-conversation-action="inspect"]').click();
console.log(JSON.stringify({
  compatible:Boolean(
    feature.selectConversationViewModel(state).mainLane.turns[0]),

  text:node.querySelector('[data-block-id="text:terminal"] .md-content').textContent,
  thinking:node.querySelector('[data-block-id="thinking:llm-1"]').textContent,
  footer:node.querySelector('[data-conversation-part="turn-footer"]').textContent,
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
const first = document.querySelector('[data-block-id="tool:call-1"]');
controller.render(conversation, makeState(makeTurn(2, 'two')));
scheduled.shift()();
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
    return `<details class="native-tool"><summary>${block.name}</summary>`
      + `<div>${block.result.content}</div></details>`;
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
thinkingDetails.open = true;
firstToolDetails.open = true;
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

// A real rich-tool delta may replace its internals, but must restore the
// reader's disclosure, keyboard focus, and local scroll position.
clonedToolDetails.scrollTop = 19;
clonedToolDetails.querySelector('summary').focus();
surface.render(makeViewModel(4, 'reason grows', 'result two'));
const updatedToolDetails = toolBlock.querySelector('details');
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
  changedToolNodeReplaced:firstToolDetails !== updatedToolDetails,
  changedToolStillOpen:updatedToolDetails.open,
  changedToolFocusPreserved:document.activeElement
    === updatedToolDetails.querySelector('summary'),
  changedToolScrollPreserved:updatedToolDetails.scrollTop,
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
        "changedToolNodeReplaced": True,
        "changedToolStillOpen": True,
        "changedToolFocusPreserved": True,
        "changedToolScrollPreserved": 19,
        "markdownRenders": 3,
        "toolRenders": 2,
        "headerRenders": 1,
        "timeTag": "TIME",
        "timeIsShort": True,
        "timeTitle": "2026年8月26日 15:31",
        "timeHasMachineValue": True,
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
    count:1, summary:'Kimi K3 request failed', model:'kimi-k3', statusCode:400,
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
    activityTimeline:{blockId:'activity-timeline', version:1, entries},
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
  activityMarkerSvgs:document.querySelectorAll('.activity-event__marker svg').length,
  switchVisible:document.body.textContent.includes('Switched from Kimi K3 to GLM 5.3'),
  retryVisible:document.body.textContent.includes('Retrying Kimi K3'),
  retryFacts:block('activity:retry-1')
    .querySelector('.activity-event__facts').textContent,
  richToolRenders,
  richToolText:block('tool:call-1').textContent,
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
            "text:terminal",
            "activity:model-1",
            "activity:switch-1",
        ],
        "domOrder": [
            "tool:call-1",
            "activity:tool-fail",
            "activity:schema-1",
            "text:terminal",
            "activity:model-1",
            "activity:switch-1",
            "activity:retry-1",
        ],
        "fallbackInTimeline": True,
        "toolBlocks": 1,
        "errorRowUnderCall": "activity:tool-fail",
        "schemaAfterErrorRow": "activity:schema-1",
        "failureAfterRoundText": "text:terminal",
        "switchAfterFailure": "activity:model-1",
        "retryAfterSwitch": "activity:switch-1",
        "timelineBlocks": 0,
        "fallbackTags": 0,
        "infoRowsHidden": True,
        "gatewayShellHidden": True,
        "failureRowStable": True,
        "schemaVisible": True,
        "schemaSummary": "write_file schema rejected",
        "activityMarkerSvgs": 5,
        "switchVisible": True,
        "retryVisible": True,
        "retryFacts": "kimi-k3",
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
  id:'compact-1', spanId:'compaction:archive-a', seq:2, occurredAt:1020,
  startedAt:1000, endedAt:1020, durationMs:20, kind:'status',
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
    activityTimeline:{blockId:'activity-timeline', version:1, entries}},
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
const vmTurn = feature.selectConversationViewModel(state).mainLane.turns[0];
const row = document.querySelector('[data-activity-id="compact-1"]');
const before = row.querySelector('.compaction-token-stage.is-before');
const after = row.querySelector('.compaction-token-stage.is-after');
row.querySelector('[data-conversation-action="open-compaction"]').click();
console.log(JSON.stringify({
  blockKinds:vmTurn.blocks.map(block => block.kind),
  infoToolHidden:!document.querySelector('[data-activity-id="tool-info"]'),
  compactionClass:row.classList.contains('activity-event--compaction'),
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
        "blockKinds": ["text", "activity-event"],
        "infoToolHidden": True,
        "compactionClass": True,
        "title": "Context compacted",
        "beforeCount": "180000",
        "beforeText": "≈180,000",
        "afterCount": "42000",
        "afterText": "≈42,000",
        "saved": "77% less",
        "snapshotAction": [["open-compaction", "archive-a"]],
    }


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
        "summaryKey": "activity.tool.skipped",
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
        "childActions": ["copy"],
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
      ? `rate limited ${values.model} ${values.attempt}` : fallback,
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
controller.render(conversation, makeState(null, 'completed'));
scheduled.shift()();
const payload = {
  stableNode:first === second,
  firstText,
  secondText,
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
        "removedAtSettlement": True,
        "stateUnchanged": True,
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
        "clearedText": "Waiting for the agent…",
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
const firstVm = feature.selectConversationViewModel(firstState);
const blockOrder = firstVm.mainLane.turns[0].blocks.map(block => block.blockId);
const firstNode = document.querySelector(
  '[data-block-id="injection:user-steer:round-2"]');
firstNode.querySelector('details').open = false;
controller.render(conversation, secondState);
scheduled.shift()();
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
first.querySelector('[data-conversation-action="undo-turn-files"]').click();
controller.render(conversation, makeState(makeTurn(2, 'undone')));
scheduled.shift()();
const second = document.querySelector('[data-block-id="file-changes"]');
second.querySelector('[data-conversation-action="redo-turn-files"]').click();
console.log(JSON.stringify({
  native:Boolean(vm.mainLane.turns[0]),
  blockKinds:vm.mainLane.turns[0].blocks.map(block => block.kind),

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
        "blocks": ["image-generation", "text:terminal"],
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
        "status": "Reasoning…3 chars...",
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
