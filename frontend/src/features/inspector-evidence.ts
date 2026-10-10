/** Explicit persisted evidence routing. Legacy inference is only for unstamped cards. */
export interface InspectorRef { taskId: string; roundNum: number; turn: string }
export function inspectorReference(row: Record<string, any>, legacyTaskId: string): InspectorRef | null {
  if ('inspectorRef' in row) {
    const ref = row.inspectorRef;
    return ref && typeof ref.taskId === 'string' && ref.taskId
      && Number.isInteger(ref.roundNum) && ref.roundNum > 0
      && ref.taskId.length <= 256
      && typeof ref.turn === 'string' && ref.turn.length > 0 && ref.turn.length <= 256
      ? { taskId: ref.taskId, roundNum: ref.roundNum, turn: ref.turn } : null;
  }
  if (!legacyTaskId || row.llmRound == null) return null;
  const agent = row.agentId ? String(row.agentId) : '';
  const roundNum = Number(row.llmRound) + (agent ? 0 : 1);
  if (!Number.isInteger(roundNum) || roundNum < 1) return null;
  return { taskId: agent ? `${legacyTaskId}#agent:${agent}` : legacyTaskId,
    roundNum, turn: '' };
}
export async function resolveInspectorView(
  fetchPayload: (task: string, round: number, turn: string, kind: string) => Promise<any>,
  task: string, round: number, turn = '',
): Promise<{kind: string; payload: any} | null> {
  for (const kind of ['state', 'request']) {
    const payload = await fetchPayload(task, round, turn, kind);
    if (payload?.messages?.length) return { kind, payload };
  }
  return null;
}
