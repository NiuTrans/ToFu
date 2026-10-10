/** Extract display-only injection rows without consuming tool occurrence queues or inflating counts. */
import type { TimelineRound, TimelineSegment } from './contracts';
export function timelineAnchor(value: number | string | null): number | null {
  if (value === null) return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}
export function classifyTimelineRounds(rounds: readonly TimelineRound[], segments: readonly TimelineSegment[],
  isSuperseded: (round: TimelineRound) => boolean) {
  const hasStallNote = segments.some(segment => segment && segment.type === 'system_note' && segment.noteKind === 'intent-stall');
  const injections = new Map<number, TimelineRound[]>(), superseded = new Set<string>();
  const real: TimelineRound[] = [];
  let hasInbox = false;
  for (const round of rounds) {
    if (!round) continue;
    hasInbox ||= Boolean(round._inboxInject);
    if (round._userSteerInject || round._peerInject || round._inboxInject || round._bgCommandInject || round._stallNudge) {
      if (round._stallNudge && hasStallNote) continue;
      const consumed = round._userSteerInject ? round.steerRound : round._peerInject ? round.peerRound
        : round._bgCommandInject ? round.bgCommandRound : round._stallNudge ? round.stallRound : round.inboxRound;
      const anchor = Number(consumed || 0) - 1;
      const rows = injections.get(anchor) || [];
      rows.push(round); injections.set(anchor, rows);
    } else if (isSuperseded(round)) {
      if (round.toolCallId) superseded.add(String(round.toolCallId));
    } else real.push(round);
  }
  return { real, injections, superseded, hasInbox };
}
