import { orchestrationRegistry } from './registry';
import { type WorkspaceSessionPort } from './workspace-command-types';
import {
  normalizeOrchestrationDefinitionAdoption,
  type DefinitionAdoptionResult,
} from './definition-adoption';

interface EmbeddedWorkspaceSession {
  currentId?: () => unknown;
  currentVersion?: () => number | null;
  documentToken?: () => unknown;
  applyDefinition?: (
    definition: unknown,
    id: unknown,
    options: { updatedAt: unknown },
  ) => unknown;
  applyDefinitionResult?: (
    definition: unknown,
    id: unknown,
    options: { updatedAt: unknown },
  ) => DefinitionAdoptionResult;
  acknowledgePersisted?: (id: unknown, version?: number) => unknown;
  detachPersisted?: () => unknown;
}

export interface WorkspaceSessionPortOptions {
  session?: EmbeddedWorkspaceSession | null;
  currentId?: () => unknown;
  currentVersion?: () => number | null;
  setCurrentId?: (id: unknown) => unknown;
  setCurrentVersion?: (version: number | null) => unknown;
  applyDefinition?: (
    definition: unknown,
    id: unknown,
    options: { updatedAt: unknown },
  ) => unknown;
}

type WorkspaceSessionWindow = Window & {
  createOrchestrationWorkspaceSessionPort?:
    typeof createOrchestrationWorkspaceSessionPort;
};

/** Prefer a native session object while retaining callback embedding. */
export function createOrchestrationWorkspaceSessionPort(
  options: WorkspaceSessionPortOptions = {},
): WorkspaceSessionPort {
  const session = options.session ?? null;

  const currentId = (): unknown => typeof session?.currentId === 'function'
    ? session.currentId()
    : typeof options.currentId === 'function' ? options.currentId() : null;

  const currentVersion = (): number | null =>
    typeof session?.currentVersion === 'function'
      ? session.currentVersion()
      : typeof options.currentVersion === 'function'
        ? options.currentVersion() : null;

  const documentToken = (): unknown =>
    typeof session?.documentToken === 'function'
      ? session.documentToken() : currentId();

  const applyDefinition = (
    definition: unknown,
    id: unknown,
    opts: { updatedAt: unknown },
  ): unknown => {
    if (typeof session?.applyDefinition === 'function') {
      return session.applyDefinition(definition, id, opts);
    }
    return typeof options.applyDefinition === 'function'
      ? options.applyDefinition(definition, id, opts) : null;
  };

  const applyDefinitionResult = (
    definition: unknown,
    id: unknown,
    opts: { updatedAt: unknown },
  ): DefinitionAdoptionResult => {
    if (typeof session?.applyDefinitionResult === 'function') {
      return session.applyDefinitionResult(definition, id, opts);
    }
    return normalizeOrchestrationDefinitionAdoption(
      applyDefinition(definition, id, opts));
  };

  const acknowledgePersisted = (
    id: unknown,
    version: unknown,
  ): unknown => {
    if (typeof session?.acknowledgePersisted === 'function') {
      return typeof version === 'number' && Number.isSafeInteger(version)
        ? session.acknowledgePersisted(id, version)
        : session.acknowledgePersisted(id);
    }
    if (id && typeof options.setCurrentId === 'function') {
      options.setCurrentId(id);
    }
    if (typeof version === 'number' && Number.isSafeInteger(version)
        && typeof options.setCurrentVersion === 'function') {
      options.setCurrentVersion(version);
    }
    return null;
  };

  const detachPersisted = (): unknown => {
    if (typeof session?.detachPersisted === 'function') {
      return session.detachPersisted();
    }
    options.setCurrentId?.(null);
    options.setCurrentVersion?.(null);
    return null;
  };

  return Object.freeze({
    currentId,
    currentVersion,
    documentToken,
    applyDefinitionResult,
    applyDefinition,
    acknowledgePersisted,
    detachPersisted,
  });
}

(orchestrationRegistry as unknown as WorkspaceSessionWindow).createOrchestrationWorkspaceSessionPort =
  createOrchestrationWorkspaceSessionPort;
