import { orchestrationRegistry } from './registry';
import {
  createOrchestrationMutationRequestClient,
} from './mutation-request';
import {
  createOrchestrationEndpointRequestClient,
  orchestrationDefinitionSelection,
  type EndpointRequestClientOptions,
} from './request-contract';
import { createOrchestrationTaskRequestClient } from './task-request';

type MutationClient = ReturnType<typeof createOrchestrationMutationRequestClient>;
type TaskClient = ReturnType<typeof createOrchestrationTaskRequestClient>;

export interface RunRequestOptions extends EndpointRequestClientOptions {
  mutations?: MutationClient;
  tasks?: TaskClient;
  normalizeTaskCreate?: unknown;
}

type RunRequestWindow = Window & {
  createOrchestrationRunRequestClient?:
    typeof createOrchestrationRunRequestClient;
};

export function createOrchestrationRunRequestClient(
  options: RunRequestOptions = {},
) {
  const requests = createOrchestrationEndpointRequestClient(options);
  const mutations = options.mutations
    ?? createOrchestrationMutationRequestClient(options);
  const tasks = options.tasks ?? createOrchestrationTaskRequestClient({
    ...options,
    normalizeCreate: options.normalizeTaskCreate,
  });
  return {
    canPlan: () => requests.available('plan'),
    canStart: () => requests.available('run-start'),
    canPoll: () => requests.available('run-poll'),
    plan: (definition: unknown, orchestrationId?: unknown) => {
      const selection = orchestrationDefinitionSelection(
        definition, orchestrationId);
      return requests.request(
        'plan', [selection.definition, selection.storedId]);
    },
    start: (
      definition: unknown,
      input: unknown,
      orchestrationId?: unknown,
    ) => {
      const selection = orchestrationDefinitionSelection(
        definition, orchestrationId);
      return requests.request('run-start', [
        selection.definition,
        input,
        selection.storedId,
        selection.originId,
      ]);
    },
    poll: (taskId: unknown, cursor?: unknown) => requests.request(
      'run-poll', [taskId, cursor]),
    createTask: (
      definition: unknown,
      input: unknown,
      orchestrationId?: unknown,
    ) => tasks.create(definition, input, orchestrationId),
    abort: (taskId: unknown) => mutations.abortEphemeral(taskId),
    approve: (requestId: unknown, approved: unknown) =>
      mutations.approveGate(requestId, approved),
    input: (requestId: unknown, response: unknown) =>
      mutations.inputGate(requestId, response),
  };
}

(orchestrationRegistry as unknown as RunRequestWindow).createOrchestrationRunRequestClient =
  createOrchestrationRunRequestClient;
