/**
 * Chronological anchoring for activity-timeline diagnostic rows.
 *
 * Responsibility: decide WHERE one activity entry lands inside a turn's
 * block list.  Entry points: ``activityAnchorWindows`` (chronology bounds
 * from the durable timeline), ``activityAnchorIndex`` (insertion index for
 * one entry), ``resolveActivityAnchorWindow`` (synthesized-row window
 * inheritance).  Dependencies: contract types only; no DOM, no globals.
 *
 * ``llmRound`` is only monotonic within ONE execution attempt: a
 * continued/resumed turn restarts the model round counter at 0, so an
 * unbounded round scan can anchor an earlier attempt's error after content
 * that did not exist yet (the resumed run's fresh round-0/1/2 tool blocks
 * sort below a round-19 failure).  Tool rows in the timeline pin tool
 * blocks to real chronology: a diagnostic sits after the latest tool row
 * that precedes it and before the earliest tool row that follows it.
 * Round-riding semantics stay intact inside that window, and the round scan
 * itself is attempt-aware so a stale counter can never leak across
 * attempts even when the window bounds are missing.
 */
import type {
  TurnActivityEntry,
  TurnActivityTimeline,
} from '../../api/conversation-sync.generated';
import type { ConversationBlockViewModel } from './conversation-view-model';

/** Activity entry plus the optional parent row a synthesized copy inherits
 * its chronology window from (gateway validation children). */
export type AnchoredActivityEntry = TurnActivityEntry & {
  anchorEntryId?: string;
};

export interface ActivityAnchorWindow {
  afterToolCallId?: string;
  beforeToolCallId?: string;
}

export function activityEntryIsVisible(entry: TurnActivityEntry): boolean {
  return entry.severity !== 'info' || (
    entry.reasonCode === 'context_compaction'
    && Boolean(entry.archiveId)
  );
}

const ACTIVITY_ENTRY_ID_PREFIX = 'activity:';

/** Durable entry ids are ``activity:{attemptId}:{seq}``; legacy/other shapes
 * yield '' so callers degrade to the compatibility path. */
export function activityEntryAttemptId(
  entry: Pick<TurnActivityEntry, 'id'>,
): string {
  const id = typeof entry.id === 'string' ? entry.id : '';
  if (!id.startsWith(ACTIVITY_ENTRY_ID_PREFIX)) return '';
  const rest = id.slice(ACTIVITY_ENTRY_ID_PREFIX.length);
  const separator = rest.indexOf(':');
  return separator > 0 ? rest.slice(0, separator) : '';
}

/** Owning attempt of one block: rounds carry execution identity directly,
 * segments carry it on the block source, and already-anchored diagnostic
 * rows carry it inside their durable entry id. '' means unstamped legacy. */
export function activityBlockAttemptId(
  block: ConversationBlockViewModel,
): string {
  if (block.kind === 'tool' || block.kind === 'program') {
    const fromRound = block.round?.attemptId;
    if (typeof fromRound === 'string' && fromRound) return fromRound;
  }
  if (block.kind === 'activity-event') {
    const fromEntry = activityEntryAttemptId(block.value);
    if (fromEntry) return fromEntry;
  }
  const fromSource = (block.source as { attemptId?: unknown }).attemptId;
  return typeof fromSource === 'string' ? fromSource : '';
}

export function activityBlockLlmRound(
  block: ConversationBlockViewModel,
): number | null {
  const source = block.source as { llmRound?: number | null };
  const round = block.kind === 'tool'
    ? block.round?.llmRound ?? source.llmRound
    : block.kind === 'program' ? block.round.llmRound : source.llmRound;
  return typeof round === 'number' && Number.isInteger(round) ? round : null;
}

/** A block joins an attempt-scoped scan when it is unstamped (legacy) or
 * provably belongs to the entry's own attempt. */
function sameAttemptScope(
  entryAttempt: string,
  block: ConversationBlockViewModel,
): boolean {
  if (!entryAttempt) return true;
  const blockAttempt = activityBlockAttemptId(block);
  return !blockAttempt || blockAttempt === entryAttempt;
}

