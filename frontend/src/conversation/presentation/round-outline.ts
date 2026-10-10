/**
 * Presentation-only model-round outline over the ordered content projection.
 *
 * Entry points: roundIdentityOf, selectRoundOutline. Never reads the bounded
 * activity timeline, transport arrival order, or Inspector round numbers to
 * invent transcript order. Noncontiguous occurrences keep separate identities.
 */
import type {
  ConversationBlockViewModel,
  ConversationTurnViewModel,
} from './view-model/types';
import { toolExecutionLlmRound, toolRoundAttention } from './tool-execution-groups';

import { swarmLaunchFailure } from './swarm-agent-presentation';

type RecordValue = Readonly<Record<string, unknown>>;
const record = (value: unknown): RecordValue => value !== null
  && typeof value === 'object' && !Array.isArray(value) ? value as RecordValue : {};
const identifier = (...values: unknown[]): string => {
  const value = values.find((item) => typeof item === 'string' && item.length > 0);
  return typeof value === 'string' ? value : '';
};

export interface RoundIdentity {
  /** Backend's zero-based model iteration, NOT the Inspector request ordinal. */
  llmRound: number;
  attemptId: string;
  taskId: string;
  key: string;
}

export function roundIdentityOf(value: unknown): RoundIdentity | null {
  const source = record(value);
  const origin = record(source._round);
  const rawRound = toolExecutionLlmRound(value);
  const llmRound = typeof rawRound === 'string' && /^\d+$/.test(rawRound)
    ? Number(rawRound) : rawRound;
  if (typeof llmRound !== 'number' || !Number.isSafeInteger(llmRound) || llmRound < 0) {
    return null;
  }
  const attemptId = identifier(source.attemptId, source._attemptId,
    origin.attemptId, origin._attemptId);
  const taskId = identifier(source.taskId, source._taskId, origin.taskId, origin._taskId);
  return { llmRound, attemptId, taskId, key: JSON.stringify([attemptId, taskId, llmRound]) };
}

export interface RoundOutlineSection {
  /** First durable content block keys this occurrence even if another round repeats. */
  id: string;
  identity: RoundIdentity;
  blockIds: string[];
  toolCount: number;
  activeCount: number;
  backgroundCount: number;
  attentionCount: number;
  state: 'active' | 'background' | 'attention' | 'returned';
  defaultExpanded: boolean;
}

export function blockRoundIdentity(block: ConversationBlockViewModel): RoundIdentity | null {
  // Deliverables are never hidden by an activity disclosure. Diagnostics cannot
  // lend their (possibly legacy/ambiguous) round metadata to content grouping.
  if (block.kind === 'text' && (block.deliverable || block.terminal)) return null;
  if (block.kind !== 'tool' && block.kind !== 'program'
      && block.kind !== 'thinking' && block.kind !== 'text') return null;
  // The ordered segment owns scope; a rich body is only a legacy fallback.
  return roundIdentityOf(block.source) ?? (
    block.kind === 'tool' || block.kind === 'program'
      ? roundIdentityOf(block.round) : null
  );
}

/** Every tool/program action owns its summary; model-round identity is metadata. */
export function toolBlockOwnsSummary(block: ConversationBlockViewModel): boolean {
  return block.kind === 'tool' || block.kind === 'program';
}

export type ActionActivityState = 'active' | 'background' | 'attention' | 'returned';

/** State belongs to this action, never its prose or neighboring model round. */
export function toolBlockActivityState(block: ConversationBlockViewModel): ActionActivityState {
  if (block.kind !== 'tool' && block.kind !== 'program') return 'returned';
  const source = record(block.source);
  const result = record(source.result);
  const value = block.round ?? { ...source, status: source.status ?? result.status };
  const attention = toolRoundAttention(value);
  if (isDetachedTool(value)) return 'background';
  if (attention === 'active') return 'active';
  if (attention === 'error' || attention === 'interactive' || hasFailedResult(value)
      || swarmLaunchFailure(record(value))) return 'attention';
  return 'returned';
}

function isDetachedTool(value: unknown): boolean {
  const source = record(value);
  if (source.backgroundCompleted === true) return false;
  const results = Array.isArray(source.results) && source.results.length
    ? source.results : [source.metadata ?? source.result ?? source];
  // Any unresolved child keeps the round live; inspect every result, not just
  // the first one. A dispatch response is not background-command completion.
  return results.some((value) => {
    const metadata = record(value);
    return (metadata.backgrounded === true || metadata.exitCode === 'background'
        || source.backgrounded === true || metadata.status === 'background')
      && metadata.backgroundCompleted !== true;
  });
}

function hasFailedResult(value: unknown): boolean {
  const source = record(value);
  const results = Array.isArray(source.results) ? source.results : [source.result];
  return results.some((value) => {
    const result = record(value);
    const exit = result.exitCode;
    return result.isError === true || Boolean(result.error)
      || result.timedOut === true || result.interrupted === true || result.notRun === true
      || (typeof exit === 'number' && exit !== 0)
      || (typeof exit === 'string' && /^(?:-?\d+|not-run|interrupted|timeout)$/.test(exit)
        && exit !== '0');
  });
}

/** Linear scan, O(blocks); keeps every member in the authored segment order. */
export function selectRoundOutline(
  turn: ConversationTurnViewModel,
  blocks: ReadonlyArray<ConversationBlockViewModel> = turn.blocks,
): RoundOutlineSection[] {
  if (turn.role !== 'assistant') return [];

  const live = turn.status === 'running' || turn.status === 'pending';
  const sections: RoundOutlineSection[] = [];
  let current: RoundOutlineSection | undefined;
  for (const block of blocks) {
    const identity = blockRoundIdentity(block);
    if (!identity) {
      current = undefined;
      continue;
    }
    if (!current || current.identity.key !== identity.key) {
      current = {
        id: `round:${block.blockId}`, identity, blockIds: [], toolCount: 0,
        activeCount: 0, backgroundCount: 0, attentionCount: 0,
        state: 'returned', defaultExpanded: true,
      };
      sections.push(current);
    }
    current.blockIds.push(block.blockId);
    if (block.kind === 'tool' || block.kind === 'program') {
      const source = record(block.source);
      const result = record(source.result);
      const value = block.round ?? {
        ...source, status: source.status ?? result.status,
      };
      const attention = toolRoundAttention(value);
      current.toolCount += 1;
      if (isDetachedTool(value)) current.backgroundCount += 1;
      else if (attention === 'active') current.activeCount += 1;
      else if (attention === 'error' || attention === 'interactive' || hasFailedResult(value)
          || swarmLaunchFailure(record(value))) current.attentionCount += 1;
    }
    // `terminal` identifies the current tail accumulator, not completion.
    // Historical pre-tool reasoning is non-terminal even in a completed turn.
    if (live && block.kind === 'thinking' && block.terminal) current.activeCount += 1;
  }
  for (const section of sections) {
    section.state = section.activeCount ? 'active'
      : section.backgroundCount ? 'background'
        : section.attentionCount ? 'attention' : 'returned';
    // Routine tool bodies stay compact even in a live turn. Prose is never
    // controlled by this disclosure; active, detached and failed tools stay visible.
    section.defaultExpanded = section.state !== 'returned';
  }
  return sections;
}
