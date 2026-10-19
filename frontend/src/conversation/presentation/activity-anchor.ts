/**
 * Place bounded diagnostics against the authoritative backend block order.
 * Entry points: activityAnchorWindows/activityAnchorIndex and timing helpers.
 * Dependencies: contract/view-model types only; no DOM or retained state.
 * Provider call ids and round counters are not identities. Resolve a unique
 * producer-scoped segment/tool occurrence, or leave the diagnostic unanchored.
 */
import type {
  TurnActivityEntry,
  TurnActivityTimeline,
  TurnTimingTrace,
} from '../../api/conversation-sync.generated';
import type { ConversationBlockViewModel } from './view-model/types';

export type AnchoredActivityEntry = TurnActivityEntry & {
  anchorEntryId?: string;
};

export interface ActivityAnchorWindow {
  /** Keep source selectors, not array offsets: diagnostics are inserted later. */
  after?: TurnActivityEntry;
  before?: TurnActivityEntry;
  /** Compatibility for callers with old, uniquely identifiable history. */
  afterToolCallId?: string;
  beforeToolCallId?: string;
}

export function activityEntryIsVisible(entry: TurnActivityEntry): boolean {
  return entry.severity !== 'info' || (
    entry.reasonCode === 'context_compaction' || entry.reasonCode === 'compacting'
  );
}

/** Display-only correction from one enclosing, completed compaction span. */
export function activityEntryWithTiming<T extends TurnActivityEntry>(
  entry: T, trace: TurnTimingTrace | undefined,
): T {
  if (entry.reasonCode !== 'context_compaction'
      || !['succeeded', 'failed'].includes(entry.status)
      || !Number.isFinite(entry.startedAt) || !Number.isFinite(entry.endedAt)) return entry;
  const spans = trace?.spans;
  if (!Array.isArray(spans) || spans.length > 2048) return entry;
  const matches = spans.filter((span) => span && span.kind === 'compaction'
    && span.status === 'done' && Number.isFinite(span.tStart) && Number.isFinite(span.tEnd)
    && span.tStart >= 0 && span.tStart <= entry.startedAt! && entry.endedAt! <= span.tEnd);
  if (matches.length !== 1) return entry;
  const durationMs = Number(matches[0].tEnd) - Number(matches[0].tStart);
  return durationMs > (entry.durationMs ?? 0) ? { ...entry, durationMs } : entry;
}

type Producer = { taskId?: string; attemptId?: string };
type BlockSource = Producer & { blockId?: string; roundNum?: number; llmRound?: number };

/** Explicit producer stamps win. Only the known old id shape is evidence. */
export function activityEntryAttemptId(
  entry: Pick<TurnActivityEntry, 'id'> & Producer,
): string {
  if (entry.attemptId) return entry.attemptId;
  if (entry.taskId) return '';
  return /^activity:(.+):\d+(?::gateway-\d+)?$/.exec(entry.id)?.[1] ?? '';
}

function entryProducer(entry: TurnActivityEntry): Producer {
  return { taskId: entry.taskId, attemptId: activityEntryAttemptId(entry) };
}

function compatibleProducer(left: Producer, right: Producer): boolean {
  return (!left.taskId || !right.taskId || left.taskId === right.taskId)
    && (!left.attemptId || !right.attemptId || left.attemptId === right.attemptId);
}

function blockProducer(block: ConversationBlockViewModel): Producer | null {
  if (block.kind === 'activity-event') return entryProducer(block.value);
  const source = block.source as BlockSource;
  const round = block.kind === 'tool' || block.kind === 'program' ? block.round : undefined;
  // Source is the ordered backend segment. A display pairing may still be
  // legacy call-id based; never let that pairing override producer evidence.
  if (source.taskId || source.attemptId) return source;
  return { taskId: round?.taskId, attemptId: round?.attemptId };
}

export function activityBlockAttemptId(block: ConversationBlockViewModel): string {
  return blockProducer(block)?.attemptId ?? '';
}