export function activityAnchorWindows(
  timeline: TurnActivityTimeline,
  blocks: ReadonlyArray<ConversationBlockViewModel>,
): Map<string, ActivityAnchorWindow> {
  const toolCallIds = new Set<string>();
  for (const block of blocks) {
    if (block.kind === 'tool' && block.toolCallId) {
      toolCallIds.add(block.toolCallId);
    }
  }
  const entries = timeline.entries ?? [];
  const windows = new Map<string, ActivityAnchorWindow>();
  let spine = '';
  for (const entry of entries) {
    if (activityEntryIsVisible(entry)) {
      windows.set(entry.id, spine ? { afterToolCallId: spine } : {});
    }
    if (entry.toolCallId && toolCallIds.has(entry.toolCallId)) {
      spine = entry.toolCallId;
    }
  }
  let limit = '';
  for (let index = entries.length - 1; index >= 0; index -= 1) {
    const entry = entries[index];
    if (activityEntryIsVisible(entry)) {
      const anchorWindow = windows.get(entry.id);
      if (anchorWindow && limit) anchorWindow.beforeToolCallId = limit;
    }
    if (entry.toolCallId && toolCallIds.has(entry.toolCallId)) {
      limit = entry.toolCallId;
    }
  }
  return windows;
}

/** Synthesized rows (gateway validation children) are not raw timeline
 * entries, so the windows map has no key for them: they inherit the parent
 * row's chronology window instead of degrading to an unbounded scan. */
export function resolveActivityAnchorWindow(
  windows: ReadonlyMap<string, ActivityAnchorWindow>,
  entry: AnchoredActivityEntry,
): ActivityAnchorWindow | undefined {
  return windows.get(entry.id)
    ?? (entry.anchorEntryId ? windows.get(entry.anchorEntryId) : undefined);
}

export function activityAnchorIndex(
  blocks: ReadonlyArray<ConversationBlockViewModel>,
  entry: TurnActivityEntry,
  anchorWindow?: ActivityAnchorWindow,
): number {
  if (entry.toolCallId) {
    const toolIndex = blocks.findIndex((candidate) => (
      candidate.kind === 'tool' && candidate.toolCallId === entry.toolCallId
    ));
    if (toolIndex >= 0) {
      let end = toolIndex + 1;
      while (end < blocks.length) {
        const next = blocks[end];
        if (next.kind !== 'activity-event'
            || next.value.toolCallId !== entry.toolCallId) break;
        end += 1;
      }
      return end;
    }
  }
  /* Confine the scan to blocks the timeline proves existed when this row
   * was recorded; a missing bound degrades to the legacy unbounded scan. */
  let start = 0;
  let end = blocks.length;
  if (anchorWindow?.afterToolCallId) {
    const spineIndex = blocks.findIndex((candidate) => (
      candidate.kind === 'tool'
      && candidate.toolCallId === anchorWindow.afterToolCallId
    ));
    if (spineIndex >= 0) start = spineIndex + 1;
  }
  if (anchorWindow?.beforeToolCallId) {
    const limitIndex = blocks.findIndex((candidate) => (
      candidate.kind === 'tool'
      && candidate.toolCallId === anchorWindow.beforeToolCallId
    ));
    if (limitIndex >= 0) end = Math.min(end, Math.max(start, limitIndex));
  }
  if (start >= end) {
    /* The window brackets no content (one tool row bounding both sides, or
     * adjacent tool rows): round numbers cannot order the rows sharing it,
     * so keep timeline order by appending after the rows already there. */
    let index = start;
    while (index < blocks.length && blocks[index].kind === 'activity-event') {
      index += 1;
    }
    return index;
  }
  const entryAttempt = activityEntryAttemptId(entry);
  const round = entry.llmRound;
  if (round != null) {
    for (let index = end - 1; index >= start; index -= 1) {
      if (!sameAttemptScope(entryAttempt, blocks[index])) continue;
      if (activityBlockLlmRound(blocks[index]) === round) return index + 1;
    }
    for (let index = end - 1; index >= start; index -= 1) {
      if (!sameAttemptScope(entryAttempt, blocks[index])) continue;
      const candidateRound = activityBlockLlmRound(blocks[index]);
      if (candidateRound != null && candidateRound < round) return index + 1;
    }
    return start;
  }
  /* Unanchored diagnostics happened before any model request (preflight
   * schema isolation) or the turn never produced round-anchored content
   * (terminal error): place them before the first anchored content block,
   * after any earlier unanchored rows, falling back to the window end. */
  for (let index = start; index < end; index += 1) {
    const candidate = blocks[index];
    if (candidate.kind === 'text' || candidate.kind === 'thinking'
        || candidate.kind === 'tool' || candidate.kind === 'program'
        || (candidate.kind === 'activity-event'
          && candidate.value.llmRound != null)) {
      return index;
    }
  }
  return end;
}
