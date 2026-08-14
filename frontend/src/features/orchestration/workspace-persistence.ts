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
  return Object.freeze({
    currentId: context.workspaceSession.currentId,
    save: saveCommand.save,
    load: loadCommand.load,
    remove: deleteCommand.remove,
  });
}

(orchestrationRegistry as unknown as WorkspacePersistenceWindow).createOrchestrationWorkspacePersistence =
  createOrchestrationWorkspacePersistence;
