/** Sidebar activity over canonical Turns and explicit shell display metadata.
 * All modes use this selector. It owns no transport, cache or settlement state. */
import type { TurnRecord } from '../../api/conversation-sync.generated';
import { presentConversationRateLimit } from './live-phase';
import { goalRunAffiliations, goalRunConcluded } from './goal-run-affiliation';

const FINISH_NORMAL = new Set(['stop', 'end_turn', 'stop_sequence', 'tool_use', 'tool_calls']);
const FINISH_ERROR = new Set(['error', 'server_offline']);

export interface ConversationActivityInput {
  turns: ReadonlyArray<TurnRecord>;
  streaming: boolean;
  livePhase?: unknown;
  syncHealth?: string;
  autopilotSummaries?: Readonly<Record<string, Readonly<Record<string, unknown>>>>;
  /** Catalog-only fallback before this conversation's TurnStore is hydrated. */
  catalog?: { lastMsgRole?: string; lastFinishReason?: string; lastMsgError?: unknown; lastMsgHasOutput?: boolean };
}

export function presentConversationActivity(input: ConversationActivityInput) {
  const { turns, streaming } = input;
  let latest: TurnRecord | undefined;
  for (let index = turns.length - 1; index >= 0; index -= 1) {
    if (turns[index].actor === 'assistant' || turns[index].actor === 'planner') {
      latest = turns[index]; break;
    }
  }
  const awaitingHuman = streaming && Boolean(latest?.projection.toolRounds?.some(
    round => round?.status === 'awaiting_human',
  ));
  let errored = false;
  let incomplete = false;
  let waiting = false;
  if (!streaming && latest) {
    const projection = latest.projection;
    // Pre-v3 import evidence only; modern settlement status remains authority.
    const legacy = projection as typeof projection & { finishReason?: string };
    const finishReason = typeof legacy.finishReason === 'string' ? legacy.finishReason : '';
    const hardError = latest.status === 'failed' || Boolean(latest.settlement?.error) || Boolean(projection.error);
    waiting = Boolean(projection.waitingOn) && !hardError;
    if (hardError) errored = true;
    else if (latest.status === 'interrupted' || latest.status === 'truncated') incomplete = true;
    else if (latest.status !== 'completed') {
      if (FINISH_ERROR.has(finishReason)) errored = true;
      else if (finishReason) incomplete = !FINISH_NORMAL.has(finishReason);
      else incomplete = !Boolean(projection.content || projection.thinking
        || projection.toolRounds?.length || projection.imageGeneration?.results?.length || projection.segments?.length);
    }
    // A concluded run explains a soft phase interruption, never a hard failure.
    if ((incomplete || errored) && !hardError && goalRunConcluded(
      goalRunAffiliations(turns).get(latest.turnId), input.autopilotSummaries,
    )) { incomplete = false; errored = false; }
  } else if (!streaming && !latest && input.catalog?.lastMsgRole === 'assistant') {
    const catalog = input.catalog;
    const finishReason = catalog.lastFinishReason || '';
    if (catalog.lastMsgError || FINISH_ERROR.has(finishReason)) errored = true;
    else if (finishReason) incomplete = !FINISH_NORMAL.has(finishReason);
    else incomplete = !catalog.lastMsgHasOutput;
  }
  if (waiting) incomplete = false;
  return {
    streaming, awaitingHuman, waiting, errored, incomplete,
    rateLimited: Boolean(streaming && presentConversationRateLimit(input.livePhase)),
    unconfirmed: streaming && ['recovering', 'degraded', 'offline'].includes(input.syncHealth || ''),
  };
}
