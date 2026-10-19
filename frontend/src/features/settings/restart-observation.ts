/** Restart health projection. Entry: projectRestartHealth; no I/O or timers.
 * Boot identity proves replacement; server phases, never elapsed time, prove
 * progress. The retained update bridge owns one observation for its lifetime.
 */
export interface RestartBoot {
  status: string;
  phaseTotal?: number;
  currentPhase?: { index: number; key: string } | null;
  failedPhase?: { index: number; key: string } | null;
  error?: string | null;
}
export type RestartHealthVerdict =
  | { kind: 'unavailable' | 'old' }
  | { kind: 'ready'; info: Record<string, unknown> }
  | { kind: 'booting' | 'failed'; boot: RestartBoot };

export function projectRestartHealth(payload: unknown, previousBootId: string | null): RestartHealthVerdict {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return { kind: 'unavailable' };
  const info = payload as Record<string, unknown>;
  if (!info.ok) return { kind: 'unavailable' };
  if (previousBootId && info.bootId === previousBootId) return { kind: 'old' };
  const boot = info.boot as RestartBoot | undefined;
  if (boot && typeof boot.status === 'string' && boot.status !== 'ready') {
    return { kind: boot.status === 'failed' ? 'failed' : 'booting', boot };
  }
  return { kind: 'ready', info };
}

/** A running phase advances only through completed predecessors. */
export function restartPhaseProgress(boot: RestartBoot): { pct: number; key: string } | null {
  const phase = boot.currentPhase;
  const total = boot.phaseTotal;
  if (!phase || !Number.isInteger(phase.index) || !total || !Number.isInteger(total)
      || phase.index < 1 || phase.index > total || !phase.key) return null;
  return { pct: 20 + 76 * (phase.index - 1) / total, key: phase.key };
}
