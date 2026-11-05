/** Pure terminal presentation derived only from the generated turn contract. */
import type { TurnRecord, TurnResumeOption, TurnTimingTrace } from '../../api/conversation-sync.generated';
import { normalizeErrorEnvelope } from '../../api/errors';

/** Presentation shape also admits pre-v3 string rows during archive migration. */
export type ResumeOption = Omit<Partial<TurnResumeOption>, 'operation'> & {
  operation: string;
};

export interface TurnFinishPresentation {
  tone: 'success' | 'warning' | 'error';
  label: 'Completed' | 'Interrupted' | 'Truncated' | 'Refused' | 'Failed';
  detail: string;
  errorKind?: string;
  retryable?: boolean;
  resumeOptions?: ResumeOption[];
}

function text(value: unknown, fallback: string): string {
  return typeof value === 'string' && value ? value : fallback;
}

/** Settlement causes of a content-filter outcome (`lib/turn_verdict`). */
const REFUSAL_CAUSES: ReadonlySet<string> = new Set(['model_refusal', 'content_filter']);

/** A truncated turn ended by a model refusal or a gateway content filter. */
export function isRefusedTurn(
  turn: Pick<TurnRecord, 'status' | 'settlement'> | null | undefined,
): boolean {
  return turn?.status === 'truncated'
    && REFUSAL_CAUSES.has(String(turn.settlement?.cause ?? ''));
}

/** Use a matching, complete server clock; never reconstruct elapsed time. */
export function presentTurnExecutionTiming(
  trace: TurnTimingTrace | null | undefined,
  settledAtMs: number,
  expectedTaskId?: string,
): { settledAtMs: number; durationMs: number | null } {
  const matchesTask = !expectedTaskId || trace?.taskId === expectedTaskId;
  const serverEnd = matchesTask && trace?.running === false
    && typeof trace.tEnd === 'number' && Number.isFinite(trace.tEnd) && trace.tEnd > 0
    ? trace.tEnd : null;
  const durationMs = serverEnd !== null && trace?.clockSource === 'server-monotonic'
    && trace.coverage === 'full' && typeof trace.totalMs === 'number'
    && Number.isFinite(trace.totalMs) && trace.totalMs >= 0
    ? trace.totalMs : null;
  return {
    settledAtMs: serverEnd ?? (Number.isFinite(settledAtMs) ? Math.max(0, settledAtMs) : 0),
    durationMs,
  };
}

/** Normalize server resume operations once before any renderer consumes them. */
export function resumeTurnOptions(
  turn: TurnRecord | null | undefined,
): ResumeOption[] {
  const raw = turn?.settlement?.resumeOptions;
  if (!Array.isArray(raw)) return [];
  return (raw as ReadonlyArray<unknown>).flatMap((item): ResumeOption[] => {
    if (typeof item === 'string') return item ? [{ operation: item }] : [];
    if (!item || typeof item !== 'object' || Array.isArray(item)) return [];
    const candidate = item as Record<string, unknown>;
    return typeof candidate.operation === 'string' && candidate.operation
      ? [{ ...candidate, operation: candidate.operation } as ResumeOption]
      : [];
  });
}

/** Pure view model for terminal turn rendering. */
export function presentTurnFinish(
  turn: TurnRecord | null | undefined,
): TurnFinishPresentation | null {
  if (!turn || turn.status === 'running' || turn.status === 'pending') return null;
  const settlement = turn.settlement ?? {};
  if (turn.status === 'completed') {
    return { tone: 'success', label: 'Completed', detail: '' };
  }
  if (turn.status === 'interrupted') {
    return {
      tone: 'warning',
      label: 'Interrupted',
      detail: text(settlement.cause, 'generation_interrupted'),
      resumeOptions: resumeTurnOptions(turn),
    };
  }
  if (isRefusedTurn(turn)) {
    const error = normalizeErrorEnvelope(settlement.error);
    return {
      tone: 'warning',
      label: 'Refused',
      detail: text(error?.message, text(error?.detail, text(settlement.cause, 'content_filter'))),
      ...(error?.kind ? { errorKind: error.kind } : {}),
      resumeOptions: resumeTurnOptions(turn),
    };
  }
  if (turn.status === 'truncated') {
    return {
      tone: 'warning',
      label: 'Truncated',
      detail: text(settlement.cause,
        text(settlement.providerFinishReason, 'limit')),
      resumeOptions: resumeTurnOptions(turn),
    };
  }
  const error = normalizeErrorEnvelope(settlement.error);
  return {
    tone: error?.severity === 'warning' ? 'warning' : 'error',
    label: 'Failed',
    /* `cause` is a machine classification used for policy, never display
     * copy. Structured envelopes carry the real user-facing explanation. */
    detail: text(error?.message, text(error?.detail, '')),
    ...(error?.kind ? { errorKind: error.kind } : {}),
    ...(error ? { retryable: Boolean(error.retryable) } : {}),
    resumeOptions: resumeTurnOptions(turn),
  };
}
