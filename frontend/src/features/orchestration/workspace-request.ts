import { orchestrationRegistry } from './registry';
import {
  normalizeOrchestrationBuiltinRead,
  normalizeOrchestrationLayoutRead,
} from './authoring-read';
import {
  createOrchestrationEndpointRequestClient,
  type EndpointRequestClientOptions,
} from './request-contract';

type WorkspaceRequestWindow = Window & {
  createOrchestrationWorkspaceRequestClient?:
    typeof createOrchestrationWorkspaceRequestClient;
};

export function createOrchestrationWorkspaceRequestClient(
  options: EndpointRequestClientOptions = {},
) {
  const requests = createOrchestrationEndpointRequestClient({
    ...options,
    normalizeBuiltin: options.normalizeBuiltin
      ?? normalizeOrchestrationBuiltinRead,
    normalizeLayout: options.normalizeLayout
      ?? normalizeOrchestrationLayoutRead,
  });
  return {
    canLoadBuiltin: () => requests.available('builtin'),
    canLayout: () => requests.available('layout'),
    loadBuiltin: (name: unknown) => requests.request('builtin', [name]),
    layout: (definition: unknown) => requests.request(
      'layout', [definition]),
  };
}

(orchestrationRegistry as unknown as WorkspaceRequestWindow).createOrchestrationWorkspaceRequestClient =
  createOrchestrationWorkspaceRequestClient;
