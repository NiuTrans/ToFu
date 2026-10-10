import { orchestrationRegistry } from './registry';
export interface OrchestrationDirtyGuardOptions {
  isDirty?: () => unknown;
  translate?: (key: string) => string;
  confirm?: (
    message: string,
    options: { danger: boolean },
  ) => unknown | PromiseLike<unknown>;
}

export interface BeforeUnloadTarget {
  addEventListener(
    type: 'beforeunload',
    listener: (event: BeforeUnloadEvent) => void,
  ): void;
  removeEventListener?(
    type: 'beforeunload',
    listener: (event: BeforeUnloadEvent) => void,
  ): void;
}

type DirtyGuardWindow = Window & {
  createOrchestrationDirtyGuard?: typeof createOrchestrationDirtyGuard;
};

/** Unsaved-document confirmation and browser unload lifecycle. */
export function createOrchestrationDirtyGuard(
  options: OrchestrationDirtyGuardOptions = {},
) {
  let unloadTarget: BeforeUnloadTarget | null = null;
  const dirty = (): boolean => typeof options.isDirty === 'function'
    && Boolean(options.isDirty());
  const translate = (key: string): string => options.translate
    ? options.translate(key) : key;

  const confirmDiscard = async (messageKey: string): Promise<boolean> => {
    if (!dirty() || typeof options.confirm !== 'function') return true;
    return Boolean(await options.confirm(
      translate(messageKey), { danger: false }));
  };

  const beforeUnload = (event: BeforeUnloadEvent): void => {
    if (!dirty()) return;
    event.preventDefault();
    event.returnValue = '';
  };

  const installUnloadGuard = (target: BeforeUnloadTarget): boolean => {
    if (unloadTarget || !target
        || typeof target.addEventListener !== 'function') return false;
    unloadTarget = target;
    target.addEventListener('beforeunload', beforeUnload);
    return true;
  };

  const destroy = (): void => {
    unloadTarget?.removeEventListener?.('beforeunload', beforeUnload);
    unloadTarget = null;
  };

  return Object.freeze({ confirmDiscard, installUnloadGuard, destroy });
}

(orchestrationRegistry as unknown as DirtyGuardWindow).createOrchestrationDirtyGuard =
  createOrchestrationDirtyGuard;
