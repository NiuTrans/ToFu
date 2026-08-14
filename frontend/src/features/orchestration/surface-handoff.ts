import { orchestrationRegistry } from './registry';
import { reportOrchestrationDiagnostic } from './diagnostic-report';

export interface OrchestrationSurfaceHandoffOptions {
  report?: (stage: string, error: unknown) => unknown;
  closeSource?: () => unknown | PromiseLike<unknown>;
  openTarget?: (payload: unknown) => unknown | PromiseLike<unknown>;
  closeTarget?: () => unknown | PromiseLike<unknown>;
  reopenSource?: () => unknown | PromiseLike<unknown>;
}
type SurfaceHandoffWindow = Window & {
  createOrchestrationSurfaceHandoff?: typeof createOrchestrationSurfaceHandoff;
};

/** Transactional handoff between Studio and Task Mode modal surfaces. */
export function createOrchestrationSurfaceHandoff(
  options: OrchestrationSurfaceHandoffOptions = {},
) {
  const report = (stage: string, error: unknown): void => {
    reportOrchestrationDiagnostic(options.report, stage, error);
  };
  const rollback = async (): Promise<void> => {
    try { await options.closeTarget?.(); }
    catch (error: unknown) { report('close-target', error); }
    try { await options.reopenSource?.(); }
    catch (error: unknown) { report('reopen-source', error); }
  };
  const transfer = async (payload: unknown): Promise<boolean> => {
    let closed: unknown;
    try { closed = options.closeSource && await options.closeSource(); }
    catch (error: unknown) {
      report('close-source', error);
      return false;
    }
    if (!closed) return false;
    try {
      if (options.openTarget && await options.openTarget(payload)) return true;
    } catch (error: unknown) { report('open-target', error); }
    await rollback();
    return false;
  };
  return Object.freeze({ transfer });
}

(orchestrationRegistry as unknown as SurfaceHandoffWindow).createOrchestrationSurfaceHandoff =
  createOrchestrationSurfaceHandoff;
