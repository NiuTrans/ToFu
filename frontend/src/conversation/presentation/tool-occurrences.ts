/** Render-local rich tool matching. Producer scope precedes provider ID occurrence;
 * no durable state, DOM or transport. Used by typed blocks and settled timelines. */
export interface ToolOccurrenceScope {
  readonly attemptId?: string;
  readonly taskId?: string;
  readonly _attemptId?: string;
  readonly _taskId?: string;
}
interface ToolOccurrenceRound extends ToolOccurrenceScope {
  readonly toolCallId?: string | number;
}
export const toolOccurrenceScope = (value: ToolOccurrenceScope): string => {
  const attempt = value.attemptId || value._attemptId;
  return attempt ? JSON.stringify(['attempt', attempt])
    : value.taskId || value._taskId ? JSON.stringify(['task', value.taskId || value._taskId]) : '';
};
export function createToolOccurrenceMatcher<Round extends ToolOccurrenceRound>(rounds: readonly Round[]) {
  const byId = new Map<string, Round[]>();
  const noId: Round[] = [];
  const scopedById = new Map<string, Map<string, Round[]>>();
  for (const round of rounds) {
    if (round.toolCallId) {
      const key = String(round.toolCallId), rows = byId.get(key) ?? [];
      rows.push(round); byId.set(key, rows);
      const producer = toolOccurrenceScope(round);
      if (producer) {
        const scopes = scopedById.get(key) ?? new Map<string, Round[]>();
        const scopedRows = scopes.get(producer) ?? [];
        scopedRows.push(round); scopes.set(producer, scopedRows); scopedById.set(key, scopes);
      }
    } else noId.push(round);
  }
  const cursors = new Map<string, number>();
  let noIdCursor = 0;
  return (id: string | number | undefined, producer: ToolOccurrenceScope = {}): Round | undefined => {
    if (id) {
      const key = String(id), rows = byId.get(key) ?? [];
      const producerScope = toolOccurrenceScope(producer);
      // Legacy unstamped rows retain ordered pairing. Once rows carry scope,
      // a missing occurrence cannot borrow another attempt's result.
      const scoped = producerScope && scopedById.has(key);
      const candidates = scoped ? (scopedById.get(key)?.get(producerScope) ?? []) : rows;
      const cursorKey = JSON.stringify([key, scoped ? producerScope : '']);
      const cursor = cursors.get(cursorKey) ?? 0;
      cursors.set(cursorKey, cursor + 1);
      if (cursor < candidates.length) return candidates[cursor];
      if (scoped) return undefined;
    }
    return noId[noIdCursor++];
  };
}
