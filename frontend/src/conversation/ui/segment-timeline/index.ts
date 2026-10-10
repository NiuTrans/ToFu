/** Compose the settled interleaved timeline. One render-scoped grouping context; no DOM, cache, timer or transport. */
import { projectChecklistHistory } from './checklist-history';
import { classifyTimelineRounds, timelineAnchor } from './injections';
import { resolveTimelineBatches } from './batches';
import { coalesceTimelineContextBatches } from './context-batches';
import type { TimelineRound, TimelineSegment, TimelinePorts } from './contracts';
export { projectChecklistHistory, isSupersededToolOrphan } from './checklist-history';
export function renderToolSegmentTimeline(segments: readonly TimelineSegment[] | null | undefined,
  rawRounds: readonly TimelineRound[], ports: TimelinePorts): string {
  if (!Array.isArray(segments) || !segments.length) return '';
  const allRounds = projectChecklistHistory(rawRounds);
  // Display-only program parents are absent from segments; grouped rendering owns that case.
  let noIdTodos = 0;
  for (const round of allRounds) {
    if (round?._programSynthetic) return '';
    if (round?.toolName === 'todo_write' && !round.toolCallId && ++noIdTodos > 1) return '';
  }
  const { real, injections, superseded, hasInbox } = classifyTimelineRounds(allRounds, segments, ports.isSuperseded);
  const batches = resolveTimelineBatches(segments, real, superseded, hasInbox, ports);
  if (!batches.length || (!batches.some(batch => batch.rounds.length) && real.length)) return '';
  const session = ports.createSession(allRounds), emitted = new Set<number>();
  const renderGroups = (rounds: readonly TimelineRound[]) => session.renderGroups(rounds);
  const renderInjections = (rows: readonly TimelineRound[]) => rows.map(round => session.renderSlot(round)).join('');
  let inner = '';
  for (const batch of coalesceTimelineContextBatches(batches, injections)) {
    const anchor = timelineAnchor(batch.llmRound);
    if (anchor !== null && injections.has(anchor) && !emitted.has(anchor)) {
      inner += renderInjections(injections.get(anchor)!); emitted.add(anchor);
    }
    inner += ports.renderBatch(batch.segs, batch.rounds, allRounds, renderGroups);
  }
  for (const [anchor, rows] of injections) { if (!emitted.has(anchor)) inner += renderInjections(rows); }
  if (!inner) return '';
  const panel = ports.panel(real, allRounds, real.some(round => round.status === 'searching' || round._swarmActive));
  return `<div class="ptool-panel seg-timeline${panel.active ? ' ptool-panel-active' : ''}${panel.collapsed ? ' collapsed' : ''}"${!panel.active ? ' data-collapsible="true"' : ''} data-attention="${panel.attention}">${panel.html}<div class="ptool-panel-body" data-full-count="${real.length}">${inner}</div></div>`;
}
