import { orchestrationRegistry } from './registry';
import {
  createOrchestrationEndpointRequestClient,
  orchestrationDefinitionSelection,
  type EndpointRequestClientOptions,
} from './request-contract';

type TaskRequestWindow = Window & {
  createOrchestrationTaskRequestClient?:
    typeof createOrchestrationTaskRequestClient;
};

export function createOrchestrationTaskRequestClient(
  options: EndpointRequestClientOptions = {},
) {
  const requests = createOrchestrationEndpointRequestClient(options);
  const list = (status?: unknown, orchestrationId?: unknown, limit?: unknown) =>
    requests.request('task-list', [status, orchestrationId, limit]);
  const get = (runId: unknown) => requests.request('task-read', [runId]);
  const create = (
    definition: unknown,
    input: unknown,
    orchestrationId?: unknown,
  ) => {
    const selection = orchestrationDefinitionSelection(
      definition, orchestrationId);
    return requests.request('task-create', [
      selection.definition,
      input,
      selection.storedId,
      selection.originId,
    ]);
  };
  const events = (runId: unknown, cursor?: unknown) => requests.request(
    'task-events', [runId, cursor]);
  return {
    canList: () => requests.available('task-list'),
    canRead: () => requests.available('task-read'),
    canCreate: () => requests.available('task-create'),
    canReadEvents: () => requests.available('task-events'),
    list,
    get,
    create,
    events,
  };
}

(orchestrationRegistry as unknown as TaskRequestWindow).createOrchestrationTaskRequestClient =
  createOrchestrationTaskRequestClient;
