/**
 * Responsibility: execute the generated Orchestration HTTP catalogue and
 * expose the stable `Api.orchestrations` compatibility surface.
 * Entry points: createOrchestrationApiClient,
 * installOrchestrationApiClient, and resolveOrchestrationApiClient.
 * Dependencies: generated request contracts, the injected typed transport
 * facade, and the immutable HTTP-result projector.
 */
import { HTTP_RESULT, type HttpResultApi } from '../../core/http-result';
import { orchestrationRegistry } from './registry';
import {
  ORCHESTRATION_REQUEST_CONTRACTS,
  type OrchestrationRequestContract,
} from './request-contracts.generated';

type UnknownRecord = Record<string, unknown>;
type ApiMethod = (...args: unknown[]) => unknown;

export interface OrchestrationApiHost {
  readonly get?: ApiMethod;
  readonly post?: ApiMethod;
  readonly put?: ApiMethod;
  readonly del?: ApiMethod;
  readonly orchestrations?: UnknownRecord;
}

export type OrchestrationApiClient = Readonly<Record<string, ApiMethod>>;

export interface OrchestrationEndpointTransport {
  request(
    name: string,
    args?: readonly unknown[],
    normalized?: boolean,
  ): unknown;
}

export interface OrchestrationEndpointTransportOptions {
  api: OrchestrationApiHost;
  httpResult?: HttpResultApi;
}

type WriteContract = {
  operations?: unknown;
  preconditionHeader?: unknown;
  tokenSyntax?: unknown;
};

type RequestOptions = {
  method?: string;
  query?: UnknownRecord;
  json?: unknown;
  headers?: Record<string, string>;
  parse?: string;
  onError?: string;
  signal?: unknown;
};

type OrchestrationRegistryPort = {
  resolveOrchestrationApiClient?: typeof resolveOrchestrationApiClient;
};

const record = (value: unknown): UnknownRecord | null => (
  value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord : null
);

const endpointError = (message: string): Error => {
  const error = new Error(message);
  error.name = 'OrchestrationEndpointTransportError';
  return error;
};

const writeContractError = (message: string): Error => {
  const error = new Error(message);
  error.name = 'OrchestrationDefinitionWriteContractError';
  return error;
};

function writeOptions(
  expectedUpdatedAt: unknown,
  contractValue: unknown,
  operation: string,
): RequestOptions {
  const contract = record(contractValue) as WriteContract | null;
  if (Array.isArray(contract?.operations)
      && !contract.operations.includes(operation)) {
    throw writeContractError(
      `Definition write contract does not publish operation ${operation}`,
    );
  }
  if (!Number.isSafeInteger(expectedUpdatedAt)
      || Number(expectedUpdatedAt) < 0) {
    throw writeContractError(
      'Definition write contract requires a version token',
    );
  }
  const header = contract ? contract.preconditionHeader : 'If-Match';
  const syntax = contract ? contract.tokenSyntax : 'quoted-decimal';
  if (typeof header !== 'string' || !header
      || syntax !== 'quoted-decimal') {
    throw writeContractError(
      'Unsupported definition write precondition contract',
    );
  }
  return {
    parse: 'response',
    headers: { [header]: `"${String(expectedUpdatedAt)}"` },
  };
}

function routeUrl(
  contract: OrchestrationRequestContract,
  args: readonly unknown[],
): string {
  const mapping = contract.pathArgs ?? {};
  return contract.route.replace(
    /<(?:[^:<>]+:)?([^<>]+)>/g,
    (_placeholder, field: string) => {
      if (!Object.prototype.hasOwnProperty.call(mapping, field)) {
        throw endpointError(
          `Missing orchestration path argument mapping ${field}`,
        );
      }
      const value = args[mapping[field]];
      if (value == null || value === '') {
        throw endpointError(
          `Missing orchestration path argument value ${field}`,
        );
      }
      return encodeURIComponent(String(value));
    },
  );
}

function queryFromContract(
  contract: OrchestrationRequestContract,
  args: readonly unknown[],
): UnknownRecord {
  const query: UnknownRecord = {};
  for (const [field, index] of Object.entries(contract.queryArgs ?? {})) {
    const value = args[index];
    query[field] = value == null || value === '' ? undefined : value;
  }
  return query;
}

function bodyFromContract(
  contract: OrchestrationRequestContract,
  args: readonly unknown[],
): unknown {
  if (Number.isInteger(contract.bodyArg)) {
    return args[Number(contract.bodyArg)];
  }
  const fields = Object.entries(contract.bodyArgs ?? {});
  if (fields.length === 0) return undefined;
  return Object.fromEntries(fields.map(([field, index]) => [field, args[index]]));
}

function requestOptions(
  contract: OrchestrationRequestContract,
  args: readonly unknown[],
  normalized: boolean,
): RequestOptions {
  const extras: RequestOptions = Object.keys(contract.queryArgs ?? {}).length
    ? { query: queryFromContract(contract, args) } : {};
  if (Number.isInteger(contract.requestOptionsArg)) {
    const declared = record(args[Number(contract.requestOptionsArg)]);
    if (declared?.signal) extras.signal = declared.signal;
  }
  if (contract.writeOperation) {
    const guarded = Number.isInteger(contract.writeVersionArg)
      ? writeOptions(
        args[Number(contract.writeVersionArg)],
        Number.isInteger(contract.writeContractArg)
          ? args[Number(contract.writeContractArg)] : null,
        contract.writeOperation,
      )
      : { parse: 'response' };
    return Object.assign(guarded, extras);
  }
  return Object.assign(
    normalized ? { parse: 'response' } : { onError: 'null' },
    extras,
  );
}

