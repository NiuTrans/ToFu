/**
 * Responsibility: own the bounded, single-flight saved-Flow catalogue shared
 * by the desktop toolbar and mobile sheet.
 * Entry point: createOrchestrationFlowCatalog.
 * Dependencies: an injected Orchestration list client, the immutable HTTP
 * result projector, and observational callbacks supplied by composition.
 */
import { HTTP_RESULT } from '../../core/http-result';
import { reportOrchestrationDiagnostic } from './diagnostic-report';

type UnknownRecord = Record<string, unknown>;

export interface OrchestrationFlowCatalogItem {
  readonly id: string;
  readonly name: string;
}

interface OrchestrationFlowCatalogApi {
  listResult?: () => unknown | PromiseLike<unknown>;
  list?: () => unknown | PromiseLike<unknown>;
}

export interface OrchestrationFlowCatalogOptions {
  api?: OrchestrationFlowCatalogApi | null
    | (() => OrchestrationFlowCatalogApi | null);
  now?: () => number;
  maxAgeMs?: number;
  onError?: (error: unknown) => unknown;
  onChange?: (items: readonly OrchestrationFlowCatalogItem[]) => unknown;
  onObserverError?: (error: unknown) => unknown;
}

export type OrchestrationFlowCatalogState =
  | 'idle' | 'loading' | 'ready' | 'refreshing'
  | 'invalidated' | 'stale' | 'failed';

export interface OrchestrationFlowCatalogStatus {
  readonly state: OrchestrationFlowCatalogState;
  readonly hasSnapshot: boolean;
  readonly failure: unknown;
}

export interface OrchestrationFlowCatalog {
  load(): Promise<readonly OrchestrationFlowCatalogItem[]>;
  refresh(): Promise<readonly OrchestrationFlowCatalogItem[]>;
  snapshot(): readonly OrchestrationFlowCatalogItem[];
  status(): Readonly<OrchestrationFlowCatalogStatus>;
  invalidate(): number;
}

const record = (value: unknown): UnknownRecord | null => (
  value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord : null
);

export function createOrchestrationFlowCatalog(
  options: OrchestrationFlowCatalogOptions = {},
): OrchestrationFlowCatalog {
  let items: readonly OrchestrationFlowCatalogItem[] = [];
  let loaded = false;
  let hasSnapshot = false;
  let lastFailure: unknown = null;
  let updatedAt = 0;
  let pending: Promise<readonly OrchestrationFlowCatalogItem[]> | null = null;
  let generation = 0;
  const configuredMaxAge = Number(options.maxAgeMs);
  const maxAgeMs = Number.isFinite(configuredMaxAge) && configuredMaxAge >= 0
    ? configuredMaxAge : 30_000;

  const api = (): OrchestrationFlowCatalogApi | null => {
    const value = typeof options.api === 'function'
      ? options.api() : options.api;
    return value ?? null;
  };
  const now = (): number => (
    typeof options.now === 'function' ? options.now() : Date.now()
  );
  const snapshot = (): readonly OrchestrationFlowCatalogItem[] => (
    Object.freeze(items.slice())
  );
  const project = (values: readonly unknown[]): readonly OrchestrationFlowCatalogItem[] => {
    const seen = new Set<string>();
    const projected: OrchestrationFlowCatalogItem[] = [];
    for (const candidate of values) {
      const value = record(candidate);
      const id = String(value?.id ?? '').trim();
      if (!id || seen.has(id)) continue;
      seen.add(id);
      projected.push(Object.freeze({
        id,
        name: typeof value?.name === 'string' ? value.name.trim() : '',
      }));
    }
    return Object.freeze(projected);
  };
  const status = (): Readonly<OrchestrationFlowCatalogStatus> => {
    const state: OrchestrationFlowCatalogState = lastFailure !== null
      ? (hasSnapshot ? 'stale' : 'failed')
      : pending ? (hasSnapshot ? 'refreshing' : 'loading')
        : loaded ? 'ready' : hasSnapshot ? 'invalidated' : 'idle';
    return Object.freeze({ state, hasSnapshot, failure: lastFailure });
  };
  const failureCause = (value: unknown): unknown => {
    let cause = HTTP_RESULT.error(value);
    const candidate = record(value);
    if (cause == null && typeof candidate?.message === 'string') cause = value;
    const causeRecord = record(cause);
    return causeRecord ? Object.freeze({ ...causeRecord }) : cause;
  };
  const report = (error: unknown): void => {
    lastFailure = failureCause(error);
    if (lastFailure === null) lastFailure = '';
    reportOrchestrationDiagnostic(options.onError, error);
  };
  const notifyChange = (
    adopted: readonly OrchestrationFlowCatalogItem[],
  ): void => {
    if (typeof options.onChange !== 'function') return;
    try {
      options.onChange(adopted);
    } catch (error) {
      reportOrchestrationDiagnostic(options.onObserverError, error);
    }
  };
  const read = async (
    owner: number,
  ): Promise<readonly OrchestrationFlowCatalogItem[]> => {
    const client = api();
    try {
      let values: unknown = null;
      if (typeof client?.listResult === 'function') {
        const result = record(await client.listResult());
        if (owner !== generation) return snapshot();
        if (result?.accepted !== true || !Array.isArray(result.items)) {
          report(result);
          return snapshot();
        }
        values = result.items;
      } else if (typeof client?.list === 'function') {
        values = await client.list();
        if (owner !== generation) return snapshot();
        if (!Array.isArray(values)) {
          report(values);
          return snapshot();
        }
      } else {
        report(null);
        return snapshot();
      }
      items = project(values as readonly unknown[]);
      loaded = true;
      hasSnapshot = true;
      lastFailure = null;
      updatedAt = now();
      const adopted = snapshot();
      notifyChange(adopted);
      return adopted;
    } catch (error) {
      if (owner === generation) report(error);
      return snapshot();
    }
  };
  const refresh = async (): Promise<readonly OrchestrationFlowCatalogItem[]> => {
    if (pending) return pending;
    const request = read(generation);
    pending = request;
    try {
      return await request;
    } finally {
      if (pending === request) pending = null;
    }
  };
  const load = (): Promise<readonly OrchestrationFlowCatalogItem[]> => (
    loaded && now() - updatedAt <= maxAgeMs
      ? Promise.resolve(snapshot()) : refresh()
  );
  const invalidate = (): number => {
    generation += 1;
    loaded = false;
    lastFailure = null;
    pending = null;
    return generation;
  };
  return Object.freeze({ load, refresh, snapshot, status, invalidate });
}
