"""Inbox display is evidence-based, never a Turn-wide sidecar presence check."""
import json

import pytest

from tests.frontend import test_frontend_conversation_surface_vite as surface_tests

conversation_bundle = surface_tests.conversation_bundle
_run = surface_tests._run

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('change,hidden', [
    ({}, True),
    ({'note': {'text': 'agent-message: unrelated same-round payload'}}, False),
    ({'note': {'llmRound': 4}}, False),
    ({'note': {'llmRound': None}}, False),
    ({'note': {'text': 'report A\n\nreport B '}}, False),
    ({'note': {'attemptId': 'other-attempt'}}, False),
    ({'note': {'taskId': 'other-producer'}}, False),
    ({'sidecar': {'attemptId': None}}, False),
    ({'sidecar': {'taskId': None}}, False),
    ({'sidecar': {'round': None}}, False),
    ({'sidecar': {'count': 3}}, False),
    ({'sidecar': {'previews': [{'text': 'report A\n\nreport B', 'textTruncated': True}]}}, False),
    ({'note': {'action': 'stop'}}, False),
    ({'note': {'reason': 'new-inbox-reason'}}, False),
    ({'note': {'attemptId': None, 'taskId': None},
      'sidecar': {'attemptId': None, 'taskId': None}}, True),
])
def test_only_exact_same_scope_complete_preview_payload_is_deduplicated(
        conversation_bundle, change, hidden):
    result = _run(conversation_bundle, "const change=" + json.dumps(change) + r""";
const note={type:'system_note',blockId:'note',noteKind:'context-injection',reason:'inbox_delivery',
 action:'prompt',text:'report A\n\nreport B',llmRound:3,attemptId:'attempt',taskId:'producer',...change.note};
const sidecar={blockId:'inbox',round:4,count:2,attemptId:'attempt',taskId:'producer',
 previews:[{text:'report A'},{text:'report B'}],...change.sidecar};
const turn={turnId:'turn',conversationId:'conv',laneId:'main',ordinal:1,actor:'assistant',
 kind:'reply',status:'completed',currentAttemptId:null,projectionRevision:1,createdAt:1,updatedAt:2,
 projection:{segments:[note,{type:'text',blockId:'answer',text:'Done',terminal:true,deliverable:true}],
 _inboxInjects:[sidecar]}};
const state={conversationId:'conv',conversationRevision:1,transport:'live',turnsById:{turn},
 laneOrder:{main:['turn']},attemptsById:{},queueItems:[],pendingEventsByTurn:{},commandPending:{},liveRoundUsageByTurn:{}};
const before=JSON.stringify(state);
const select=value=>feature.selectConversationViewModel(value).mainLane.turns[0].blocks;
const blocks=select(state), reload=select(JSON.parse(before));
console.log(JSON.stringify({notes:blocks.filter(b=>b.kind==='system-note').map(b=>b.text),
 chips:blocks.filter(b=>b.kind==='injections').length,unchanged:before===JSON.stringify(state),
 reloadEqual:JSON.stringify(blocks)===JSON.stringify(reload)}));
""")
    assert result['notes'] == ([] if hidden else [change.get('note', {}).get('text', 'report A\n\nreport B')])
    assert result['chips'] == 1
    assert result['unchanged'] and result['reloadEqual']


def test_mixed_completion_and_agent_message_keep_only_proven_duplicate_hidden(conversation_bundle):
    result = _run(conversation_bundle, r"""
const note=(id,text,llmRound)=>({type:'system_note',blockId:id,noteKind:'context-injection',
 reason:'inbox_delivery',action:'prompt',text,llmRound,attemptId:'a',taskId:'t'});
const turn={turnId:'turn',conversationId:'conv',laneId:'main',ordinal:1,actor:'assistant',kind:'reply',
 status:'completed',currentAttemptId:null,projectionRevision:1,createdAt:1,updatedAt:2,
 projection:{segments:[note('duplicate','completion',1),note('agent','agent-message',1),
 note('later','later delivery',2),{type:'text',blockId:'answer',text:'Done',terminal:true,deliverable:true}],
 _inboxInjects:[{blockId:'completion',round:2,count:1,attemptId:'a',taskId:'t',previews:[{text:'completion'}]}]}};
const vm=feature.selectConversationViewModel({conversationId:'conv',conversationRevision:1,transport:'live',
 turnsById:{turn},laneOrder:{main:['turn']},attemptsById:{},queueItems:[],pendingEventsByTurn:{},commandPending:{},liveRoundUsageByTurn:{}});
const blocks=vm.mainLane.turns[0].blocks;
const notes=blocks.flatMap(b=>b.kind==='system-note'?[b.text]:b.kind==='system-note-group'?b.items.map(n=>n.text):[]);
console.log(JSON.stringify({notes,chips:blocks.filter(b=>b.kind==='injections').length}));
""")
    assert result == {'notes': ['agent-message', 'later delivery'], 'chips': 1}


