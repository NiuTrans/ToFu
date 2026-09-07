"""Swarm live telemetry belongs to a transient Turn, never projection carry-over.

Conversation Sync frames replace durable Turn values. Swarm push telemetry must
therefore survive in the presentation overlay keyed by ``turnId`` while the
durable store and its compatibility message view remain pristine. Terminal
pushes hydrate backend truth before releasing the overlay.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLER = os.path.join(ROOT, 'scripts', 'vite_test_bundle.mjs')
CONVERSATION_ENTRY = os.path.join(
    ROOT, 'frontend', 'src', 'conversation', 'index.ts')

_HARNESS = r"""
const fs = require('fs');
globalThis.window = globalThis;
const conversationFeature = require(process.argv[2]);
const swarmPresentationOverlay = conversationFeature.swarmPresentationOverlay;

const output = [];
function check(name, condition) {
  output.push((condition ? 'PASS ' : 'FAIL ') + name);
}

let subscribed = null;
let subscribeCalls = 0;
let unsubscribeCalls = 0;

function durableTurn(revision, parentAdvanced = false) {
  const round = {
    roundNum: 1, llmRound: 0, toolCallId: 'tool-1', toolName: 'spawn_agents',
    status: 'done', _swarm: true,
    _swarmSnapshot: { settled: false, agents: [{ id: 'agent-1', status: 'pending' }] },
  };
  const parentRound = {
    roundNum: 2, llmRound: 1, toolCallId: 'tool-2', toolName: 'read_files',
    status: 'done', results: 'parent kept working',
  };
  return {
    turnId: 'turn-1', actor: 'assistant', kind: 'reply', laneId: 'main',
    parentTurnId: null, status: 'running', currentAttemptId: 'attempt-1',
    projectionRevision: revision,
    projection: {
      content: parentAdvanced ? 'parent continued after spawn' : '',
      toolRounds: parentAdvanced ? [round, parentRound] : [round],
      segments: [
        { type: 'tool_use', id: 'tool-1', llmRound: 0, _round: round },
        ...(parentAdvanced
          ? [{ type: 'tool_use', id: 'tool-2', llmRound: 1, _round: parentRound }]
          : []),
      ],
    },
    settlement: {}, createdAt: 1,
  };
}

const firstDurable = durableTurn(1);
const state = {
  turnsById: { 'turn-1': firstDurable },
  laneOrder: { main: ['turn-1'] },
};
const conv = { id: 'conv-1' };
globalThis.conversations = [conv];
const overlays = new Map();
const lifecycle = [];

globalThis.ConversationTurnStore = {
  ensureRuntimeStore() { return { getState() { return state; } }; },
  hydrateConversation() { lifecycle.push('hydrate'); return Promise.resolve(); },
};
globalThis.ConversationTransientTurns = {
  get(conversationId, turnId) { return overlays.get(conversationId + ':' + turnId) || null; },
  upsert(conversation, turn) {
    overlays.set(conversation.id + ':' + turn.turnId, turn);
    lifecycle.push('upsert');
    return true;
  },
  remove(conversation, turnId) {
    lifecycle.push('remove');
    return overlays.delete(conversation.id + ':' + turnId);
  },
};

(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));
const swarmRuntime = conversationFeature.createSwarmPushRuntime({
  findConversation(conversationId) {
    return conversations.find((item) => item?.id === conversationId) || null;
  },
  readTurnState(conversationId) {
    return ConversationTurnStore.ensureRuntimeStore(conversationId).getState();
  },
  readOverlay(conversationId, turnId) {
    return ConversationTransientTurns.get(conversationId, turnId);
  },
  upsertOverlay(conversation, turn) {
    if (process.argv[3] !== 'neuter-upsert') {
      ConversationTransientTurns.upsert(conversation, turn);
    }
  },
  removeOverlay(conversation, turnId) {
    ConversationTransientTurns.remove(conversation, turnId);
  },
  hydrateConversation(conversation) {
    return ConversationTurnStore.hydrateConversation(conversation);
  },
  attachAutoContinue() {},
  reducePhase(frame, context) { _handleSwarmPhase(frame, context); },
  reduceAgent(frame, context) { _handleSwarmAgent(frame, context); },
  debug() {},
  warn() {},
  subscribe(handler) {
    subscribeCalls += 1;
    subscribed = handler;
  },
  unsubscribe(handler) {
    unsubscribeCalls += 1;
    if (subscribed === handler) subscribed = null;
  },
});
globalThis.ConversationSwarmPresentation = swarmRuntime.presentation;
swarmRuntime.start();
swarmRuntime.start();

