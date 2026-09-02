/**
 * Construct a browser-lifecycle status Turn for work visible before a durable
 * backend Turn exists. The result uses the normal Turn/block identity path and
 * carries no transcript content that could enter model context.
 */
import type { TransientTurnRecord } from '../domain/transient-turn';

export interface CreateTransientStatusTurnInput {
  conversationId: string;
  turnId: string;
  phase: string;
  label: string;
  detail?: string;
  timestamp?: number;
}

export function createTransientStatusTurn(
  input: CreateTransientStatusTurnInput,
): TransientTurnRecord {
  if (!input.conversationId || !input.turnId) {
    throw new Error('Transient status Turns require conversationId and turnId.');
  }
  const timestamp = input.timestamp ?? Date.now();
  return {
    turnId: input.turnId,
    conversationId: input.conversationId,
    laneId: 'main',
    parentTurnId: null,
    ordinal: Number.MAX_SAFE_INTEGER,
    actor: 'assistant',
    kind: 'lifecycle_status',
    runId: input.turnId,
    status: 'running',
    currentAttemptId: null,
    projection: { segments: [], timestamp },
    projectionRevision: timestamp,
    settlement: {},
    transientPresentation: {
      kind: 'preparation',
      phase: input.phase,
      label: input.label,
      detail: input.detail ?? '',
    },
    createdAt: timestamp,
    updatedAt: timestamp,
  };
}
