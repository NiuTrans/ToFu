import { orchestrationRegistry } from './registry';
import { createOrchestrationBoundedState } from './bounded-state';

export interface OrchestrationScrollStateOptions {
  maxEntries?: unknown;
}

export interface OrchestrationScrollState {
  capture(element: Element & { scrollTop: number } | null): number;
  restore(element: (Element & { scrollTop: number }) | null, key: unknown): number;
  project<T>(
    element: (Element & { scrollTop: number }) | null,
    key: unknown,
    render?: () => T,
  ): T | undefined;
  reset(key?: unknown): void;
}

type ScrollStateWindow = Window & {
  orchestrationScrollScope?: typeof orchestrationScrollScope;
  createOrchestrationScrollState?: typeof createOrchestrationScrollState;
};

/** Stable, collision-safe key for one independently owned scroll surface. */
export function orchestrationScrollScope(parts: unknown): string {
  return JSON.stringify((Array.isArray(parts) ? parts : [parts]).map(
    (part) => String(part == null ? '' : part),
  ));
}

/** Bounded scroll restoration shared by the Studio and Task Mode inspectors. */
export function createOrchestrationScrollState(
  options: OrchestrationScrollStateOptions = {},
): OrchestrationScrollState {
  const offsets = createOrchestrationBoundedState<number>({
    maxEntries: options.maxEntries,
    fallbackMaxEntries: 128,
  });
  let activeKey = '';
  const capture = (element: (Element & { scrollTop: number }) | null): number => {
    if (!element || !activeKey) return 0;
    const value = Math.max(0, Number(element.scrollTop) || 0);
    offsets.set(activeKey, value);
    return value;
  };
  const restore = (
    element: (Element & { scrollTop: number }) | null,
    key: unknown,
  ): number => {
    activeKey = offsets.key(key);
    const value = offsets.has(activeKey) ? offsets.get(activeKey) ?? 0 : 0;
    if (element) element.scrollTop = value;
    return value;
  };
  const project = <T>(
    element: (Element & { scrollTop: number }) | null,
    key: unknown,
    render?: () => T,
  ): T | undefined => {
    capture(element);
    try {
      return typeof render === 'function' ? render() : undefined;
    } finally {
      restore(element, key);
    }
  };
  const reset = (...keys: readonly unknown[]): void => {
    if (keys.length) {
      const normalized = offsets.key(keys[0]);
      offsets.remove(normalized);
      if (activeKey === normalized) activeKey = '';
      return;
    }
    offsets.clear();
    activeKey = '';
  };
  return { capture, restore, project, reset };
}

Object.assign(orchestrationRegistry as unknown as ScrollStateWindow, {
  orchestrationScrollScope,
  createOrchestrationScrollState,
});
