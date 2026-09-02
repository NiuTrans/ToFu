import { orchestrationRegistry } from './registry';
import {
  normalizeOrchestrationDefinitionListRead,
  normalizeOrchestrationDefinitionRead,
} from './definition-read';
import {
  normalizeOrchestrationDefinitionDelete,
  normalizeOrchestrationDefinitionSave,
} from './definition-mutation-read';
import { type ContractSource } from './contracts';
import {
  createOrchestrationEndpointRequestClient,
  type EndpointRequestClientOptions,
} from './request-contract';

export interface DefinitionRequestOptions extends EndpointRequestClientOptions {
  definitionWriteContract?: ContractSource;
}

type DefinitionRequestWindow = Window & {
  createOrchestrationDefinitionRequestClient?:
    typeof createOrchestrationDefinitionRequestClient;
};

export function createOrchestrationDefinitionRequestClient(
  options: DefinitionRequestOptions = {},
) {
  const requests = createOrchestrationEndpointRequestClient({
    ...options,
    normalizeList: options.normalizeList
      ?? normalizeOrchestrationDefinitionListRead,
    normalizeRead: options.normalizeRead
      ?? normalizeOrchestrationDefinitionRead,
    normalizeSave: options.normalizeSave
      ?? normalizeOrchestrationDefinitionSave,
    normalizeDelete: options.normalizeDelete
      ?? normalizeOrchestrationDefinitionDelete,
  });
  const writeContract = (): unknown => {
    if (typeof options.definitionWriteContract !== 'function') {
      return options.definitionWriteContract ?? null;
    }
    return options.definitionWriteContract();
  };
  const list = () => requests.request('definition-list');
  const get = (id: unknown) => requests.request('definition-read', [id]);
  const save = (
    id: unknown,
    definition: unknown,
    expectedUpdatedAt?: unknown,
  ) => {
    const contract = id ? writeContract() : null;
    const directArgs = id
      ? [id, definition, expectedUpdatedAt, contract] : [definition];
    return requests.request(
      id ? 'definition-update' : 'definition-create',
      [id, definition, expectedUpdatedAt, contract],
      directArgs,
    );
  };
  const remove = (id: unknown, expectedUpdatedAt?: unknown) =>
    requests.request(
      'definition-delete', [id, expectedUpdatedAt, writeContract()]);
  return {
    canList: () => requests.available('definition-list'),
    canRead: () => requests.available('definition-read'),
    canSave: (id: unknown) => requests.available(
      id ? 'definition-update' : 'definition-create'),
    canRemove: () => requests.available('definition-delete'),
    list,
    get,
    save,
    remove,
  };
}

(orchestrationRegistry as unknown as DefinitionRequestWindow).createOrchestrationDefinitionRequestClient =
  createOrchestrationDefinitionRequestClient;
