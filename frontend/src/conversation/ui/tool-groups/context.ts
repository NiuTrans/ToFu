/** Render-scoped batch membership and title indexes; first matching context occurrence preserves wire semantics. */
import { computeToolBatches, type ToolRoundBatch } from '../../presentation/tool-execution-groups';
import { deriveSiblingTitleDiscriminators } from '../../presentation/tool-sibling-titles';
import type { ToolGroupRound } from './contracts';
export function createToolGroupContext(rounds: readonly ToolGroupRound[]) {
  const groups = computeToolBatches(rounds);
  const byObject = new Map<ToolGroupRound, number>();
  const byId = new Map<string, number>();
  groups.forEach((group, index) => {
    for (const round of group.rounds) {
      if (!byObject.has(round)) byObject.set(round, index);
      if (round?.toolCallId && !byId.has(String(round.toolCallId))) byId.set(String(round.toolCallId), index);
    }
  });
  let titles: ReadonlyMap<string, string> | undefined;
  return {
    groups,
    groupFor(local: ToolRoundBatch<ToolGroupRound>) {
      const first = local.rounds[0];
      const identity = byObject.get(first) ?? Infinity;
      const call = first?.toolCallId ? byId.get(String(first.toolCallId)) ?? Infinity : Infinity;
      return groups[Number.isFinite(identity) ? identity : call] || local;
    },
    suffix(round: ToolGroupRound) {
      if (round.toolCallId == null) return '';
      titles ??= deriveSiblingTitleDiscriminators(groups);
      return titles.get(String(round.toolCallId)) || '';
    },
  };
}
/** Only immutable canonical arrays use the weak standalone index. Render sessions always build fresh context. */
export function createStandaloneTitleIndex() {
  const cache = new WeakMap<readonly ToolGroupRound[], { length: number; titles: ReadonlyMap<string, string> }>();
  return (round: ToolGroupRound, rounds: readonly ToolGroupRound[]) => {
    if (round.toolCallId == null) return '';
    let index = cache.get(rounds);
    if (!index || index.length !== rounds.length) {
      index = { length: rounds.length, titles: deriveSiblingTitleDiscriminators(computeToolBatches(rounds)) };
      cache.set(rounds, index);
    }
    return index.titles.get(String(round.toolCallId)) || '';
  };
}
