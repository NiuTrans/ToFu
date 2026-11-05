/**
 * Conversation DOM budget constants, option validation and the pure geometric
 * residency planner (`planResidentRange`) that scroll demand applies to Turn
 * lanes and oversized block ranges. Does not truncate TurnStore, read layout
 * or retain DOM; the Surface measures and commits.
 */
import type { ConversationWindowOptions } from './surface-contracts';

export const CONVERSATION_DOM_WINDOW_MAX_TURNS = 80;

export const CONVERSATION_DOM_WINDOW_BATCH_TURNS = 20;

export const CONVERSATION_HISTORY_PAGE_TURNS = 64;

/* Older history is requested while less than this many viewports of loaded
 * content (mounted Turns above the reader plus loaded Turns not yet mounted)
 * remain above the reader. Pages took 0.15-1.3 s on the loaded production host
 * (p99 2.5 s) and a brisk reader (2400 px/s) crosses a viewport in about
 * 0.35 s; twelve viewports also keep the depth the former 20-unmounted-Turn
 * rule gave small-Turn chats. */
export const CONVERSATION_HISTORY_READ_AHEAD_VIEWPORTS = 12;

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

/* Scroll demand keeps Turn residency geometric: history mounts while this many
 * viewports of rendered content still separate the reader from the mounted
 * edge. Chrome disables native scroll anchoring at scroll offset 0, and the
 * programmatic correction needed there cancels in-flight smooth/momentum
 * scrolling, so the reader must never reach the edge to trigger a mount. */
export const CONVERSATION_HISTORY_PREFETCH_VIEWPORTS = 1.5;

/* Mounted Turns entirely farther than this from the viewport retire. Its gap
 * to the prefetch distance keeps mounting and retirement from undoing each
 * other, and no visible Turn can ever be retired by scroll demand. */
export const CONVERSATION_HISTORY_RETAIN_VIEWPORTS = 3;

/* Mounting refills this far past the prefetch distance and retirement waits
 * this far past the retention distance, so a steady reader commits about once
 * per viewport travelled instead of once per Turn crossing a threshold. */
export const CONVERSATION_HISTORY_HYSTERESIS_VIEWPORTS = 1;

/* Consecutive frames a single reader gesture may keep refilling its prefetch
 * buffer without further scroll input. Each frame mounts one paging batch. */
export const CONVERSATION_HISTORY_FILL_FRAMES = 16;

export interface ResidentRange {
  start: number;
  end: number;
}

/** Viewport-relative extent of a mounted item; null when it has no box. */
export type ResidentBounds = (offset: number) => { top: number; bottom: number } | null;

export interface ResidentPlan {
  /** Replacement range, or null when residency is already satisfied. */
  range: ResidentRange | null;
  /** The reader is within the prefetch distance of this mounted edge. */
  nearEdge: boolean;
}

/**
 * Plan one ordered mounted range from measured geometry. While less than the
 * prefetch distance remains mounted ahead of the reader, grows toward them by
 * the items estimated (from the mounted ones) to refill the hysteresis band,
 * at most `batch` per frame. Retires trailing items beyond the retention
 * distance only once content extends past the band beyond it, and honours
 * `cap` by retiring trailing items beyond the prefetch distance, never by
 * retiring an item the reader can see.
 */
