import { orchestrationRegistry } from './registry';
export interface OrchestrationActionOwner {
  readonly key: string;
  readonly name: string;
  readonly generation: number;
}

export interface OrchestrationActionSnapshot {
  readonly pending: boolean;
  readonly name: string;
  readonly generation: number;
}

export interface KeyedActionLockOptions {
  onChange?: (state: OrchestrationActionSnapshot, key: string) => void;
}

export interface OrchestrationKeyedActionLock {
  acquire(resourceKey: unknown, name: unknown): OrchestrationActionOwner | null;
  current(resourceKey: unknown): OrchestrationActionOwner | null;
  release(owner: OrchestrationActionOwner | null | undefined): boolean;
  pending(...args: readonly unknown[]): boolean;
  snapshot(resourceKey: unknown): OrchestrationActionSnapshot;
  perform<T, TDuplicate>(
    resourceKey: unknown,
    name: unknown,
    operation: (owner: OrchestrationActionOwner) => T | PromiseLike<T>,
    duplicateValue: TDuplicate,
  ): Promise<T | TDuplicate>;
}

export interface ActionLockOptions {
  onChange?: (state: OrchestrationActionSnapshot) => void;
}

export interface OrchestrationActionLock {
  acquire(name: unknown): OrchestrationActionOwner | null;
  release(owner: OrchestrationActionOwner | null | undefined): boolean;
  pending(...name: readonly unknown[]): boolean;
  snapshot(): OrchestrationActionSnapshot;
  perform<T, TDuplicate>(
    name: unknown,
    operation: (owner: OrchestrationActionOwner) => T | PromiseLike<T>,
    duplicateValue: TDuplicate,
  ): Promise<T | TDuplicate>;
}

type ActionLockWindow = Window & {
  createOrchestrationKeyedActionLock?:
    typeof createOrchestrationKeyedActionLock;
  createOrchestrationActionLock?: typeof createOrchestrationActionLock;
};

function actionKey(value: unknown): string {
  return String(value == null ? 'default' : value);
}

/** Owner-token mutex partitioned by an arbitrary resource key. */
export function createOrchestrationKeyedActionLock(
  options: KeyedActionLockOptions = {},
): OrchestrationKeyedActionLock {
  const active = new Map<string, OrchestrationActionOwner>();
  let generation = 0;

  function current(resourceKey: unknown): OrchestrationActionOwner | null {
    return active.get(actionKey(resourceKey)) ?? null;
  }

  function snapshot(resourceKey: unknown): OrchestrationActionSnapshot {
    const owner = current(resourceKey);
    return Object.freeze({
      pending: Boolean(owner),
      name: owner?.name ?? '',
      generation: owner?.generation ?? generation,
    });
  }

  function notify(resourceKey: unknown): void {
    const key = actionKey(resourceKey);
    options.onChange?.(snapshot(key), key);
  }

  function acquire(
    resourceKey: unknown,
    name: unknown,
  ): OrchestrationActionOwner | null {
    const key = actionKey(resourceKey);
    if (active.has(key)) return null;
    generation += 1;
    const owner = Object.freeze({
      key,
      name: String(name || 'action'),
      generation,
    });
    active.set(key, owner);
    try {
      notify(key);
    } catch (error: unknown) {
      active.delete(key);
      throw error;
    }
    return owner;
  }

  function release(
    owner: OrchestrationActionOwner | null | undefined,
  ): boolean {
    if (!owner || active.get(owner.key) !== owner) return false;
    active.delete(owner.key);
    notify(owner.key);
    return true;
  }

  function pending(...args: readonly unknown[]): boolean {
    if (args.length === 0) return active.size > 0;
    const owner = current(args[0]);
    return Boolean(owner) && (args.length < 2
      || owner?.name === String(args[1] || 'action'));
  }

  function perform<T, TDuplicate>(
    resourceKey: unknown,
    name: unknown,
    operation: (owner: OrchestrationActionOwner) => T | PromiseLike<T>,
    duplicateValue: TDuplicate,
  ): Promise<T | TDuplicate> {
    const owner = acquire(resourceKey, name);
    if (!owner) return Promise.resolve(duplicateValue);
    let result: T | PromiseLike<T>;
    try {
      result = operation(owner);
    } catch (error: unknown) {
      result = Promise.reject(error);
    }
    return Promise.resolve(result).finally(() => { release(owner); });
  }

  return Object.freeze({
    acquire, current, release, pending, snapshot, perform,
  });
}

/** Global action lock facade used by the Studio run drawer. */
export function createOrchestrationActionLock(
  options: ActionLockOptions = {},
): OrchestrationActionLock {
  const resourceKey = 'global';
  const keyed = createOrchestrationKeyedActionLock({
    onChange: (state) => options.onChange?.(state),
  });

  function snapshot(): OrchestrationActionSnapshot {
    return keyed.snapshot(resourceKey);
  }

  function acquire(name: unknown): OrchestrationActionOwner | null {
    return keyed.acquire(resourceKey, name);
  }

  function release(
    owner: OrchestrationActionOwner | null | undefined,
  ): boolean {
    return keyed.release(owner);
  }

  function pending(...name: readonly unknown[]): boolean {
    return name.length > 0
      ? keyed.pending(resourceKey, name[0])
      : keyed.pending(resourceKey);
  }

  function perform<T, TDuplicate>(
    name: unknown,
    operation: (owner: OrchestrationActionOwner) => T | PromiseLike<T>,
    duplicateValue: TDuplicate,
  ): Promise<T | TDuplicate> {
    return keyed.perform(resourceKey, name, operation, duplicateValue);
  }

  return Object.freeze({ acquire, release, pending, snapshot, perform });
}

const bridge = orchestrationRegistry as unknown as ActionLockWindow;
bridge.createOrchestrationKeyedActionLock = createOrchestrationKeyedActionLock;
bridge.createOrchestrationActionLock = createOrchestrationActionLock;
