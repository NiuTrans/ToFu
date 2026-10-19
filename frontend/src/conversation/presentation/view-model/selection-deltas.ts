/** Qualification of canonical reducer deltas for selection reuse. No authority
 * or event history: unknown/structural inputs always rebuild the common graph.
 * Entry: canReuseSelection, snapshotPresentation, samePresentation.
 * Dependencies: turn-store and view-model types only. */
import type { TurnState, TurnStateUpdate } from '../../domain/turn-store';
import type { ConversationPresentationState } from './types';

const localTurnFields = new Set(['projection', 'projectionRevision', 'updatedAt']);
const localProjectionFields = new Set([
  'content', 'segments', 'thinking', 'originalContent', 'translatedContent',
]);

function sameExcept(left: object, right: object, local: ReadonlySet<string>): boolean {
  const before = left as Record<string, unknown>, after = right as Record<string, unknown>;
  return [...new Set([...Object.keys(before), ...Object.keys(after)])]
    .every((key) => local.has(key) || Object.is(before[key], after[key]));
}

export function canReuseSelection(before: TurnState, after: TurnState, update: TurnStateUpdate): boolean {
  if (!update.changes || update.changes.reset
      || before.conversationId !== after.conversationId
      || before.laneOrder !== after.laneOrder || before.historyByLane !== after.historyByLane
      || before.queueItems !== after.queueItems || before.livePhase !== after.livePhase
      || before.pushWithheld !== after.pushWithheld) return false;
  for (const id of update.changes.turnIds) {
    const left = before.turnsById[id], right = after.turnsById[id];
    // Explicit local-field admission fences branch, Goal, completion evidence,
    // lifecycle and future cross-Turn joins without guessing from event names.
    if (!left || !right || !sameExcept(left, right, localTurnFields)
        || !sameExcept(left.projection, right.projection, localProjectionFields)) return false;
  }
  for (const id of update.changes.attemptIds) {
    const left = before.attemptsById[id], right = after.attemptsById[id];
    if (!left || !right || left.turnId !== right.turnId || left.taskId !== right.taskId
        || left.createdAt !== right.createdAt || left.queueBinding !== right.queueBinding) return false;
  }
  return true;
}

type DisplaySnapshot = ReturnType<typeof snapshotPresentation>;

/** Maps may be changed in place by the presentation owner; copy their entries.
 * Values are immutable presentation DTOs. Health is legacy mutable JSON, so
 * capture its consumed value rather than its identity. Run summaries are
 * replaced wholesale (both the conversation-map assignment in
 * runtime/sections/ui/streaming_render.js and every run-record entry), so
 * identity already reports any change; serializing the whole map on every
 * snapshot and again inside samePresentation made each streaming frame O(n²). */
export function snapshotPresentation(value: ConversationPresentationState) {
  return {
    taskTimePresentationsByAttempt: new Map(value.taskTimePresentationsByAttempt),
    translationModeByTurn: new Map(value.translationModeByTurn),
    translationActivityByTurn: new Map(value.translationActivityByTurn),
    artifactsByTurn: new Map(value.artifactsByTurn),
    costSignatureByTurnId: new Map(value.costSignatureByTurnId),
    expandedBranchLaneId: value.expandedBranchLaneId,
    requestInspectorEnabled: value.requestInspectorEnabled,
    syncHealth: value.syncHealth?.state,
    autopilotSummaries: value.autopilotSummaries,
  } satisfies Record<keyof ConversationPresentationState, unknown>;
}

export function samePresentation(previous: DisplaySnapshot, value: ConversationPresentationState): boolean {
  const current = snapshotPresentation(value);
  return (Object.keys(current) as Array<keyof DisplaySnapshot>).every((key) => {
    const before = previous[key], after = current[key];
    if (!(before instanceof Map) || !(after instanceof Map)) return Object.is(before, after);
    if (before.size !== after.size) return false;
    for (const [id, item] of before) {
      if (!after.has(id) || !Object.is(item, after.get(id))) return false;
    }
    return true;
  });
}
