import { orchestrationRegistry } from './registry';
import { orchestrationRequestFailureKey } from './request-failure';
import { record } from './contracts';
import { orchestrationResultError } from './result';
import { type WorkspacePersistenceContext } from './workspace-command-types';

export interface WorkspaceLoadOptions {
  skipConfirm?: boolean;
}

type WorkspaceLoadWindow = Window & {
  createOrchestrationWorkspaceLoadCommand?:
    typeof createOrchestrationWorkspaceLoadCommand;
};

/** Adopts a read only while its document and mutation fences are current. */
export function createOrchestrationWorkspaceLoadCommand(
  context: WorkspacePersistenceContext,
) {
  let loadGeneration = 0;

  const load = async (
    id: unknown,
    options: WorkspaceLoadOptions = {},
  ): Promise<unknown> => {
    const generation = ++loadGeneration;
    if (!options.skipConfirm && context.has('confirmReplace')
        && !await context.call('confirmReplace')) return null;
    if (generation !== loadGeneration) return null;
    if (!context.definitions.canRead()) {
      context.toast(context.translate('orch.api.unavailable'), true);
      return null;
    }
    const lifecycle = context.lifecycle;
    const revision = lifecycle && typeof lifecycle.revision === 'function'
      ? lifecycle.revision() : null;
    const session = context.workspaceSession;
    const sourceId = session.currentId();
    const mutationGeneration = context.mutations.generation(id);
    const documentUnchanged = (): boolean => {
      const currentRevision = lifecycle
        && typeof lifecycle.revision === 'function'
        ? lifecycle.revision() : revision;
      return generation === loadGeneration
        && currentRevision === revision
        && session.currentId() === sourceId
        && context.mutations.generation(id) === mutationGeneration;
    };
    try {
      const read = await context.definitions.get(id);
      if (read.cause) context.call('onError', 'read', read.cause);
      if (generation !== loadGeneration) return null;
      if (!documentUnchanged()) {
        context.toast(context.translate('orch.store.loadStale'));
        return null;
      }
      if (!read.ok) {
        context.toast(read.reason === 'not-found'
          ? context.translate('orch.store.notFound')
          : context.translate('orch.store.loadFailed', {
            error: String(read.error
              || context.translate(orchestrationRequestFailureKey(read))),
          }), true);
        return null;
      }
      const entry = record(read.entry);
      if (!entry || !entry.definition) {
        context.toast(context.translate('orch.store.notFound'), true);
        return null;
      }
      const adoption = session.applyDefinitionResult(
        entry.definition, id, { updatedAt: read.version });
      if (!adoption.ok) {
        if (adoption.cause) context.call('onError', 'adopt', adoption.cause);
        context.toast(context.translate('orch.store.readFailed'), true);
        return null;
      }
      context.call('closeStore');
      context.toast(context.translate('orch.store.loaded', {
        name: String(entry.name || context.translate('orch.store.flow')),
      }));
      return entry;
    } catch (error: unknown) {
      if (!documentUnchanged()) return null;
      context.toast(context.translate('orch.store.loadFailed', {
        error: orchestrationResultError(error, 'unknown'),
      }), true);
      return null;
    }
  };

  return Object.freeze({ load });
}

(orchestrationRegistry as unknown as WorkspaceLoadWindow).createOrchestrationWorkspaceLoadCommand =
  createOrchestrationWorkspaceLoadCommand;
