/** Join ordered Turns to stamped Goal runs. Human input fences affiliation;
 * virtual users remain participants, never human authorization. No state/cache. */
import type { TurnRecord } from '../../api/conversation-sync.generated';

export function goalRunAffiliations(turns: ReadonlyArray<TurnRecord>): ReadonlyMap<string, string> {
  const result = new Map<string, string>();
  let runId: string | undefined;
  for (const turn of turns) {
    if (turn.actor === 'human') runId = undefined;
    const stamped = turn.projection.orchestration?.goalRunId || turn.projection._autopilotRunId;
    if (stamped) runId = stamped;
    if (runId) result.set(turn.turnId, runId);
  }
  return result;
}

export function goalRunConcluded(
  runId: string | undefined,
  summaries: Readonly<Record<string, Readonly<Record<string, unknown>>>> | undefined,
): boolean {
  const record = runId ? summaries?.[runId] : undefined;
  return Boolean(record && (record.status === 'concluded' || record.content));
}
