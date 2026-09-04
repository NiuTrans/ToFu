/**
 * Responsibility: own eager, bounded client diagnostics and debug snapshots.
 * Entry point: createDebugRuntimeOwner. Dependencies: injected shell, report,
 * clipboard, event, and Turn-read ports. This module performs no import-time
 * browser work and owns no presentation or server transport policy.
 */

export const DEBUG_RUNTIME_LIMITS = Object.freeze({
  diagnosticLines: 80,
  reportedErrors: 200,
  conversations: 20,
  tasks: 20,
  roundsPerTask: 64,
  statesPerTask: 64,
  messageCharacters: 4_000,
});

export type DebugSnapshot = Record<string, unknown> & {
  readonly kind?: unknown;
  readonly turn?: unknown;
  readonly roundNum?: unknown;
  messages?: unknown;
  tools?: unknown;
  contextManifest?: unknown;
  _stripped?: boolean;
};

export interface DebugTaskSnapshots {
  readonly rounds: Record<string, DebugSnapshot>;
  readonly roundOrder: string[];
  readonly states: DebugSnapshot[];
}

export interface DebugShellState {
  activeConversationId: unknown;
  readonly conversations: readonly Record<string, unknown>[];
  readonly config: Record<string, unknown>;
  visible: boolean;
  readonly cache: Record<string, Record<string, unknown>>;
  readonly requests: Record<string, DebugTaskSnapshots>;
  recordSnapshot(taskId: unknown, snapshot: DebugSnapshot | null): void;
  reportError(message: unknown, extra?: unknown): void;
}

export interface DebugErrorEvent {
  readonly message?: unknown;
  readonly filename?: unknown;
  readonly lineno?: unknown;
  readonly colno?: unknown;
  readonly error?: { readonly stack?: unknown } | null;
}

export interface DebugRejectionEvent {
  readonly reason?: unknown;
}

export interface DebugClipboardTextarea {
  value: string;
  readonly style: { cssText: string };
  select(): void;
}

export interface DebugRuntimePorts {
  now(): number;
  writeConsole(level: string, message: unknown): void;
  warnConsole(message: string, error: unknown): void;
  currentUrl(): unknown;
  userAgent(): unknown;
  conversationCount(): unknown;
  report(payload: Record<string, unknown>): unknown;
  subscribeError(listener: (event: DebugErrorEvent) => void): () => void;
  subscribeUnhandledRejection(
    listener: (event: DebugRejectionEvent) => void,
  ): () => void;
  resolveClipboardWrite(): ((text: string) => Promise<unknown>) | null;
  createClipboardTextarea(): DebugClipboardTextarea;
  appendClipboardTextarea(textarea: DebugClipboardTextarea): void;
  removeClipboardTextarea(textarea: DebugClipboardTextarea): void;
  executeClipboardCopy(): void;
  activeConversationId(): unknown;
  conversations(): readonly Record<string, unknown>[];
  config(): Record<string, unknown>;
  visible(): boolean;
  setVisible(value: boolean): void;
  readTurnState(conversationId: unknown): unknown;
}

export interface DebugRuntimeOwner {
  readonly diagnosticRing: string[];
  readonly shellState: DebugShellState;
  debugLog(message: unknown, level?: string): void;
  reportClientError(message: unknown, extra?: unknown): void;
  safeClipboardWrite(text: unknown): Promise<void>;
  taskIdForRound(round: unknown): string;
  start(): void;
  dispose(): void;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object'
    ? value as Record<string, unknown>
    : null;
}

function boundedText(value: unknown, maximum: number): string {
  return String(value ?? '').slice(0, maximum);
}

function stripSnapshot(snapshot: DebugSnapshot): void {
  if (!snapshot.messages) return;
  snapshot.messages = null;
  snapshot.tools = null;
  snapshot.contextManifest = null;
  snapshot._stripped = true;
}

