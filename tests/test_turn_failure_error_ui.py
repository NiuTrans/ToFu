"""Terminal Turns retain and present their typed error envelope.

The turn authority stores rich errors under ``settlement.error``. The typed
selector and finish presentation expose that same envelope without projecting
a compatibility message document.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import native_module_path, runtime_section


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
BUNDLER = ROOT / 'scripts' / 'vite_test_bundle.mjs'


@pytest.mark.skipif(not shutil.which('node') or not BUNDLER.is_file(),
                    reason='node + vite test bundler not installed')
def test_failed_turn_projection_and_presentation_keep_typed_error(tmp_path):
    entry = tmp_path / 'turn-failure-entry.ts'
    view_model = ROOT / 'frontend/src/conversation/presentation/conversation-view-model.ts'
    presentation = ROOT / 'frontend/src/core/turn-presentation.ts'
    turn_state = ROOT / 'frontend/src/core/turn-state.ts'
    errors = ROOT / 'frontend/src/api/errors.ts'
    entry.write_text(
        f"export {{ selectConversationViewModel }} from {json.dumps(str(view_model))};\n"
        f"export {{ presentTurnFinish }} from {json.dumps(str(presentation))};\n"
        f"export {{ createTurnState, reduceTurnState }} from {json.dumps(str(turn_state))};\n"
        f"export {{ errorEnvelopeFingerprint, isErrorEnvelope, normalizeErrorEnvelope }} from {json.dumps(str(errors))};\n",
        encoding='utf-8',
    )
    built = tmp_path / 'turn-failure.cjs'
    compile_result = subprocess.run(
        [str(BUNDLER), str(entry), '--bundle', '--format=cjs',
         '--platform=node', f'--outfile={built}'],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert compile_result.returncode == 0, compile_result.stderr

    script = f"""
const {{ selectConversationViewModel, presentTurnFinish, createTurnState, reduceTurnState, errorEnvelopeFingerprint, isErrorEnvelope, normalizeErrorEnvelope }} = require({json.dumps(str(built))});
const error = {{
  kind:'tool_loop', severity:'warning', retryable:true,
  message:'Model stuck in a repeated tool-call loop',
  detail:'Repeated edit_file returned anchor not found',
  hint:'Continue from the checkpoint', model:'gpt-5.6-sol',
  context:'tool-loop', source:'orchestrator', raw:'repeats=4',
}};
const failed = {{
  turnId:'turn-failed', conversationId:'conv-a', laneId:'main', ordinal:1,
  actor:'assistant', kind:'reply', status:'failed', currentAttemptId:null,
  projectionRevision:9, projection:{{content:'partial', segments:[{{
    type:'text', blockId:'text:terminal', text:'partial', terminal:true,
  }}]}},
  settlement:{{outcome:'failed', cause:'generation_error', error}},
  createdAt:1, updatedAt:1,
}};
const finish = presentTurnFinish(failed);
if (finish.detail !== error.message) throw Error('detail=' + finish.detail);
if (finish.detail === 'generation_error') throw Error('internal cause leaked');
if (finish.errorKind !== 'tool_loop') throw Error('kind=' + finish.errorKind);
if (finish.tone !== 'warning') throw Error('tone=' + finish.tone);
if (finish.retryable !== true) throw Error('retryability lost');
const changed = {{...error, kind:'timeout', message:'Different same-length failure text'}};
if (errorEnvelopeFingerprint(error) === errorEnvelopeFingerprint(changed))
  throw Error('structured failure identity was collapsed');
const permissionWithStatus = {{...error, kind:'permission', statusCode:403,
  message:'Request rejected (HTTP 403)'}};
const permissionWithoutStatus = {{...permissionWithStatus}};
delete permissionWithoutStatus.statusCode;
if (errorEnvelopeFingerprint(permissionWithStatus)
    === errorEnvelopeFingerprint(permissionWithoutStatus))
  throw Error('HTTP status was omitted from error presentation identity');
if (errorEnvelopeFingerprint('alpha') === errorEnvelopeFingerprint('bravo'))
  throw Error('equal-length legacy failures were collapsed');
