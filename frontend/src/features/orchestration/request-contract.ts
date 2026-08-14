import { orchestrationRegistry } from './registry';
import {
  createOrchestrationApiRequestInvoker,
  type ApiRequestInvokerOptions,
  type OrchestrationApiRequestInvoker,
} from './api-request';
import { type ContractRecord } from './contracts';
import {
  projectOrchestrationHttpRead,
} from './http-read';
import { type OrchestrationReadOptions } from './read-core';
import {
  ORCHESTRATION_REQUEST_CONTRACTS,
  type OrchestrationRequestContract,
} from './request-contracts.generated';

export type OrchestrationEndpointContract = OrchestrationRequestContract;
export { ORCHESTRATION_REQUEST_CONTRACTS };

export interface EndpointRequestClient {
  available(name: string): boolean;
  request(
    name: string,
    args?: readonly unknown[],
    directArgs?: readonly unknown[],
  ): Promise<ContractRecord>;
}

export interface EndpointRequestClientOptions
  extends OrchestrationReadOptions, ApiRequestInvokerOptions {
  requests?: OrchestrationApiRequestInvoker;
}

type RequestContractWindow = Window & {
  _ORCHESTRATION_REQUEST_CONTRACTS?:
    Readonly<Record<string, OrchestrationEndpointContract>>;
  orchestrationRequestContract?: typeof orchestrationRequestContract;
  orchestrationDefinitionSelection?: typeof orchestrationDefinitionSelection;
  createOrchestrationEndpointRequestClient?:
    typeof createOrchestrationEndpointRequestClient;
};

export function orchestrationRequestContract(
  name: string,
): OrchestrationEndpointContract | null {
  return ORCHESTRATION_REQUEST_CONTRACTS[name] ?? null;
}

export interface OrchestrationDefinitionSelection {
  definition: object | undefined;
  storedId: string | undefined;
  originId: string | undefined;
}

export function orchestrationDefinitionSelection(
  definition: unknown,
  orchestrationId?: unknown,
): Readonly<OrchestrationDefinitionSelection> {
  const inline = definition !== null && typeof definition === 'object'
    && !Array.isArray(definition) ? definition : undefined;
  const identity = String(orchestrationId || '');
  return Object.freeze({
    definition: inline,
    storedId: inline ? undefined : (identity || undefined),
    originId: inline && identity ? identity : undefined,
  });
}

export function createOrchestrationEndpointRequestClient(
  options: EndpointRequestClientOptions = {},
): EndpointRequestClient {
  const requests = options.requests
    ?? createOrchestrationApiRequestInvoker({ api: options.api });

  const contract = (name: string): OrchestrationEndpointContract => {
    const value = orchestrationRequestContract(name);
    if (value) return value;
    const error = new Error(
      `Unknown orchestration request contract: ${String(name || '')}`);
    error.name = 'OrchestrationRequestContractError';
    throw error;
  };

  const available = (name: string): boolean => {
    const value = orchestrationRequestContract(name);
    return Boolean(value) && requests.available(
      value?.resultMethod ?? '', value?.directMethod ?? '');
  };

  const request = (
    name: string,
    args?: readonly unknown[],
    directArgs?: readonly unknown[],
  ): Promise<ContractRecord> => {
    const value = contract(name);
    const resultArgs = Array.isArray(args) ? args : [];
    return requests.request({
      resultMethod: value.resultMethod,
      directMethod: value.directMethod,
      resultArgs,
      directArgs: Array.isArray(directArgs) ? directArgs : resultArgs,
      normalize: (response) => projectOrchestrationHttpRead(
        options,
        value.optionName,
        value.responseContract,
        response,
        { endpointName: name, requestArgs: resultArgs.slice(),
          responseRequiredFields: value.responseRequiredFields },
      ) as ContractRecord,
    });
  };

  return { available, request };
}

Object.assign(orchestrationRegistry as unknown as RequestContractWindow, {
  _ORCHESTRATION_REQUEST_CONTRACTS: ORCHESTRATION_REQUEST_CONTRACTS,
  orchestrationRequestContract,
  orchestrationDefinitionSelection,
  createOrchestrationEndpointRequestClient,
});
