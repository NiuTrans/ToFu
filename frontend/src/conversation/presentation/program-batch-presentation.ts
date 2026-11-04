/**
 * Sibling-program batches: a model round's programs read as one summary.
 *
 * Responsibility: state each program's outcome as its card does, count a
 * batch's outcomes in a fixed reading order, and choose its head attention and
 * default fold. A lone program is not a batch and keeps its own card behavior.
 * Entry points: `programBatchStatus`, `summarizeProgramBatch`,
 * `programBatchLabelParts`, `programCardOpensItself`. Consumers: the Surface
 * `RoundOutline` (Turn blocks), the classic tool-group batch (rounds), the
 * program card and rich-card reconciliation; grouping stays theirs.
 * Dependencies: program gateway/execution presentation and the tool attention
 * fold policy. Pure: no DOM, state or transport.
 */
import { normalizeProgramPresentation } from './program-gateway-presentation';
import { programExecutionPresentation } from './program-execution-presentation';
import {
  shouldCollapseProgramBatch, summarizeToolAttention, type ToolAttentionLevel,
} from './tool-execution-groups';

/** Reading order of a batch head: work in flight first, then outcomes. */
export const PROGRAM_BATCH_STATUSES = [
  'running', 'waiting', 'completed', 'returned', 'handoff', 'failed', 'incomplete',
] as const;
export type ProgramBatchStatus = typeof PROGRAM_BATCH_STATUSES[number];

export type ProgramBatchLabel = 'roundOutline.programs' | 'roundOutline.programsRunning'
  | 'roundOutline.programsWaiting' | 'roundOutline.programsCompleted'
  | 'roundOutline.programsReturned' | 'roundOutline.programsHandoff'
  | 'roundOutline.programsFailed' | 'roundOutline.programsIncomplete';

const STATUS_LABELS: Readonly<Record<ProgramBatchStatus, ProgramBatchLabel>> = {
  running: 'roundOutline.programsRunning',
  waiting: 'roundOutline.programsWaiting',
  completed: 'roundOutline.programsCompleted',
  returned: 'roundOutline.programsReturned',
  handoff: 'roundOutline.programsHandoff',
  failed: 'roundOutline.programsFailed',
  incomplete: 'roundOutline.programsIncomplete',
};

export interface ProgramBatchSummary {
  readonly programCount: number;
  /** Non-empty outcomes in `PROGRAM_BATCH_STATUSES` order. */
  readonly statuses: ReadonlyArray<{ readonly status: ProgramBatchStatus; readonly count: number }>;
  /** Head attention in the direct-batch vocabulary; a handoff is `important`. */
  readonly attention: ToolAttentionLevel;
  readonly defaultCollapsed: boolean;
}

/**
 * A program's outcome as its card states it: a gateway call in flight has not
 * started (`waiting`). `live` is the owning Turn's lifecycle when the caller
 * knows it; nothing in a settled Turn is still in flight.
 */
export function programBatchStatus(value: unknown, live?: boolean): ProgramBatchStatus {
  const row = normalizeProgramPresentation(value);
  const waiting = row.programStatus === 'waiting';
  if (waiting || row.status === 'searching' || row.programStatus === 'running') {
    return live === false ? 'incomplete' : waiting ? 'waiting' : 'running';
  }
  if (programExecutionPresentation(row).needsModel) return 'handoff';
  if (row.programStatus === 'error') return 'failed';
  if (row.programStatus === 'completed') return 'completed';
  if (row.programStatus === 'returned') return 'returned';
  return 'incomplete';
}

/**
 * Summary of two or more sibling programs, or null for fewer. `members` are
 * every value the batch folds (programs and their execution children); a
 * member awaiting the reader keeps the batch open and marks its head. A child
 * failure is its program's evidence, already returned to the model.
 */
export function summarizeProgramBatch(
  programs: readonly unknown[],
  live?: boolean,
  members: readonly unknown[] = programs,
): ProgramBatchSummary | null {
  if (programs.length < 2) return null;
  const counts = new Map<ProgramBatchStatus, number>();
  for (const program of programs) {
    const status = programBatchStatus(program, live);
    counts.set(status, (counts.get(status) ?? 0) + 1);
  }
  const attention: ToolAttentionLevel = counts.has('running') || counts.has('waiting') ? 'active'
    : counts.has('failed') ? 'error'
      : summarizeToolAttention(members).interactiveCount ? 'interactive' : 'important';
  return {
    programCount: programs.length,
    statuses: PROGRAM_BATCH_STATUSES.filter(status => counts.has(status))
      .map(status => ({ status, count: counts.get(status)! })),
    attention,
    defaultCollapsed: shouldCollapseProgramBatch(members),
  };
}

/**
 * Whether a program card (`data-program-attention`) opens without a reader
 * choice. A failure or an incomplete run always does, as direct-batch failures
 * do. A handoff returns to the model for judgment: only a lone program reveals
 * it; inside a batch its head counts it instead.
 */
export function programCardOpensItself(attention: string | null, batched: boolean): boolean {
  return attention === 'error' || attention === 'incomplete' || (attention === 'handoff' && !batched);
}

/** Localization keys and counts of a batch head, in reading order. */
export function programBatchLabelParts(summary: ProgramBatchSummary): Array<{
  key: ProgramBatchLabel; count: number; status?: ProgramBatchStatus;
}> {
  return [
    { key: 'roundOutline.programs', count: summary.programCount },
    ...summary.statuses.map(({ status, count }) => ({ key: STATUS_LABELS[status], count, status })),
  ];
}
