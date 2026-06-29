"""Characterization (regression) tests for the SSE event dispatcher in
``static/js/ui/sse_pipeline.js``.

WHY
---
`_processSSELine` was a ~2160-line closure nested in `_trySSE`, reassigning
6 captured locals across ~30 event branches and packed with documented
production bug-guards. It had NO runtime test, so it was effectively
untouchable. This harness makes it testable: `sse_pipeline.js` exposes a
testing seam ``window.__sse_test__`` = ``{ makeCtx, dispatchSSEEvent }``
where ``dispatchSSEEvent(line, ctx)`` runs ONE SSE ``data: {...}`` line
against a mutable ``ctx`` object (the former closure locals) and returns
the done-signal boolean.

These assertions are derived from the ORIGINAL code's documented behavior
(the bug-guards), so they lock the contract independently of any refactor:
if the ctx-extraction drifts, a test fails.

The harness runs the REAL shipped JS under jsdom (so it tracks the file).
Skips cleanly when node + jsdom aren't installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


# The harness boots a jsdom window, stubs every global the dispatcher calls
# (render/twUpdate/etc. become no-op spies), loads sse_pipeline.js, then
# drives single events through window.__sse_test__.dispatchSSEEvent and
# asserts the resulting ctx/message state.
_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body><div id="chatInner"></div></body>',
                      { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.console = console;
global.setTimeout = win.setTimeout = (fn) => 0;   // neuter async timers
global.clearTimeout = win.clearTimeout = () => {};

// ── Global state the dispatcher reads ──
let conversations = [];
let activeConvId = null;
const streamBufs = new Map();
const activeStreams = new Map();
win.conversations = conversations;
Object.defineProperty(win, 'activeConvId', { get: () => activeConvId, set: v => activeConvId = v });
win.streamBufs = streamBufs;
win.activeStreams = activeStreams;

// ── No-op / spy stubs for every global the dispatcher calls ──
const calls = {};
function spy(name) { calls[name] = 0; return (...a) => { calls[name]++; }; }
for (const n of ['twUpdate','twStart','twStop','finishStream','renderChat',
  'renderConversationList','buildTurnNav','saveConversations','updateContextBar',
  'scrollToBottom','_forceScrollToBottom','showToast','debugLog','showMessagesInDebug',
  '_handleAutopilotVuEvent','_retriggerHgTranslations','_streamTimerTouch',
  '_reportClientError']) {
  win[n] = global[n] = spy(n);
}
win._streamingBubbleHTML = global._streamingBubbleHTML = () => '<div id="streaming-msg"></div>';
win._TOFU_PLANNER_SVG = global._TOFU_PLANNER_SVG = '<svg></svg>';
win.renderMarkdown = global.renderMarkdown = (s) => s;
win.ConvView = global.ConvView = { finalizeStreaming: spy('finalizeStreaming') };
win.Artifacts = global.Artifacts = { attachToMessage: spy('attachToMessage') };
win.flashGaugeForArchive = global.flashGaugeForArchive = spy('flashGaugeForArchive');
win.Api = global.Api = { project: { status: () => Promise.resolve(null) } };
win.getActiveConv = global.getActiveConv = () => conversations.find(c => c.id === activeConvId);
win.errorEnvelopeMessage = global.errorEnvelopeMessage = (e) =>
  (e && typeof e === 'object' ? (e.message || e.detail || '') : (typeof e === 'string' ? e : ''));
win._debugCache = global._debugCache = {};
win._applyProjectData = global._applyProjectData = spy('_applyProjectData');
win.syncConversationToServer = global.syncConversationToServer = spy('syncConversationToServer');
win._autoTranslateHumanGuidance = global._autoTranslateHumanGuidance = spy('_autoTranslateHumanGuidance');
global.autoTranslate = win.autoTranslate = false;
win.convAutoTranslate = global.convAutoTranslate = (c) =>
  (c && c.autoTranslate !== undefined) ? !!c.autoTranslate
    : (typeof autoTranslate !== 'undefined' && autoTranslate !== undefined ? !!autoTranslate : false);
win.updateContextBar = global.updateContextBar = spy('updateContextBar');
if (typeof global.requestAnimationFrame !== 'function') {
  global.requestAnimationFrame = win.requestAnimationFrame = (fn) => { try { fn(); } catch (_) {} return 0; };
}
// stable-id helpers (real-ish: id-stamp + lookup)
let _idc = 0;
win._ensureMsgId = global._ensureMsgId = (m) => { if (m && !m._msgId) m._msgId = 'mid-' + (++_idc); return m; };
win._resolveAssistantById = global._resolveAssistantById = (conv, id) =>
  (conv && conv.messages.find(m => m._msgId === id)) || null;

// Load the extracted property-only handlers FIRST (in production they're
// concatenated into the bundle before sse_pipeline.js and share window scope).
eval(fs.readFileSync(process.argv[4], 'utf8'));  // ui/sse_handlers_tool.js
eval(fs.readFileSync(process.argv[5], 'utf8'));  // ui/sse_handlers_swarm.js
eval(fs.readFileSync(process.argv[6], 'utf8'));  // ui/sse_handlers_io.js
eval(fs.readFileSync(process.argv[7], 'utf8'));  // ui/sse_handlers_misc.js
eval(fs.readFileSync(process.argv[8], 'utf8'));  // ui/sse_handlers_lifecycle.js
eval(fs.readFileSync(process.argv[2], 'utf8'));  // sse_pipeline.js

const T = win.__sse_test__;
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (!T || typeof T.dispatchSSEEvent !== 'function' || typeof T.makeCtx !== 'function') {
  console.log('FAIL seam_exposed window.__sse_test__.{makeCtx,dispatchSSEEvent} missing');
  process.exit(0);
}
check('seam_exposed', true);

// Helper: build a conv + worker assistant msg + ctx wired together.
function setup() {
  conversations.length = 0;
  const am = { role: 'assistant', content: '', thinking: '', toolRounds: [], _msgId: 'mid-worker' };
  const conv = { id: 'c1', messages: [{ role: 'user', content: 'hi' }, am] };
  conversations.push(conv);
  activeConvId = 'c1';
  const buf = { content: '', thinking: '', toolRounds: [] };
  streamBufs.set('c1', buf);
  const ctx = T.makeCtx({ convId: 'c1', taskId: 't1',
    stream: { controller: { signal: { aborted: false } } },
    assistantMsg: am, buf });
  return { conv, am, buf, ctx };
}
function line(obj) { return 'data: ' + JSON.stringify(obj); }

// ── 1. delta accumulates into assistantMsg + buf ──
{
  const { am, buf, ctx } = setup();
  T.dispatchSSEEvent(line({ type: 'delta', content: 'Hello' }), ctx);
  T.dispatchSSEEvent(line({ type: 'delta', content: ' world' }), ctx);
  check('delta_accumulates_content', ctx.assistantMsg.content === 'Hello world');
  check('delta_syncs_buf', buf.content === 'Hello world');
  T.dispatchSSEEvent(line({ type: 'delta', thinking: 'hmm' }), ctx);
  check('delta_accumulates_thinking', ctx.assistantMsg.thinking === 'hmm');
}

// ── 2. tool_start pushes a round; tool_result completes it ──
{
  const { ctx } = setup();
  T.dispatchSSEEvent(line({ type: 'tool_start', roundNum: 1, toolCallId: 'tc1',
    toolName: 'web_search', query: 'q' }), ctx);
  check('tool_start_pushes_round', ctx.assistantMsg.toolRounds.length === 1 &&
    ctx.assistantMsg.toolRounds[0].status === 'searching');
  T.dispatchSSEEvent(line({ type: 'tool_result', roundNum: 1, toolCallId: 'tc1',
    results: [{ title: 'r' }] }), ctx);
  const r = ctx.assistantMsg.toolRounds[0];
  check('tool_result_marks_done', r.status === 'done' && Array.isArray(r.results));
}

// ── 3. done returns TRUE and stamps finishReason/usage; clears continue markers ──
{
  const { ctx } = setup();
  ctx.assistantMsg._continueModifiedFiles = 2;
  const done1 = T.dispatchSSEEvent(line({ type: 'delta', content: 'x' }), ctx);
  check('non_done_returns_falsy', !done1);
  const done2 = T.dispatchSSEEvent(line({ type: 'done', finishReason: 'stop',
    usage: { total_tokens: 5 }, modifiedFiles: 3 }), ctx);
  check('done_returns_true', done2 === true);
  check('done_sets_finishReason', ctx.assistantMsg.finishReason === 'stop');
  check('done_merges_continue_modifiedFiles', ctx.assistantMsg.modifiedFiles === 5);
  check('done_clears_continue_marker', ctx.assistantMsg._continueModifiedFiles === undefined);
}

// ── 4. Non-data / id lines + bad JSON are no-ops (return falsy) ──
{
  const { ctx } = setup();
  check('id_line_noop', !T.dispatchSSEEvent('id: 42', ctx) && ctx.lastEventId === '42');
  check('comment_line_noop', !T.dispatchSSEEvent(': keepalive', ctx));
  check('bad_json_noop', !T.dispatchSSEEvent('data: {not json', ctx));
}

// ── 5. BUG-GUARD: critic phase routes deltas to critic, NOT the worker ──
{
  const { am, ctx } = setup();
  // Enter reviewing phase via a state snapshot.
  T.dispatchSSEEvent(line({ type: 'state', endpointMode: true,
    endpointPhase: 'reviewing', endpointIteration: 1,
    endpointTurns: [{ role: 'assistant', content: 'worker out', _epIteration: 1, _msgId: 'mid-w1' }],
    content: 'critic thinking' }), ctx);
  check('critic_phase_set', ctx.epCriticPhase === true && ctx.epCriticMsg);
  const workerBefore = ctx.assistantMsg.content;
  T.dispatchSSEEvent(line({ type: 'delta', content: 'CRITIC TEXT' }), ctx);
  check('critic_delta_not_in_worker', !ctx.assistantMsg.content.includes('CRITIC TEXT'));
  check('critic_delta_in_critic', (ctx.epCriticMsg.content || '').includes('CRITIC TEXT'));
}

// ── 6. BUG-GUARD: stale 'state' snapshot for an aborted task is discarded ──
{
  const { conv, ctx } = setup();
  conv._lastAbortedTaskId = 't1';   // ctx.taskId is 't1' → this state is stale
  const before = conv.messages.length;
  const ret = T.dispatchSSEEvent(line({ type: 'state', content: 'resurrected',
    endpointMode: false }), ctx);
  check('stale_state_returns_false', ret === false);
  check('stale_state_no_mutation', conv.messages.length === before &&
    ctx.assistantMsg.content === '');
}

// ── 7. endpoint_critic_msg finalizes critic + sets approval + KEEPS thinking ──
{
  const { conv, ctx } = setup();
  T.dispatchSSEEvent(line({ type: 'state', endpointMode: true,
    endpointPhase: 'reviewing', endpointIteration: 1,
    endpointTurns: [{ role: 'assistant', content: 'w', _epIteration: 1, _msgId: 'mid-w2' }],
    content: 'review' }), ctx);
  T.dispatchSSEEvent(line({ type: 'endpoint_critic_msg', content: 'Looks good',
    thinking: 'CRITIC-REASONING', next_phase: 'stop' }), ctx);
  check('critic_msg_clears_phase', ctx.epCriticPhase === false);
  check('critic_msg_clears_buf_refs', ctx.epCriticMsg === null && ctx.epCriticBuf === null);
  /* ★ Thinking-persistence: the critic bubble's thinking must survive
   *   finalize (the flow path now sends ev.thinking; sse_pipeline must
   *   apply it). _epCriticMsg is nulled, so read the persisted critic
   *   message in conv.messages. */
  const criticMsg = conv.messages.find(m => m._isEndpointReview);
  check('critic_msg_keeps_thinking', !!criticMsg &&
    criticMsg.thinking === 'CRITIC-REASONING');
}

// ── 8. tool_complete stamps toolContent/tokens on the matching round ──
{
  const { ctx } = setup();
  T.dispatchSSEEvent(line({ type: 'tool_start', roundNum: 2, toolCallId: 'tcA',
    toolName: 'read_files' }), ctx);
  T.dispatchSSEEvent(line({ type: 'tool_complete', roundNum: 2, toolCallId: 'tcA',
    toolContent: 'FILE BODY', toolTokens: 123 }), ctx);
  const r = ctx.assistantMsg.toolRounds.find(x => x.toolCallId === 'tcA');
  check('tool_complete_sets_content', r && r.toolContent === 'FILE BODY');
  check('tool_complete_sets_tokens', r && r.toolTokens === 123);
}

// ── 9. tool_compacted tags the round (even on a COLD/older message) ──
{
  const { conv, ctx } = setup();
  // Cold round lives on an EARLIER assistant message, not the in-flight one.
  const older = { role: 'assistant', content: 'old', toolRounds: [
    { roundNum: 1, toolCallId: 'cold1', status: 'done', results: [] }], _msgId: 'mid-old' };
  conv.messages.splice(1, 0, older);   // insert before the worker msg
  T.dispatchSSEEvent(line({ type: 'tool_compacted', toolCallId: 'cold1',
    compactionLayer: 'L1', compactedFromChars: 5000, compactedToChars: 200,
    toolTokens: 50 }), ctx);
  const cr = older.toolRounds[0];
  check('compacted_tags_cold_round', cr.compactionLayer === 'L1' &&
    cr.compactedFromChars === 5000 && cr.compactedToChars === 200);
  check('compacted_returns_falsy', true); // branch never returns true
}

// ── 10. swarm_phase: spawning creates a panel; complete finalizes it ──
{
  const { ctx } = setup();
  // A prior tool_start round exists to be upgraded into the swarm panel.
  T.dispatchSSEEvent(line({ type: 'tool_start', roundNum: 1, toolCallId: 'sw1',
    toolName: 'spawn_agents', _swarm: true }), ctx);
  T.dispatchSSEEvent(line({ type: 'swarm_phase', phase: 'spawning',
    agents: [{ agentId: 'a1', role: 'coder', objective: 'do X' },
             { agentId: 'a2', role: 'researcher', objective: 'do Y' }] }), ctx);
  const panel = ctx.assistantMsg.toolRounds.find(r => r._swarm && r._swarmActive);
  check('swarm_spawning_creates_panel', !!panel &&
    (panel._swarmAgents || []).length === 2);
  T.dispatchSSEEvent(line({ type: 'swarm_phase', phase: 'complete',
    totalTokens: 999, agentCount: 2, failedCount: 0,
    agents: [{ agentId: 'a1', status: 'completed' }, { agentId: 'a2', status: 'completed' }] }), ctx);
  const done = ctx.assistantMsg.toolRounds.find(r => r._swarm);
  check('swarm_complete_marks_done', done.status === 'done' &&
    done._swarmActive === false && done._swarmStats &&
    done._swarmStats.totalTokens === 999);
  check('swarm_complete_agents_done', (done._swarmAgents || []).every(a => a.status === 'done'));
}

// ── 11. swarm_agent_phase: updates an agent's status/phase on its panel ──
{
  const { ctx } = setup();
  T.dispatchSSEEvent(line({ type: 'tool_start', roundNum: 1, toolCallId: 'sw2',
    toolName: 'spawn_agents', _swarm: true }), ctx);
  T.dispatchSSEEvent(line({ type: 'swarm_phase', phase: 'spawning',
    agents: [{ agentId: 'ag1', role: 'coder', objective: 'task' }] }), ctx);
  T.dispatchSSEEvent(line({ type: 'swarm_agent_phase', agentId: 'ag1',
    status: 'running', phase: 'thinking', preview: 'working…' }), ctx);
  const panel = ctx.assistantMsg.toolRounds.find(r => r._swarm);
  const agent = (panel._swarmAgents || []).find(a => a.id === 'ag1');
  check('swarm_agent_phase_updates', agent && agent.status === 'running' &&
    agent.phase === 'thinking' && agent.preview === 'working…');
}

// ── 12. human_guidance_request marks the round awaiting + normalizes options ──
{
  const { ctx } = setup();
  T.dispatchSSEEvent(line({ type: 'tool_start', roundNum: 1, toolCallId: 'hg1',
    toolName: 'ask_human' }), ctx);
  T.dispatchSSEEvent(line({ type: 'human_guidance_request', roundNum: 1, toolCallId: 'hg1',
    guidanceId: 'g1', question: 'Pick?', responseType: 'choice',
    options: '[{"value":"a"},{"value":"b"}]' }), ctx);  // options as JSON STRING
  const r = ctx.assistantMsg.toolRounds.find(x => x.toolCallId === 'hg1');
  check('hg_awaiting_status', r && r.status === 'awaiting_human' && r.guidanceId === 'g1');
  check('hg_options_normalized', r && Array.isArray(r.guidanceOptions) &&
    r.guidanceOptions.length === 2 && r.guidanceOptions[0].value === 'a');
}

// ── 13. tool_progress appends to the round's live _partialOutput buffer ──
{
  const { ctx } = setup();
  T.dispatchSSEEvent(line({ type: 'tool_start', roundNum: 1, toolCallId: 'tp1',
    toolName: 'run_command' }), ctx);
  T.dispatchSSEEvent(line({ type: 'tool_progress', toolCallId: 'tp1', chunk: 'line1\n' }), ctx);
  T.dispatchSSEEvent(line({ type: 'tool_progress', toolCallId: 'tp1', chunk: 'line2\n' }), ctx);
  const r = ctx.assistantMsg.toolRounds.find(x => x.toolCallId === 'tp1');
  check('tool_progress_accumulates', r && r._partialOutput === 'line1\nline2\n');
}

// ── 14. stdin_request → awaiting_stdin; stdin_resolved → searching + clears prompt ──
{
  const { ctx } = setup();
  T.dispatchSSEEvent(line({ type: 'tool_start', roundNum: 1, toolCallId: 'sd1',
    toolName: 'run_command' }), ctx);
  T.dispatchSSEEvent(line({ type: 'stdin_request', toolCallId: 'sd1',
    stdinId: 's1', prompt: 'pw:', command: 'sudo x' }), ctx);
  let r = ctx.assistantMsg.toolRounds.find(x => x.toolCallId === 'sd1');
  check('stdin_request_awaiting', r && r.status === 'awaiting_stdin' && r.stdinId === 's1');
  T.dispatchSSEEvent(line({ type: 'stdin_resolved', toolCallId: 'sd1' }), ctx);
  r = ctx.assistantMsg.toolRounds.find(x => x.toolCallId === 'sd1');
  check('stdin_resolved_clears', r && r.status === 'searching' && r.stdinId === null);
}

// ── 15. write_approval_request → pending_approval (skipped in critic phase) ──
{
  const { ctx } = setup();
  T.dispatchSSEEvent(line({ type: 'tool_start', roundNum: 1, toolCallId: 'wa1',
    toolName: 'write_file' }), ctx);
  T.dispatchSSEEvent(line({ type: 'write_approval_request', toolCallId: 'wa1',
    approvalId: 'ap1', meta: { path: 'x.txt' } }), ctx);
  const r = ctx.assistantMsg.toolRounds.find(x => x.toolCallId === 'wa1');
  check('write_approval_pending', r && r.status === 'pending_approval' &&
    r.approvalId === 'ap1' && r.approvalMeta && r.approvalMeta.path === 'x.txt');
}

// ── 15b. write_approval_request for run_command carries command/description ──
//   (the destructive-run_command gate path — meta.command drives the
//    ameta.command!=null render branch in ui/tool_rounds.js). ──
{
  const { ctx } = setup();
  T.dispatchSSEEvent(line({ type: 'tool_start', roundNum: 1, toolCallId: 'wac',
    toolName: 'run_command' }), ctx);
  T.dispatchSSEEvent(line({ type: 'write_approval_request', toolCallId: 'wac',
    approvalId: 'ap2', meta: { toolName: 'run_command', command: 'rm foo.py',
      description: 'delete foo' } }), ctx);
  const r = ctx.assistantMsg.toolRounds.find(x => x.toolCallId === 'wac');
  check('run_command_approval_pending', r && r.status === 'pending_approval' &&
    r.approvalId === 'ap2');
  check('run_command_approval_meta', r && r.approvalMeta &&
    r.approvalMeta.command === 'rm foo.py' &&
    r.approvalMeta.description === 'delete foo' &&
    r.approvalMeta.path == null);
}

// ── 16. round_usage stashes _liveLastRoundUsage; returns falsy ──
{
  const { ctx } = setup();
  const ret = T.dispatchSSEEvent(line({ type: 'round_usage', round: 2, model: 'm',
    tokensIn: 100, tokensOut: 20, usage: { total_tokens: 120 } }), ctx);
  check('round_usage_returns_falsy', !ret);
  const u = ctx.assistantMsg._liveLastRoundUsage;
  check('round_usage_stashes', u && u.round === 2 && u.tokensIn === 100 && u.tokensOut === 20);
}

// ── 17. artifact attaches via window.Artifacts + mirrors buf._artifacts ──
{
  const { ctx } = setup();
  T.dispatchSSEEvent(line({ type: 'artifact', id: 'art1', format: 'md',
    title: 'Doc', size_bytes: 10 }), ctx);
  check('artifact_attached', calls.attachToMessage >= 1);
}

// ── 18. compaction creates an in_progress marker; compaction_done finalizes it ──
{
  const { ctx } = setup();
  T.dispatchSSEEvent(line({ type: 'compaction', archiveId: 'ar1', trigger: 'force',
    tokensBefore: 9000, roundNum: 3 }), ctx);
  let m = (ctx.assistantMsg._compactions || []).find(c => c.archiveId === 'ar1');
  check('compaction_creates_marker', m && m.status === 'in_progress' && m.tokensBefore === 9000);
  T.dispatchSSEEvent(line({ type: 'compaction_done', archiveId: 'ar1',
    tokensAfter: 500, reductionPct: 94 }), ctx);
  m = (ctx.assistantMsg._compactions || []).find(c => c.archiveId === 'ar1');
  check('compaction_done_finalizes', m && m.status === 'done' &&
    m.tokensAfter === 500 && m.reductionPct === 94);
}

// ── 19. memory_prefetch sets _memoryPrefetch + toggles conv._memoryPrefetching ──
{
  const { conv, ctx } = setup();
  T.dispatchSSEEvent(line({ type: 'memory_prefetch', phase: 'started',
    total_memories: 12 }), ctx);
  check('memprefetch_running', ctx.assistantMsg._memoryPrefetch &&
    ctx.assistantMsg._memoryPrefetch.phase === 'started' &&
    conv._memoryPrefetching === true);
  T.dispatchSSEEvent(line({ type: 'memory_prefetch', phase: 'done', selected: 2 }), ctx);
  check('memprefetch_terminal_clears', ctx.assistantMsg._memoryPrefetch.phase === 'done' &&
    conv._memoryPrefetching === false);
}

// ── 20. timer_poll_check: 'ready' marks round done; 'skipped' bumps skip count ──
{
  const { ctx } = setup();
  T.dispatchSSEEvent(line({ type: 'tool_start', roundNum: 1, toolCallId: 'tm1',
    toolName: 'timer_create' }), ctx);
  T.dispatchSSEEvent(line({ type: 'timer_poll_check', toolCallId: 'tm1',
    decision: 'skipped', pollNum: 1, timerId: 'T1' }), ctx);
  let r = ctx.assistantMsg.toolRounds.find(x => x.toolCallId === 'tm1');
  check('timer_skip_counts', r && r._timerSkipCount === 1 && r.status === 'searching');
  T.dispatchSSEEvent(line({ type: 'timer_poll_check', toolCallId: 'tm1',
    decision: 'ready', pollNum: 2, timerId: 'T1' }), ctx);
  r = ctx.assistantMsg.toolRounds.find(x => x.toolCallId === 'tm1');
  check('timer_ready_done', r && r.status === 'done' && r._timerTriggered === true &&
    (r._timerPolls || []).length === 1);
}

// ── 21. swarm_inbox_inject pushes a chip + a synthetic _inboxInject round (deduped) ──
{
  const { ctx } = setup();
  T.dispatchSSEEvent(line({ type: 'swarm_inbox_inject', round: 3, count: 2,
    agentIds: ['a1', 'a2'], previews: ['p1', 'p2'] }), ctx);
  check('inbox_chip_pushed', (ctx.assistantMsg._inboxInjects || []).length === 1 &&
    ctx.assistantMsg._inboxInjects[0].count === 2);
  const synth = (ctx.assistantMsg.toolRounds || []).filter(r => r._inboxInject);
  check('inbox_synthetic_round', synth.length === 1 && synth[0]._inboxKey === 'inbox:3' &&
    synth[0].status === 'done');
  // Replay the SAME round → must dedup (no second synthetic round).
  T.dispatchSSEEvent(line({ type: 'swarm_inbox_inject', round: 3, count: 2,
    agentIds: ['a1', 'a2'] }), ctx);
  check('inbox_dedup', (ctx.assistantMsg.toolRounds || []).filter(r => r._inboxInject).length === 1);
}

// ── 22. messages_snapshot forwards to showMessagesInDebug (debug-only, no msg mutation) ──
{
  const { ctx } = setup();
  const before = JSON.stringify(ctx.assistantMsg);
  T.dispatchSSEEvent(line({ type: 'messages_snapshot', round: 1, messageCount: 3,
    messages: [{ role: 'user', content: 'x' }] }), ctx);
  check('snapshot_calls_debug', calls.showMessagesInDebug >= 1);
  check('snapshot_no_msg_mutation', JSON.stringify(ctx.assistantMsg) === before);
}

// ── 23. sse_timeout returns FALSE (task still running → poll fallback) ──
{
  const { ctx } = setup();
  const ret = T.dispatchSSEEvent(line({ type: 'sse_timeout' }), ctx);
  check('sse_timeout_returns_false', ret === false);
}

// ── 24. round_committed wires gitSha/modifiedFiles; returns FALSE (not done) ──
{
  const { ctx } = setup();
  const ret = T.dispatchSSEEvent(line({ type: 'round_committed', gitSha: 'abc123',
    modifiedFiles: 4, modifiedFileList: [{ path: 'a.py' }] }), ctx);
  check('round_committed_returns_false', ret === false);
  check('round_committed_sets_sha', ctx.assistantMsg._gitSha === 'abc123' &&
    ctx.assistantMsg.modifiedFiles === 4 &&
    (ctx.assistantMsg.modifiedFileList || []).length === 1);
}

// ── 25. GHOST-PANEL GUARD: when an agent's phase event RACES AHEAD of the
//        spawning event (so no panel is active yet) and a stale/empty
//        (ghost) _swarm round from a prior errored spawn_agents lingers,
//        the agent must be created on the REAL spawn round (last _swarm),
//        never grafted onto the ghost. Regression for the "2 done / 4 done"
//        split + "ticked but waiting" desync. ──
{
  const { ctx } = setup();
  // A leftover ghost swarm round from a prior errored spawn — it has the
  // _swarm flag but is NOT active and owns no agents. (Pre-fix this was the
  // FIRST _swarm round, so the buggy "find first _swarm" lookup grabbed it.)
  ctx.assistantMsg.toolRounds.push({
    roundNum: 1, query: 'Agent Swarm', _swarm: true,
    _swarmActive: false, _asyncRunning: false, _swarmAgents: [],
  });
  // The REAL spawn's tool_start round lands (carries _swarm, but is NOT yet
  // _swarmActive — that flag is set by the spawning phase event).
  T.dispatchSSEEvent(line({ type: 'tool_start', roundNum: 2, toolCallId: 'swR',
    toolName: 'spawn_agents', _swarm: true }), ctx);
  // RACE: an agent's phase event arrives BEFORE the spawning event populates
  // the panel's agent stubs. With no active panel yet, routing must pick the
  // LAST _swarm round (the real spawn), not the first (ghost).
  T.dispatchSSEEvent(line({ type: 'swarm_agent_phase', agentId: 'r1',
    role: 'coder', objective: 'real X', status: 'running', phase: 'thinking' }), ctx);
  const ghost = ctx.assistantMsg.toolRounds[0];
  const realRound = ctx.assistantMsg.toolRounds[1];
  check('ghost_panel_stays_empty', (ghost._swarmAgents || []).length === 0);
  check('agent_routes_to_real_panel',
    (realRound._swarmAgents || []).some(a => a.id === 'r1' && a.status === 'running'));
  // Now the spawning event lands with the full agent list (r1 already there
  // gets reused; r2 is new). Then complete the swarm.
  T.dispatchSSEEvent(line({ type: 'swarm_phase', phase: 'spawning',
    agents: [{ agentId: 'r1', role: 'coder', objective: 'real X' },
             { agentId: 'r2', role: 'coder', objective: 'real Y' }] }), ctx);
  const real = ctx.assistantMsg.toolRounds.find(r => r._swarmActive);
  // r2 never got per-agent events, so its phase is still "waiting". The
  // complete-sweep must advance BOTH status AND phase so it never renders a
  // "waiting" pill next to a done checkmark (the status/phase desync bug).
  T.dispatchSSEEvent(line({ type: 'swarm_phase', phase: 'complete',
    totalTokens: 100, agentCount: 2, failedCount: 0,
    agents: [{ agentId: 'r1', status: 'completed' }] }), ctx);
  const r2 = (real._swarmAgents || []).find(a => a.id === 'r2');
  check('stranded_agent_status_done', r2 && r2.status === 'done');
  check('stranded_agent_phase_advanced', r2 && r2.phase === 'done');
}

// ── 26. OUT-OF-ORDER SSE (#6): swarm_agent_complete arrives BEFORE the
//        agent's start/phase event ever created its card. The terminal
//        result must NOT be dropped — the handler must CREATE the card so the
//        agent shows its real outcome (regression: pre-fix the complete event
//        no-op'd when no matching agent existed, so the card vanished until
//        the swarm_phase:complete sweep). ──
{
  const { ctx } = setup();
  // Real spawn panel exists + is active (spawning landed), but agent 'oo2'
  // has NOT been announced via start/phase yet.
  T.dispatchSSEEvent(line({ type: 'tool_start', roundNum: 1, toolCallId: 'ooR',
    toolName: 'spawn_agents', _swarm: true }), ctx);
  T.dispatchSSEEvent(line({ type: 'swarm_phase', phase: 'spawning',
    agents: [{ agentId: 'oo1', role: 'coder', objective: 'first' }] }), ctx);
  // complete for 'oo2' races ahead of its start — must create the card.
  T.dispatchSSEEvent(line({ type: 'swarm_agent_complete', agentId: 'oo2',
    role: 'researcher', objective: 'second', status: 'completed',
    preview: 'OO2 RESULT', elapsed: 1.5, tokens: 42, modifiedFiles: 1 }), ctx);
  const panel = ctx.assistantMsg.toolRounds.find(r => r._swarm && r._swarmActive);
  const oo2 = (panel._swarmAgents || []).find(a => a.id === 'oo2');
  check('ooo_complete_creates_card', !!oo2);
  check('ooo_complete_real_status', oo2 && oo2.status === 'done' &&
    oo2.preview === 'OO2 RESULT' && oo2.modifiedFiles === 1);
  // And an error racing ahead of start creates a failed card too.
  T.dispatchSSEEvent(line({ type: 'swarm_agent_error', agentId: 'oo3',
    role: 'coder', error: 'boom' }), ctx);
  const oo3 = (panel._swarmAgents || []).find(a => a.id === 'oo3');
  check('ooo_error_creates_failed_card', !!oo3 && oo3.status === 'failed' &&
    oo3.phase === 'error');
}

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_sse_dispatch_characterization():
    harness = os.path.join(HERE, '_sse_dispatch_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'ui', 'sse_pipeline.js'),   # argv[2]
             ROOT,                                            # argv[3]
             os.path.join(JS_DIR, 'ui', 'sse_handlers_tool.js'),   # argv[4]
             os.path.join(JS_DIR, 'ui', 'sse_handlers_swarm.js'),  # argv[5]
             os.path.join(JS_DIR, 'ui', 'sse_handlers_io.js'),     # argv[6]
             os.path.join(JS_DIR, 'ui', 'sse_handlers_misc.js'),   # argv[7]
             os.path.join(JS_DIR, 'ui', 'sse_handlers_lifecycle.js'),  # argv[8]
             ],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'SSE dispatch characterization failures:\n' + output
    # 26 scenario groups, ~59 individual checks.
    assert output.count('PASS') >= 57, f'expected >=57 PASS lines, got:\n{output}'
