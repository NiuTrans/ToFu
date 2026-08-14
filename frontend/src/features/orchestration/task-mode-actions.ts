import { orchestrationRegistry } from './registry';
import { createOrchestrationSingleFlight } from './single-flight';
import type { createOrchestrationMutationRequestClient } from './mutation-request';
import { reportOrchestrationDiagnostic } from './diagnostic-report';

type Port = Record<string, unknown>;
export interface TaskModeActionControllerOptions extends Record<string, unknown> {
  mutationClient?: Port;
  taskClient?: Port | (() => Port | null) | null;
  confirm?: (message: unknown, options: unknown) => unknown;
  report?: (context: string, result: unknown) => unknown;
}
type TaskModeActionWindow = Window & {
  createOrchestrationMutationRequestClient?:
    typeof createOrchestrationMutationRequestClient;
  createTaskModeActionController?: typeof createTaskModeActionController;
};

export function createTaskModeActionController(
  options: TaskModeActionControllerOptions = {},
) {
  const flights = createOrchestrationSingleFlight();
  let mutationRequests: Port | null = null;
  const mutationClient = (): Port => {
    if (mutationRequests) return mutationRequests;
    if (options.mutationClient) mutationRequests = options.mutationClient;
    else {
      const factory = (orchestrationRegistry as unknown as TaskModeActionWindow)
        .createOrchestrationMutationRequestClient;
      if (!factory) throw new Error('Orchestration mutation request owner unavailable');
      mutationRequests = factory({
        api: options.api,
        mutationContract: options.mutationContract,
      }) as unknown as Port;
    }
    return mutationRequests;
  };
  const once = async (
    scope: unknown, id: unknown, operation: () => unknown,
  ): Promise<unknown> => flights.tryRun(
    `${String(scope || 'mutation')}:${String(id || '')}`, operation, null);
  const confirmed = async (spec: Port | null | undefined): Promise<boolean> => {
    if (!spec || typeof options.confirm !== 'function') return true;
    return Boolean(await options.confirm(spec.message, spec.options || {}));
  };
  const mutation = async (
    scope: string, id: unknown, method: string, args: unknown[],
    confirmation?: Port,
  ): Promise<unknown> => {
    if (!id) return null;
    return once(scope, id, async () => {
      if (!await confirmed(confirmation)) return null;
      const client = mutationClient();
      const operation = client[method];
      const result = typeof operation === 'function'
        ? await (operation as (...values: unknown[]) => unknown).apply(client, args)
        : null;
      reportOrchestrationDiagnostic(
        options.report, `mutation ${method}`, result);
      return result;
    });
  };
  const approveGate = (requestId: unknown, approved: unknown) =>
    mutation('gate', requestId, 'approveGate', [requestId, approved]);
  const inputGate = (requestId: unknown, value: unknown) =>
    mutation('gate', requestId, 'inputGate', [requestId, value]);
  const abortRun = (runId: unknown, confirmation?: Port) =>
    mutation('run', runId, 'abortDurable', [runId], confirmation);
  const deleteRun = (runId: unknown, confirmation?: Port) =>
    mutation('run', runId, 'removeDurable', [runId], confirmation);
  const rerun = (runValue: unknown): Promise<unknown> => {
    const run = runValue && typeof runValue === 'object' ? runValue as Port : null;
    if (!run?.id || !run.definition) return Promise.resolve(null);
    return once('run', run.id, async () => {
      const client = typeof options.taskClient === 'function'
        ? options.taskClient() : options.taskClient;
      const result = client && typeof client.create === 'function'
        ? await (client.create as (...values: unknown[]) => unknown).call(
          client, run.definition, String(run.input == null ? '' : run.input),
          run.orch_id || '') : null;
      reportOrchestrationDiagnostic(options.report, 'rerun', result);
      return result;
    });
  };
  const failureMessage = (resultValue: unknown, fallback: unknown): unknown => {
    const result = resultValue && typeof resultValue === 'object'
      ? resultValue as Port : {};
    if (typeof options.mutationMessage === 'function') {
      return (options.mutationMessage as (...args: unknown[]) => unknown)(
        result.response,
        options.translate, fallback, options.mutationContract);
    }
    return typeof options.resultError === 'function'
      ? (options.resultError as (...args: unknown[]) => unknown)(
        result.response, fallback) : fallback;
  };
  return Object.freeze({
    approveGate, inputGate, abortRun, deleteRun, rerun, failureMessage,
  });
}

(orchestrationRegistry as unknown as TaskModeActionWindow).createTaskModeActionController =
  createTaskModeActionController;
