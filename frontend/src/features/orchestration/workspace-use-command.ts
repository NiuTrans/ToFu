/** Save-and-use coordination for the Studio → current-chat handoff.
 *
 * A successful write is not enough to switch execution modes: the same
 * editor document and revision must still be current when the write settles.
 * This keeps a slow save from selecting an older snapshot after the user has
 * edited or opened another workflow.
 */
import { orchestrationRegistry } from './registry';
import { record } from './contracts';

export interface WorkspaceUseCommandOptions {
  save?: () => unknown | PromiseLike<unknown>;
  currentId?: () => unknown;
  documentToken?: () => unknown;
  revision?: () => unknown;
  useDefinition?: (id: string) => unknown | PromiseLike<unknown>;
  translate?: (key: string) => string;
  toast?: (message: string, error?: boolean) => unknown;
  onError?: (stage: string, error: unknown) => unknown;
}

type WorkspaceUseWindow = Window & {
  createOrchestrationWorkspaceUseCommand?:
    typeof createOrchestrationWorkspaceUseCommand;
};

export function createOrchestrationWorkspaceUseCommand(
  options: WorkspaceUseCommandOptions = {},
) {
  let pending: Promise<boolean> | null = null;
  const translate = (key: string): string => String(
    typeof options.translate === 'function' ? options.translate(key) : key);
  const toast = (key: string): void => {
    options.toast?.(translate(key), true);
  };

  const execute = async (): Promise<boolean> => {
    if (typeof options.save !== 'function'
        || typeof options.useDefinition !== 'function') {
      toast('orch.use.failed');
      return false;
    }
    const documentToken = options.documentToken?.();
    const revision = options.revision?.();
    let result: unknown;
    try {
      result = await options.save();
    } catch (error: unknown) {
      options.onError?.('save-current-chat', error);
      toast('orch.use.failed');
      return false;
    }
    if (result == null) return false;
    if (options.documentToken?.() !== documentToken
        || options.revision?.() !== revision) {
      toast('orch.use.stale');
      return false;
    }
    const id = String(options.currentId?.() || record(result)?.id || '');
    if (!id) {
      toast('orch.use.failed');
      return false;
    }
    try {
      const accepted = await options.useDefinition(id);
      if (accepted === true) return true;
      toast('orch.use.busy');
    } catch (error: unknown) {
      options.onError?.('use-current-chat', error);
      toast('orch.use.failed');
    }
    return false;
  };

  const saveAndUse = (): Promise<boolean> => {
    if (pending) return pending;
    pending = execute().finally(() => { pending = null; });
    return pending;
  };

  return Object.freeze({ saveAndUse });
}

(orchestrationRegistry as unknown as WorkspaceUseWindow)
  .createOrchestrationWorkspaceUseCommand =
    createOrchestrationWorkspaceUseCommand;
