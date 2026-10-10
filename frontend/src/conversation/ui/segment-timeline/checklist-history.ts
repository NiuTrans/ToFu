/** Accepted checklist update history. Retain every audit occurrence; clone only the last display row/metadata. */
import type { TimelineRound } from './contracts';
interface History { roundNum?: string | number; revision: unknown; operation: unknown; done: number; total: number }
const metadata = (round: TimelineRound): Record<string, unknown> => {
  const first = round.results?.[0];
  return first && typeof first === 'object' ? first as Record<string, unknown> : {};
};
export function projectChecklistHistory(source: readonly TimelineRound[]): readonly TimelineRound[] {
  const positions: number[] = [];
  source.forEach((round, index) => { if (round?.toolName === 'todo_write') positions.push(index); });
  if (positions.length <= 1) return source;
  const seen = new Set<string>();
  const history: History[] = [];
  for (const index of positions) {
    const round = source[index], result = metadata(round);
    if (result.todoRejected || result.todoNoop || (round.status && round.status !== 'done') || !Array.isArray(result.todos)) continue;
    const revision = Number(result.todoRevision || 0), updateCount = Number(result.todoUpdateCount || 0);
    const identity = updateCount > 0
      ? JSON.stringify(['update', round.attemptId || round.taskId || 'task', round.agentId || 'root', updateCount])
      : revision > 0 ? JSON.stringify(['revision', result.checklistId || 'checklist', revision])
      : JSON.stringify(['round', round.toolCallId || round.roundNum || index]);
    if (seen.has(identity)) continue;
    seen.add(identity);
    let done = 0;
    for (const item of result.todos) { if (item && item.status === 'completed') done++; }
    history.push({ roundNum: round.roundNum, revision: revision || result.todoRevision,
      operation: result.todoOperation || (revision > 0 ? 'sync' : ''), done, total: result.todos.length });
  }
  const keep = positions[positions.length - 1], latest = source[keep];
  const count = Number(metadata(latest).todoUpdateCount ?? history.length);
  let copied: unknown[] | undefined;
  if (Array.isArray(latest.results) && latest.results.length) {
    copied = latest.results.slice();
    if (copied[0] && typeof copied[0] === 'object') {
      copied[0] = { ...metadata(latest), todoDisplayUpdateCount: count, todoDisplayHistory: history };
    }
  }
  const output = source.slice(); output[keep] = { ...latest, todoDisplayUpdateCount: count, ...(copied ? { results: copied } : {}) };
  return output;
}
export function isSupersededToolOrphan(round: TimelineRound | null | undefined): boolean {
  if (!round) return false;
  const result = metadata(round);
  const hasResult = round.toolContent != null || result.fetched || (Number(result.fetchedChars) | 0) > 0;
  return result.badge === 'superseded' && !hasResult;
}
