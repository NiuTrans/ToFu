import { orchestrationRegistry } from './registry';
import { orchestrationRequestFailureKey } from './request-failure';
import { type WorkspacePersistenceContext } from './workspace-command-types';

type WorkspaceDeleteWindow = Window & {
  createOrchestrationWorkspaceDeleteCommand?:
    typeof createOrchestrationWorkspaceDeleteCommand;
};

/** Guarded delete with optimistic versioning and session reconciliation. */
export function createOrchestrationWorkspaceDeleteCommand(
  context: WorkspacePersistenceContext,
) {
  const execute = async (
    id: unknown,
    listedUpdatedAt?: unknown,
  ): Promise<boolean> => {
    if (context.has('confirmDelete')
        && !await context.call('confirmDelete')) return false;
    if (!context.definitions.canRemove()) {
      context.toast(context.translate('orch.api.unavailable'), true);
      return false;
    }
    const session = context.workspaceSession;
    let expectedUpdatedAt = typeof listedUpdatedAt === 'number'
      && Number.isSafeInteger(listedUpdatedAt) && listedUpdatedAt >= 0
      ? listedUpdatedAt : null;
    if (expectedUpdatedAt === null && session.currentId() === id) {
      expectedUpdatedAt = session.currentVersion();
    }
    if (!Number.isSafeInteger(expectedUpdatedAt)
        || Number(expectedUpdatedAt) < 0) {
      context.toast(context.translate('orch.store.deleteConflict'), true);
      if (context.has('refreshStore')) await context.call('refreshStore');
      return false;
    }
    const result = await context.definitions.remove(id, expectedUpdatedAt);
    if (result.cause) context.call('onError', 'delete', result.cause);
    if (result.conflict) {
      context.toast(context.translate('orch.store.deleteConflict'), true);
      if (context.has('refreshStore')) await context.call('refreshStore');
      return false;
    }
    if (!result.ok) {
      context.toast(`${context.translate('orch.store.deleteFailed')}: ${String(
        result.error
          || context.translate(orchestrationRequestFailureKey(result)))}`,
      true);
      return false;
    }
    context.mutations.advance(id);
    context.definitionsChanged();
    if (session.currentId() === id) {
      session.detachPersisted();
      context.lifecycle?.detachPersistedCheckpoint?.();
    }
    context.toast(context.translate('orch.store.deleted'));
    if (context.has('refreshStore')) await context.call('refreshStore');
    return true;
  };

  const remove = (
    id: unknown,
    listedUpdatedAt?: unknown,
  ): Promise<boolean> => context.mutations.share(
    'delete', id, () => execute(id, listedUpdatedAt));

  return Object.freeze({ remove });
}

(orchestrationRegistry as unknown as WorkspaceDeleteWindow).createOrchestrationWorkspaceDeleteCommand =
  createOrchestrationWorkspaceDeleteCommand;
