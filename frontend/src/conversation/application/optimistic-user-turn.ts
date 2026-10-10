/**
 * Construct the optimistic local Turn pair of a composer submission — the
 * user bubble and its stable assistant container render before the command
 * round-trips.
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
  TurnMediaAttachment,
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
  attachments?: ReadonlyArray<TurnMediaAttachment>;
  replyQuotes?: ReadonlyArray<string>;
  convRefs?: ReadonlyArray<TurnConversationReference>;
  contextSnapshot?: Record<string, unknown>;
}

export function optimisticUserTurnId(commandId: string): string {
  return `transient:outgoing:${commandId}`;
}

export function optimisticAssistantTurnId(commandId: string): string {
  return `transient:outgoing:${commandId}:output`;
}

export interface OptimisticTurnPair {
  inputTurn: TransientTurnRecord;
  outputTurn: TransientTurnRecord;
}

/**
 * Re-label the optimistic assistant container as the send command advances
 * (preparing → connecting → translating). The same transient Turn identity
 * is preserved so the keyed renderer updates one bubble in place instead of
 * stacking a separate status row.
 */
export function withOptimisticAssistantPreparation(
  turn: TransientTurnRecord,
  phase: 'preparing' | 'connecting' | 'translating',
  label: string,
): TransientTurnRecord {
  return {
    ...turn,
    transientPresentation: {
      kind: 'preparation',
      phase,
      label,
      detail: '',
    },
  };
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
  if (input.attachments?.length) projection.attachments = [...input.attachments];
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
    presentationId: `${input.commandId}:input`,
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

export function createOptimisticTurnPair(
  input: CreateOptimisticUserTurnInput,
): OptimisticTurnPair {
  const inputTurn = createOptimisticUserTurn(input);
  const outputTurnId = optimisticAssistantTurnId(input.commandId);
  return {
    inputTurn,
    outputTurn: withOptimisticAssistantPreparation(
      {
        turnId: outputTurnId,
        presentationId: `${input.commandId}:output`,
        conversationId: input.conversationId,
        laneId: 'main',
        parentTurnId: inputTurn.turnId,
        ordinal: Number.MAX_SAFE_INTEGER,
        actor: 'assistant',
        kind: 'reply',
        runId: '',
        status: 'pending',
        currentAttemptId: `transient:attempt:${input.commandId}`,
        projection: { segments: [], timestamp: input.timestamp },
        projectionRevision: 1,
        settlement: {},
        createdAt: input.timestamp,
        updatedAt: input.timestamp,
      },
      'preparing',
      'Preparing response',
    ),
  };
}
