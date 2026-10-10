/**
 * Responsibility: passive, bounded evidence for recovered browser main-thread stalls.
 * Entry: startBrowserResponsivenessMonitor; dependencies are observer, visibility,
 * monotonic clock, canonical context reader and existing log relay ports.
 * No polling, network owner, transcript copies, or import-time side effects.
 */
import type { TurnState } from './turn-state';

/** Read only bounded identifiers from the latest canonical main-lane turn.
 * Context is sampled at observer delivery, not proof of stall causation. */
export function responsivenessContext(conversationId: unknown, state?: TurnState | null): Record<string, unknown> {
  const label = (value: unknown) => typeof value === 'string' ? value.slice(0, 64) : '';
  const ids = state?.laneOrder.main ?? [];
  const turn = state?.turnsById[ids[ids.length - 1]];
  const attempt = turn?.currentAttemptId ? state?.attemptsById[turn.currentAttemptId] : null;
  const rounds = turn?.projection?.toolRounds;
  const round = Array.isArray(rounds) ? rounds[rounds.length - 1] : null;
  return { conversationId: label(conversationId), turnId: label(turn?.turnId),
    attemptId: label(turn?.currentAttemptId), taskId: label(attempt?.taskId),
    transport: label(state?.transport),
    tool: label(round?.toolName), toolCallId: label(round?.toolCallId),
    toolStatus: label(round?.status) };
}

export interface ResponsivenessEntry {
  entryType: string;
  startTime: number;
  duration: number;
  blockingDuration?: number;
  scripts?: ReadonlyArray<{ sourceURL?: string; sourceFunctionName?: string;
    duration?: number; forcedStyleAndLayoutDuration?: number }>;
}
export interface ResponsivenessPorts {
  observe(type: string, receive: (entries: ResponsivenessEntry[]) => void): (() => void) | null;
  visibility: Pick<Document, 'hidden' | 'addEventListener' | 'removeEventListener'>;
  now(): number;
  readonly timeOriginMs?: number;
  context(): Record<string, unknown>;
  report(message: string): void;
}

export function startBrowserResponsivenessMonitor(ports: ResponsivenessPorts): { dispose(): void } {
  let disposed = false;
  let generation = 0;
  let disconnectors: Array<() => void> = [];
  const checkpoints = new Map<string, { at: number; suppressed: number; maximumMs: number }>();
  const receive = (entries: ResponsivenessEntry[]): void => {
    if (disposed || ports.visibility.hidden) return;
    for (const entry of entries) {
      if (!Number.isFinite(entry.duration) || entry.duration < 250) continue;
      const previous = checkpoints.get(entry.entryType);
      const now = ports.now();
      if (previous && now - previous.at < 5000) {
        previous.suppressed++;
        previous.maximumMs = Math.max(previous.maximumMs, entry.duration);
        continue;
      }
      checkpoints.set(entry.entryType, { at: now, suppressed: 0, maximumMs: 0 });
      try {
        const evidence: Record<string, unknown> = {
          event: 'browser.main_thread_stall', kind: entry.entryType,
          startMs: Math.round(entry.startTime), durationMs: Math.round(entry.duration),
          startAtMs: typeof ports.timeOriginMs === 'number'
            ? Math.round(ports.timeOriginMs + entry.startTime) : undefined,
          observedDelayMs: Math.max(0, Math.round(now - entry.startTime - entry.duration)),
          ...ports.context(),
        };
        if (previous?.suppressed) {
          evidence.suppressed = previous.suppressed;
          evidence.suppressedMaximumMs = Math.round(previous.maximumMs);
        }
        if (typeof entry.blockingDuration === 'number') {
          evidence.blockingMs = Math.round(entry.blockingDuration);
        }
        // One slowest script; omit URL query/fragment and all code/arguments/results.
        let slowest: NonNullable<ResponsivenessEntry['scripts']>[number] | undefined;
        for (const script of entry.scripts ?? []) {
          if (!slowest || (script.duration ?? 0) > (slowest.duration ?? 0)) slowest = script;
        }
        if (slowest) {
          let path = '';
          try { path = new URL(slowest.sourceURL ?? '').pathname.slice(-120); } catch { /* unavailable */ }
          evidence.script = { path, function: String(slowest.sourceFunctionName ?? '').slice(0, 60),
            durationMs: Math.round(slowest.duration ?? 0),
            layoutMs: Math.round(slowest.forcedStyleAndLayoutDuration ?? 0) };
        }
        // Keep each structured record intact inside the relay's 800-character cap.
        let encoded = JSON.stringify(evidence);
        if (encoded.length > 760) { delete evidence.script; encoded = JSON.stringify(evidence); }
        if (encoded.length > 760) {
          delete evidence.toolStatus; delete evidence.transport;
          encoded = JSON.stringify(evidence);
        }
        ports.report('[browser-responsiveness] ' + encoded);
      } catch { /* diagnostics never interrupt rendering or observer delivery */ }
    }
  };
  const disconnect = (): void => {
    generation++;
    for (const stop of disconnectors.splice(0)) { try { stop(); } catch { /* fail soft */ } }
  };
  const onVisibility = (): void => {
    disconnect();
    checkpoints.clear();
    if (disposed || ports.visibility.hidden) return;
    for (const type of ['longtask', 'long-animation-frame']) {
      try {
        const observedGeneration = generation;
        const stop = ports.observe(type, entries => {
          if (observedGeneration === generation) receive(entries);
        });
        if (stop) disconnectors.push(stop);
      } catch { /* unsupported browser entry type */ }
    }
  };
  ports.visibility.addEventListener('visibilitychange', onVisibility);
  onVisibility();
  return { dispose() {
    if (disposed) return;
    disposed = true;
    disconnect();
    checkpoints.clear();
    ports.visibility.removeEventListener('visibilitychange', onVisibility);
  } };
}
