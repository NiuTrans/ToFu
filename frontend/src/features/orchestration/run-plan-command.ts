import { orchestrationRegistry } from './registry';
import { type OrchestrationActionLock } from './action-lock';
import { record, type ContractRecord } from './contracts';
import { reportOrchestrationDiagnostic } from './diagnostic-report';

export interface OrchestrationRunPlanCommandOptions {
  actionLock: OrchestrationActionLock;
  runnableSnapshot(key: string): unknown;
  permits(key: string): boolean | PromiseLike<boolean>;
  clearLog(): void;
  requests: { plan(snapshot: unknown): Promise<ContractRecord> };
  report(context: string, error: unknown): void;
  translate(key: string): string;
  resultError(error: unknown, fallback: string): string;
  view: {
    failure(message: string): unknown;
    render(response: ContractRecord): unknown;
  };
}

type RunPlanCommandWindow = Window & {
  createOrchestrationRunPlanCommand?:
    typeof createOrchestrationRunPlanCommand;
};

export function createOrchestrationRunPlanCommand(
  options: OrchestrationRunPlanCommandOptions,
) {
  const run = async (): Promise<unknown> => {
    if (options.actionLock.pending()) return false;
    const snapshot = options.runnableSnapshot('orch.run.nothingToPlan');
    if (!snapshot) return false;
    return options.actionLock.perform('plan', async () => {
      try {
        if (!await options.permits('orch.run.previewPlan')) return false;
        options.clearLog();
        const response = await options.requests.plan(snapshot);
        if (response.cause) {
          reportOrchestrationDiagnostic(
            options.report, 'plan', response.cause);
        }
        if (!response.ok) {
          return options.view.failure(String(
            response.error || options.translate('orch.run.planFailed')));
        }
        return options.view.render(record(response) ?? {});
      } catch (error: unknown) {
        reportOrchestrationDiagnostic(options.report, 'plan', error);
        return options.view.failure(options.resultError(
          error, options.translate('orch.run.planFailed')));
      }
    }, false);
  };

  return Object.freeze({ run });
}

(orchestrationRegistry as unknown as RunPlanCommandWindow).createOrchestrationRunPlanCommand =
  createOrchestrationRunPlanCommand;
