import { orchestrationRegistry } from './registry';
import { type OrchestrationActionLock } from './action-lock';
import { record, type ContractRecord } from './contracts';
import { projectOrchestrationDurableStartOutcome } from './durable-start-outcome';
import { reportOrchestrationDiagnostic } from './diagnostic-report';

export interface OrchestrationDurableRunCommandOptions {
  actionLock: OrchestrationActionLock;
  runnableSnapshot(key: string): unknown;
  permits(key: string): boolean | PromiseLike<boolean>;
  requests: {
    createTask(
      snapshot: unknown,
      input: unknown,
      orchestrationId: unknown,
    ): Promise<ContractRecord>;
  };
  inputFor(snapshot: unknown): unknown;
  currentId?: () => unknown;
  report(context: string, error: unknown): void;
  toast(message: string, error?: boolean): void;
  translate(key: string): string;
  resultError(error: unknown, fallback: string): string;
  handoff?: (runId: string) => boolean | PromiseLike<boolean>;
}

type DurableRunCommandWindow = Window & {
  createOrchestrationDurableRunCommand?:
    typeof createOrchestrationDurableRunCommand;
};

export function createOrchestrationDurableRunCommand(
  options: OrchestrationDurableRunCommandOptions,
) {
  const run = async (): Promise<boolean> => {
    if (options.actionLock.pending()) return false;
    const snapshot = options.runnableSnapshot('orch.run.nothingToRun');
    if (!snapshot) return false;
    return options.actionLock.perform('durable', async () => {
      try {
        if (!await options.permits('orch.run.asTask')) return false;
        const response = await options.requests.createTask(
          snapshot,
          options.inputFor(snapshot),
          options.currentId ? options.currentId() : '',
        );
        const outcome = projectOrchestrationDurableStartOutcome(response);
        const cause = record(response)?.cause;
        if (cause) {
          reportOrchestrationDiagnostic(
            options.report, 'durable-start', cause);
        }
        if (!outcome.accepted) {
          options.toast(
            outcome.error || options.translate('orch.run.taskStartFailed'),
            true,
          );
          if (outcome.recoverableFailure && options.handoff
              && !await options.handoff(outcome.targetRunId)) {
            options.toast(options.translate('orch.run.taskOpenFailed'), true);
          }
          return false;
        }
        options.toast(options.translate('orch.run.taskStarted'));
        if (options.handoff
            && !await options.handoff(outcome.targetRunId)) {
          options.toast(options.translate('orch.run.taskOpenFailed'), true);
          return false;
        }
        return true;
      } catch (error: unknown) {
        reportOrchestrationDiagnostic(
          options.report, 'durable-start', error);
        options.toast(options.resultError(
          error, options.translate('orch.run.taskStartFailed')), true);
        return false;
      }
    }, false);
  };

  return Object.freeze({ run });
}

(orchestrationRegistry as unknown as DurableRunCommandWindow).createOrchestrationDurableRunCommand =
  createOrchestrationDurableRunCommand;