(async () => {
  check('push_owner_subscribed_once',
    subscribeCalls === 1 && typeof subscribed === 'function');
  subscribed({
    type: 'swarm_phase', convId: 'conv-1', phase: 'spawning',
    swarmKey: 'conv-1',
    agents: [{ agentId: 'agent-1', role: 'researcher', objective: 'inspect' }],
  });
  const firstOverlay = overlays.get('conv-1:turn-1');
  check('overlay_created', Boolean(firstOverlay));
  check('overlay_keyed_by_turn', firstOverlay?.turnId === 'turn-1');
  check('overlay_live_state_present',
    firstOverlay?.projection.toolRounds[0]._swarmActive === true);
  check('durable_turn_untouched',
    firstDurable.projection.toolRounds[0]._swarmActive === undefined);
  check('segment_round_rebound_to_overlay',
    firstOverlay?.projection.segments[0]._round
      === firstOverlay?.projection.toolRounds[0]);

  /* Attempts restart their local LLM-round counter. This is the production
     shape that made six later R17 run_command segments render as six copies of
     an earlier R17 todo_write card: an exact call ID must win before any legacy
     round-number fallback is considered. */
  const oldTodoRound = {
    roundNum: 17, llmRound: 16, attemptId: 'attempt-old',
    toolCallId: 'todo-r17', toolName: 'todo_write', status: 'done',
  };
  const laterParallelRounds = Array.from({ length: 6 }, (_, index) => ({
    roundNum: 100 + index, llmRound: 16, attemptId: 'attempt-new',
    toolCallId: `run-r17-${index}`, toolName: 'run_command', status: 'done',
  }));
  const repeatedR17Durable = {
    ...firstDurable,
    projectionRevision: 2,
    projection: {
      content: '',
      toolRounds: [oldTodoRound, ...laterParallelRounds],
      segments: laterParallelRounds.map((round, index) => ({
        type: 'tool_use', blockId: `tool:run-r17-${index}`,
        id: round.toolCallId, name: round.toolName, llmRound: round.llmRound,
        attemptId: round.attemptId, result: { status: 'done' },
      })),
    },
  };
  const repeatedR17Overlay = swarmPresentationOverlay.rebase(
    repeatedR17Durable, firstOverlay,
  );
  const reboundR17Segments = repeatedR17Overlay?.projection.segments ?? [];
  check('rebind_prefers_exact_call_id_across_attempt_local_round_reuse',
    reboundR17Segments.every((segment) =>
      segment._round?.toolCallId === segment.id));
  check('later_parallel_tools_do_not_render_as_repeated_old_checklists',
    reboundR17Segments.every((segment) =>
      segment._round?.toolName === 'run_command'));


  const uniqueLegacyProjection = {
    toolRounds: [{
      llmRound: 7, toolCallId: 'legacy-only', toolName: 'read_files',
    }],
    segments: [{
      type: 'tool_use', id: '', name: 'read_files', llmRound: 7,
      result: { status: 'done' },
    }],
  };
  swarmPresentationOverlay.rebindSegments(uniqueLegacyProjection);
  check('idless_legacy_segment_uses_unique_round_fallback',
    uniqueLegacyProjection.segments[0]._round?.toolCallId === 'legacy-only');

  const ambiguousLegacyProjection = {
    toolRounds: [
      { llmRound: 9, attemptId: 'attempt-a', toolCallId: 'legacy-a' },
      { llmRound: 9, attemptId: 'attempt-b', toolCallId: 'legacy-b' },
    ],
    segments: [{
      type: 'tool_use', id: '', name: 'run_command', llmRound: 9,
      result: { status: 'done' },
    }],
  };
  swarmPresentationOverlay.rebindSegments(ambiguousLegacyProjection);
  check('unscoped_legacy_segment_refuses_cross_attempt_guess',
    ambiguousLegacyProjection.segments[0]._round === undefined);

  const scopedLegacyProjection = {
    toolRounds: ambiguousLegacyProjection.toolRounds,
    segments: [{
      type: 'tool_use', id: '', name: 'run_command', llmRound: 9,
      attemptId: 'attempt-b', result: { status: 'done' },
    }],
  };
  swarmPresentationOverlay.rebindSegments(scopedLegacyProjection);
  check('legacy_segment_fallback_honors_attempt_scope',
    scopedLegacyProjection.segments[0]._round?.toolCallId === 'legacy-b');

  /* A fresh authoritative projection arrives without session telemetry. The
     presentation must keep those private fields while exposing the parent's
     new content immediately; the next push continues from that rebased copy. */
  const secondDurable = durableTurn(2, true);
  state.turnsById['turn-1'] = secondDurable;
  const transientState = {
    ...state,
    turnsById: {
      ...state.turnsById,
      ...(firstOverlay ? { 'turn-1': firstOverlay } : {}),
    },
  };
  const presentedState = ConversationSwarmPresentation.compose(
    conv, state, transientState,
  );
  const presentedTurn = presentedState.turnsById['turn-1'];
  check('authoritative_parent_content_visible_between_swarm_pushes',
    presentedTurn?.projection.content
      === 'parent continued after spawn');
  check('authoritative_parent_tool_visible_between_swarm_pushes',
    presentedTurn?.projection.toolRounds
      .some((round) => round.toolCallId === 'tool-2'));
  check('live_swarm_state_survives_authoritative_rebase',
    presentedTurn?.projection.toolRounds[0]
      ._swarmAgents?.[0]?.id === 'agent-1');
  subscribed({
    type: 'swarm_agent_tool_call', convId: 'conv-1',
    agentId: 'agent-1', callId: 'child-tool-1', toolName: 'grep_search',
    argsBrief: 'pattern=swarm', callStatus: 'running',
  });
  const secondOverlay = overlays.get('conv-1:turn-1');
  check('overlay_survives_authoritative_frame',
    secondOverlay?.projection.toolRounds[0]._swarmActive === true);
  check('overlay_absorbs_latest_parent_content',
    secondOverlay?.projection.content === 'parent continued after spawn');
  check('overlay_absorbs_latest_parent_tool',
    secondOverlay?.projection.toolRounds.some(
      (round) => round.toolCallId === 'tool-2'));
  const projectedChild = secondOverlay?.projection.toolRounds
    .flatMap((round) => round._swarmAgents || [])
    .find((agent) => agent.id === 'agent-1');
  check('child_tool_call_reduced_into_panel',
    projectedChild?._toolCalls?.[0]?.toolName === 'grep_search');
  check('replacement_durable_turn_untouched',
    secondDurable.projection.toolRounds[0]._swarmAgents === undefined);

  subscribed({ type: 'swarm_phase', convId: 'conv-1', phase: 'complete' });
  await new Promise((resolve) => setTimeout(resolve, 0));
  check('terminal_hydrates', lifecycle.includes('hydrate'));
  check('terminal_removes_overlay', !overlays.has('conv-1:turn-1'));
  check('hydrate_precedes_remove',
    lifecycle.indexOf('hydrate') >= 0
      && lifecycle.indexOf('hydrate') < lifecycle.lastIndexOf('remove'));
  swarmRuntime.destroy();
  swarmRuntime.destroy();
  check('push_owner_unsubscribed_once',
    unsubscribeCalls === 1 && subscribed === null);
  console.log(output.join('\n'));
})();
"""


def _run_harness(*, neuter_upsert: bool = False) -> str:
    with tempfile.TemporaryDirectory(prefix='tofu-swarm-module-') as temp:
        bundle = os.path.join(temp, 'conversation.cjs')
        build = subprocess.run(
            [
                BUNDLER, CONVERSATION_ENTRY, '--bundle', '--format=cjs',
                '--platform=node', f'--outfile={bundle}',
            ],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
        assert build.returncode == 0, build.stderr
        proc = subprocess.run(
            [
                'node', '-e', _HARNESS,
                runtime_section_path('ui/swarm_live_projection.js'), bundle,
                'neuter-upsert' if neuter_upsert else '',
            ],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()


_REDUCER_HARNESS = r"""
const fs = require('fs');
/* Real cross-section wiring: the production bundle concatenates sections
   into one scope, so `_handleSwarmAgent` sees the real `_recoverSwarmAgents`
   exactly as in the browser. */
