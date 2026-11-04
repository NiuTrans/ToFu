import { describe, expect, it } from 'vitest';
import type { AttemptRecord, TurnRecord } from '../../api/conversation-sync.generated';
import { createTurnState, type TurnState } from '../domain/turn-store';
import { selectConversationViewModel } from './conversation-view-model';

/* `report_to_user` appends a settled report Turn behind the Coordinator reply
 * that is still publishing it. The live reply keeps growing (tool rows, steered
 * messages), so it must read below the report, and the order must not change
 * when the reply later settles. */

function turn(id: string, ordinal: number, fields: Partial<TurnRecord>): TurnRecord {
  return {
    turnId: id, presentationId: id, conversationId: 'c', laneId: 'main', ordinal,
    actor: 'assistant', kind: 'output', runId: '', status: 'completed',
    projectionRevision: 1, createdAt: ordinal * 10, updatedAt: ordinal * 10 + 1,
    projection: { content: id }, settlement: {},
    ...fields,
  } as TurnRecord;
}

function attempt(turnId: string, fields: Partial<AttemptRecord> = {}): AttemptRecord {
  return {
    attemptId: `attempt-${turnId}`, conversationId: 'c', turnId,
    commandId: `cmd-${turnId}`, taskId: `task-${turnId}`, operation: 'generate',
    status: 'running', baseProjectionRevision: 0, resumeAnchor: {}, createdAt: 10,
    startedAt: 10, settledAt: null, ...fields,
  } as unknown as AttemptRecord;
}

function state(turns: TurnRecord[], attempts: AttemptRecord[]): TurnState {
  const next = createTurnState('c');
  next.laneOrder.main = turns.map((item) => item.turnId);
  next.turnsById = Object.fromEntries(turns.map((item) => [item.turnId, item]));
  next.attemptsById = Object.fromEntries(attempts.map((item) => [item.attemptId, item]));
  return next;
}

const order = (value: TurnState) => selectConversationViewModel(value, {}, {})
  .mainLane.turns.map((item) => item.turnId);

const request = turn('request', 1, { actor: 'human', kind: 'input' });
const report = (id: string, ordinal: number, createdAt: number) => turn(id, ordinal, {
  kind: 'coordinator_report.progress', createdAt, updatedAt: createdAt,
  projection: { content: id, origin: { blockId: 'origin', initiator: 'brain' } },
});

describe('coordinator report placement', () => {
  it('reads reports published by a live reply above that reply', () => {
    const reply = turn('reply', 2, {
      parentTurnId: 'request', status: 'running', currentAttemptId: 'attempt-reply',
    });
    expect(order(state(
      [request, reply, report('report-1', 3, 50), report('report-2', 4, 60)],
      [attempt('reply')],
    ))).toEqual(['request', 'report-1', 'report-2', 'reply']);
  });

  it('keeps the same order after the reply settles', () => {
    const reply = turn('reply', 2, {
      parentTurnId: 'request', status: 'completed', currentAttemptId: 'attempt-reply',
      updatedAt: 999,
    });
    expect(order(state(
      [request, reply, report('report-1', 3, 50)],
      [attempt('reply', { status: 'completed', settledAt: 70 })],
    ))).toEqual(['request', 'report-1', 'reply']);
  });

  it('leaves a report published after the reply settled in durable order', () => {
    const reply = turn('reply', 2, {
      parentTurnId: 'request', status: 'completed', currentAttemptId: 'attempt-reply',
      updatedAt: 999,
    });
    expect(order(state(
      [request, reply, report('later', 3, 80)],
      [attempt('reply', { status: 'completed', settledAt: 70 })],
    ))).toEqual(['request', 'reply', 'later']);
  });

  it('never moves a report above a human Turn or a queued reply', () => {
    const queued = turn('queued', 2, {
      parentTurnId: 'request', status: 'pending', currentAttemptId: 'attempt-queued',
    });
    expect(order(state(
      [request, report('after-input', 2, 15)], [],
    ))).toEqual(['request', 'after-input']);
    const value = state(
      [request, queued, report('after-queued', 3, 40)],
      [attempt('queued', { status: 'pending', queueBinding: { queueId: 'q', state: 'pending' } })],
    );
    expect(order(value)).toEqual(['request', 'after-queued']);
  });
});
