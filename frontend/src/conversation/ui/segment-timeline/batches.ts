/** Apply canonical attempt-aware grouping, then attach rich tool occurrences without rewriting segment values. */
import { toolExecutionLlmRound } from '../../presentation/tool-execution-groups';

import { isDuplicatedInboxDeliveryNote } from '../../presentation/context-note-grouping';
import { createToolOccurrenceResolver } from './occurrences';
import type { TimelineBatch, TimelineRound, TimelineSegment, TimelinePorts } from './contracts';
export function resolveTimelineBatches(segments: readonly TimelineSegment[], rounds: readonly TimelineRound[],
  superseded: ReadonlySet<string>, displayRounds: readonly TimelineRound[],
  ports: Pick<TimelinePorts, 'groupSegments'>): TimelineBatch[] {
  // Adapt retained synthetic rows to the same durable preview receipt shape.
  const inboxReceipts = displayRounds.filter(round => round._inboxInject).map(round => ({
    ...round, round: round.inboxRound, count: round.inboxCount, previews: round.inboxPreviews,
  }));
  const peerReceipts = displayRounds.filter(round => round._peerInject).map(round => ({
    ...round, round: round.peerRound, count: round.peerCount, previews: round.peerPreviews,
  }));
  const visible = segments.filter(segment => segment && !segment.terminal
    && !(segment.type === 'text' && segment.deliverable)
    && !(segment.type === 'system_note' && isDuplicatedInboxDeliveryNote(segment, inboxReceipts, peerReceipts)));
  const hasLlm = visible.some(segment => toolExecutionLlmRound(segment) != null);
  const resolve = createToolOccurrenceResolver(rounds);
  const batches: TimelineBatch[] = [];
  for (const group of ports.groupSegments(visible, hasLlm)) {
    const batch: TimelineBatch = { key: group.key, llmRound: group.llmRound, segs: [], rounds: [],
      attemptOrdinal: group.attemptOrdinal, totalAttempts: group.totalAttempts };
    batches.push(batch);
    for (const segment of group.items) {
      if (segment.type === 'tool_use') {
        // A dropped superseded occurrence must never consume a no-id result.
        if (segment.id && superseded.has(String(segment.id))) continue;
        batch.segs.push(segment);
        const round = resolve(segment.id, segment);
        if (round) batch.rounds.push(round);
      } else batch.segs.push(segment);
    }
  }
  return batches;
}