const partial = {{kind:'task_start_failed', message:'executor did not start'}};
if (isErrorEnvelope(partial)) throw Error('partial envelope passed full-shape guard');
const completed = normalizeErrorEnvelope(partial);
if (!isErrorEnvelope(completed)) throw Error('partial envelope was not completed');
if (completed.kind !== 'task_start_failed' || completed.message !== partial.message)
  throw Error('partial envelope identity was not preserved');
const longLegacy = 'legacy-' + 'x'.repeat(420) + '-legacy-tail';
if (normalizeErrorEnvelope(longLegacy).detail !== longLegacy)
  throw Error('frontend normalizer truncated a legacy error');

const stateFor = turn => ({{conversationId:'conv-a',conversationRevision:9,
  transport:'live',turnsById:{{[turn.turnId]:turn}},laneOrder:{{main:[turn.turnId]}},
  attemptsById:{{}},queueItems:[],pendingEventsByTurn:{{}},commandPending:{{}},
  liveRoundUsageByTurn:{{}}}});
const failedVm = selectConversationViewModel(stateFor(failed)).mainLane.turns[0];
if (failedVm.finish.errorKind !== 'tool_loop') throw Error('view model lost error');
if (failedVm.blocks[0].markdown !== 'partial') throw Error('partial output lost');
const terminalErrorBlock = failedVm.blocks.find(block =>
  block.kind === 'activity-event' && block.terminalError);
if (!terminalErrorBlock) throw Error('failed turn without timeline hid its error');
if (terminalErrorBlock.terminalError.hint !== error.hint)
  throw Error('synthetic terminal disclosure did not keep the complete envelope');
const permissionTurn = {{...failed, turnId:'turn-permission',
  settlement:{{outcome:'failed', cause:'generation_error',
    error:permissionWithStatus}}}};
const permissionVm = selectConversationViewModel(
  stateFor(permissionTurn)).mainLane.turns[0];
const permissionBlock = permissionVm.blocks.find(block =>
  block.kind === 'activity-event' && block.terminalError);
if (permissionBlock?.value?.statusCode !== 403)
  throw Error('terminal permission error did not expose HTTP 403 as a fact');
const permissionTimelineTurn = {{...permissionTurn,
  turnId:'turn-permission-timeline', projection:{{
    ...permissionTurn.projection, activityTimeline:{{
      blockId:'activity-timeline', version:1, entries:[{{
        id:'terminal-existing', spanId:'terminal-existing', seq:1,
        occurredAt:1, kind:'error', status:'failed', severity:'error', count:1,
        summary:'Request rejected', reasonCode:'permission',
      }}],
    }},
  }},
}};
const permissionTimelineVm = selectConversationViewModel(
  stateFor(permissionTimelineTurn)).mainLane.turns[0];
const permissionTimelineBlock = permissionTimelineVm.blocks.find(block =>
  block.kind === 'activity-event' && block.terminalError);
if (permissionTimelineBlock?.value?.statusCode !== 403)
  throw Error('existing terminal activity row lost settlement HTTP 403');
const recovered = {{
  ...failed, status:'completed', settlement:{{outcome:'completed'}},
  projection:{{content:'done', error, segments:[{{type:'text',
    blockId:'text:terminal',text:'done',terminal:true}}]}},
}};
const recoveredVm = selectConversationViewModel(stateFor(recovered)).mainLane.turns[0];
if (recoveredVm.finish && recoveredVm.finish.errorKind)
  throw Error('stale failure resurrected');
if (recoveredVm.blocks[0].markdown !== 'done') throw Error('recovery output lost');
if (recoveredVm.blocks.some(block => block.terminalError))
  throw Error('successful retry kept the terminal error disclosure');

