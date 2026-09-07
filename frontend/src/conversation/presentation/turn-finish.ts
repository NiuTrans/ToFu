/** Pure terminal presentation derived only from the generated turn contract. */
import type { TurnRecord, TurnResumeOption } from '../../api/conversation-sync.generated';
import { normalizeErrorEnvelope } from '../../api/errors';

/** Presentation shape also admits pre-v3 string rows during archive migration. */
export type ResumeOption = Omit<Partial<TurnResumeOption>, 'operation'> & {
  operation: string;
};

export interface TurnFinishPresentation {
  tone: 'success' | 'warning' | 'error';
  label: 'Completed' | 'Interrupted' | 'Truncated' | 'Failed';
  detail: string;
  errorKind?: string;
  retryable?: boolean;
  resumeOptions?: ResumeOption[];
}

function text(value: unknown, fallback: string): string {
  return typeof value === 'string' && value ? value : fallback;
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