export function createDebugRuntimeOwner(
  ports: DebugRuntimePorts,
): DebugRuntimeOwner {
  const diagnosticRing: string[] = [];
  const reportedErrors = new Set<string>();
  const requests: Record<string, DebugTaskSnapshots> = {};
  const cacheStorage: Record<string, Record<string, unknown>> = {};
  const cache = new Proxy(cacheStorage, {
    set(target, property, value: unknown): boolean {
      const key = String(property);
      if (Object.hasOwn(target, key)) delete target[key];
      target[key] = value as Record<string, unknown>;
      while (Object.keys(target).length > DEBUG_RUNTIME_LIMITS.conversations) {
        delete target[Object.keys(target)[0]];
      }
      return true;
    },
  });
  let started = false;
  let disposeError: (() => void) | null = null;
  let disposeRejection: (() => void) | null = null;

  const reportClientError = (message: unknown, extra?: unknown): void => {
    try {
      const rendered = boundedText(message, DEBUG_RUNTIME_LIMITS.messageCharacters);
      const key = rendered.slice(0, 200);
      if (reportedErrors.has(key)) return;
      reportedErrors.add(key);
      while (reportedErrors.size > DEBUG_RUNTIME_LIMITS.reportedErrors) {
        const oldest = reportedErrors.values().next().value;
        if (typeof oldest !== 'string') break;
        reportedErrors.delete(oldest);
      }
      const count = Number(ports.conversationCount());
      const payload: Record<string, unknown> = {
        message: rendered,
        url: boundedText(ports.currentUrl(), 2_000),
        userAgent: boundedText(ports.userAgent(), 1_000),
        timestamp: new Date(ports.now()).toISOString(),
        conversationCount: Number.isFinite(count) && count > 0 ? count : 0,
      };
      if (extra !== undefined) payload.extra = extra;
      void Promise.resolve(ports.report(payload)).catch(() => undefined);
    } catch {
      // Client reporting must never become another application failure.
    }
  };

  const debugLog = (message: unknown, level = ''): void => {
    let normalizedLevel = typeof level === 'string' ? level.toLowerCase() : '';
    if (normalizedLevel === 'warning') normalizedLevel = 'warn';
    try {
      ports.writeConsole(normalizedLevel || 'info', message);
    } catch {
      // Console access is diagnostic-only.
    }
    try {
      diagnosticRing.push(
        `${ports.now()} [${normalizedLevel || 'info'}] ${boundedText(message, 300)}`,
      );
      if (diagnosticRing.length > DEBUG_RUNTIME_LIMITS.diagnosticLines) {
        diagnosticRing.splice(
          0,
          diagnosticRing.length - DEBUG_RUNTIME_LIMITS.diagnosticLines,
        );
      }
    } catch {
      // The diagnostic ring is best-effort.
    }
    if (normalizedLevel === 'error' || normalizedLevel === 'warn') {
      reportClientError(`[debugLog][${normalizedLevel}] ${String(message)}`);
    }
  };

  const recordSnapshot = (
    taskId: unknown,
    snapshot: DebugSnapshot | null,
  ): void => {
    const id = String(taskId ?? '');
    if (!id || !snapshot) return;
    const task = requests[id] ?? { rounds: {}, roundOrder: [], states: [] };
    if (requests[id]) delete requests[id];
    requests[id] = task;
    if (snapshot.kind === 'state') {
      task.states.push(snapshot);
      if (task.states.length > DEBUG_RUNTIME_LIMITS.statesPerTask) {
        task.states.splice(
          0,
          task.states.length - DEBUG_RUNTIME_LIMITS.statesPerTask,
        );
      }
    } else {
      const key = snapshot.turn
        ? `${String(snapshot.turn)}|${String(snapshot.roundNum)}`
        : String(snapshot.roundNum);
      if (!Object.hasOwn(task.rounds, key)) task.roundOrder.push(key);
      task.rounds[key] = snapshot;
      while (task.roundOrder.length > DEBUG_RUNTIME_LIMITS.roundsPerTask) {
        const oldest = task.roundOrder.shift();
        if (oldest !== undefined) delete task.rounds[oldest];
      }
    }
    for (const [otherId, otherTask] of Object.entries(requests)) {
      if (otherId === id) continue;
      Object.values(otherTask.rounds).forEach(stripSnapshot);
      otherTask.states.forEach(stripSnapshot);
    }
    while (Object.keys(requests).length > DEBUG_RUNTIME_LIMITS.tasks) {
      delete requests[Object.keys(requests)[0]];
    }
  };

  const safeClipboardWrite = async (value: unknown): Promise<void> => {
    const text = String(value ?? '');
    try {
      const nativeWrite = ports.resolveClipboardWrite();
      if (nativeWrite) {
        await nativeWrite(text);
        return;
      }
    } catch {
      // A denied clipboard capability falls through to the textarea path.
    }
    const textarea = ports.createClipboardTextarea();
    try {
      textarea.value = text;
      textarea.style.cssText = 'position:fixed;opacity:0;left:-9999px';
      ports.appendClipboardTextarea(textarea);
      textarea.select();
      ports.executeClipboardCopy();
    } finally {
      ports.removeClipboardTextarea(textarea);
    }
  };

  const taskIdForRound = (roundValue: unknown): string => {
    try {
      const round = asRecord(roundValue);
      const direct = round?.taskId ?? round?._taskId;
      if (direct) return String(direct);
      const turnId = String(round?._turnId ?? '');
      const state = asRecord(ports.readTurnState(ports.activeConversationId()));
      const attemptsById = asRecord(state?.attemptsById) ?? {};
      const attemptId = String(round?.attemptId ?? '');
      const ownedAttempt = attemptId ? asRecord(attemptsById[attemptId]) : null;
      if (ownedAttempt?.taskId) return String(ownedAttempt.taskId);
      const turnsById = asRecord(state?.turnsById) ?? {};
      const turn = turnId ? asRecord(turnsById[turnId]) : null;
      if (!turn || turn.actor === 'virtual_user') return '';
      const candidates = Object.values(attemptsById)
        .map(asRecord)
        .filter((attempt): attempt is Record<string, unknown> => (
          attempt !== null && attempt.turnId === turnId && Boolean(attempt.taskId)
        ))
        .sort((left, right) => (
          Number(right.createdAt ?? 0) - Number(left.createdAt ?? 0)
        ));
      return candidates[0]?.taskId ? String(candidates[0].taskId) : '';
    } catch (error: unknown) {
      try {
        ports.warnConsole('[ri] taskId-for-round resolve failed:', error);
      } catch {
        // Identity diagnostics are best-effort.
      }
      return '';
    }
  };

  const shellState: DebugShellState = Object.freeze({
    get activeConversationId(): unknown { return ports.activeConversationId(); },
    get conversations(): readonly Record<string, unknown>[] {
      return ports.conversations();
    },
    get config(): Record<string, unknown> { return ports.config(); },
    get visible(): boolean { return ports.visible(); },
    set visible(value: boolean) { ports.setVisible(Boolean(value)); },
    get cache(): Record<string, Record<string, unknown>> { return cache; },
    get requests(): Record<string, DebugTaskSnapshots> { return requests; },
    recordSnapshot,
    reportError: reportClientError,
  });

  const dispose = (): void => {
    try { disposeError?.(); } catch { /* best-effort listener cleanup */ }
    try { disposeRejection?.(); } catch { /* best-effort listener cleanup */ }
    disposeError = null;
    disposeRejection = null;
    started = false;
  };

  const start = (): void => {
    if (started) return;
    started = true;
    try {
      disposeError = ports.subscribeError((event) => {
        reportClientError(`[uncaught] ${String(event.message ?? '')}`, {
          source: event.filename,
          line: event.lineno,
          col: event.colno,
          stack: boundedText(event.error?.stack, 1_000),
        });
      });
    } catch (error: unknown) {
      try { ports.warnConsole('[debug] error listener unavailable:', error); } catch {}
    }
    try {
      disposeRejection = ports.subscribeUnhandledRejection((event) => {
        const reason = asRecord(event.reason);
        reportClientError(
          `[unhandledRejection] ${String(reason?.message ?? event.reason ?? 'unknown')}`,
          { stack: boundedText(reason?.stack, 1_000) },
        );
      });
    } catch (error: unknown) {
      try { ports.warnConsole('[debug] rejection listener unavailable:', error); } catch {}
    }
  };

  return Object.freeze({
    diagnosticRing,
    shellState,
    debugLog,
    reportClientError,
    safeClipboardWrite,
    taskIdForRound,
    start,
    dispose,
  });
}
