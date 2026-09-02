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

from tests._runtime_sections import runtime_section, runtime_section_path

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TURN_PROJECTION_TS = os.path.join(
    ROOT, 'frontend', 'src', 'core', 'turn-projection.ts')

_HARNESS = r"""
const fs = require('fs');
globalThis.window = globalThis;

const output = [];
function check(name, condition) {
  output.push((condition ? 'PASS ' : 'FAIL ') + name);
}

let subscribed = null;
globalThis.pushSubscribe = (channel, scope, handler) => {
  if (channel === 'swarm' && scope === '*') subscribed = handler;
};

function durableTurn(revision) {
  const round = {
    roundNum: 1, llmRound: 0, toolCallId: 'tool-1', toolName: 'spawn_agents',
    status: 'done', _swarm: true,
    _swarmSnapshot: { settled: false, agents: [{ id: 'agent-1', status: 'pending' }] },
  };
  return {
    turnId: 'turn-1', actor: 'assistant', kind: 'reply', laneId: 'main',
    parentTurnId: null, status: 'running', currentAttemptId: 'attempt-1',
    projectionRevision: revision,
    projection: {
      toolRounds: [round],
      segments: [{ type: 'tool_use', id: 'tool-1', llmRound: 0, _round: round }],
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

globalThis._handleSwarmPhase = (frame, context) => {
  const round = context.assistantProjection.toolRounds[0];
  if (frame.phase === 'spawning') {
    round._swarmActive = true;
    round._swarmStartTime = 1111;
    round._swarmKey = 'conv-1';
    round._swarmAgents = [{
      id: 'agent-1', status: 'running', phase: 'thinking', preview: '',
    }];
  } else if (frame.phase === 'complete') {
    round._swarmActive = false;
    round._swarmEndTime = 2222;
    round.status = 'done';
  }
};
globalThis._handleSwarmAgent = (frame, context) => {
  const round = context.assistantProjection.toolRounds[0];
  const agent = round._swarmAgents.find((item) => item.id === frame.agentId);
  if (agent) {
    agent.status = 'running';
    agent.phase = 'tool_use';
    agent.preview = frame.preview;
  }
};

(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));

(async () => {
  check('push_owner_subscribed', typeof subscribed === 'function');
  subscribed({ type: 'swarm_phase', convId: 'conv-1', phase: 'spawning' });
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

  /* A fresh authoritative projection arrives without session telemetry. The
     next push must reduce from the existing overlay, not copy private fields
     into the new durable Turn. */
  const secondDurable = durableTurn(2);
  state.turnsById['turn-1'] = secondDurable;
  subscribed({
    type: 'swarm_agent_progress', convId: 'conv-1',
    agentId: 'agent-1', preview: 'still working',
  });
  const secondOverlay = overlays.get('conv-1:turn-1');
  check('overlay_survives_authoritative_frame',
    secondOverlay?.projection.toolRounds[0]._swarmActive === true);
  check('overlay_progress_reduced',
    secondOverlay?.projection.toolRounds[0]._swarmAgents[0].preview
      === 'still working');
  check('replacement_durable_turn_untouched',
    secondDurable.projection.toolRounds[0]._swarmAgents === undefined);

  subscribed({ type: 'swarm_phase', convId: 'conv-1', phase: 'complete' });
  await new Promise((resolve) => setTimeout(resolve, 0));
  check('terminal_hydrates', lifecycle.includes('hydrate'));
  check('terminal_removes_overlay', !overlays.has('conv-1:turn-1'));
  check('hydrate_precedes_remove',
    lifecycle.indexOf('hydrate') >= 0
      && lifecycle.indexOf('hydrate') < lifecycle.lastIndexOf('remove'));
  console.log(output.join('\n'));
})();
"""


def _run_harness(section_path: str) -> str:
    proc = subprocess.run(
        ['node', '-e', _HARNESS, section_path],
        cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_swarm_push_uses_transient_turn_overlay():
    output = _run_harness(runtime_section_path('ui/swarm_push.js'))
    failures = [line for line in output.splitlines() if line.startswith('FAIL')]
    assert not failures, 'swarm overlay failures:\n' + output
    assert output.count('PASS') >= 12, output


@pytest.mark.skipif(not shutil.which('node'), reason='node is not installed')
def test_NEUTER_overlay_upsert_is_load_bearing():
    source = runtime_section('ui/swarm_push.js')
    needle = 'runtimeScope.ConversationTransientTurns?.upsert?.(conv, overlay);'
    assert source.count(needle) == 1
    with tempfile.TemporaryDirectory(prefix='tofu-swarm-overlay-neuter-') as temp:
        path = os.path.join(temp, 'swarm_push.js')
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(source.replace(
                needle, '/* overlay upsert neutered */', 1))
        output = _run_harness(path)
    assert 'FAIL overlay_created' in output, output


def test_projection_has_no_swarm_session_carryover():
    with open(TURN_PROJECTION_TS, encoding='utf-8') as handle:
        projection = handle.read()
    panel = runtime_section('ui/streaming_swarm_panel.js')
    push = runtime_section('ui/swarm_push.js')
    assert 'carrySwarmLiveState' not in projection
    assert 'swarmLiveByTurn' not in projection
    assert 'ConversationSwarmPresentation' in push
    reconciler = panel[panel.index('async function _reconcileStuckSwarmPanels'):]
    assert 'conv.messages' not in reconciler
    assert 'saveConversations' not in reconciler
    assert 'ConvView.replaceAll' not in reconciler
