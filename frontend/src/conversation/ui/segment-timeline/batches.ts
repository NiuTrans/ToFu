/** Apply canonical attempt-aware grouping, then attach rich tool occurrences without rewriting segment values. */
import { toolExecutionLlmRound } from '../../presentation/tool-execution-groups';
import { createToolOccurrenceResolver } from './occurrences';
import type { TimelineBatch, TimelineRound, TimelineSegment, TimelinePorts } from './contracts';
export function resolveTimelineBatches(segments: readonly TimelineSegment[], rounds: readonly TimelineRound[],
  superseded: ReadonlySet<string>, hasInbox: boolean, ports: Pick<TimelinePorts, 'groupSegments'>): TimelineBatch[] {
  const visible = segments.filter(segment => segment && !segment.terminal
    && !(segment.type === 'text' && segment.deliverable)
    && !(hasInbox && segment.type === 'system_note' && segment.noteKind === 'context-injection' && segment.reason === 'inbox_delivery'));
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