export function orchestrationEndpointContract(
  name: string,
): OrchestrationRequestContract | null {
  return ORCHESTRATION_REQUEST_CONTRACTS[name] ?? null;
}

export function orchestrationEndpointContracts(): Readonly<
  Record<string, OrchestrationRequestContract>
> {
  return ORCHESTRATION_REQUEST_CONTRACTS;
}

export function createOrchestrationEndpointTransport(
  options: OrchestrationEndpointTransportOptions,
): OrchestrationEndpointTransport {
  const api = options.api;
  const httpResult = options.httpResult ?? HTTP_RESULT;
  if (!api || !httpResult || typeof httpResult.normalize !== 'function') {
    throw new TypeError(
      'orchestration endpoint transport requires an API host and HTTP result port',
    );
  }
  const verbs: Readonly<Record<string, ApiMethod | undefined>> = {
    GET: api.get,
    POST: api.post,
    PUT: api.put,
    DELETE: api.del,
  };
  const request = (
    name: string,
    args: readonly unknown[] = [],
    normalized = false,
  ): unknown => {
    const contract = orchestrationEndpointContract(name);
    if (!contract) {
      throw endpointError(
        `Unknown orchestration HTTP endpoint: ${String(name || '')}`,
      );
    }
    const verb = verbs[contract.method];
    if (typeof verb !== 'function') {
      throw new TypeError(
        `Api does not implement orchestration HTTP verb ${contract.method}`,
      );
    }
    const url = routeUrl(contract, args);
    const configured = requestOptions(contract, args, normalized);
    const response = contract.method === 'GET' || contract.method === 'DELETE'
      ? verb.call(api, url, configured)
      : verb.call(api, url, bodyFromContract(contract, args), configured);
    return normalized ? httpResult.normalize(response) : response;
  };
  return Object.freeze({ request });
}

export function createOrchestrationApiClient(
  options: OrchestrationEndpointTransportOptions,
): OrchestrationApiClient {
  const transport = createOrchestrationEndpointTransport(options);
  const methods: Record<string, ApiMethod> = Object.create(null);
  const request = (
    name: string,
    args: readonly unknown[],
    normalized: boolean,
  ): unknown => transport.request(name, args, normalized);

  const listResult = async (): Promise<UnknownRecord> => {
    const value = await request('definition-list', [], true);
    const result = record(value) ?? {};
    const body = result.data;
    const bodyRecord = record(body);
    const items = Array.isArray(body)
      ? body : (Array.isArray(bodyRecord?.items) ? bodyRecord.items : []);
    const accepted = result.ok === true && (
      Array.isArray(body)
      || Boolean(bodyRecord) && bodyRecord?.ok !== false
        && Array.isArray(bodyRecord?.items)
    );
    return Object.assign({}, result, {
      accepted,
      items: accepted ? items : [],
    });
  };

  methods.listResult = listResult;
  methods.list = async () => {
    const result = await listResult();
    return result.accepted === true ? result.items : [];
  };
  methods.save = (
    id: unknown,
    definition: unknown,
    expectedUpdatedAt?: unknown,
    writeContract?: unknown,
  ) => request(
    id ? 'definition-update' : 'definition-create',
    id
      ? [id, definition, expectedUpdatedAt, writeContract]
      : [definition],
    true,
  );

  const methodOwners = new Map<string, string>();
  const installMethod = (
    endpoint: string,
    method: string,
    normalized: boolean,
  ): void => {
    if (method === 'list' || method === 'listResult' || method === 'save') {
      return;
    }
    const owner = methodOwners.get(method);
    if (owner && owner !== endpoint) {
      const error = new Error(
        `Orchestration API method ${method} is owned by both ${owner} and ${endpoint}`,
      );
      error.name = 'OrchestrationEndpointFacadeError';
      throw error;
    }
    methodOwners.set(method, endpoint);
    if (!methods[method]) {
      methods[method] = (...args: unknown[]) => request(
        endpoint, args, normalized,
      );
    }
  };
  for (const [endpoint, contract] of Object.entries(
    ORCHESTRATION_REQUEST_CONTRACTS,
  )) {
    installMethod(endpoint, contract.resultMethod, true);
    installMethod(endpoint, contract.directMethod, false);
  }
  return Object.freeze(methods);
}

let installedHost: OrchestrationApiHost | null = null;
let installedClient: OrchestrationApiClient | null = null;

export function installOrchestrationApiClient(
  api: OrchestrationApiHost,
): OrchestrationApiClient {
  if (api === installedHost && installedClient) return installedClient;
  const target = api?.orchestrations;
  if (!target || typeof target !== 'object') {
    throw new TypeError(
      'orchestration API installation requires Api.orchestrations',
    );
  }
  const client = createOrchestrationApiClient({ api });
  Object.assign(target, client);
  installedHost = api;
  installedClient = target as OrchestrationApiClient;
  return installedClient;
}

export function resolveOrchestrationApiClient(): OrchestrationApiClient | null {
  return installedClient;
}

(orchestrationRegistry as OrchestrationRegistryPort)
  .resolveOrchestrationApiClient = resolveOrchestrationApiClient;