@pytest.mark.parametrize('payload,hidden', [
    ('completion\n\npeer update', True),
    ('completion\n\npeer update\n\nagent message', False),
    ('peer update\n\ncompletion', False),
])
def test_legacy_combined_completion_peer_carrier_requires_exact_payload(conversation_bundle, payload, hidden):
    result = _run(conversation_bundle, 'const payload=' + json.dumps(payload) + r""";
const turn={turnId:'turn',conversationId:'conv',laneId:'main',ordinal:1,actor:'assistant',kind:'reply',
 status:'completed',currentAttemptId:null,projectionRevision:1,createdAt:1,updatedAt:2,
 projection:{segments:[{type:'system_note',blockId:'old-note',noteKind:'context-injection',
 reason:'inbox_delivery',action:'prompt',text:payload,llmRound:0},
 {type:'text',blockId:'answer',text:'Done',terminal:true,deliverable:true}],
 _inboxInjects:[{blockId:'completion',round:1,count:1,previews:[{text:'completion'}]}],
 _peerInjects:[{blockId:'peer',round:1,count:1,previews:[{text:'peer update'}]}]}};
const vm=feature.selectConversationViewModel({conversationId:'conv',conversationRevision:1,transport:'live',
 turnsById:{turn},laneOrder:{main:['turn']},attemptsById:{},queueItems:[],pendingEventsByTurn:{},commandPending:{},liveRoundUsageByTurn:{}});
console.log(JSON.stringify({notes:vm.mainLane.turns[0].blocks.filter(b=>b.kind==='system-note').length}));
""")
    assert result['notes'] == (0 if hidden else 1)


@pytest.fixture(scope='module')
def inbox_render_harness():
    # Reuse the retained-source extractor rather than copying its row policy.
    from tests._paper_vite import compiled_typescript
    from tests.tasks_pkg.test_inject_row_positioning import _extract_core_inject_fns

    entry = """
import { createToolInjectionPresentation } from './tool-injection-presentation';
(window as any).createInboxTestPresentation = createToolInjectionPresentation;
"""
    owner = surface_tests.ROOT / 'frontend/src/conversation/presentation/tool-injection-presentation.ts'
    with compiled_typescript(owner, contents=entry) as bundle:
        rehydrate = _extract_core_inject_fns() + '\nreturn _rehydrateInjectRows;'
        yield (
            'new Function(require("fs").readFileSync(' + json.dumps(bundle) + ', "utf8"))();\n'
            'const rehydrate = new Function("runtimeScope", ' + json.dumps(rehydrate) + ')({});\n'
            + r"""
const document = window.document;
global.Element = window.Element;
const escapeText = text => {
  const node = document.createElement('span');
  node.textContent = text;
  return node.innerHTML;
};
const presentation = window.createInboxTestPresentation({
  translate:key => key, renderMarkdown:escapeText,
  iconHtml:() => '', resolveConversationTitle:() => '',
});
const renderers = feature.createClassicConversationRenderers({
  renderSafeMarkdownHtml:escapeText,
  renderInjectionBlockHtml(block) {
    // Match the production port: each selected receipt gets a fresh base,
    // not a bulk rehydrate of unrelated receipts that share a model round.
    const field = {inbox:'_inboxInjects', peer:'_peerInjects'}[block.channel];
    return rehydrate({[field]:block.items}, [])
      .map(row => presentation.renderInjectionHtml(row)).join('');
  },
});
const makeState = (segments, receipts, channel = 'inbox', revision = 1, status = 'completed') => {
  const turn = {turnId:'turn',conversationId:'conv',laneId:'main',ordinal:1,
    actor:'assistant',kind:'reply',status,currentAttemptId:status === 'running' ? 'attempt' : null,
    projectionRevision:revision,createdAt:1,updatedAt:revision,
    projection:{segments,[channel === 'inbox' ? '_inboxInjects' : '_peerInjects']:receipts}};
  return {conversationId:'conv',conversationRevision:revision,transport:'live',turnsById:{turn},
    laneOrder:{main:['turn']},attemptsById:{},queueItems:[],pendingEventsByTurn:{},
    commandPending:{},liveRoundUsageByTurn:{}};
};
const note = (blockId, text) => ({type:'system_note',blockId,noteKind:'context-injection',
  reason:'inbox_delivery',action:'prompt',text,llmRound:3,attemptId:'attempt',taskId:'producer'});
const receipt = previews => ({blockId:'receipt',round:4,count:previews.length,
  attemptId:'attempt',taskId:'producer',previews});
const renderSelected = state => {
  const turn = feature.selectConversationViewModel(state).mainLane.turns[0];
  const root = document.createElement('div');
  for (const block of turn.blocks) {
    const node = document.createElement('div');
    renderers.renderBlock(node, block, {turn});
    root.appendChild(node);
  }
  return root;
};
const noteTexts = root => Array.from(root.querySelectorAll(
  '.conversation-block--system-note .sw-card-raw-pre'), node => node.textContent);
"""
        )