export function planResidentRange(
  range: ResidentRange,
  total: number,
  direction: 'earlier' | 'later',
  bounds: ResidentBounds,
  limits: {
    viewportHeight: number; cap: number; batch: number; edgeLimit: number;
    /** Extra distance the reader may cover before the next frame. */
    lookahead?: number;
  },
): ResidentPlan {
  const count = range.end - range.start;
  const height = limits.viewportHeight;
  if (count <= 0 || !(height > 0)) return { range: null, nearEdge: false };
  const prefetch = height * CONVERSATION_HISTORY_PREFETCH_VIEWPORTS + Math.max(0, limits.lookahead ?? 0);
  const band = height * CONVERSATION_HISTORY_HYSTERESIS_VIEWPORTS;
  const retain = height * CONVERSATION_HISTORY_RETAIN_VIEWPORTS;
  const earlier = direction === 'earlier';
  // Offsets ordered from the reader's leading edge to the trailing edge.
  const at = (rank: number): number => (earlier ? rank : count - 1 - rank);
  let lead: { top: number; bottom: number } | null = null;
  let leadRank = 0;
  for (; leadRank < count && !lead; leadRank += 1) lead = bounds(at(leadRank));
  if (!lead) return { range: null, nearEdge: false };
  leadRank -= 1;
  let far = lead;
  let farRank = leadRank;
  for (let rank = count - 1; rank > leadRank; rank -= 1) {
    const box = bounds(at(rank));
    if (box) { far = box; farRank = rank; break; }
  }
  // A lane edge low in the viewport (or beyond it) is not where the reader is
  // heading; mounting there would move visible content.
  const ahead = earlier ? -lead.top : lead.bottom - height;
  const edgeInReach = earlier ? lead.top <= limits.edgeLimit
    : lead.bottom >= height - limits.edgeLimit;
  const nearEdge = edgeInReach && ahead < prefetch;
  const available = earlier ? range.start : total - range.end;
  let grow = 0;
  if (nearEdge && available > 0) {
    const span = earlier ? far.bottom - lead.top : lead.bottom - far.top;
    const average = Math.max(1, span / (farRank - leadRank + 1));
    grow = Math.min(available, limits.batch,
      Math.max(1, Math.ceil((prefetch + band - ahead) / average)));
  }
  const beyond = (box: { top: number; bottom: number }, distance: number): boolean => (
    earlier ? box.top > height + distance : box.bottom < -distance);
  // Trailing items without a box retire with the run beyond them.
  const trailingBeyond = (distance: number): number => {
    let retired = 0;
    for (let rank = count - 1; rank > 0; rank -= 1) {
      const box = bounds(at(rank));
      if (box && !beyond(box, distance)) break;
      retired += 1;
    }
    return retired;
  };
  // A growth commit retires what lies beyond retention anyway; otherwise
  // retirement waits for the band so it does not commit per Turn.
  let retire = grow > 0 || beyond(far, retain + band) ? trailingBeyond(retain) : 0;
  let size = count + grow - retire;
  if (size > limits.cap) {
    const extra = Math.min(size - limits.cap, trailingBeyond(prefetch) - retire);
    retire += extra;
    size -= extra;
  }
  if (size > limits.cap) grow = Math.max(0, grow - (size - limits.cap));
  if (grow === 0 && retire === 0) return { range: null, nearEdge };
  return {
    range: earlier
      ? { start: range.start - grow, end: range.end - retire }
      : { start: range.start + retire, end: range.end + grow },
    nearEdge,
  };
}

/** Measured loaded history above the reader for one lane. */
export interface HistoryReadAheadGeometry {
  viewportHeight: number;
  /** Viewport-relative top of the loaded content mounted for the lane. */
  mountedTop: number;
  /** Loaded Turns above the mounted range. */
  unmountedTurns: number;
  /** Mean measured height of the mounted Turns. */
  averageTurnHeight: number;
  /** Extra distance the reader may cover before the next frame. */
  lookahead?: number;
  /** Due distance in viewports; the read-ahead distance by default. */
  viewports?: number;
}

/**
 * Whether the next older page is due: less than the read-ahead distance of
 * loaded content remains above the viewport top. Loaded but unmounted Turns
 * count at the mean mounted height; they are readable without a round trip.
 */
export function olderHistoryDue(geometry: HistoryReadAheadGeometry): boolean {
  const height = geometry.viewportHeight;
  if (!(height > 0) || !Number.isFinite(geometry.mountedTop)) return false;
  const loadedAbove = Math.max(0, -geometry.mountedTop)
    + Math.max(0, geometry.unmountedTurns) * Math.max(0, geometry.averageTurnHeight || 0);
  return loadedAbove < height * (geometry.viewports ?? CONVERSATION_HISTORY_READ_AHEAD_VIEWPORTS)
    + Math.max(0, geometry.lookahead ?? 0);
}

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
