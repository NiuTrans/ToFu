/** Initial/history transport working set follows visible reading demand.
 * Pure policy: no globals, timers, caches, or durable-history truncation.
 * A byte budget is soft for one indivisible Turn; storage owns exact cursors.
 */
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
  const windowBytes = Math.min(16 * 1024 * 1024, Math.max(256 * 1024,
    Math.ceil((height / 800) * (width / 960) * 1024 * 1024)));
  return { windowLimit, windowBytes };
}
