/**
 * Contiguous thinking and context-note coalescing plus terminal reasoning activity.
 * Explicit scope prevents crossing authored round boundaries.
 * Entry: coalesceAdjacentThinkingBlocks, coalesceAdjacentSystemNoteBlocks,
 * coalesceAdjacentRetryActivityBlocks, settleThinkingActivity.
 * Dependencies: conversation-sync.generated, round-outline, context-note-grouping,
 * types. */
import {
  type TurnRecord,
} from '../../../api/conversation-sync.generated';
import {
  roundIdentityOf,
} from '../round-outline';
import {
  isContextInjectionNote,
  sameContextNoteScope,
} from '../context-note-grouping';
import {
  type ActivityEventBlockViewModel,
  type ThinkingBlockViewModel,
  type SystemNoteBlockViewModel,
  type ConversationBlockViewModel,
} from './types';

const ADJACENT_THINKING_SEPARATOR = '\n\n';

/**
 * Present uninterrupted reasoning as one disclosure without changing Turn
 * authority. Hidden transport/gateway shells are intentionally not reader-
 * visible boundaries, while any rendered tool, program, prose, or diagnostic
 * block ends the run. The first projected block remains the DOM identity so a
 * live run can grow without replacing the disclosure node; memberBlockIds
 * retains every durable per-round identity for diagnostics.
 */
export function coalesceAdjacentThinkingBlocks(
  blocks: ConversationBlockViewModel[],
): void {
  const coalesced: ConversationBlockViewModel[] = [];
  for (let index = 0; index < blocks.length;) {
    const first = blocks[index];
    if (first.kind !== 'thinking') {
      coalesced.push(first);
      index += 1;
      continue;
    }
    const run: ThinkingBlockViewModel[] = [first];
    index += 1;
    while (index < blocks.length && blocks[index].kind === 'thinking'
        && roundIdentityOf(first.source)?.key
          === roundIdentityOf(blocks[index].source)?.key) {
      run.push(blocks[index] as ThinkingBlockViewModel);
      index += 1;
    }
    if (run.length === 1) {
      coalesced.push(first);
      continue;
    }
    const everyDisplayTranslated = run.every(
      (block) => block.displayMode === 'translated',
    );
    const everyDurablyTranslated = run.every(
      (block) => block.translatedMarkdown !== undefined,
    );
    const markdown = run.map((block) => block.markdown)
      .join(ADJACENT_THINKING_SEPARATOR);
    const translatedMarkdown = everyDurablyTranslated
      ? run.map((block) => block.translatedMarkdown ?? '')
        .join(ADJACENT_THINKING_SEPARATOR)
      : undefined;
    coalesced.push({
      blockId: first.blockId,
      kind: 'thinking',
      identitySource: first.identitySource,
      source: first.source,
      memberBlockIds: run.flatMap((block) => block.memberBlockIds),
      markdown,
      ...(translatedMarkdown !== undefined ? { translatedMarkdown } : {}),
      displayMarkdown: everyDisplayTranslated
        ? run.map((block) => block.displayMarkdown)
          .join(ADJACENT_THINKING_SEPARATOR)
        : markdown,
      displayMode: everyDisplayTranslated ? 'translated' : 'original',
      terminal: run[run.length - 1].terminal,
    });
  }
  blocks.splice(0, blocks.length, ...coalesced);
}

/* Reasons (registry ids) that specialize a context-injection note into a
 * first-class intervention (active-checklist re-pin, modified-files recall).
 * Kept in sync with the renderers' reason-presentation maps
 * (classic-conversation-renderers.ts SYSTEM_NOTE_REASON_PRESENTATION,
 * tool_rounds.js _SEG_NOTE_REASON_PRESENTATION): these notes must keep their
 * own row, never join the anonymous context-injection group. */


/**
 * Merge each maximal run of adjacent context-injection notes into ONE row
 * without changing Turn authority or member-note positions. The session-start
 * context stack (and any run of notes injected together mid-turn) collapses
 * from N sibling rows to a single disclosure; a lone context note, or any
 * intervention note (stall nudge, checklist re-pin, modified-files recall),
 * keeps its own row. The first note's blockId remains the DOM identity so a
 * live-growing run re-renders in place. Grouping never crosses a non-note
 * block, so a mid-turn group still sits at its exact wire slot.
 */