export function activityBlockLlmRound(block: ConversationBlockViewModel): number | null {
  const source = block.source as BlockSource;
  const round = source.llmRound ?? (block.kind === 'tool' || block.kind === 'program'
    ? block.round?.llmRound : undefined);
  return typeof round === 'number' && Number.isSafeInteger(round) && round >= 0 ? round : null;
}

function isContent(block: ConversationBlockViewModel): boolean {
  return block.kind === 'text' || block.kind === 'thinking'
    || block.kind === 'tool' || block.kind === 'program';
}

function explicitTarget(entry: TurnActivityEntry): boolean {
  return entry.anchorBlockId != null || entry.anchorToolRoundNum != null;
}

/** Never weaken an absent/ambiguous exact target to its recycled call id. */
export function resolveActivityOccurrence(
  blocks: ReadonlyArray<ConversationBlockViewModel>, entry: TurnActivityEntry,
): number | null {
  const exact = explicitTarget(entry);
  if (!exact && !entry.toolCallId) return null;
  const producer = entryProducer(entry);
  const matches: number[] = [];
  blocks.forEach((block, index) => {
    if (block.kind === 'activity-event') return;
    const candidateProducer = blockProducer(block);
    if (!candidateProducer || !compatibleProducer(producer, candidateProducer)) return;
    const source = block.source as BlockSource;
    if (entry.anchorBlockId != null && source.blockId !== entry.anchorBlockId) return;
    if (entry.anchorToolRoundNum != null) {
      if (block.kind !== 'tool' && block.kind !== 'program') return;
      const ordinal = source.roundNum ?? block.round?.roundNum;
      if (!Number.isSafeInteger(entry.anchorToolRoundNum) || entry.anchorToolRoundNum < 0
          || ordinal !== entry.anchorToolRoundNum) return;
    }
    if (!exact && (block.kind !== 'tool' || block.toolCallId !== entry.toolCallId)) return;
    matches.push(index);
  });
  return matches.length === 1 ? matches[0] : null;
}

export function activityAnchorWindows(
  timeline: TurnActivityTimeline,
  blocks: ReadonlyArray<ConversationBlockViewModel>,
): Map<string, ActivityAnchorWindow> {
  const entries = timeline.entries ?? [];
  const windows = new Map<string, ActivityAnchorWindow>();
  let spine: TurnActivityEntry | undefined;
  let spineIndex = -1;
  for (const entry of entries) {
    windows.set(entry.id, spine ? { after: spine } : {});
    const index = resolveActivityOccurrence(blocks, entry);
    if (index != null && index >= spineIndex) {
      spine = entry;
      spineIndex = index;
    }
  }
  let limit: TurnActivityEntry | undefined;
  let limitIndex = blocks.length;
  for (let offset = entries.length - 1; offset >= 0; offset -= 1) {
    const entry = entries[offset];
    if (limit) windows.get(entry.id)!.before = limit;
    const index = resolveActivityOccurrence(blocks, entry);
    if (index != null && index <= limitIndex) {
      limit = entry;
      limitIndex = index;
    }
  }
  return windows;
}

/** Gateway children inherit the exact parent window, not an unbounded scan. */
export function resolveActivityAnchorWindow(
  windows: ReadonlyMap<string, ActivityAnchorWindow>, entry: AnchoredActivityEntry,
): ActivityAnchorWindow | undefined {
  return windows.get(entry.id)
    ?? (entry.anchorEntryId ? windows.get(entry.anchorEntryId) : undefined);
}

function afterDiagnostics(blocks: ReadonlyArray<ConversationBlockViewModel>, start: number): number {
  let index = start;
  while (index < blocks.length && blocks[index].kind === 'activity-event') index += 1;
  return index;
}

