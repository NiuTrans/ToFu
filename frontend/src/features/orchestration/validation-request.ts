import { orchestrationRegistry } from './registry';
import { normalizeOrchestrationValidationRead } from './authoring-read';
import {
  createOrchestrationEndpointRequestClient,
  type EndpointRequestClientOptions,
} from './request-contract';

export interface ValidationClientOptions extends EndpointRequestClientOptions {
  validate?: (...args: unknown[]) => unknown;
  canValidate?: () => boolean;
}

type ValidationRequestWindow = Window & {
  createOrchestrationValidationClient?:
    typeof createOrchestrationValidationClient;
};

export function createOrchestrationValidationClient(
  options: ValidationClientOptions = {},
) {
  const requestApi = () => {
    if (typeof options.validate === 'function') {
      return { validate: options.validate };
    }
    return typeof options.api === 'function' ? options.api() : options.api;
  };
  const requests = createOrchestrationEndpointRequestClient({
    ...options,
    api: requestApi,
    normalizeRead: options.normalizeRead
      ?? normalizeOrchestrationValidationRead,
  });
  return {
    available: () => {
      if (typeof options.canValidate === 'function'
          && !options.canValidate()) return false;
      return requests.available('validation');
    },
    validate: (definition: unknown, requestOptions?: unknown) =>
      requests.request('validation', [definition, requestOptions || {}]),
  };
}

(orchestrationRegistry as unknown as ValidationRequestWindow).createOrchestrationValidationClient =
  createOrchestrationValidationClient;
