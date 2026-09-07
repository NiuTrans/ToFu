import { orchestrationRegistry } from './registry';
import { record } from './contracts';
import {
  projectOrchestrationDefinitionAdoption,
  type DefinitionAdoptionProjection,
  type DefinitionAdoptionResult,
} from './definition-adoption';
import { type OrchestrationEditLifecycle } from './edit-lifecycle';

export interface OrchestrationSessionOptions extends DefinitionAdoptionProjection {
  lifecycle?: Pick<OrchestrationEditLifecycle, 'markDirty' | 'adoptBaseline'> | null;
  resetStack?: () => unknown;
  workspaceFromDefinition?: (definition: unknown) => unknown;
  adoptWorkspace?: (workspace: unknown) => unknown;
  render?: () => unknown;
  fitView?: () => unknown;
  nodeCount?: () => unknown;
  tidy?: (options: {
    silent: boolean;
    preserveDocumentState: boolean;
  }) => unknown;
}

export interface OrchestrationSessionIdentity {
  id: unknown;
  updatedAt: number | null;
}

export interface ApplyDefinitionOptions {
  dirty?: unknown;
  updatedAt?: unknown;
  inspection?: unknown;
  [key: string]: unknown;
}

type SessionWindow = Window & {
  createOrchestrationSessionController?:
    typeof createOrchestrationSessionController;
};

/** Active persisted identity, CAS version and definition adoption boundary. */
export function createOrchestrationSessionController(
  options: OrchestrationSessionOptions = {},
) {
  const lifecycle = options.lifecycle ?? null;
  let persistedId: unknown = null;
  let persistedVersion: number | null = null;
  let documentGeneration = 0;
  const version = (value: unknown): number | null =>
    typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
      ? value : null;
  const identity = (): OrchestrationSessionIdentity => ({
    id: persistedId,
    updatedAt: persistedVersion,
  });
  const currentId = (): unknown => persistedId;
  const currentVersion = (): number | null => persistedVersion;
  const documentToken = (): number => documentGeneration;

  function acknowledgePersisted(
    id: unknown,
    updatedAt?: unknown,
  ): OrchestrationSessionIdentity {
    if (id) {
      if (id !== persistedId && arguments.length < 2) persistedVersion = null;
      persistedId = id;
    }
    if (arguments.length > 1) persistedVersion = version(updatedAt);
    return identity();
  }

  const detachPersisted = (): OrchestrationSessionIdentity => {
    persistedId = null;
    persistedVersion = null;
    documentGeneration += 1;
    return identity();
  };

  const applyDefinitionResult = (
    definition: unknown,
    id: unknown,
    applyOptions: ApplyDefinitionOptions = {},
  ): DefinitionAdoptionResult => {
    const projection = projectOrchestrationDefinitionAdoption(
      options, definition);
    if (!projection.ok) return projection;
    const workspace = projection.workspace;
    const previousId = persistedId;
    const nextId = id || null;
    if (!applyOptions.dirty || nextId !== previousId) {
      documentGeneration += 1;
    }
    persistedId = nextId;
    if (Object.prototype.hasOwnProperty.call(applyOptions, 'updatedAt')) {
      persistedVersion = version(applyOptions.updatedAt);
    } else if (!persistedId || previousId !== persistedId) {
      persistedVersion = null;
    }

    options.resetStack?.();
    options.adoptWorkspace?.(workspace);
    if (applyOptions.dirty) lifecycle?.markDirty?.();
    else lifecycle?.adoptBaseline?.(
      Boolean(persistedId), applyOptions.inspection || null);
    options.render?.();
    options.fitView?.();
    if (record(workspace)?.needsLayout
        && options.nodeCount?.()
        && typeof options.tidy === 'function') {
      options.tidy({ silent: true, preserveDocumentState: true });
    }
    return { ok: true, workspace };
  };

  const applyDefinition = (
    definition: unknown,
    id: unknown,
    applyOptions: ApplyDefinitionOptions = {},
  ): unknown => {
    const result = applyDefinitionResult(definition, id, applyOptions);
    return result.ok ? result.workspace : null;
  };

  return {
    identity,
    currentId,
    currentVersion,
    documentToken,
    acknowledgePersisted,
    detachPersisted,
    applyDefinitionResult,
    applyDefinition,
  };
}

(orchestrationRegistry as unknown as SessionWindow).createOrchestrationSessionController =
  createOrchestrationSessionController;
