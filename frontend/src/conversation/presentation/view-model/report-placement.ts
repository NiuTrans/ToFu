/**
 * Reading order for Coordinator report Turns.
 *
 * `report_to_user` appends a settled `coordinator_report.*` Turn while the
 * publishing reply is still running, so in durable lane order the report sits
 * AFTER that live reply. Rendering that order leaves the live reply — and every
 * later tool row or steered message it absorbs — above a newer, already
 * settled bubble. Presentation places each report published inside a reply's
 * execution window immediately before that reply instead. The window test uses
 * only durable timestamps (report createdAt < reply settledAt, or the reply is
 * still live), so the order is the same while the reply runs and after it
 * settles: nothing jumps at settlement. Durable lane order is never rewritten.
 *
 * Entry point: presentationTurnOrder. Dependencies: TurnState records only.
 */
import type { TurnState } from '../../domain/turn-store';

type TurnRecordOf = NonNullable<TurnState['turnsById'][string]>;

const COORDINATOR_REPORT_KIND_PREFIX = 'coordinator_report.';

function isCoordinatorReport(turn: TurnRecordOf): boolean {
  return turn.actor === 'assistant'
    && !turn.parentTurnId
    && String(turn.kind || '').startsWith(COORDINATOR_REPORT_KIND_PREFIX);
}

function replyWindowCovers(state: TurnState, reply: TurnRecordOf, report: TurnRecordOf): boolean {
  if (reply.actor === 'human') return false;
  const attempt = reply.currentAttemptId ? state.attemptsById[reply.currentAttemptId] : undefined;
  if (attempt?.queueBinding) return false;
  if (reply.status === 'pending' || reply.status === 'running') return true;
  const settledAt = Number(attempt?.settledAt ?? reply.updatedAt ?? 0);
  return Number(report.createdAt || 0) < settledAt;
}

/** Returns `turnIds` itself when no report moves. */
export function presentationTurnOrder(
  state: TurnState, turnIds: readonly string[],
): readonly string[] {
  const ordered: string[] = [];
  let reply: TurnRecordOf | undefined;
  let replyIndex = -1;
  let moved = false;
  for (const turnId of turnIds) {
    const turn = state.turnsById[turnId];
    if (turn && isCoordinatorReport(turn)) {
      if (reply && replyWindowCovers(state, reply, turn)) {
        ordered.splice(replyIndex, 0, turnId);
        replyIndex += 1;
        moved = true;
      } else {
        ordered.push(turnId);
      }
      continue;
    }
    reply = turn;
    replyIndex = ordered.length;
    ordered.push(turnId);
  }
  return moved ? ordered : turnIds;
}
