/**
 * Construct the optimistic local echo of a composer submission — the user
 * bubble rendered IMMEDIATELY on send, before the turn command round-trips.
 *
 * The record rides the transient overlay (never the durable TurnStore) and
 * mirrors the authoritative human input turn the server creates, so the
 * acknowledgement swaps it with zero visible change. Every send exit path
 * removes it exactly once: accepted (the real turn replaces it),
 * queued/steered (the queue bar owns the trace), failed/aborted (the draft
 * returns to the composer).
 */
import type {
  TurnConversationReference,
  TurnDocumentAttachment,
  TurnImageAttachment,
  TurnProjection,
  TurnVideoAttachment,
} from '../../api/conversation-sync.generated';
import type { TransientTurnRecord } from '../domain/transient-turn';

export interface CreateOptimisticUserTurnInput {
  conversationId: string;
  commandId: string;
  text: string;
  timestamp: number;
  images?: ReadonlyArray<TurnImageAttachment>;
  pdfTexts?: ReadonlyArray<TurnDocumentAttachment>;
  videos?: ReadonlyArray<TurnVideoAttachment>;
  replyQuotes?: ReadonlyArray<string>;
  convRefs?: ReadonlyArray<TurnConversationReference>;
  contextSnapshot?: Record<string, unknown>;
}

export function optimisticUserTurnId(commandId: string): string {
  return `transient:outgoing:${commandId}`;
}

export function createOptimisticUserTurn(
  input: CreateOptimisticUserTurnInput,
): TransientTurnRecord {
  if (!input.conversationId || !input.commandId) {
    throw new Error('Optimistic user Turns require conversationId and commandId.');
  }
  const projection: TurnProjection = {
    content: input.text,
    timestamp: input.timestamp,
    segments: [{
      type: 'text',
      blockId: 'text:terminal',
      text: input.text,
      deliverable: true,
      terminal: true,
    }],
  };
  if (input.images?.length) projection.images = [...input.images];
  if (input.pdfTexts?.length) projection.pdfTexts = [...input.pdfTexts];
  if (input.videos?.length) projection.videos = [...input.videos];
  if (input.replyQuotes?.length) projection.replyQuotes = [...input.replyQuotes];
  if (input.convRefs?.length) projection.convRefs = [...input.convRefs];
  if (input.contextSnapshot) {
    projection.contextSnapshot = {
      blockId: 'turn-context',
      snapshot: { ...input.contextSnapshot },
    };
  }
  return {
    turnId: optimisticUserTurnId(input.commandId),
    conversationId: input.conversationId,
    laneId: 'main',
    parentTurnId: null,
    ordinal: Number.MAX_SAFE_INTEGER,
    actor: 'human',
    kind: 'input',
    runId: '',
    status: 'completed',
    currentAttemptId: null,
    projection,
    projectionRevision: 1,
    settlement: { outcome: 'completed', cause: 'submitted', resumeOptions: [] },
    createdAt: input.timestamp,
    updatedAt: input.timestamp,
  };
}
