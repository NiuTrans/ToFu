import { describe, expect, it } from 'vitest';
import type {
  AttemptRecord, ConversationQueueItem, TurnRecord,
} from '../../api/conversation-sync.generated';
import { createTurnState, type TurnState } from '../domain/turn-store';
import { selectConversationViewModel } from './conversation-view-model';

/* A queued Turn pair is durable before its attempt starts: the input Turn and a
 * pending output Turn whose attempt carries `queueBinding`. The queue ROW is a
 * separate read of that same commit, so it can lag or be absent. Neither
 * surface may be the sole justification for rendering something, or the output
 * Turn becomes a header-only ghost with no block at all. */

function humanTurn(id: string, content: string, ordinal: number): TurnRecord {
  return {
    turnId: id, presentationId: id, conversationId: 'c', laneId: 'main', ordinal,
    actor: 'human', kind: 'input', runId: '', status: 'completed',
    projectionRevision: 1, createdAt: 1, updatedAt: 2,
    projection: { content }, settlement: { cause: 'submitted', error: null },
  } as TurnRecord;
}

function queuedOutputTurn(id: string, parentTurnId: string, ordinal: number): TurnRecord {
  return {
    turnId: id, presentationId: id, conversationId: 'c', laneId: 'main', ordinal,
    parentTurnId, actor: 'assistant', kind: 'output', runId: '', status: 'pending',
    currentAttemptId: `attempt-${id}`, projectionRevision: 1,
    createdAt: 3, updatedAt: 4, projection: {},
    settlement: {},
  } as TurnRecord;
}

function queuedAttempt(turnId: string, queueId: string): AttemptRecord {
  return {
    attemptId: `attempt-${turnId}`, conversationId: 'c', turnId,
    commandId: 'cmd-1', taskId: '', operation: 'generate', status: 'pending',
    baseProjectionRevision: 0, resumeAnchor: {}, createdAt: 3,
    startedAt: null, settledAt: null,
    queueBinding: { queueId, state: 'pending' },
  } as unknown as AttemptRecord;
}

function buildState(
  turns: TurnRecord[], attempts: AttemptRecord[], queueItems: ConversationQueueItem[],
): TurnState {
  const state = createTurnState('c');
  state.laneOrder.main = turns.map((turn) => turn.turnId);
  state.turnsById = Object.fromEntries(turns.map((turn) => [turn.turnId, turn]));
  state.attemptsById = Object.fromEntries(
    attempts.map((attempt) => [attempt.attemptId, attempt]),
  );
  state.queueItems = queueItems;
  return state;
}

function mainTurns(state: TurnState) {
  return selectConversationViewModel(state, {}, {}).mainLane.turns;
}

function queueBlock(state: TurnState, turnId: string) {
  return mainTurns(state)
    .find((turn) => turn.turnId === turnId)
    ?.blocks.find((block) => block.kind === 'queue-status');
}

function queueRow(overrides: Partial<ConversationQueueItem> = {}): ConversationQueueItem {
  return {
    queueId: 'queue-1', position: 1, kind: 'real', priority: 100,
    timestamp: 1_792_472_341_899, text: 'queued text',
    inputTurnId: 'in', outputTurnId: 'out', attemptId: 'attempt-out',
    ...overrides,
  } as ConversationQueueItem;
}

describe('queued Turn presentation', () => {
  it('hides the pending output Turn and explains the wait on its input Turn', () => {
    const state = buildState(
      [humanTurn('in', 'queued text', 2), queuedOutputTurn('out', 'in', 3)],
      [queuedAttempt('out', 'queue-1')],
      [queueRow()],
    );
    expect(mainTurns(state).map((turn) => turn.turnId)).toEqual(['in']);
    const block = queueBlock(state, 'in');
    expect(block?.kind === 'queue-status' && block.value).toMatchObject({
      queueId: 'queue-1', position: 1, text: 'queued text',
    });
  });

  it('keeps explaining the wait when the queue row is absent from this read', () => {
    const state = buildState(
      [humanTurn('in', 'queued text', 2), queuedOutputTurn('out', 'in', 3)],
      [queuedAttempt('out', 'queue-1')],
      [],
    );
    expect(mainTurns(state).map((turn) => turn.turnId)).toEqual(['in']);
    const block = queueBlock(state, 'in');
    expect(block).toBeDefined();
    /* No invented position: 0 renders the unpositioned chip. */
    expect(block?.kind === 'queue-status' && block.value).toMatchObject({
      queueId: 'queue-1', position: 0, text: 'queued text',
    });
  });

  it('never renders a block-less pending output Turn', () => {
    const state = buildState(
      [humanTurn('in', 'queued text', 2), queuedOutputTurn('out', 'in', 3)],
      [queuedAttempt('out', 'queue-1')],
      [],
    );
    const rendered = mainTurns(state).filter((turn) => turn.turnId !== 'in');
    expect(rendered).toEqual([]);
  });
});
