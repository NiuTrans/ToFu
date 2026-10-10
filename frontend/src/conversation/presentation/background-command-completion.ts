/** Join immutable handoff receipts to conversation-scoped completion evidence.
 * Activity snapshots are NOT an input: disappearance is not completion.
 * Weak caches follow TurnStore lifetimes; unchanged turns keep identity.
 */
import type { ProjectionTurn } from '../../core/turn-projection';
import type { TurnState } from '../domain/turn-store';

type Row = Record<string, any>;
const row = (value: unknown): Row => value && typeof value === 'object' && !Array.isArray(value) ? value as Row : {};
const list = (value: unknown): Row[] => Array.isArray(value) ? value : [];
const completionFields = ['commandId', 'status', 'exitCode', 'timedOut', 'interrupted', 'resultPreview', 'resultTruncated'] as const;
const sameCompletion = (left: Row | undefined, right: Row | undefined) => left === right
  || Boolean(left && right && completionFields.every(key => left[key] === right[key]));
const isHandoff = (meta: Row) => meta.backgrounded || meta.exitCode === 'background';

export function createBackgroundCommandCompletionProjection() {
  const evidenceCache = new WeakMap<object, Map<string, Row>>();
  const candidateCache = new WeakMap<ProjectionTurn, string[]>();
  const turnCache = new WeakMap<ProjectionTurn, { values: Array<Row | undefined>; turn: ProjectionTurn }>();
  let conversationId = '';
  let completions = new Map<string, Row>();

  function evidence(projection: object): Map<string, Row> {
    const cached = evidenceCache.get(projection);
    if (cached) return cached;
    const found = new Map<string, Row>();
    const add = (value: unknown) => {
      const item = row(value);
      if (item.status === 'completed' && typeof item.commandId === 'string' && item.commandId) {
        found.set(item.commandId, item);
      }
    };
    const source = row(projection);
    add(source.backgroundCommand);
    for (const injection of list(source._bgCommandInjects)) {
      for (const preview of list(injection.previews)) add(preview.backgroundCommand);
    }
    for (const round of list(source.toolRounds)) {
      for (const preview of list(round.bgCommandPreviews)) add(preview.backgroundCommand);
    }
    evidenceCache.set(projection, found);
    return found;
  }

  function projectRound(original: Row): Row {
    let changed = false;
    const results = list(original.results).map(meta => {
      const completion = isHandoff(meta) && completions.get(meta.backgroundCommandId);
      if (!completion) return meta;
      changed = true;
      return {
        ...meta, backgrounded: false, backgroundCompleted: true,
        exitCode: completion.exitCode ?? '?', timedOut: completion.timedOut === true,
        interrupted: completion.interrupted === true, notRun: false,
        output: completion.resultPreview || '', outputTruncated: completion.resultTruncated === true,
      };
    });
    return changed ? { ...original, results } : original;
  }

  return {
    update(state: TurnState): void {
      conversationId = state.conversationId;
      const next = new Map<string, Row>();
      for (const item of state.queueItems) {
        const completion = row(item.backgroundCommand);
        if (completion.status === 'completed' && typeof completion.commandId === 'string') next.set(completion.commandId, completion);
      }
      for (const turn of Object.values(state.turnsById)) {
        if (!turn || turn.conversationId !== conversationId || !turn.projection) continue;
        for (const [id, value] of evidence(turn.projection)) next.set(id, value);
      }
      completions = next;
    },
    project(turn: ProjectionTurn): ProjectionTurn {
      if (turn.conversationId !== conversationId) return turn;
      let ids = candidateCache.get(turn);
      if (!ids) {
        const rounds = [...list(turn.projection?.toolRounds),
          ...list(turn.projection?.segments).flatMap(segment => segment._round ? [segment._round] : [])];
        ids = [...new Set(rounds.flatMap(round => list(round.results)
          .filter(meta => isHandoff(meta) && typeof meta.backgroundCommandId === 'string')
          .map(meta => meta.backgroundCommandId as string)))];
        candidateCache.set(turn, ids);
      }
      if (!ids.length) return turn;
      const values = ids.map(id => completions.get(id));
      if (!values.some(Boolean)) return turn;
      const cached = turnCache.get(turn);
      if (cached && values.every((value, index) => sameCompletion(value, cached.values[index]))) return cached.turn;
      // Segment-owned rounds take precedence in the selector. Project them by
      // execution ID too; provider call IDs can repeat and must never be a join key.
      const rounds = new Map<Row, Row>();
      const project = (round: Row) => {
        if (!rounds.has(round)) rounds.set(round, projectRound(round));
        return rounds.get(round)!;
      };
      const projected = { ...turn, projection: { ...turn.projection,
        toolRounds: list(turn.projection?.toolRounds).map(project),
        ...(turn.projection?.segments ? { segments: turn.projection.segments.map(segment => {
          const ownRound = row(segment)._round;
          if (!ownRound) return segment;
          const next = project(ownRound);
          return next === ownRound ? segment : { ...segment, _round: next };
        }) } : {}),
      } };
      turnCache.set(turn, { values, turn: projected });
      return projected;
    },
  };
}

/** Semantic invalidation only: token deltas and elapsed clocks do not wake reads. */
export function createBackgroundActivityEvidenceObserver() {
  const cache = new WeakMap<object, string>();
  let previous = '';
  return (state: TurnState): boolean => {
    const tokens = [state.conversationId || ''];
    for (const item of state.queueItems) {
      if (item.backgroundCommand?.commandId) tokens.push(`queue:${item.backgroundCommand.commandId}`);
    }
    for (const turn of Object.values(state.turnsById)) {
      if (!turn || turn.conversationId !== state.conversationId) continue;
      const projection = row(turn.projection);
      const rounds = list(projection.toolRounds);
      let token = cache.get(rounds);
      if (token === undefined) {
        token = rounds.flatMap(round => list(round.results).map(meta => meta.backgroundCommandId || '')).filter(Boolean).join(',');
        cache.set(rounds, token);
      }
      if (token) tokens.push(token);
      if (projection.backgroundCommand?.commandId) tokens.push(`done:${projection.backgroundCommand.commandId}`);
      for (const injection of list(projection._bgCommandInjects)) {
        tokens.push(injection.blockId || list(injection.previews).map(preview => preview.commandId).join(','));
      }
    }
    const next = tokens.join('|');
    if (next === previous) return false;
    previous = next;
    return true;
  };
}
