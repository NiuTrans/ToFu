import { orchestrationRegistry } from './registry';
export interface OrchestrationRunOwner {
  readonly runId: string | null;
  readonly generation: number;
  readonly readGeneration: number;
}

export interface RunReleaseOptions {
  clearId?: boolean;
}

export interface OrchestrationRunSession {
  id(): string | null;
  generation(): number;
  readGeneration(): number;
  isPolling(): boolean;
  snapshot(): OrchestrationRunOwner;
  owns(owner: OrchestrationRunOwner | null | undefined, requireRead?: boolean): boolean;
  begin(runId?: unknown): OrchestrationRunOwner;
  adopt(
    runId: unknown,
    owner: OrchestrationRunOwner | null | undefined,
  ): OrchestrationRunOwner | null;
  beginRead(
    owner?: OrchestrationRunOwner | null,
  ): OrchestrationRunOwner | null;
  startPolling(owner: OrchestrationRunOwner | null | undefined): boolean;
  acceptsPoll(owner: OrchestrationRunOwner | null | undefined): boolean;
  stopPolling(owner?: OrchestrationRunOwner | null): boolean;
  release(
    owner?: OrchestrationRunOwner | null,
    options?: RunReleaseOptions,
  ): boolean;
  invalidate(options?: RunReleaseOptions): OrchestrationRunOwner;
}

type RunSessionWindow = Window & {
  createOrchestrationRunSession?: typeof createOrchestrationRunSession;
};

/** Shared identity and generation fence for run reads and polling. */
export function createOrchestrationRunSession(): OrchestrationRunSession {
  let runId: string | null = null;
  let generation = 0;
  let readGeneration = 0;
  let polling = false;
  const idValue = (value: unknown): string | null =>
    value == null || value === '' ? null : String(value);
  const snapshot = (): OrchestrationRunOwner => Object.freeze({
    runId,
    generation,
    readGeneration,
  });
  const owns = (
    owner: OrchestrationRunOwner | null | undefined,
    requireRead?: boolean,
  ): boolean => Boolean(owner) && owner?.runId === runId
    && owner.generation === generation
    && (!requireRead || owner.readGeneration === readGeneration);
  const begin = (nextRunId?: unknown): OrchestrationRunOwner => {
    generation += 1;
    readGeneration += 1;
    runId = idValue(nextRunId);
    polling = false;
    return snapshot();
  };
  const adopt = (
    nextRunId: unknown,
    owner: OrchestrationRunOwner | null | undefined,
  ): OrchestrationRunOwner | null => {
    if (!owns(owner, false)) return null;
    runId = idValue(nextRunId);
    return snapshot();
  };
  const beginRead = (
    owner?: OrchestrationRunOwner | null,
  ): OrchestrationRunOwner | null => {
    if (owner && !owns(owner, false)) return null;
    readGeneration += 1;
    return snapshot();
  };
  const startPolling = (
    owner: OrchestrationRunOwner | null | undefined,
  ): boolean => {
    if (!owns(owner, false)) return false;
    polling = true;
    return true;
  };
  const stopPolling = (
    owner?: OrchestrationRunOwner | null,
  ): boolean => {
    if (owner && !owns(owner, false)) return false;
    polling = false;
    return true;
  };
  const release = (
    owner?: OrchestrationRunOwner | null,
    options?: RunReleaseOptions,
  ): boolean => {
    if (owner && !owns(owner, false)) return false;
    polling = false;
    if (!options || options.clearId !== false) runId = null;
    return true;
  };
  const invalidate = (
    options?: RunReleaseOptions,
  ): OrchestrationRunOwner => {
    generation += 1;
    readGeneration += 1;
    polling = false;
    if (!options || options.clearId !== false) runId = null;
    return snapshot();
  };

  return {
    id: () => runId,
    generation: () => generation,
    readGeneration: () => readGeneration,
    isPolling: () => polling,
    snapshot,
    owns,
    begin,
    adopt,
    beginRead,
    startPolling,
    acceptsPoll: (owner) => polling && owns(owner, false),
    stopPolling,
    release,
    invalidate,
  };
}

(orchestrationRegistry as unknown as RunSessionWindow).createOrchestrationRunSession =
  createOrchestrationRunSession;
