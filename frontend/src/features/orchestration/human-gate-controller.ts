import { orchestrationRegistry } from './registry';
import { createOrchestrationMutationCommand } from './mutation-command';
import {
  createOrchestrationHumanGateView,
  type HumanGateViewOptions,
  type OrchestrationHumanGateView,
} from './human-gate-view';

export interface HumanGateControllerOptions extends HumanGateViewOptions {
  view?: OrchestrationHumanGateView;
  failureMessage?: (result: unknown, fallback: string) => unknown;
  report?: (context: string, error: unknown) => unknown;
  toast?: (message: string, error?: boolean) => unknown;
  approve?: (requestId: unknown, approved: unknown) => unknown;
  input?: (requestId: unknown, value: string) => unknown;
}

type HumanGateControllerWindow = Window & {
  createOrchestrationHumanGateController?:
    typeof createOrchestrationHumanGateController;
};

/** Mutation outcome and request-scoped view coordination for Studio gates. */
export function createOrchestrationHumanGateController(
  options: HumanGateControllerOptions = {},
) {
  const translate = (key: string): string => options.translate
    ? options.translate(key) : key;
  const toast = (message: string, isError?: boolean): void => {
    options.toast?.(message, Boolean(isError));
  };
  const command = createOrchestrationMutationCommand({
    failureMessage: options.failureMessage,
    report: options.report,
  });
  let approve: (requestId: unknown, approved: unknown) => Promise<boolean>;
  let input: (requestId: unknown) => Promise<boolean>;
  const view = options.view ?? createOrchestrationHumanGateView({
    ...options,
    document: options.document ?? document,
    translate,
    onApprove: (requestId, approved) => approve(requestId, approved),
    onInput: (requestId) => input(requestId),
  });
  const resolve = async (
    context: string,
    requestId: unknown,
    request: () => unknown,
  ): Promise<boolean> => {
    const outcome = await command.execute({
      context,
      fallback: translate('orch.run.gateFailed'),
      request,
    });
    if (outcome.ok || outcome.targetAbsent) view.clear(requestId);
    if (!outcome.ok) toast(outcome.message, true);
    return outcome.ok;
  };
  approve = async (requestId, approved): Promise<boolean> => {
    const owner = view.begin(requestId);
    if (!owner) return false;
    try {
      return await resolve('human-approve', requestId, () =>
        typeof options.approve === 'function'
          ? options.approve(requestId, approved) : null);
    } finally {
      view.end(requestId, owner);
    }
  };
  input = async (requestId): Promise<boolean> => {
    const value = view.inputValue(requestId);
    if (!value.trim()) {
      toast(translate('orch.gate.enterResponse'), true);
      return false;
    }
    const owner = view.begin(requestId);
    if (!owner) return false;
    try {
      return await resolve('human-input', requestId, () =>
        typeof options.input === 'function'
          ? options.input(requestId, value) : null);
    } finally {
      view.end(requestId, owner);
    }
  };

  return Object.freeze({
    approve,
    clear: view.clear,
    clearAll: view.clearAll,
    input,
    render: view.render,
  });
}

(orchestrationRegistry as unknown as HumanGateControllerWindow).createOrchestrationHumanGateController =
  createOrchestrationHumanGateController;
