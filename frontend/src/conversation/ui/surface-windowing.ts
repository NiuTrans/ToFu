/**
 * Conversation DOM budget constants and option validation. Does not truncate
 * TurnStore or retain DOM.
 */
import type { ConversationWindowOptions } from './surface-contracts';

export const CONVERSATION_DOM_WINDOW_MAX_TURNS = 80;

export const CONVERSATION_DOM_WINDOW_BATCH_TURNS = 20;

export const CONVERSATION_HISTORY_PAGE_TURNS = 64;

/* A single Turn can carry thousands of blocks (agentic loops accumulate one
 * tool block plus one thinking block per round). The Turn-level window above
 * does not bound that case: rendering every block of such a Turn froze the
 * main thread for seconds on conversation switch (>100k DOM nodes). Cap the
 * blocks mounted per Turn; earlier blocks expand in batches on demand. */
export const CONVERSATION_BLOCK_WINDOW_MAX_BLOCKS = 200;

export const CONVERSATION_BLOCK_WINDOW_BATCH_BLOCKS = 100;

export const CONVERSATION_BLOCK_WINDOW_HARD_CEILING = 500;

// Two viewports cover navigation/scroll overscan. Tool/thinking/text blocks
// vary in height; this estimate sizes first paint, never deletes history.
export const CONVERSATION_BLOCK_ESTIMATED_HEIGHT = 64;

export const CONVERSATION_TURN_ESTIMATED_HEIGHT = 160;

export const CONVERSATION_TURN_INITIAL_MINIMUM = 8;

export const CONVERSATION_BLOCK_OVERSCAN_VIEWPORTS = 2;

export const CONVERSATION_BLOCK_INITIAL_MINIMUM = 20;

export function positiveInteger(value: unknown, fallback: number): number {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback;
}

export function conversationWindowOptions(
  options: ConversationWindowOptions | undefined,
): Required<Pick<ConversationWindowOptions,
  'maxTurns' | 'batchSize' | 'maxBlocksPerTurn' | 'blockBatchSize'>> {
  const maxTurns = Math.min(
    positiveInteger(options?.maxTurns, CONVERSATION_DOM_WINDOW_MAX_TURNS),
    CONVERSATION_DOM_WINDOW_MAX_TURNS,
  );
  const batchSize = Math.min(
    positiveInteger(options?.batchSize, CONVERSATION_DOM_WINDOW_BATCH_TURNS),
    maxTurns,
    CONVERSATION_DOM_WINDOW_BATCH_TURNS,
  );
  const maxBlocksPerTurn = Math.min(
    positiveInteger(
      options?.maxBlocksPerTurn, CONVERSATION_BLOCK_WINDOW_MAX_BLOCKS,
    ),
    CONVERSATION_BLOCK_WINDOW_HARD_CEILING,
  );
  const blockBatchSize = Math.min(
    positiveInteger(
      options?.blockBatchSize, CONVERSATION_BLOCK_WINDOW_BATCH_BLOCKS,
    ),
    maxBlocksPerTurn,
    CONVERSATION_BLOCK_WINDOW_HARD_CEILING,
  );
  return { maxTurns, batchSize, maxBlocksPerTurn, blockBatchSize };
}
