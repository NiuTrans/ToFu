import { orchestrationRegistry } from './registry';
import { createTaskModeContractSession } from './task-mode-contract-session';
import { reportOrchestrationDiagnostic } from './diagnostic-report';

type ContractSession = ReturnType<typeof createTaskModeContractSession>;
export interface TaskModeContractControllerOptions {
  session?: ContractSession;
  source?: unknown | (() => unknown);
  report?: (error: unknown) => unknown;
  onAdopt?: (contracts: unknown) => unknown;
}
type TaskModeContractControllerWindow = Window & {
  createTaskModeContractController?: typeof createTaskModeContractController;
};

export function createTaskModeContractController(
  options: TaskModeContractControllerOptions = {},
) {
  const session = options.session ?? createTaskModeContractSession();
  const source = (): unknown => typeof options.source === 'function'
    ? options.source() : options.source;
  const refresh = async (): Promise<boolean> => {
    const capability = source() as Record<string, unknown> | null;
    if (!capability || typeof capability.refreshAuthoringContract !== 'function') {
      return false;
    }
    const result = await session.refresh(async () => {
      const contract = await (capability.refreshAuthoringContract as
        () => Promise<unknown>)();
      const value = contract as Record<string, unknown> | null;
      if (value?.ready === false) {
        throw value.error || {
          name: 'OrchestrationContractReadError',
          message: 'Authoring contract is unavailable',
          reason: 'contract-not-ready',
        };
      }
      return contract;
    });
    if (result.error && !result.stale) {
      reportOrchestrationDiagnostic(options.report, result.error);
    }
    if (!result.adopted) return false;
    options.onAdopt?.(result.contracts);
    return true;
  };
  return Object.freeze({
    refresh,
    invalidate: session.invalidate,
    snapshot: session.snapshot,
  });
}

(orchestrationRegistry as unknown as TaskModeContractControllerWindow).createTaskModeContractController =
  createTaskModeContractController;
