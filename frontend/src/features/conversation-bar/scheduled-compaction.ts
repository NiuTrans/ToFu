/**
 * Responsibility: recognize the server's scheduled working-view compaction
 * reply (and its cancellation), and remember, for this page, which
 * conversations still wait for the request to apply.
 * Entry points: readScheduledCompaction, scheduledCompactionNotice,
 * scheduledCompactionState, readCancelledCompaction, cancelledCompactionNotice,
 * createScheduledCompactions. The durable owner-scoped marker lives on the
 * server (docs/CONTEXT_COMPACTION.md#coordinator-context-lifecycle); this
 * module holds only a reconstructible acknowledgment. No DOM or application globals.
 */
import { COMPACTION_HISTORY_LIMITS } from '../../core/compaction-history-state';

/** When the request applies: a woken running reply's next model call, or the next reply. */
export type ScheduledCompactionApplies = 'next_model_call' | 'next_reply';

export interface ScheduledCompaction {
  readonly requestId: string;
  readonly requestedAt: number;
  readonly alreadyScheduled: boolean;
  /** null: an ordinary conversation scheduled because its task was running. */
  readonly protection: 'coordinator' | 'project' | null;
  readonly appliesAt: ScheduledCompactionApplies;
}

export function readScheduledCompaction(result: unknown): ScheduledCompaction | null {
  if (result === null || typeof result !== 'object' || Array.isArray(result)) return null;
  const value = result as Readonly<Record<string, unknown>>;
  const { requestId, requestedAt, alreadyScheduled, protection, appliesAt } = value;
  if (value.ok !== true || value.scheduled !== true
      || typeof requestId !== 'string' || !requestId
      || typeof requestedAt !== 'number' || !Number.isFinite(requestedAt)
      || typeof alreadyScheduled !== 'boolean'
      || (protection !== 'coordinator' && protection !== 'project' && protection !== null)
      || (appliesAt !== 'next_model_call' && appliesAt !== 'next_reply')) return null;
  return { requestId, requestedAt, alreadyScheduled, protection, appliesAt };
}

type Translate = (key: string, fallback: string) => string;

export function scheduledCompactionNotice(scheduled: ScheduledCompaction, translate: Translate): string {
  if (scheduled.alreadyScheduled) {
    return scheduled.appliesAt === 'next_model_call'
      ? translate('compactNow.alreadyScheduledNextCall',
        'Compaction is already scheduled for the next model call')
      : translate('compactNow.alreadyScheduled', 'Compaction is already scheduled for the next reply');
  }
  return scheduled.appliesAt === 'next_model_call'
    ? translate('compactNow.scheduledNextCall',
      'Compaction scheduled: the running reply compacts its working context before its next model call; the conversation history stays intact')
    : translate('compactNow.scheduled',
      'Compaction scheduled: the next reply compacts the working context; the conversation history stays intact');
}

/** The waiting state shown on the context control. */
export function scheduledCompactionState(appliesAt: ScheduledCompactionApplies, translate: Translate): string {
  return appliesAt === 'next_model_call'
    ? translate('compactNow.scheduledStateNextCall', 'Compaction scheduled for the next model call')
    : translate('compactNow.scheduledState', 'Compaction scheduled for the next reply');
}

export interface CancelledCompaction {
  /** The pending marker was withdrawn before any model call claimed it. */
  readonly cancelled: boolean;
  /** A running reply had already started compacting for this request. */
  readonly applying: boolean;
}

export function readCancelledCompaction(result: unknown): CancelledCompaction | null {
  if (result === null || typeof result !== 'object' || Array.isArray(result)) return null;
  const { ok, cancelled, applying } = result as Readonly<Record<string, unknown>>;
  if (ok !== true || typeof cancelled !== 'boolean' || typeof applying !== 'boolean') return null;
  return { cancelled, applying };
}

export function cancelledCompactionNotice(
  result: CancelledCompaction, translate: Translate,
): { msg: string; level: 'info' | 'success' } {
  if (result.applying) {
    return { level: 'info', msg: translate('compactNow.cancelApplying',
      'The running reply had already started this compaction; it completes, and no later model call repeats it') };
  }
  return result.cancelled
    ? { level: 'success', msg: translate('compactNow.cancelled', 'Scheduled compaction cancelled') }
    : { level: 'info', msg: translate('compactNow.cancelNothing',
      'No compaction was still scheduled; it may already have been applied') };
}

export interface RunObservation {
  readonly busy: boolean;
  /** Turns currently known for the conversation; a new run adds Turns. */
  readonly turns: number;
}

export interface ScheduledCompactions {
  /** Remember a validated reply; returns it, or null for any other reply. */
  readonly accept: (convId: string, result: unknown, observed: RunObservation) => ScheduledCompaction | null;
  /** True while the run that applies the request has not settled. */
  readonly pending: (convId: string | null, observed: RunObservation) => boolean;
  /** The acknowledged request still waiting for this conversation, or null. */
  readonly current: (convId: string | null) => ScheduledCompaction | null;
  /** Drop the acknowledgment (after the server confirmed a cancellation). */
  readonly forget: (convId: string) => void;
}

/* awaiting-idle: a run that cannot apply the request is still going;
 * awaiting-run: the next run applies it; applying: that run is in progress. */
type Phase = 'awaiting-idle' | 'awaiting-run' | 'applying';

/**
 * A woken running reply applies the request before its next model call, so
 * the acknowledgment retires once that run settles. A request for the next
 * reply first waits for any run that cannot apply it, then for the next run
 * (busy, or new Turns appeared while this page looked away) to settle. A
 * request that run could not apply stays pending on the server and a
 * repeated click reports it as scheduled.
 */
export function createScheduledCompactions(): ScheduledCompactions {
  const entries = new Map<string, { scheduled: ScheduledCompaction; phase: Phase; turns: number }>();
  return {
    accept(convId, result, observed) {
      const scheduled = readScheduledCompaction(result);
      if (!scheduled || !convId) return scheduled;
      entries.delete(convId);
      const phase: Phase = !observed.busy ? 'awaiting-run'
        : scheduled.appliesAt === 'next_model_call' ? 'applying' : 'awaiting-idle';
      entries.set(convId, { scheduled, phase, turns: observed.turns });
      // Same per-page conversation bound as the compaction-history presentation.
      while (entries.size > COMPACTION_HISTORY_LIMITS.conversations) {
        entries.delete(entries.keys().next().value as string);
      }
      return scheduled;
    },
    pending(convId, observed) {
      const entry = convId ? entries.get(convId) : undefined;
      if (!entry || !convId) return false;
      if (entry.phase === 'awaiting-idle') {
        if (!observed.busy) { entry.phase = 'awaiting-run'; entry.turns = observed.turns; }
        return true;
      }
      if (observed.busy) {
        entry.phase = 'applying';
        return true;
      }
      if (entry.phase === 'applying' || observed.turns > entry.turns) {
        entries.delete(convId);
        return false;
      }
      return true;
    },
    current(convId) {
      return (convId && entries.get(convId)?.scheduled) || null;
    },
    forget(convId) {
      entries.delete(convId);
    },
  };
}