export function activityAnchorIndex(
  blocks: ReadonlyArray<ConversationBlockViewModel>,
  entry: TurnActivityEntry,
  anchorWindow?: ActivityAnchorWindow,
): number {
  const occurrence = resolveActivityOccurrence(blocks, entry);

  if (entry.anchorPosition === 'producer-start') {
    const producer = entryProducer(entry);
    const exact = blocks.findIndex(block => {
      const candidate = blockProducer(block);
      return isContent(block) && candidate != null
        && (!producer.taskId || candidate.taskId === producer.taskId)
        && (!producer.attemptId || candidate.attemptId === producer.attemptId);
    });
    // Prefer a positively stamped scope over legacy blocks with unknown owner.
    // With no such evidence, neutral placement is conservative, not an alias.
    const first = exact >= 0 ? exact : blocks.findIndex(block => isContent(block)
      && compatibleProducer(producer, blockProducer(block) ?? {}));
    return afterDiagnostics(blocks, first < 0 ? blocks.length : first);
  }
  if (occurrence != null) {
    let index = occurrence + (entry.anchorPosition === 'before' ? 0 : 1);
    while (index < blocks.length) {
      const next = blocks[index];
      if (next.kind !== 'activity-event' || resolveActivityOccurrence(blocks, next.value) !== occurrence) break;
      index += 1;
    }
    return index;
  }
  let start = 0;
  let end = blocks.length;
  const after = anchorWindow?.after ?? (anchorWindow?.afterToolCallId
    ? { ...entry, anchorBlockId: undefined, anchorToolRoundNum: undefined,
      toolCallId: anchorWindow.afterToolCallId } : undefined);
  const before = anchorWindow?.before ?? (anchorWindow?.beforeToolCallId
    ? { ...entry, anchorBlockId: undefined, anchorToolRoundNum: undefined,
      toolCallId: anchorWindow.beforeToolCallId } : undefined);
  const afterIndex = after ? resolveActivityOccurrence(blocks, after) : null;
  const beforeIndex = before ? resolveActivityOccurrence(blocks, before) : null;
  if (afterIndex != null) start = afterIndex + 1;
  if (beforeIndex != null) end = beforeIndex;
  // Out-of-order completion rows cannot reverse backend segment chronology.
  if (start > end) end = start;

  const producer = entryProducer(entry);
  const candidates: { index: number; round: number | null; producer: Producer }[] = [];
  for (let index = start; index < end; index += 1) {
    const block = blocks[index];
    const candidateProducer = blockProducer(block);
    if (isContent(block) && candidateProducer && compatibleProducer(producer, candidateProducer)) {
      candidates.push({ index, round: activityBlockLlmRound(block), producer: candidateProducer });
    }
  }
  const neutral = afterDiagnostics(blocks, candidates[0]?.index ?? end);
  // Supplied exact identities fail closed, including missing trimmed targets.
  // A repeated legacy call id also cannot silently become a model-round guess.
  if (explicitTarget(entry) || entry.toolCallId) return neutral;
  // Missing historical start evidence is not a license to attach after future
  // round output (especially pre_turn 0). Never fetch archives or rewrite Turns.
  if (['context_compaction', 'compacting'].includes(entry.reasonCode ?? '')) return neutral;
  if (entry.llmRound == null || !candidates.length) return neutral;

  // Round fallback is safe only inside ONE monotonic producer run. Missing
  // stamps can coexist with an old unambiguous run, never hide a known reset.
  let scope: Producer = {};
  let priorRound = -1;
  let priorToolOrdinal = -1;
  for (const candidate of candidates) {
    if (!compatibleProducer(scope, candidate.producer)) return neutral;
    scope = { taskId: scope.taskId || candidate.producer.taskId,
      attemptId: scope.attemptId || candidate.producer.attemptId };
    const block = blocks[candidate.index];
    if (block.kind === 'tool' || block.kind === 'program') {
      const ordinal = (block.source as BlockSource).roundNum ?? block.round?.roundNum;
      if (typeof ordinal === 'number' && Number.isSafeInteger(ordinal) && ordinal >= 0) {
        // Legacy resumes can repeat model round 0; tool ordinals restarting
        // (including equality) still prove more than one execution run.
        if (ordinal <= priorToolOrdinal) return neutral;
        priorToolOrdinal = ordinal;
      }
    }
    if (candidate.round == null) continue;
    if (candidate.round < priorRound) return neutral;
    priorRound = candidate.round;
  }
  const eligible = candidates.filter((candidate) => candidate.round != null
    && candidate.round <= entry.llmRound!);
  const last = eligible[eligible.length - 1];
  return last ? afterDiagnostics(blocks, last.index + 1) : neutral;
}
