import { orchestrationRegistry } from './registry';
import {
  createOrchestrationEndpointRequestClient,
  type EndpointRequestClientOptions,
} from './request-contract';

type ComposerRequestWindow = Window & {
  createOrchestrationComposerRequestClient?:
    typeof createOrchestrationComposerRequestClient;
};

export function createOrchestrationComposerRequestClient(
  options: EndpointRequestClientOptions = {},
) {
  const requests = createOrchestrationEndpointRequestClient(options);
  return {
    available: () => requests.available('compose'),
    compose: (requirement: unknown, current: unknown, history: unknown) =>
      requests.request('compose', [requirement, current, history]),
  };
}

(orchestrationRegistry as unknown as ComposerRequestWindow).createOrchestrationComposerRequestClient =
  createOrchestrationComposerRequestClient;