let fallbackState = reduceTurnState(createTurnState('conv-fallback'), {{
  type:'snapshot', snapshot:{{conversationRevision:1, turns:[{{
    turnId:'turn-fallback', conversationId:'conv-fallback', laneId:'main',
    ordinal:1, actor:'assistant', kind:'reply', status:'running',
    currentAttemptId:'attempt-fallback', projectionRevision:1,
    projection:{{content:''}}, createdAt:1, updatedAt:1,
  }}], attempts:[{{attemptId:'attempt-fallback', turnId:'turn-fallback',
    status:'running'}}]}},
}});
const fallbackProjection = {{
  content:'', fallbackFrom:'kimi-k3', fallbackModel:'glm-5.3',
  fallbackReason:'credential delivery anomaly', fallbackKind:'gateway',
  activityTimeline:{{blockId:'activity-timeline', version:1, entries:[{{
    id:'fallback-1', spanId:'fallback-1', seq:1, occurredAt:2,
    kind:'model', status:'switched', severity:'warning', count:1,
    summary:'Model switched: kimi-k3 → glm-5.3',
    fromModel:'kimi-k3', toModel:'glm-5.3', action:'fallback',
  }}]}},
}};
fallbackState = reduceTurnState(fallbackState, {{type:'event', event:{{
  conversationId:'conv-fallback', turnId:'turn-fallback',
  attemptId:'attempt-fallback', seq:1, projectionRevision:2,
  type:'projection_updated', payload:{{
    projection:fallbackProjection, phase:{{status:'waiting_model'}},
  }},
}}}});
const switchedVm = selectConversationViewModel(fallbackState).mainLane.turns[0];
if (switchedVm.status !== 'running') throw Error('fallback prematurely settled');
if (!switchedVm.metadata.fallbackInTimeline
    || switchedVm.metadata.fallback?.model !== 'glm-5.3')
  throw Error('fallback fact was not rendered while live');

const fallbackError = {{
  kind:'rate_limit', severity:'error', retryable:true,
  message:'Fallback model remained rate limited after 3 attempts',
  detail:'HTTP 429', hint:'Retry later', model:'glm-5.3',
  context:'fallback', source:'orchestrator', raw:'HTTP 429',
}};
fallbackState = reduceTurnState(fallbackState, {{type:'event', event:{{
  conversationId:'conv-fallback', turnId:'turn-fallback',
  attemptId:'attempt-fallback', seq:2, projectionRevision:3,
  type:'terminal_settlement', payload:{{
    status:'failed', phase:null, projection:fallbackProjection,
    settlement:{{outcome:'failed', cause:'generation_error', error:fallbackError}},
  }},
}}}});
const settledTurn = fallbackState.turnsById['turn-fallback'];
if (settledTurn.status !== 'failed') throw Error('fallback failure stayed live');
if (fallbackState.livePhase !== null) throw Error('terminal phase stayed active');
if (Object.values(fallbackState.turnsById).some(turn =>
    turn && (turn.status === 'pending' || turn.status === 'running')))
  throw Error('fallback failure kept the conversation busy');
const settledVm = selectConversationViewModel(fallbackState).mainLane.turns[0];
if (settledVm.blocks.some(block => block.kind === 'live-status'))
  throw Error('fallback failure kept the spinner surface');
if (!settledVm.metadata.fallbackInTimeline)
  throw Error('terminal error erased the fallback fact');
const fallbackTerminalError = settledVm.blocks.find(block =>
  block.kind === 'activity-event' && block.terminalError)?.terminalError;
if (fallbackTerminalError?.message !== fallbackError.message)
  throw Error('fallback terminal error was not rendered');
if (settledVm.finish?.label !== 'Failed'
    || settledVm.finish?.detail !== fallbackError.message)
  throw Error('fallback terminal finish was not rendered');
"""
    run = subprocess.run(
        [shutil.which('node'), '-e', script], cwd=ROOT,
        capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_failed_finish_chip_and_visible_card_use_real_error_kind(tmp_path):
    typed_error_owner = native_module_path(
        'typed-error-envelope-for-finish-ui.js',
        ROOT / 'frontend/src/api/errors.ts',
    )
    error_presentation_owner = native_module_path(
        'typed-error-presentation-for-finish-ui.js',
        ROOT / 'frontend/src/error-presentation.ts',
    )
    finish_owner = runtime_section('ui/finish_info.js')
    fn_start = finish_owner.index('function renderFinishInfo(msg, turnId) {')
    fn_end_marker = (
        '  if (parts.length === 0) return "";\n'
        '  return `<div class="message-finish">${parts.join("")}</div>`;\n'
        '}'
    )
    fn_end = finish_owner.index(fn_end_marker, fn_start) + len(fn_end_marker)
    source = finish_owner[fn_start:fn_end]
    source_path = tmp_path / 'failure-render.js'
    source_path.write_text(source, encoding='utf-8')

    harness = tmp_path / 'failure-render-harness.js'
    harness.write_text(r"""
