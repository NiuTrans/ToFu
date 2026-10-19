/**
 * Responsibility: passive, bounded evidence for recovered browser main-thread stalls.
 * Entry: startBrowserResponsivenessMonitor; dependencies are observer, visibility,
 * monotonic clock, canonical context reader and existing log relay ports.
 * One demand-only flush timer, no polling, transcript copies or network owner.
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
    sourceCharPosition?: number; invokerType?: string;
    duration?: number; forcedStyleAndLayoutDuration?: number }>;
}
export interface ResponsivenessObservation {
  disconnect(): void;
  takeRecords?(): ResponsivenessEntry[];
}
export interface ResponsivenessPorts {
  observe(type: string, receive: (entries: ResponsivenessEntry[]) => void): (() => void) | ResponsivenessObservation | null;
  visibility: Pick<Document, 'hidden' | 'addEventListener' | 'removeEventListener'>;
  lifecycle?: Pick<Window, 'addEventListener' | 'removeEventListener'>;
  schedule: { setTimeout(callback: () => void, delayMs: number): unknown; clearTimeout(handle: unknown): void };
  now(): number;
  wallNow?(): number;
  readonly timeOriginMs?: number;
  context(): Record<string, unknown>;
  interactionContext?(startMs: number, endMs: number): Record<string, unknown>;
  report(message: string): void;
}

export function startBrowserResponsivenessMonitor(ports: ResponsivenessPorts): { dispose(): void } {
  let disposed = false;
  let generation = 0;
  let observations: Array<(() => void) | ResponsivenessObservation> = [];
  let timer: unknown = null;
  let timerAt: number | null = null;
  type Evidence = Record<string, unknown> & { durationMs: number };
  const checkpoints = new Map<string, { at: number; reportedMs: number;
    suppressed: number; pending: Evidence | null }>();
  const report = (evidence: Evidence, suppressed = 0): void => {
    try {
      const record = { ...evidence };
      if (suppressed) record.suppressed = suppressed;
      // Keep attribution ahead of optional turn/tool context. Every fallback
      // reserializes an intact JSON object; never truncate the encoded line.
      let encoded = JSON.stringify(record);
      for (const key of ['toolStatus', 'transport', 'toolCallId', 'tool', 'attemptId', 'taskId', 'turnId',
        'interactionConversationId', 'observedAtMs', 'startAtMs', 'script', 'conversationId']) {
        if (encoded.length <= 760) break;
        delete record[key];
        encoded = JSON.stringify(record);
      }
      ports.report('[browser-responsiveness] ' + encoded);
    } catch { /* Diagnostic serialization/relay failure cannot interrupt rendering. */ }
  };
  const cancelTimer = (): void => {
    if (timer !== null) ports.schedule.clearTimeout(timer);
    timer = null;
    timerAt = null;
  };
  const flushPending = (all = false): void => {
    const now = ports.now();
    for (const checkpoint of checkpoints.values()) {
      if (!checkpoint.pending || (!all && now - checkpoint.at < 5000)) continue;
      report(checkpoint.pending, checkpoint.suppressed);
      checkpoint.reportedMs = checkpoint.pending.durationMs;
      checkpoint.pending = null;
      checkpoint.suppressed = 0;
      checkpoint.at = now;
    }
  };
  const schedulePending = (): void => {
    if (disposed || ports.visibility.hidden) { cancelTimer(); return; }
    const deadlines = [...checkpoints.values()].filter(item => item.pending).map(item => item.at + 5000);
    if (!deadlines.length) { cancelTimer(); return; }
    const deadline = Math.min(...deadlines);
    if (timer !== null && timerAt === deadline) return;
    cancelTimer();
    timerAt = deadline;
    timer = ports.schedule.setTimeout(() => {
      timer = null;
      timerAt = null;
      if (disposed) return;
      flushPending(ports.visibility.hidden);
      schedulePending();
    }, Math.max(0, deadline - ports.now()));
  };
  const evidenceFor = (entry: ResponsivenessEntry): Evidence | null => {
    try {
      const evidence: Evidence = {
        event: 'browser.main_thread_stall', kind: entry.entryType,
        startMs: Math.round(entry.startTime), durationMs: Math.round(entry.duration),
        startAtMs: typeof ports.timeOriginMs === 'number'
          ? Math.round(ports.timeOriginMs + entry.startTime) : undefined,
        observedDelayMs: Math.max(0, Math.round(ports.now() - entry.startTime - entry.duration)),
        observedAtMs: ports.wallNow?.(),
        ...ports.context(),
        ...ports.interactionContext?.(entry.startTime, entry.startTime + entry.duration),
      };
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
          charPosition: Number.isFinite(slowest.sourceCharPosition) ? slowest.sourceCharPosition : undefined,
          invokerType: String(slowest.invokerType ?? '').slice(0, 32),
          durationMs: Math.round(slowest.duration ?? 0),
          layoutMs: Math.round(slowest.forcedStyleAndLayoutDuration ?? 0) };
      }
      return evidence;
    } catch { return null; /* Diagnostic context/entry access may be unavailable during teardown. */ }
  };
  const receive = (entries: ResponsivenessEntry[], draining = false): void => {
    if (disposed || (!draining && ports.visibility.hidden)) return;
    // A recovered batch may contain an early short stall followed by the hang.
    // Pick its longest entry before rate limiting, with at most two groups.
    const batches = new Map<string, { longest: ResponsivenessEntry; count: number }>();
    for (const entry of entries) {
      if (!['longtask', 'long-animation-frame'].includes(entry.entryType)
          || !Number.isFinite(entry.duration) || !Number.isFinite(entry.startTime) || entry.duration < 250) continue;
      const batch = batches.get(entry.entryType);
      if (!batch) batches.set(entry.entryType, { longest: entry, count: 1 });
      else { batch.count++; if (entry.duration > batch.longest.duration) batch.longest = entry; }
    }
    for (const [kind, batch] of batches) {
      const evidence = evidenceFor(batch.longest);
      if (!evidence) continue;
      const now = ports.now();
      const previous = checkpoints.get(kind);
      if (previous && now - previous.at < 5000) {
        previous.suppressed += batch.count;
        if (!previous.pending || evidence.durationMs > previous.pending.durationMs) previous.pending = evidence;
        // One severity promotion per window. Later maxima still have a finite
        // drain deadline, even when no further stalls occur.
        if (evidence.durationMs >= 1000 && previous.reportedMs < 1000) {
          report(previous.pending, previous.suppressed);
          previous.reportedMs = previous.pending.durationMs;
          previous.pending = null;
          previous.suppressed = 0;
        }
      } else {
        const pending = previous?.pending;
        const selected = pending && pending.durationMs > evidence.durationMs ? pending : evidence;
        report(selected, (previous?.suppressed ?? 0) + batch.count - 1);
        checkpoints.set(kind, { at: now, reportedMs: selected.durationMs, suppressed: 0, pending: null });
      }
    }
    schedulePending();
  };
  const disconnect = (): void => {
    generation++;
    for (const observation of observations.splice(0)) {
      try {
        if (typeof observation === 'function') observation();
        else {
          try { receive(observation.takeRecords?.() ?? [], true); }
          finally { observation.disconnect(); }
        }
      } catch { /* Observer records/disconnection may be unavailable during renderer teardown. */ }
    }
  };
  const onVisibility = (): void => {
    disconnect();
    flushPending(true);
    cancelTimer();
    checkpoints.clear();
    if (disposed || ports.visibility.hidden) return;
    for (const type of ['longtask', 'long-animation-frame']) {
      try {
        const observedGeneration = generation;
        const stop = ports.observe(type, entries => {
          if (observedGeneration === generation) receive(entries);
        });
        if (stop) observations.push(stop);
      } catch { /* unsupported browser entry type */ }
    }
  };
  const onPageHide = (): void => {
    disconnect();
    flushPending(true);
    cancelTimer();
    checkpoints.clear();
  };
  ports.visibility.addEventListener('visibilitychange', onVisibility);
  ports.lifecycle?.addEventListener('pagehide', onPageHide);
  ports.lifecycle?.addEventListener('pageshow', onVisibility);
  onVisibility();
  return { dispose() {
    if (disposed) return;
    disconnect();
    flushPending(true);
    disposed = true;
    cancelTimer();
    checkpoints.clear();
    ports.visibility.removeEventListener('visibilitychange', onVisibility);
    ports.lifecycle?.removeEventListener('pagehide', onPageHide);
    ports.lifecycle?.removeEventListener('pageshow', onVisibility);
  } };
}