export function coalesceAdjacentSystemNoteBlocks(
  blocks: ConversationBlockViewModel[],
): void {
  const isContextNote = (block: ConversationBlockViewModel): boolean => (
    block.kind === 'system-note' && isContextInjectionNote(block)
  );
  const coalesced: ConversationBlockViewModel[] = [];
  for (let index = 0; index < blocks.length;) {
    const first = blocks[index];
    if (!isContextNote(first)) {
      coalesced.push(first);
      index += 1;
      continue;
    }
    const run: SystemNoteBlockViewModel[] = [first as SystemNoteBlockViewModel];
    index += 1;
    while (index < blocks.length && isContextNote(blocks[index])
        && sameContextNoteScope(first.source, blocks[index].source)) {
      run.push(blocks[index] as SystemNoteBlockViewModel);
      index += 1;
    }
    if (run.length === 1) {
      coalesced.push(first);
      continue;
    }
    coalesced.push({
      blockId: first.blockId,
      kind: 'system-note-group',
      identitySource: first.identitySource,
      source: first.source,
      items: run,
    });
  }
  blocks.splice(0, blocks.length, ...coalesced);
}

/* Failed model requests and rate-limit/backoff waits are transport noise: one
 * 429 storm produces a ladder of identical red rows in the transcript. A
 * maximal ADJACENT run of them collapses into one collapsed disclosure whose
 * members are unchanged (same summary, facts and durations), so a reader can
 * still open it. Rows that carry their own meaning never join a run — a
 * terminal error opens its diagnostic receipt, a compaction receipt is a
 * durable context boundary, and a model switch explains where service
 * resumed. A run of one also stays a plain row: a lone failure is not noise. */
function isRetryEvidenceBlock(
  block: ConversationBlockViewModel,
): block is ActivityEventBlockViewModel {
  if (block.kind !== 'activity-event' || block.terminalError) return false;
  const entry = block.value;
  if (entry.reasonCode === 'context_compaction' || entry.reasonCode === 'compacting') {
    return false;
  }
  if (entry.kind === 'model') return entry.status === 'failed';
  return entry.kind === 'status'
    && (entry.phase === 'retrying' || (entry.statusCode ?? 0) >= 400);
}

/**
 * Merge each maximal run of adjacent retry/failure rows into ONE collapsed
 * group without changing the member rows or their chronology. The first
 * row's blockId remains the DOM identity so a live-growing storm re-renders
 * in place instead of restarting the disclosure.
 */
export function coalesceAdjacentRetryActivityBlocks(
  blocks: ConversationBlockViewModel[],
): void {
  const coalesced: ConversationBlockViewModel[] = [];
  for (let index = 0; index < blocks.length;) {
    const first = blocks[index];
    if (!isRetryEvidenceBlock(first)) {
      coalesced.push(first);
      index += 1;
      continue;
    }
    const run: ActivityEventBlockViewModel[] = [first];
    index += 1;
    while (index < blocks.length && isRetryEvidenceBlock(blocks[index])) {
      run.push(blocks[index] as ActivityEventBlockViewModel);
      index += 1;
    }
    if (run.length === 1) {
      coalesced.push(first);
      continue;
    }
    coalesced.push({
      blockId: first.blockId,
      kind: 'activity-event-group',
      identitySource: first.identitySource,
      source: first.source,
      items: run,
    });
  }
  blocks.splice(0, blocks.length, ...coalesced);
}

/**
 * Reconcile thinking-block activity with the turn lifecycle.
 *
 * ``segment.terminal`` marks the TERMINAL round's reasoning accumulator (a
 * channel-projection concern — see ``derive_thinking``); it does NOT mean
 * "this reasoning text may still grow". Inter-round thinking segments are
 * stamped closed onto their tool-round batch, so on a settled turn every
 * thinking block is complete. On a live turn a thinking block stays active
 * only while it remains the tail of the activity stream: once narration or
 * a tool round lands after it, that reasoning round is finished and the
 * block closes immediately (previously it stayed on "Thinking…" until the
 * NEXT reasoning round started or the whole turn settled — a full round
 * late). Diagnostic/injection rows anchored after it do not close it. The
 * renderer keys its label / disclosure default / pulse off this flag.
 */
export function settleThinkingActivity(
  turn: TurnRecord,
  blocks: ConversationBlockViewModel[],
): void {
  const settled = turn.status === 'completed' || turn.status === 'interrupted'
    || turn.status === 'truncated' || turn.status === 'failed';
  let laterContent = false;
  for (let index = blocks.length - 1; index >= 0; index -= 1) {
    const block = blocks[index];
    if (block.kind === 'thinking') {
      block.terminal = settled || laterContent;
    }
    if (block.kind === 'thinking' || block.kind === 'text'
        || block.kind === 'tool' || block.kind === 'program'
        || block.kind === 'system-note') {
      laterContent = true;
    }
  }
}
