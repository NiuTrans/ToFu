import { orchestrationRegistry } from './registry';
import { orchestrationRequestFailureKey } from './request-failure';
import { record } from './contracts';
import { orchestrationResultError } from './result';
import { type WorkspacePersistenceContext } from './workspace-command-types';

type WorkspaceSaveWindow = Window & {
  createOrchestrationWorkspaceSaveCommand?:
    typeof createOrchestrationWorkspaceSaveCommand;
};

/** Validation, CAS and editor-ownership fence for one save operation. */
export function createOrchestrationWorkspaceSaveCommand(
  context: WorkspacePersistenceContext,
) {
  let activeSaves = 0;

  const beginSave = (): void => {
    activeSaves += 1;
    if (activeSaves === 1) context.lifecycle.setSaveBusy(true);
  };

  const endSave = (): void => {
    activeSaves = Math.max(0, activeSaves - 1);
    if (activeSaves === 0) context.lifecycle.setSaveBusy(false);
  };

  const execute = async (): Promise<unknown> => {
    const session = context.workspaceSession;
    const persistedId = session.currentId();
    const expectedUpdatedAt = session.currentVersion();
    if (!context.definitions.canSave(persistedId)) {
      context.toast(context.translate('orch.api.unavailable'), true);
      return null;
    }
    const lifecycle = context.lifecycle;
    const inspection = typeof lifecycle?.requireValid === 'function'
      ? await lifecycle.requireValid(context.translate('orch.doc.saveAction'))
      : null;
    if (!inspection) return null;
    const definition = context.call('rootDefinition');
    const saveCheckpoint = lifecycle.createSaveCheckpoint();
    const documentToken = session.documentToken();
    const wasCreate = !persistedId;
    beginSave();
    try {
      const result = await context.definitions.save(
        persistedId, definition, expectedUpdatedAt);
      if (result.cause) context.call('onError', 'save', result.cause);
      const data = record(result.data) ?? {};
      if (!result.ok) {
        if (result.conflict) {
          lifecycle.markWriteConflict?.(result.conflict);
          context.toast(context.translate('orch.save.conflict'), true);
          return null;
        }
        const errorMessage = String(result.error
          || context.translate(orchestrationRequestFailureKey(result)));
        const detail = errorMessage ? `: ${errorMessage}` : '';
        context.toast(context.translate(
          'orch.save.rejected', { errors: detail }), true);
        return null;
      }
      if (wasCreate && !data.id) {
        context.toast(context.translate('orch.save.missingId'), true);
        return null;
      }
      context.mutations.advance(persistedId || data.id);
      context.definitionsChanged();
      const inspectionRecord = record(inspection) ?? {};
      const savedInspection = context.normalizeInspection(
        data.inspection || {
          ok: true,
          errors: [],
          warnings: data.warnings || inspectionRecord.warnings || [],
          contract: data.contract || inspectionRecord.contract || null,
        },
      );
      if (session.documentToken() !== documentToken
          || !lifecycle.isSaveCheckpointCurrent(saveCheckpoint)) {
        context.toast(context.translate('orch.doc.savedSnapshot'));
        return data;
      }
      session.acknowledgePersisted(data.id, result.version);
      const completion = record(lifecycle.completeSaveCheckpoint(
        saveCheckpoint, savedInspection)) ?? {};
      if (completion.current) {
        const normalized = record(savedInspection) ?? {};
        context.call('warn', context.translate('orch.save.done', {
          name: context.call('currentName') || '',
        }), normalized.warnings);
      } else {
        context.toast(context.translate('orch.doc.savedSnapshot'));
      }
      return data;
    } catch (error: unknown) {
      context.toast(context.translate('orch.save.failed', {
        error: orchestrationResultError(error, 'unknown'),
      }), true);
      return null;
    } finally {
      endSave();
    }
  };

  const save = (): Promise<unknown> => {
    const scope = context.workspaceSession.documentToken();
    return context.mutations.share(
      'save', `document:${String(scope)}`, execute);
  };

  return Object.freeze({ save });
}

(orchestrationRegistry as unknown as WorkspaceSaveWindow).createOrchestrationWorkspaceSaveCommand =
  createOrchestrationWorkspaceSaveCommand;
