import { orchestrationRegistry } from './registry';
import { type OrchestrationDocumentController } from './document';
import {
  type OrchestrationHistoryController,
  type OrchestrationHistoryState,
} from './history';

export interface OrchestrationSaveCheckpoint {
  readonly revision: number;
  readonly historySnapshot: unknown;
  readonly scope: unknown;
}

export interface OrchestrationSaveOutcome {
  readonly current: boolean;
  readonly exactRevision: boolean;
  readonly staleScope: boolean;
}

export interface OrchestrationEditLifecycleOptions {
  documentLifecycle: OrchestrationDocumentController;
  history: OrchestrationHistoryController;
  scope?: () => unknown;
}

type EditLifecycleWindow = Window & {
  createOrchestrationEditLifecycle?: typeof createOrchestrationEditLifecycle;
};

/** Coordinates document revisions, history and late save ownership. */
export function createOrchestrationEditLifecycle(
  options: OrchestrationEditLifecycleOptions,
) {
  const documentLifecycle = options?.documentLifecycle;
  const history = options?.history;
  if (!documentLifecycle || !history) {
    throw new TypeError('documentLifecycle and history are required');
  }
  const scope = (): unknown => typeof options.scope === 'function'
    ? options.scope() : null;

  const markDirty = (historyGroup?: unknown): boolean => {
    documentLifecycle.markDirty();
    return history.recordCurrent(historyGroup || '');
  };
  const adoptBaseline = (
    persisted: unknown,
    inspection?: unknown,
  ): unknown => {
    documentLifecycle.setBaseline(Boolean(persisted), inspection || null);
    return history.resetCurrent({ persisted: Boolean(persisted) });
  };
  const createSaveCheckpoint = (): OrchestrationSaveCheckpoint =>
    Object.freeze({
      revision: documentLifecycle.revision(),
      historySnapshot: history.captureCurrent(),
      scope: scope(),
    });
  const isSaveCheckpointCurrent = (
    checkpoint: OrchestrationSaveCheckpoint | null | undefined,
  ): boolean => Boolean(checkpoint) && checkpoint?.scope === scope();
  const saveOutcome = (
    current: unknown,
    exactRevision: unknown,
    staleScope: unknown,
  ): OrchestrationSaveOutcome => Object.freeze({
    current: Boolean(current),
    exactRevision: Boolean(exactRevision),
    staleScope: Boolean(staleScope),
  });
  const completeSaveCheckpoint = (
    checkpoint: OrchestrationSaveCheckpoint,
    inspection?: unknown,
  ): OrchestrationSaveOutcome => {
    if (!isSaveCheckpointCurrent(checkpoint)) {
      return saveOutcome(false, false, true);
    }
    const exactRevision = documentLifecycle.acknowledgeSaved(
      checkpoint.revision, inspection);
    const historyState: OrchestrationHistoryState =
      checkpoint.historySnapshot == null
        ? history.state()
        : history.markBaseline(checkpoint.historySnapshot, true);
    const current = exactRevision || Boolean(historyState.atBaseline);
    if (!exactRevision && current) {
      documentLifecycle.setBaseline(true, inspection || null);
    }
    return saveOutcome(current, exactRevision, false);
  };
  const detachPersistedCheckpoint = (): OrchestrationHistoryState => {
    documentLifecycle.detachPersistedCopy();
    return history.detachBaseline();
  };

  return Object.freeze({
    revision: documentLifecycle.revision,
    requireValid: documentLifecycle.requireValid,
    setSaveBusy: documentLifecycle.setSaveBusy,
    markWriteConflict: documentLifecycle.markWriteConflict,
    setDocumentBaseline: documentLifecycle.setBaseline,
    markDirty,
    adoptBaseline,
    restoreHistory: () => documentLifecycle.restoreHistory(history.state()),
    resetHistory: history.resetCurrent,
    syncHistory: history.syncCurrent,
    historySnapshot: history.captureCurrent,
    historyState: history.state,
    undo: history.undoAndApply,
    redo: history.redoAndApply,
    createSaveCheckpoint,
    isSaveCheckpointCurrent,
    completeSaveCheckpoint,
    detachPersistedCheckpoint,
  });
}

export type OrchestrationEditLifecycle = ReturnType<
  typeof createOrchestrationEditLifecycle
>;

(orchestrationRegistry as unknown as EditLifecycleWindow).createOrchestrationEditLifecycle =
  createOrchestrationEditLifecycle;
