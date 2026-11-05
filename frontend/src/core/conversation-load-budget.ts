/** Initial/history transport working set follows visible reading demand.
 * Pure policy: no globals, timers, caches, or durable-history truncation.
 * A byte budget is soft for one indivisible Turn; storage owns exact cursors.
 */
const MAX_WINDOW_BYTES = 16 * 1024 * 1024;

/* A chained history page may grow to this many viewport byte budgets. */
export const CONVERSATION_HISTORY_BYTE_GROWTH = 4;

export interface ConversationSnapshotViewport {
  width: number;
  height: number;
}

export function conversationLoadBudget(viewport?: ConversationSnapshotViewport): {
  windowLimit: number;
  windowBytes: number;
} {
  const finitePositive = (value: number | undefined, fallback: number): number =>
    typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : fallback;
  const height = finitePositive(viewport?.height, 800);
  const width = finitePositive(viewport?.width, 960);
  // Two lightweight Turns per estimated reading row plus one surrounding pair.
  // Tall panes can request the existing complete 96-Turn tail; small panes do
  // not pay for history they cannot display. Bulk within one Turn is byte-bound.
  const windowLimit = Math.min(96, Math.max(8, Math.ceil(height / 160) * 2 + 4));
  const windowBytes = Math.min(MAX_WINDOW_BYTES, Math.max(256 * 1024,
    Math.ceil((height / 800) * (width / 960) * 1024 * 1024)));
  return { windowLimit, windowBytes };
}

/**
 * Byte budget for the continuous history page that chains from `page`. Every
 * page admits one indivisible Turn, so a viewport budget smaller than one
 * agent Turn pays a round trip per Turn, and another for each small input Turn
 * whose reply does not fit beside it. A page cut short by bytes (fewer Turns
 * than its count while more remain) doubles the next budget, up to
 * CONVERSATION_HISTORY_BYTE_GROWTH viewport budgets and the contract ceiling;
 * a page bounded by its Turn count returns to the viewport budget.
 */
export function nextHistoryPageBytes(viewportBytes: number, page: {
  windowBytes: number;
  turns: number;
  limit: number;
  hasMore: boolean;
}): number {
  if (!page.hasMore || page.turns >= page.limit) return viewportBytes;
  return Math.min(MAX_WINDOW_BYTES, viewportBytes * CONVERSATION_HISTORY_BYTE_GROWTH,
    Math.max(viewportBytes, page.windowBytes) * 2);
}
