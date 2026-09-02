import { orchestrationRegistry } from './registry';
import {
  createOrchestrationWorkspaceDeleteCommand,
} from './workspace-delete-command';
import {
  createOrchestrationWorkspaceLoadCommand,
} from './workspace-load-command';
import {
  createOrchestrationWorkspacePersistenceContext,
  type WorkspacePersistenceOptions,
} from './workspace-persistence-context';
import {
  createOrchestrationWorkspaceSaveCommand,
} from './workspace-save-command';
import {
  createOrchestrationWorkspaceUseCommand,
} from './workspace-use-command';

type WorkspacePersistenceWindow = Window & {
  createOrchestrationWorkspacePersistence?:
    typeof createOrchestrationWorkspacePersistence;
};

/** Stable facade over commands sharing one session/request/mutation context. */
export function createOrchestrationWorkspacePersistence(
  options: WorkspacePersistenceOptions = {},
) {
  const context = createOrchestrationWorkspacePersistenceContext(options);
  const saveCommand = createOrchestrationWorkspaceSaveCommand(context);
  const loadCommand = createOrchestrationWorkspaceLoadCommand(context);
  const deleteCommand = createOrchestrationWorkspaceDeleteCommand(context);
  const useCommand = createOrchestrationWorkspaceUseCommand({
    save: saveCommand.save,
    currentId: context.workspaceSession.currentId,
    documentToken: context.workspaceSession.documentToken,
    revision: context.lifecycle?.revision,
    useDefinition: (id) => context.call('onUseDefinition', id),
    translate: context.translate,
    toast: context.toast,
    onError: (stage, error) => context.call('onError', stage, error),
  });
  return Object.freeze({
    currentId: context.workspaceSession.currentId,
    save: saveCommand.save,
    saveAndUse: useCommand.saveAndUse,
    load: loadCommand.load,
    remove: deleteCommand.remove,
  });
}

(orchestrationRegistry as unknown as WorkspacePersistenceWindow).createOrchestrationWorkspacePersistence =
  createOrchestrationWorkspacePersistence;
