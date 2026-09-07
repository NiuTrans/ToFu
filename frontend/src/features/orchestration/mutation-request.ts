import { orchestrationRegistry } from './registry';
import {
  createOrchestrationEndpointRequestClient,
  type EndpointRequestClientOptions,
} from './request-contract';

type MutationRequestWindow = Window & {
  createOrchestrationMutationRequestClient?:
    typeof createOrchestrationMutationRequestClient;
};

const ENDPOINTS: Readonly<Record<string, string>> = Object.freeze({
  runAbort: 'run-abort',
  humanApprove: 'human-approve',
  humanInput: 'human-input',
  taskAbort: 'task-abort',
  taskRemove: 'task-remove',
});

export function createOrchestrationMutationRequestClient(
  options: EndpointRequestClientOptions = {},
) {
  const requests = createOrchestrationEndpointRequestClient(options);
  return {
    available: (method: string) => Boolean(ENDPOINTS[method])
      && requests.available(ENDPOINTS[method]),
    abortEphemeral: (taskId: unknown) => requests.request(
      'run-abort', [taskId]),
    approveGate: (requestId: unknown, approved: unknown) => requests.request(
      'human-approve', [requestId, approved]),
    inputGate: (requestId: unknown, response: unknown) => requests.request(
      'human-input', [requestId, response]),
    abortDurable: (runId: unknown) => requests.request(
      'task-abort', [runId]),
    removeDurable: (runId: unknown) => requests.request(
      'task-remove', [runId]),
  };
}

(orchestrationRegistry as unknown as MutationRequestWindow).createOrchestrationMutationRequestClient =
  createOrchestrationMutationRequestClient;