const fs = require('fs');
global.window = global;
global.runtimeScope = global;
global.escapeHtml = (value) => String(value ?? '')
  .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
const copy = {
  'err.k.tool_loop.chip':'重复调用循环',
  'err.k.tool_loop.title':'模型陷入重复工具调用循环，已主动停止',
  'err.k.tool_loop.hint':'请从最近检查点继续',
  'err.k._howToFix':'解决办法：',
  'err.k._modelSuffix':'（模型：{model}）',
  'finishInfo.reasonFailed':'生成失败',
  'finishInfo.reasonFailedTip':'服务端没有记录具体错误说明',
};
global.t = (key, params) => {
  let value = Object.hasOwn(copy, key) ? copy[key] : key;
  for (const [name, replacement] of Object.entries(params || {}))
    value = value.replaceAll('{' + name + '}', String(replacement));
  return value;
};
global.Icon = () => '';
global._detectBrand = () => 'generic';
global._brandSvg = () => '';
global._isThinkingCapable = () => false;
global._providerDisplayName = (value) => String(value || '');
global.calcCostCny = () => ({ costCny:0 });
global._subscriptionQuotaForMessage = () => null;
global.ConversationTurnStore = {
  finishPresentation: (turn) => turn.settlement?.error ? ({
      tone:'warning', label:'Failed',
      detail:'Model stuck in a repeated tool-call loop',
    }) : ({tone:'error', label:'Failed', detail:''}),
};
/* renderFinishInfo also renders swarm wait blocks (added after this
 * extraction was written); the failure assertions here do not involve
 * waitingOn, so the helper is stubbed outside the extracted span. */
global.renderWaitingOnBlock = () => '';

eval(fs.readFileSync(process.argv[2], 'utf8'));
global.isTypedErrorEnvelope = global.isErrorEnvelope;
global.normalizeTypedErrorEnvelope = global.normalizeErrorEnvelope;
eval(fs.readFileSync(process.argv[3], 'utf8'));
Object.assign(global, createErrorEnvelopePresentation({
  translate: global.t,
  iconHtml: global.Icon,
}));
eval(fs.readFileSync(process.argv[4], 'utf8'));
const error = {
  kind:'tool_loop', severity:'warning', retryable:true,
  message:'legacy bilingual fallback', detail:'Repeated edit_file: anchor missing',
  hint:'legacy hint', model:'gpt-5.6-sol', context:'tool-loop',
  source:'orchestrator', raw:'repeats=4',
  titleKey:'err.k.tool_loop.title', hintKey:'err.k.tool_loop.hint',
};
const card = renderErrorEnvelope(error);
const chip = renderFinishInfo({
  _turnStatus:'failed', _turnSettlement:{
    cause:'generation_error', error,
  }, error,
}, false);
const fallbackChip = renderFinishInfo({
  _turnStatus:'failed', _turnSettlement:{cause:'generation_error'},
}, false);
console.log(JSON.stringify({card, chip, fallbackChip}));
""", encoding='utf-8')
    run = subprocess.run(
        [shutil.which('node'), str(harness), typed_error_owner,
         error_presentation_owner, str(source_path)],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
    rendered = json.loads(run.stdout.strip().splitlines()[-1])
    assert 'data-error-kind="tool_loop"' in rendered['card']
    assert '模型陷入重复工具调用循环，已主动停止' in rendered['card']
    assert '请从最近检查点继续' in rendered['card']
    assert 'Repeated edit_file: anchor missing' in rendered['card']
    assert 'terminal-failure' in rendered['chip']
    assert '重复调用循环' in rendered['chip']
    assert 'generation_error' not in rendered['chip']
    assert '生成失败' in rendered['fallbackChip']
    assert '服务端没有记录具体错误说明' in rendered['fallbackChip']
    assert 'generation_error' not in rendered['fallbackChip']


def test_terminal_failure_styles_keep_complete_details_user_collapsible():
    styles = (ROOT / 'static/styles.css').read_text(encoding='utf-8')
    assert '.finish-tag.terminal-failure{' in styles
    terminal_rule = styles.split('.finish-tag.terminal-failure{', 1)[1].split('}', 1)[0]
    assert 'max-width:100%' in terminal_rule
    assert 'white-space:normal' in terminal_rule
    assert 'text-overflow' not in terminal_rule
    assert ('.conversation-surface .conversation-turn-footer '
            '.finish-tag.terminal-failure {') in styles
    assert '.message .error-block{margin-top:10px;text-align:left;user-select:text}' in styles
    assert '.activity-event__terminal-envelope {' in styles


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_finish_bar_shows_task_completion_timing(tmp_path):
    """Terminal assistant Turns render settle clock time + wall-clock duration.

    The finish bar owns the tag; the turn store passes the authoritative
    TurnRecord lifecycle (createdAt/updatedAt, epoch ms) through as
    ``_turnCreatedAt``/``_turnUpdatedAt``.
    """
    finish_owner = runtime_section('ui/finish_info.js')
    fn_start = finish_owner.index('function _formatTurnDuration(ms) {')
    fn_end_marker = (
        '  if (parts.length === 0) return "";\n'
        '  return `<div class="message-finish">${parts.join("")}</div>`;\n'
        '}'
    )
    fn_end = finish_owner.index(fn_end_marker, fn_start) + len(fn_end_marker)
    source_path = tmp_path / 'finish-timing.js'
    source_path.write_text(finish_owner[fn_start:fn_end], encoding='utf-8')

    harness = tmp_path / 'finish-timing-harness.js'
    harness.write_text(r"""
