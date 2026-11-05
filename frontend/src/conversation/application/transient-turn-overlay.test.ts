import { describe, expect, it } from 'vitest';
import type { TurnRecord } from '../../api/conversation-sync.generated';
import { createTurnStore } from '../domain/turn-store';
import { createOptimisticTurnPair } from './optimistic-user-turn';
import { createSwarmPushPresentationController } from './swarm-presentation-overlay';
import { createTransientTurnOverlay } from './transient-turn-overlay';

const turn = (turnId: string, ordinal: number, overrides: Partial<TurnRecord> = {}): TurnRecord => ({
  turnId, presentationId: turnId, conversationId: 'conv', laneId: 'main', parentTurnId: null,
  ordinal, actor: 'assistant', kind: 'reply', runId: 'run', status: 'completed',
  currentAttemptId: null, projection: { segments: [] }, projectionRevision: 1,
  settlement: {}, createdAt: ordinal, updatedAt: ordinal, ...overrides,
} as TurnRecord);

const history = turn('turn-5', 5, { status: 'interrupted', projection: { segments: [], toolRounds: [{
  roundNum: 1, toolName: 'spawn_agents', toolCallId: 'call-spawn', _swarm: true,
  _swarmRunId: 'run-5', _swarmAgents: [{ id: 'agent', status: 'running' }],
}] } as TurnRecord['projection'] });
const input = turn('turn-6', 6, { actor: 'human', kind: 'input' });
const live = turn('turn-7', 7, { status: 'running', currentAttemptId: 'attempt-7' });

function fixture() {
  const store = createTurnStore('conv');
  const overlays = createTransientTurnOverlay();
  const conversation = { id: 'conv' };
  const swarm = createSwarmPushPresentationController({
    findConversation: () => conversation,
    readTurnState: () => store.getState(),
    readOverlay: (conversationId, turnId) => overlays.get(conversationId, turnId),
    upsertOverlay: (_conversation, overlay) => overlays.upsert(overlay),
    removeOverlay: (_conversation, turnId) => { overlays.remove('conv', turnId); },
    hydrateConversation: () => null,
    attachAutoContinue: () => {},
    reducePhase: () => {},
    reduceAgent: () => {},
    debug: () => {},
    warn: () => {},
  });
  const showTurns = (...turns: TurnRecord[]) => store.dispatch({ type: 'snapshot', snapshot: {
    authoritativeFull: true, conversationRevision: store.getState().conversationRevision + 1,
    turns, attempts: [],
  } });
  // Same two passes as the retained surface render.
  const render = () => swarm.presentation.compose(
    conversation, store.getState(), overlays.compose(store.getState()),
  );
  return { store, overlays, swarm, conversation, showTurns, render };
}

describe('transient Turn overlay lane composition', () => {
  it('never appends a swarm copy of evicted history below the live Turn', () => {
    const f = fixture();
    f.showTurns(history, input, live);
    // The stuck-swarm reconciler confirms the loaded historical roster.
    expect(f.swarm.presentation.update(f.conversation, 'turn-5', (projection) => {
      projection.toolRounds![0]._swarmActive = true;
    })).not.toBeNull();
    expect(f.render().laneOrder.main).toEqual(['turn-5', 'turn-6', 'turn-7']);

    // A byte-bounded authoritative tail snapshot evicts that history page.
    f.showTurns(input, live);
    const tail = f.render();
    expect(tail.laneOrder.main).toEqual(['turn-6', 'turn-7']);
    expect(tail.turnsById['turn-5']).toBeUndefined();

    // Paging the history back in restores the copy at its own position.
    f.showTurns(history, input, live);
    const restored = f.render();
    expect(restored.laneOrder.main).toEqual(['turn-5', 'turn-6', 'turn-7']);
    expect(restored.turnsById['turn-5']!.projection.toolRounds![0]._swarmActive).toBe(true);
  });

  it('still appends lifecycle Turns and accepted Turns beyond the durable tail', () => {
    const f = fixture();
    f.showTurns(input, live);
    const pair = createOptimisticTurnPair({
      conversationId: 'conv', commandId: 'command', text: 'next', timestamp: 8,
    });
    f.overlays.upsert(pair.inputTurn);
    f.overlays.upsert(pair.outputTurn);
    expect(f.render().laneOrder.main)
      .toEqual(['turn-6', 'turn-7', pair.inputTurn.turnId, pair.outputTurn.turnId]);

    const accepted = createTransientTurnOverlay();
    accepted.upsert(turn('turn-8', 8));
    expect(accepted.compose(f.store.getState()).laneOrder.main)
      .toEqual(['turn-6', 'turn-7', 'turn-8']);
  });
});
