import { orchestrationRegistry } from './registry';
export interface OrchestrationHistoryState {
  canUndo: boolean;
  canRedo: boolean;
  atBaseline: boolean;
  baselinePersisted: boolean;
  index: number;
  length: number;
}

interface HistoryEntry {
  snapshot: unknown;
  key: string;
  fingerprint: string;
}

export interface OrchestrationHistoryOptions {
  limit?: unknown;
  coalesceWindow?: unknown;
  fingerprint?: (snapshot: unknown) => unknown;
  capture?: () => unknown;
  apply?: (snapshot: unknown) => unknown;
  now?: () => number;
  onChange?: (state: OrchestrationHistoryState) => unknown;
}

export interface OrchestrationHistoryController {
  state(): OrchestrationHistoryState;
  captureCurrent(): unknown;
  reset(snapshot: unknown, options?: { persisted?: unknown }): unknown;
  resetCurrent(options?: { persisted?: unknown }): unknown;
  record(snapshot: unknown, group?: unknown): boolean;
  recordCurrent(group?: unknown): boolean;
  sync(snapshot: unknown): boolean | unknown;
  syncCurrent(): boolean | unknown;
  markBaseline(snapshot: unknown, persisted?: unknown): OrchestrationHistoryState;
  detachBaseline(): OrchestrationHistoryState;
  undo(): unknown;
  redo(): unknown;
  undoAndApply(): unknown;
  redoAndApply(): unknown;
}

type HistoryWindow = Window & {
  createOrchestrationHistoryController?:
    typeof createOrchestrationHistoryController;
};

/** Bounded detached snapshots plus a persistence fingerprint baseline. */
export function createOrchestrationHistoryController(
  options: OrchestrationHistoryOptions = {},
): OrchestrationHistoryController {
  let entries: HistoryEntry[] = [];
  let index = -1;
  let baselineFingerprint: string | null = null;
  let baselinePersisted = false;
  let lastGroup = '';
  let lastRecordedAt = 0;
  const limit = Math.max(2, Number(options.limit) || 100);
  const coalesceWindow = Math.max(
    0, Number(options.coalesceWindow) || 700);

  const clone = (value: unknown): unknown => value == null
    ? value : JSON.parse(JSON.stringify(value));
  const serialize = (value: unknown): string =>
    JSON.stringify(value == null ? null : value) as string;
  const fingerprint = (snapshot: unknown): string => {
    const value = typeof options.fingerprint === 'function'
      ? options.fingerprint(snapshot) : snapshot;
    return typeof value === 'string' ? value : serialize(value);
  };
  const captureCurrent = (): unknown => clone(
    typeof options.capture === 'function' ? options.capture() : null);
  const entry = (snapshot: unknown): HistoryEntry => {
    const detached = clone(snapshot);
    return {
      snapshot: detached,
      key: serialize(detached),
      fingerprint: fingerprint(detached),
    };
  };

  const state = (): OrchestrationHistoryState => {
    const current = index >= 0 ? entries[index] : null;
    return {
      canUndo: index > 0,
      canRedo: index >= 0 && index < entries.length - 1,
      atBaseline: Boolean(current) && baselineFingerprint !== null
        && current?.fingerprint === baselineFingerprint,
      baselinePersisted,
      index,
      length: entries.length,
    };
  };
  const emit = (): OrchestrationHistoryState => {
    const value = state();
    options.onChange?.(value);
    return value;
  };

  const reset = (
    snapshot: unknown,
    resetOptions: { persisted?: unknown } = {},
  ): unknown => {
    const initial = entry(snapshot);
    entries = [initial];
    index = 0;
    baselineFingerprint = initial.fingerprint;
    baselinePersisted = Boolean(resetOptions.persisted);
    lastGroup = '';
    lastRecordedAt = 0;
    emit();
    return clone(initial.snapshot);
  };

  const recordSnapshot = (snapshot: unknown, group?: unknown): boolean => {
    const next = entry(snapshot);
    const current = index >= 0 ? entries[index] : null;
    if (current && current.key === next.key) return false;
    const now = typeof options.now === 'function'
      ? options.now() : Date.now();
    const groupKey = String(group || '');
    if (index < entries.length - 1) entries = entries.slice(0, index + 1);
    if (groupKey && groupKey === lastGroup && index > 0
        && now - lastRecordedAt <= coalesceWindow) {
      entries[index] = next;
    } else {
      entries.push(next);
      index = entries.length - 1;
      if (entries.length > limit) {
        entries.shift();
        index -= 1;
      }
    }
    lastGroup = groupKey;
    lastRecordedAt = now;
    emit();
    return true;
  };

  const sync = (snapshot: unknown): boolean | unknown => {
    if (index < 0) return reset(snapshot);
    const prior = entries[index];
    const next = entry(snapshot);
    if (prior?.key === next.key) return false;
    const wasBaseline = baselineFingerprint !== null
      && prior?.fingerprint === baselineFingerprint;
    entries[index] = next;
    if (wasBaseline) baselineFingerprint = next.fingerprint;
    lastGroup = '';
    lastRecordedAt = 0;
    emit();
    return true;
  };

  const markBaseline = (
    snapshot: unknown,
    persisted?: unknown,
  ): OrchestrationHistoryState => {
    baselineFingerprint = fingerprint(clone(snapshot));
    baselinePersisted = Boolean(persisted);
    lastGroup = '';
    lastRecordedAt = 0;
    return emit();
  };
  const detachBaseline = (): OrchestrationHistoryState => {
    baselineFingerprint = null;
    baselinePersisted = false;
    lastGroup = '';
    lastRecordedAt = 0;
    return emit();
  };
  const move = (delta: number): unknown => {
    const target = index + delta;
    if (target < 0 || target >= entries.length) return null;
    index = target;
    lastGroup = '';
    lastRecordedAt = 0;
    emit();
    return clone(entries[index]?.snapshot);
  };
  const moveAndApply = (delta: number): unknown => {
    const snapshot = move(delta);
    if (snapshot === null) return false;
    if (typeof options.apply !== 'function') return snapshot;
    return options.apply(snapshot) !== false;
  };

  return {
    state,
    captureCurrent,
    reset,
    resetCurrent: (resetOptions) => reset(captureCurrent(), resetOptions),
    record: recordSnapshot,
    recordCurrent: (group) => recordSnapshot(captureCurrent(), group),
    sync,
    syncCurrent: () => sync(captureCurrent()),
    markBaseline,
    detachBaseline,
    undo: () => move(-1),
    redo: () => move(1),
    undoAndApply: () => moveAndApply(-1),
    redoAndApply: () => moveAndApply(1),
  };
}

(orchestrationRegistry as unknown as HistoryWindow).createOrchestrationHistoryController =
  createOrchestrationHistoryController;