@pytest.mark.parametrize('channel', ['inbox', 'peer'])
@pytest.mark.parametrize('preview_count', [16, 17])
def test_preview_cap_cannot_hide_the_only_copy_of_the_last_payload(
        conversation_bundle, inbox_render_harness, channel, preview_count):
    result = _run(conversation_bundle, inbox_render_harness
                  + f'const channel={json.dumps(channel)}, count={preview_count};\n' + r"""
const previews = Array.from({length:count}, (_, index) => ({text:`unique-payload-${index + 1}-end`}));
const payload = previews.map(item => item.text).join('\n\n');
const state = makeState([note('note', payload)], [receipt(previews)], channel);
const before = JSON.stringify(state);
const root = renderSelected(state);
const dedicated = root.querySelector('.conversation-block--injections');
console.log(JSON.stringify({
  notes:noteTexts(root),
  dedicatedRows:root.querySelectorAll('.conversation-block--injections').length,
  dedicatedPreviews:dedicated.querySelectorAll('.sw-card').length,
  dedicatedHasSixteenth:dedicated.textContent.includes('unique-payload-16-end'),
  dedicatedHasLast:dedicated.textContent.includes(previews.at(-1).text),
  hasElision:!!dedicated.querySelector('.ptool-result-trunc'),
  lastPayloadSurvives:root.textContent.includes(previews.at(-1).text),
  reloadEqual:root.innerHTML === renderSelected(JSON.parse(before)).innerHTML,
  unchanged:before === JSON.stringify(state),
}));
""")
    payload = '\n\n'.join(f'unique-payload-{index}-end' for index in range(1, preview_count + 1))
    assert result == {
        'notes': [] if preview_count == 16 else [payload],
        'dedicatedRows': 1,
        'dedicatedPreviews': 16,
        'dedicatedHasSixteenth': True,
        'dedicatedHasLast': preview_count == 16,
        'hasElision': preview_count == 17,
        'lastPayloadSurvives': True,
        'reloadEqual': True,
        'unchanged': True,
    }


@pytest.mark.parametrize('channel,body_size,structured,hidden', [
    ('inbox', 12001, False, True),
    ('peer', 12001, False, True),
    ('inbox', 16385, False, False),
    ('peer', 16385, False, False),
    ('inbox', 12001, True, False),
])
def test_long_or_structured_preview_requires_complete_display_coverage(
        conversation_bundle, inbox_render_harness, channel, body_size, structured, hidden):
    config = json.dumps({'channel': channel, 'size': body_size, 'structured': structured})
    result = _run(conversation_bundle, inbox_render_harness + 'const config=' + config + ';\n' + r"""
const tail = 'only-copy-at-payload-tail';
const body = 'x'.repeat(config.size - tail.length) + tail;
// Structured cards show selected fields, not this entire serialized payload.
const payload = config.structured
  ? `<swarm-update><agent-id>worker</agent-id><result>${body}</result><extra>unrendered-field</extra></swarm-update>`
  : body;
const state = makeState([note('note', payload)], [receipt([{text:payload}])], config.channel);
const before = JSON.stringify(state);
const root = renderSelected(state);
const dedicated = root.querySelector('.conversation-block--injections');
const notes = noteTexts(root);
console.log(JSON.stringify({
  noteCount:notes.length,
  noteIsVerbatim:notes.length === 1 && notes[0] === payload,
  dedicatedHasTail:dedicated.textContent.includes(tail),
  dedicatedHasUnrenderedField:dedicated.textContent.includes('unrendered-field'),
  hasElision:!!dedicated.querySelector('.ptool-result-trunc'),
  tailSurvives:root.textContent.includes(tail),
  structuredFieldSurvives:root.textContent.includes('unrendered-field'),
  reloadEqual:root.innerHTML === renderSelected(JSON.parse(before)).innerHTML,
  unchanged:before === JSON.stringify(state),
}));
""")
    assert result == {
        'noteCount': 0 if hidden else 1,
        'noteIsVerbatim': not hidden,
        'dedicatedHasTail': body_size == 12001,
        'dedicatedHasUnrenderedField': False,
        'hasElision': body_size == 16385,
        'tailSurvives': True,
        'structuredFieldSurvives': structured,
        'reloadEqual': True,
        'unchanged': True,
    }


