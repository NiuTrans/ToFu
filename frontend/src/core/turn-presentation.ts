import type { ProjectionTurn } from './turn-projection';

export interface ResumeOption extends Record<string, unknown> {
  operation: string;
}

export interface TurnFinishPresentation {
  tone: 'success' | 'warning' | 'error';
  label: 'Completed' | 'Interrupted' | 'Truncated' | 'Failed';
  detail: string;
  resumeOptions?: ResumeOption[];
}

function text(value: unknown, fallback: string): string {
  return typeof value === 'string' && value ? value : fallback;
}

/** Normalize server resume operations once before any renderer consumes them. */
export function resumeTurnOptions(
  turn: ProjectionTurn | null | undefined,
): ResumeOption[] {
  const raw = turn?.settlement?.resumeOptions;
  if (!Array.isArray(raw)) return [];
  return raw.flatMap((item): ResumeOption[] => {
    if (typeof item === 'string') return item ? [{ operation: item }] : [];
    if (!item || typeof item !== 'object' || Array.isArray(item)) return [];
    const candidate = item as Record<string, unknown>;
    return typeof candidate.operation === 'string' && candidate.operation
      ? [candidate as ResumeOption]
      : [];
  });
}

/** Pure view model for terminal turn rendering. */
export function presentTurnFinish(
  turn: ProjectionTurn | null | undefined,
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
  return {
    tone: 'error',
    label: 'Failed',
    detail: text(settlement.error,
      text(settlement.cause, 'generation_failed')),
    resumeOptions: resumeTurnOptions(turn),
  };
}
