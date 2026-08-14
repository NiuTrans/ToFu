import { orchestrationRegistry } from './registry';
import { type ContractSource } from './contracts';
import {
  createOrchestrationDefinitionMutationCoordinator,
} from './definition-mutations';
import {
  createOrchestrationDefinitionRequestClient,
} from './definition-request';
import {
  projectOrchestrationInspection,
  type InspectionProjectionOptions,
} from './inspection-result';
import { type OrchestrationSingleFlight } from './single-flight';
import {
  type WorkspaceDefinitionRequests,
  type WorkspaceEditLifecycle,
  type WorkspaceMutationCoordinator,
  type WorkspacePersistenceContext,
  type WorkspaceSessionPort,
} from './workspace-command-types';
import {
  createOrchestrationWorkspaceSessionPort,
  type WorkspaceSessionPortOptions,
} from './workspace-session-port';

export interface WorkspacePersistenceOptions
  extends WorkspaceSessionPortOptions, InspectionProjectionOptions {
  [key: string]: unknown;
  workspaceSession?: WorkspaceSessionPort;
  mutations?: WorkspaceMutationCoordinator;
  definitionRequest?: WorkspaceDefinitionRequests;
  lifecycle?: WorkspaceEditLifecycle;
  api?: unknown | (() => unknown);
  definitionWriteContract?: ContractSource;
  definitionListContract?: ContractSource;
  definitionEntryContract?: ContractSource;
  flights?: OrchestrationSingleFlight;
}

type WorkspacePersistenceContextWindow = Window & {
  createOrchestrationWorkspacePersistenceContext?:
    typeof createOrchestrationWorkspacePersistenceContext;
};

export function createOrchestrationWorkspacePersistenceContext(
  options: WorkspacePersistenceOptions = {},
): WorkspacePersistenceContext {
  const workspaceSession = options.workspaceSession
    ?? createOrchestrationWorkspaceSessionPort(options);
  const mutations = options.mutations
    ?? createOrchestrationDefinitionMutationCoordinator({
      flights: options.flights,
    });
  const definitions = options.definitionRequest
    ?? createOrchestrationDefinitionRequestClient({
      api: options.api,
      definitionWriteContract: options.definitionWriteContract,
      definitionListContract: options.definitionListContract,
      definitionEntryContract: options.definitionEntryContract,
    });

  const has = (name: string): boolean => typeof options[name] === 'function';

  const call = (name: string, ...args: readonly unknown[]): any => {
    const callback = options[name];
    return typeof callback === 'function'
      ? callback(...args) : undefined;
  };

  const translate = (
    key: string,
    params?: Record<string, unknown>,
  ): string => String(has('translate') ? call('translate', key, params) : key);

  const toast = (message: string, error?: boolean): void => {
    if (has('toast')) call('toast', message, error);
  };

  const definitionsChanged = (): void => {
    call('onDefinitionsChanged');
  };

  const normalizeInspection = (value: unknown): unknown =>
    projectOrchestrationInspection(options, value);

  return Object.freeze({
    workspaceSession,
    mutations,
    definitions,
    lifecycle: options.lifecycle as WorkspaceEditLifecycle,
    has,
    call,
    translate,
    toast,
    definitionsChanged,
    normalizeInspection,
  });
}

(orchestrationRegistry as unknown as WorkspacePersistenceContextWindow)
  .createOrchestrationWorkspacePersistenceContext =
    createOrchestrationWorkspacePersistenceContext;