@pytest.mark.parametrize('channel', ['inbox', 'peer'])
def test_running_same_round_receipt_replacement_and_removal_refresh_mounted_surface(
        conversation_bundle, inbox_render_harness, channel):
    result = _run(conversation_bundle, inbox_render_harness
                  + 'const channel=' + json.dumps(channel) + ';\n' + r"""
const originalNote = note('note', 'original completion');
const agentNote = note('agent-note', 'independent agent message');
const originalReceipt = receipt([{text:'original completion'}]);
const replacementReceipt = receipt([{text:'replacement completion'}]);
const siblingReceipt = {...receipt([{text:'same-round sibling'}]), blockId:'sibling-receipt'};
const states = [
  makeState([originalNote], [originalReceipt, siblingReceipt], channel, 1, 'running'),
  makeState([originalNote, agentNote], [originalReceipt, siblingReceipt], channel, 2, 'running'),
  makeState([originalNote, agentNote], [replacementReceipt, siblingReceipt], channel, 3, 'running'),
  makeState([originalNote, agentNote], [siblingReceipt], channel, 4, 'running'),
];
const before = JSON.stringify(states);
const container = document.createElement('main');
document.body.appendChild(container);
const surface = feature.createConversationSurface(container, renderers);
const render = state => surface.render(feature.selectConversationViewModel(state));
const read = () => ({notes:noteTexts(container),
  receipts:Array.from(container.querySelectorAll(
    '.conversation-block--injections .sw-card-raw-pre, '
      + '.conversation-block--injections .sw-card-preview'), node => node.textContent)});
render(states[0]);
const initial = read();
const turnNode = container.querySelector('[data-turn-id="turn"]');
const receiptNode = container.querySelector('[data-block-id="receipt"]');
const siblingNode = container.querySelector('[data-block-id="sibling-receipt"]');
receiptNode.querySelector('details').open = true;
render(states[1]);
const appended = read();
render(states[2]);
const replaced = read();
const sameReceiptNode = receiptNode === container.querySelector('[data-block-id="receipt"]');
const stayedOpen = receiptNode.querySelector('details').open;
render(states[3]);
const removed = read();
const result = {initial,appended,replaced,removed,sameReceiptNode,stayedOpen,
  sameTurnNode:turnNode === container.querySelector('[data-turn-id="turn"]'),
  sameSiblingNode:siblingNode === container.querySelector('[data-block-id="sibling-receipt"]'),
  receiptRemoved:!container.querySelector('[data-block-id="receipt"]') && !receiptNode.isConnected,
  unchanged:before === JSON.stringify(states)};
surface.dispose();
harnessDom.window.close();
console.log(JSON.stringify(result));
""")
    assert result == {
        'initial': {'notes': [], 'receipts': ['original completion', 'same-round sibling']},
        'appended': {
            'notes': ['independent agent message'],
            'receipts': ['original completion', 'same-round sibling'],
        },
        'replaced': {
            'notes': ['original completion', 'independent agent message'],
            'receipts': ['replacement completion', 'same-round sibling'],
        },
        'removed': {
            'notes': ['original completion', 'independent agent message'],
            'receipts': ['same-round sibling'],
        },
        'sameReceiptNode': True,
        'stayedOpen': True,
        'sameTurnNode': True,
        'sameSiblingNode': True,
        'receiptRemoved': True,
        'unchanged': True,
    }
