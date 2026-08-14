import { orchestrationRegistry } from './registry';
export interface OrchestrationBoundedStateOptions {
  maxEntries?: unknown;
  fallbackMaxEntries?: unknown;
  onRemove?: (key: string) => void;
}

export interface OrchestrationBoundedState<T> {
  key(value: unknown): string;
  has(value: unknown): boolean;
  get(value: unknown): T | undefined;
  set(value: unknown, item: T): T;
  remove(value: unknown): boolean;
  keys(): string[];
  clear(): void;
}

type BoundedStateWindow = Window & {
  createOrchestrationBoundedState?: typeof createOrchestrationBoundedState;
};

/** Shared bounded key/value storage for transient orchestration UI state. */
export function createOrchestrationBoundedState<T>(
  options: OrchestrationBoundedStateOptions = {},
): OrchestrationBoundedState<T> {
  const parsedFallback = Number(options.fallbackMaxEntries);
  const fallbackMaxEntries = Number.isFinite(parsedFallback)
    && parsedFallback > 0 ? Math.floor(parsedFallback) : 128;
  const parsedLimit = Number(options.maxEntries);
  const maxEntries = Number.isFinite(parsedLimit) && parsedLimit > 0
    ? Math.floor(parsedLimit) : fallbackMaxEntries;
  const onRemove = typeof options.onRemove === 'function'
    ? options.onRemove : null;
  const values: Record<string, T> = Object.create(null) as Record<string, T>;
  let order: string[] = [];
  const key = (value: unknown): string => String(value == null ? '' : value);
  const untouch = (normalized: string): void => {
    const index = order.indexOf(normalized);
    if (index >= 0) order.splice(index, 1);
  };
  const remove = (value: unknown): boolean => {
    const normalized = key(value);
    const existed = Object.prototype.hasOwnProperty.call(values, normalized);
    untouch(normalized);
    delete values[normalized];
    if (existed) onRemove?.(normalized);
    return existed;
  };
  const set = (value: unknown, item: T): T => {
    const normalized = key(value);
    untouch(normalized);
    values[normalized] = item;
    order.push(normalized);
    while (order.length > maxEntries) remove(order[0] ?? '');
    return item;
  };
  const has = (value: unknown): boolean =>
    Object.prototype.hasOwnProperty.call(values, key(value));
  const get = (value: unknown): T | undefined => values[key(value)];
  const keys = (): string[] => order.slice();
  const clear = (): void => { order.slice().forEach(remove); };
  return { key, has, get, set, remove, keys, clear };
}

(orchestrationRegistry as unknown as BoundedStateWindow).createOrchestrationBoundedState =
  createOrchestrationBoundedState;