const fs = require('fs');
global.window = global;
global.runtimeScope = global;
global.escapeHtml = (value) => String(value ?? '')
  .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
const copy = {
  'finishInfo.timingTip': '完成于 {time} · 用时 {duration}',
  'finishInfo.timingTipNoDuration': '完成于 {time}',
};
global.t = (key, params) => {
  let value = Object.hasOwn(copy, key) ? copy[key] : key;
  for (const [name, replacement] of Object.entries(params || {}))
    value = value.replaceAll('{' + name + '}', String(replacement));
  return value;
};
global.Icon = () => '';
global._detectBrand = () => 'generic';
global._brandSvg = () => '';
global._isThinkingCapable = () => false;
global._providerDisplayName = (value) => String(value || '');
global.calcCostCny = () => ({ costCny: 0 });
global._subscriptionQuotaForMessage = () => null;
global.ConversationTurnStore = { finishPresentation: () => null };

eval(fs.readFileSync(process.argv[2], 'utf8'));
const base = {
  _turnStatus: 'completed', _turnSettlement: {}, model: 'kimi-k3',
  usage: { prompt_tokens: 10, completion_tokens: 5 },
};
const timed = renderFinishInfo({
  ...base, _turnCreatedAt: 1000, _turnUpdatedAt: 1000 + 65000,
}, false);
const subSecond = renderFinishInfo({
  ...base, _turnCreatedAt: 1000, _turnUpdatedAt: 1000 + 8400,
}, false);
const hours = renderFinishInfo({
  ...base, _turnCreatedAt: 1000, _turnUpdatedAt: 1000 + 75 * 60000,
}, false);
const noDuration = renderFinishInfo({ ...base, _turnUpdatedAt: 66000 }, false);
const noClock = renderFinishInfo({ ...base }, false);
console.log(JSON.stringify({ timed, subSecond, hours, noDuration, noClock }));
""", encoding='utf-8')
    run = subprocess.run(
        [shutil.which('node'), str(harness), str(source_path)],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
    rendered = json.loads(run.stdout.strip().splitlines()[-1])
    assert 'finish-tag timing' in rendered['timed']
    assert '1m05s' in rendered['timed']
    assert '用时 1m05s' in rendered['timed']
    assert '8.4s' in rendered['subSecond']
    assert '1h15m' in rendered['hours']
    assert 'finish-tag timing' in rendered['noDuration']
    assert '1m05s' not in rendered['noDuration']
    assert '完成于' in rendered['noDuration']
    assert 'finish-tag timing' not in rendered['noClock']