(0, eval)(fs.readFileSync(process.argv[2], 'utf8'));  // ui/streaming_swarm_panel.js
(0, eval)(fs.readFileSync(process.argv[3], 'utf8'));  // ui/swarm_live_projection.js

const output = [];
function check(name, condition) {
  output.push((condition ? 'PASS ' : 'FAIL ') + name);
}

/* Multi-wave turn: spawn_agents ran twice in one assistant turn (e.g. after
   a continued generation). Wave 2 events must land on wave 2's panel —
   tool_call/progress have no fallback, so a first-round-only ownership scan
   silently dropped them and the panel showed cards with an empty tool
   timeline. */
const wave1 = {
  roundNum: 1, toolCallId: 'spawn-1', toolName: 'spawn_agents', status: 'done',
  _swarm: true, _swarmActive: false,
  _swarmAgents: [{ id: 'agent-1', role: 'coder', status: 'done', phase: 'done', tools: [] }],
};
const wave2 = {
  roundNum: 2, toolCallId: 'spawn-2', toolName: 'spawn_agents', status: 'searching',
  _swarm: true, _swarmActive: true,
  _swarmAgents: [{ id: 'agent-2', role: 'researcher', status: 'running', phase: 'tool_use', tools: [] }],
};
const multiWave = { convId: 'conv-1', taskId: 'task-1',
                    assistantProjection: { toolRounds: [wave1, wave2] } };
