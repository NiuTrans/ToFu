import { orchestrationRegistry } from './registry';
import { projectOrchestrationRuntimeContracts } from './runtime-contracts';

type RuntimeContracts = ReturnType<typeof projectOrchestrationRuntimeContracts>;
type TaskModeContractSessionWindow = Window & {
  createTaskModeContractSession?: typeof createTaskModeContractSession;
};

function own<T>(value: T): Readonly<T> {
  const clone = JSON.parse(JSON.stringify(value)) as T;
  const freeze = (entry: unknown): unknown => {
    if (!entry || typeof entry !== 'object' || Object.isFrozen(entry)) return entry;
    Object.keys(entry).forEach((key) => {
      freeze((entry as Record<string, unknown>)[key]);
    });
    return Object.freeze(entry);
  };
  return freeze(clone) as Readonly<T>;
}

export function createTaskModeContractSession() {
  let generation = 0;
  let contracts: RuntimeContracts = projectOrchestrationRuntimeContracts(null);
  const snapshot = (): RuntimeContracts => contracts;
  const invalidate = (): void => { generation += 1; };
  const refresh = async (load: () => unknown | Promise<unknown>) => {
    if (typeof load !== 'function') {
      throw new TypeError('Task Mode contract refresh requires a loader');
    }
    const owner = ++generation;
    let value: unknown;
    try { value = await load(); } catch (error: unknown) {
      return Object.freeze({
        adopted: false, stale: owner !== generation, error, contracts,
      });
    }
    if (owner !== generation) return Object.freeze({
      adopted: false, stale: true, error: null, contracts,
    });
    contracts = own(projectOrchestrationRuntimeContracts(value));
    return Object.freeze({
      adopted: true, stale: false, error: null, contracts,
    });
  };
  return Object.freeze({ snapshot, invalidate, refresh });
}

(orchestrationRegistry as unknown as TaskModeContractSessionWindow).createTaskModeContractSession =
  createTaskModeContractSession;
