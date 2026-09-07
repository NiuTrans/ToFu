import { orchestrationRegistry } from './registry';
import { record, type ContractRecord } from './contracts';
import { reportOrchestrationDiagnostic } from './diagnostic-report';

export interface OrchestrationMutationCommandOptions {
  failureMessage?: (result: unknown, fallback: string) => unknown;
  report?: (context: string, error: unknown) => unknown;
}

export interface OrchestrationMutationCommandSpec {
  request?: () => unknown | PromiseLike<unknown>;
  context?: string;
  fallback?: string;
  acceptAbsent?: boolean;
}

export interface OrchestrationMutationCommandOutcome {
  readonly attempted: boolean;
  readonly ok: boolean;
  readonly satisfied: boolean;
  readonly targetAbsent: boolean;
  readonly mutation: ContractRecord | null;
  readonly result: unknown;
  readonly cause: unknown;
  readonly message: string;
}

type MutationCommandWindow = Window & {
  createOrchestrationMutationCommand?:
    typeof createOrchestrationMutationCommand;
};

/** One accepted/rejected projection shared by Studio and Task Mode. */
export function createOrchestrationMutationCommand(
  options: OrchestrationMutationCommandOptions = {},
) {
  const report = (context: string, error: unknown): void => {
    reportOrchestrationDiagnostic(options.report, context, error);
  };
  const rejected = (
    result: unknown, fallback: string, context?: string,
  ): string => {
    try {
      const message = options.failureMessage?.(result, fallback);
      return String(message || fallback || '');
    } catch (error: unknown) {
      report(`${context || 'mutation'}:failure-message`, error);
      return fallback;
    }
  };

  const outcome = (
    fields: Partial<OrchestrationMutationCommandOutcome> = {},
  ): OrchestrationMutationCommandOutcome => Object.freeze({
    attempted: false,
    ok: false,
    satisfied: false,
    targetAbsent: false,
    mutation: null,
    result: null,
    cause: null,
    message: '',
    ...fields,
  });

  const execute = async (
    spec: OrchestrationMutationCommandSpec = {},
  ): Promise<OrchestrationMutationCommandOutcome> => {
    if (typeof spec.request !== 'function') return outcome();
    try {
      const result = await spec.request();
      if (!result) return outcome();
      const root = record(result) ?? {};
      if (root.cause) {
        report(spec.context || 'mutation', root.cause);
      }
      const mutation = record(root.mutation);
      const ok = Boolean(mutation) && mutation?.ok === true;
      const targetAbsent = Boolean(mutation)
        && mutation?.targetExists === false;
      const satisfied = ok || (spec.acceptAbsent === true && targetAbsent);
      return outcome({
        attempted: true,
        ok,
        satisfied,
        targetAbsent,
        mutation,
        result,
        cause: root.cause || null,
        message: satisfied ? '' : rejected(
          result, spec.fallback || '', spec.context),
      });
    } catch (error: unknown) {
      report(spec.context || 'mutation', error);
      return outcome({
        attempted: true,
        cause: error,
        message: spec.fallback || '',
      });
    }
  };

  return { execute };
}

(orchestrationRegistry as unknown as MutationCommandWindow).createOrchestrationMutationCommand =
  createOrchestrationMutationCommand;
