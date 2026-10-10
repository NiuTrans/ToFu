"""Executable completion join: durable evidence, identity reuse and no activity guessing."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.unit


def test_completion_join_and_lifecycle_wakes():
    script = r"""
const assert = require('node:assert/strict');
(async () => {
 const { createServer } = await import('vite');
 const server = await createServer({root: process.cwd(), configFile: false, optimizeDeps:{noDiscovery:true,include:[]}, server: {middlewareMode:true,watch:null}, appType:'custom'});
 try {
  const {createTurnState} = await server.ssrLoadModule('/frontend/src/core/turn-state.ts');
  const {selectConversationViewModel} = await server.ssrLoadModule('/frontend/src/conversation/presentation/conversation-view-model.ts');
  const {createBackgroundCommandCompletionProjection,createBackgroundActivityEvidenceObserver} = await server.ssrLoadModule('/frontend/src/conversation/presentation/background-command-completion.ts');
  const {createToolCommandExecutionPresentation} = await server.ssrLoadModule('/frontend/src/conversation/presentation/tool-command-execution-presentation.ts');
  const join = createBackgroundCommandCompletionProjection();
  const changed = createBackgroundActivityEvidenceObserver();
  const completion = {commandId:'bg_one',status:'completed',command:'echo test',resultPreview:'final result',resultTruncated:false,exitCode:0};
  const round = {toolName:'run_command',round:1,status:'done',callId:'reused',content:'historical handoff',results:[{backgrounded:true,exitCode:'background',backgroundCommandId:'bg_one'}]};
  const turn = {turnId:'origin',conversationId:'c',role:'assistant',laneId:'main',ordinal:1,status:'completed',projectionRevision:1,projection:{toolRounds:[round],segments:[{type:'tool_use',id:'reused',name:'run_command',input:{command:'echo test'}}]}};
  const unrelated = {...turn,turnId:'other',ordinal:2,projection:{content:'unchanged'}};
  const state = {...createTurnState('c'),turnsById:{origin:turn,other:unrelated},laneOrder:{main:['origin','other']}};
  join.update(state); assert.equal(join.project(turn),turn); assert.equal(changed(state),true); assert.equal(changed(state),false);
  const before = selectConversationViewModel(state);
  const pending = {...state,queueItems:[{queueId:'q',kind:'background_receipt',text:'done',position:0,backgroundCommand:completion}]};
  join.update(pending); const done = join.project(turn);
  assert.notEqual(done,turn); assert.equal(join.project(unrelated),unrelated);
  assert.equal(done.projection.toolRounds[0].results[0].exitCode,0);
  assert.equal(round.results[0].exitCode,'background');
  assert.equal(done.projection.toolRounds[0].results[0].output,'final result');
  assert.equal(join.project(turn),done); assert.equal(changed(pending),true); assert.equal(changed({...pending,livePhase:{seq:2}}),false);
  const after = selectConversationViewModel(pending);
  assert.equal(before.mainLane.turns[1],after.mainLane.turns[1]);
  assert.notEqual(before.mainLane.turns[0],after.mainLane.turns[0]);
  const otherRound = {...round,results:[{...round.results[0],backgroundCommandId:'bg_two'}]};
  const embedded = {...turn,projection:{toolRounds:[round,otherRound],segments:[
   {...turn.projection.segments[0],_round:round},
   {...turn.projection.segments[0],_round:otherRound}
  ]}};
  const embeddedState = {...pending,turnsById:{origin:embedded},laneOrder:{main:['origin']}};
  const blocks = selectConversationViewModel(embeddedState).mainLane.turns[0].blocks.filter(b=>b.kind==='tool');
  assert.equal(blocks[0].round.results[0].exitCode,0,'embedded round converges');
  assert.equal(blocks[1].round.results[0].exitCode,'background','same call ID does not conflate commands');
  assert.equal(embedded.projection.segments[0]._round,round,'immutable source segment');
  const receipt = {...unrelated,turnId:'receipt',projection:{_bgCommandInjects:[{blockId:'ack',previews:[{commandId:'bg_one',backgroundCommand:completion}]}]}};
  join.update({...state,turnsById:{...state.turnsById,receipt}});
  assert.equal(join.project(turn),done,'queue→receipt leaves same command projection');
  const renderer = createToolCommandExecutionPresentation({translate:(key)=>key});
  const render = (round) => renderer.renderSettledCommandHtml(round,round.results[0],{iconHtml:'',rootPillHtml:'',timerHtml:'',interruptHtml:'',rightControlsHtml:''},{bodyExpanded:true,outputExpanded:true});
  const html = render(done.projection.toolRounds[0]);
  assert.ok(html.includes('ptool-cmd-ok')); assert.ok(html.includes('final result')); assert.ok(!html.includes('bgCommand.moved'));
  join.update({...pending,queueItems:[{...pending.queueItems[0],backgroundCommand:{...completion,exitCode:7}}]});
  assert.ok(render(join.project(turn).projection.toolRounds[0]).includes('ptool-cmd-err'));
  const old = {...completion}; delete old.exitCode;
  join.update({...pending,queueItems:[{...pending.queueItems[0],backgroundCommand:old}]});
  const neutral = render(join.project(turn).projection.toolRounds[0]);
  assert.ok(neutral.includes('toolCmd.finished')); assert.ok(!neutral.includes('ptool-cmd-err'));
  join.update({...state,conversationId:'different',turnsById:{receipt}}); assert.equal(join.project(turn),turn,'no cross-conversation completion');
  console.log('PASS completion convergence, immutable receipts, stable unrelated turns');
 } finally { await server.close(); }
})().catch(e=>{console.error(e);process.exit(1)});
"""
    result = subprocess.run(['node', '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_handoff_normalization_is_conservative():
    from lib.background_command_projection import normalize_handoff_round
    meta = {'backgrounded': True, 'exitCode': 'background'}
    record = {'toolName': 'run_command', 'results': [meta], 'content': '$ sleep 1\n\n[Command moved to background: bg_0123456789ab. The command is still running; wait.]'}
    normalized = normalize_handoff_round(record)
    assert normalized['results'][0]['backgroundCommandId'] == 'bg_0123456789ab'
    assert 'backgroundCommandId' not in meta
    assert normalize_handoff_round({**record, 'toolName': 'read_files'})['results'] == [meta]
    assert normalize_handoff_round({**record, 'content': 'untrusted bg_0123456789ab'})['results'] == [meta]

    from lib.turn_projection_patch import normalize_projection_document
    projection = normalize_projection_document({
        'toolRounds': [record], 'segments': [{'type': 'tool_use', '_round': record}],
    })
    assert projection['segments'][0]['_round']['results'][0]['backgroundCommandId'] == 'bg_0123456789ab'
    assert projection['toolRounds'][0]['results'][0]['backgroundCommandId'] == 'bg_0123456789ab'
    assert 'backgroundCommandId' not in record['results'][0]

def test_public_completion_preserves_only_structured_outcome():
    from lib.background_command_projection import background_command_completion
    payload = {'_backgroundCommand': 'bg_one', '_backgroundCommandCompletion': {'commandId':'bg_one','exitCode':7,'timedOut':False,'interrupted':True}}
    result = background_command_completion(payload)
    assert result['exitCode'] == 7 and result['interrupted'] is True
    payload['_backgroundCommandCompletion']['exitCode'] = True
    assert 'exitCode' not in background_command_completion(payload)


def test_terminal_status_does_not_interpret_stdout_markers():
    from lib.background_command_projection import command_result_status
    assert command_result_status('$ echo x\n[Command timed out]\nmore output\n[exit code: 0]') == {
        'exitCode': 0, 'timedOut': False, 'interrupted': False}
    assert command_result_status('$ sleep 1\n[Command timed out]\n[exit code: -9]')['timedOut']
    assert command_result_status('$ sleep 1\n[Command interrupted by user]\n[exit code: -15]')['interrupted']
    assert command_result_status('[Command timed out]') == {}
