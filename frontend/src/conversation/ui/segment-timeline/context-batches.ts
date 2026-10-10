/** Fold adjacent context-only batches for display; preserve injection wire slots and canonical attempt metadata. */
import { isContextInjectionNote, sameContextNoteScope } from '../../presentation/context-note-grouping';
import { timelineAnchor } from './injections';
import type { TimelineBatch } from './contracts';
const contextOnly = (batch: TimelineBatch): boolean => batch.rounds.length === 0 && batch.segs.length > 0
  && batch.segs.every(segment => segment.type === 'system_note' && Boolean(segment.text) && isContextInjectionNote(segment));
export function coalesceTimelineContextBatches(batches: readonly TimelineBatch[], anchors: ReadonlyMap<number, unknown>): TimelineBatch[] {
  const output: TimelineBatch[] = [];
  for (const batch of batches) {
    const previous = output[output.length - 1], anchor = timelineAnchor(batch.llmRound);
    const separateInjection = anchor !== null && anchors.has(anchor) && previous
      && timelineAnchor(previous.llmRound) !== anchor;
    if (previous?.contextNoteGroup && contextOnly(batch) && !separateInjection
        && sameContextNoteScope(previous.segs[0], batch.segs[0])) {
      previous.segs.push(...batch.segs);
    } else if (contextOnly(batch)) {
      output.push({ ...batch, segs: batch.segs.slice(), rounds: [], contextNoteGroup: true });
    } else output.push(batch);
  }
  return output;
}