_handleSwarmAgent({
  type: 'swarm_agent_tool_call', agentId: 'agent-2',
  callId: 'call-1', toolName: 'web_search', argsBrief: 'q=swarm', callStatus: 'running',
}, multiWave);
check('multiwave_tool_call_lands_on_owning_wave',
  wave2._swarmAgents[0]._toolCalls?.[0]?.toolName === 'web_search'
  && wave1._swarmAgents[0]._toolCalls === undefined);
_handleSwarmAgent({
  type: 'swarm_agent_progress', agentId: 'agent-2', status: 'running',
  toolNames: ['web_search', 'fetch_url'],
}, multiWave);
check('multiwave_progress_lands_on_owning_wave',
  wave2._swarmAgents[0].tools.includes('fetch_url')
  && !wave1._swarmAgents[0].tools.includes('fetch_url'));

/* Post-reload mid-flight: `_swarmAgents` is live-only (empty), but the
   durable `_swarmSnapshot` names every agent. Live tool_call events must
   hydrate the missing card from the round's own snapshot and attach the
   timeline row instead of vanishing until the next phase event. */
const reloadRound = {
  roundNum: 1, toolCallId: 'spawn-1', toolName: 'spawn_agents', status: 'searching',
  _swarm: true,
  _swarmSnapshot: {
    settled: false,
    agents: [{
      id: 'agent-9', role: 'coder', model: 'm1', status: 'running',
      objective: 'implement x', startedAt: Date.now() - 5000,
      tools: ['read_files'], toolCalls: [],
    }],
  },
};
const reloadCtx = { convId: 'conv-1', taskId: 'task-1',
                    assistantProjection: { toolRounds: [reloadRound] } };
_handleSwarmAgent({
  type: 'swarm_agent_tool_call', agentId: 'agent-9',
  callId: 'call-9', toolName: 'grep_search', argsBrief: 'pattern=x', callStatus: 'running',
}, reloadCtx);
const hydrated = (reloadRound._swarmAgents || []).find(a => a.id === 'agent-9');
check('postreload_tool_call_hydrates_card_from_snapshot',
  !!hydrated && hydrated.objective === 'implement x');
check('postreload_tool_call_attaches_timeline_row',
  !!hydrated && (hydrated._toolCalls || [])
    .some(c => c.callId === 'call-9' && c.toolName === 'grep_search'));
check('postreload_snapshot_tools_preserved',
  !!hydrated && (hydrated.tools || []).includes('read_files'));

/* Strict ownership (B11) unchanged: an agent no roster/snapshot knows is
   still dropped for tool_call, never grafted. */
_handleSwarmAgent({
  type: 'swarm_agent_tool_call', agentId: 'agent-ghost',
  callId: 'call-g', toolName: 'run_command', callStatus: 'running',
}, reloadCtx);
check('unknown_agent_tool_call_still_dropped',
  !(reloadRound._swarmAgents || []).some(a => a.id === 'agent-ghost'));

/* Phase events still create genuinely new cards on the ACTIVE panel. */
_handleSwarmAgent({
  type: 'swarm_agent_phase', agentId: 'agent-new', phase: 'running',
  role: 'analyst', objective: 'brand new', status: 'running',
}, multiWave);
check('phase_event_still_creates_card_on_active_panel',
  wave2._swarmAgents.some(a => a.id === 'agent-new'));

console.log(output.join('\n'));
if (output.some(line => line.startsWith('FAIL'))) process.exit(1);
"""


def _run_reducer_harness() -> str:
  with tempfile.TemporaryDirectory(prefix='tofu-swarm-reducer-') as temp:
    script = os.path.join(temp, 'harness.cjs')
    with open(script, 'w', encoding='utf-8') as fh:
      fh.write(_REDUCER_HARNESS)
    proc = subprocess.run(
      [
        'node', script,
        runtime_section_path('ui/streaming_swarm_panel.js'),
        runtime_section_path('ui/swarm_live_projection.js'),
      ],
      cwd=ROOT, capture_output=True, text=True, timeout=60)
    return (proc.stdout or '').strip() + '\n' + (proc.stderr or '').strip()


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_swarm_agent_events_survive_multiwave_and_reload():
  out = _run_reducer_harness()
  assert 'FAIL' not in out, out
  assert 'PASS multiwave_tool_call_lands_on_owning_wave' in out, out
  assert 'PASS postreload_tool_call_hydrates_card_from_snapshot' in out, out


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_swarm_push_uses_transient_turn_overlay():
    output = _run_harness()
    failures = [line for line in output.splitlines() if line.startswith('FAIL')]
    assert not failures, 'swarm overlay failures:\n' + output
    assert output.count('PASS') >= 23, output


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_NEUTER_overlay_upsert_is_load_bearing():
    output = _run_harness(neuter_upsert=True)
    assert 'FAIL overlay_created' in output, output
