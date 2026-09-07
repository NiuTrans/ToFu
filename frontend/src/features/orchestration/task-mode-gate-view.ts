import { orchestrationRegistry } from './registry';
import {
  createOrchestrationKeyedActionLock,
  type OrchestrationActionOwner,
} from './action-lock';
import { createOrchestrationDraftState, type OrchestrationDraftState } from './draft-state';
import { createOrchestrationHumanGateInteraction } from './human-gate-interaction';
import {
  orchestrationRequestLimitPolicy,
  type OrchestrationRequestLimitPolicy,
} from './request-limits';
import {
  createTaskModeGatePresentation,
  type TaskModeGateEvent,
  type TaskModeGatePresentationOptions,
} from './task-mode-gate-presentation';
import { reportOrchestrationDiagnostic } from './diagnostic-report';

interface TaskModeGatePresentationPort {
  gateCard(event?: TaskModeGateEvent, index?: unknown): string;
  project(gates?: Record<string, TaskModeGateEvent>): {
    gateIds: string[];
    html: string;
  };
}

export interface TaskModeGateViewOptions extends TaskModeGatePresentationOptions {
  limitPolicy?: unknown;
  requestLimits?: unknown;
  draftState?: OrchestrationDraftState;
  presentation?: TaskModeGatePresentationPort;
  document?: Document;
  hostId?: string;
  report?: (context: string, error: unknown) => unknown;
  onApprove?: (requestId: string, approved: boolean) => unknown;
  onInput?: (requestId: string, input: string) => unknown;
}

type TaskModeGateViewWindow = Window & {
  createTaskModeGateView?: typeof createTaskModeGateView;
};

export function createTaskModeGateView(options: TaskModeGateViewOptions = {}) {
  const limitPolicy: OrchestrationRequestLimitPolicy =
    orchestrationRequestLimitPolicy(options.limitPolicy || options.requestLimits);
  const drafts = options.draftState ?? createOrchestrationDraftState();
  let gateDraftKeys: Record<string, string[]> = Object.create(null) as
    Record<string, string[]>;
  const draftUnbinds: Array<() => void> = [];
  const interactionUnbinds: Array<() => void> = [];
  let boundInteractions: Record<string, ReturnType<
    typeof createOrchestrationHumanGateInteraction>> = Object.create(null) as
      Record<string, ReturnType<typeof createOrchestrationHumanGateInteraction>>;
  const actions = createOrchestrationKeyedActionLock();
  const presentation = options.presentation ?? createTaskModeGatePresentation(options);
  const doc = (): Document => options.document ?? document;
  const translate = (key: string): string => String(
    options.translate ? options.translate(key) : key);
  const report = (context: string, error: unknown): void => {
    reportOrchestrationDiagnostic(options.report, context, error);
  };
  const host = (root?: Element | null): Element | null => root
    ?? doc().getElementById(options.hostId || 'tmInspBody');
  const gateDraftKey = (runId: unknown, requestId: unknown): string =>
    JSON.stringify([
      'task-human-gate', String(runId || 'none'), String(requestId || ''),
    ]);
  const syncDrafts = (
    runId: unknown,
    gates: Record<string, TaskModeGateEvent>,
    gateIds: string[],
  ): void => {
    const runKey = String(runId || 'none');
    const current = gateIds.filter((requestId) =>
      gates[requestId]?.mode !== 'approve').map((requestId) =>
      gateDraftKey(runKey, requestId));
    (gateDraftKeys[runKey] || []).forEach((key) => {
      if (!current.includes(key)) drafts.clear(key);
    });
    gateDraftKeys[runKey] = current;
  };
  const releaseBindings = (): void => {
    draftUnbinds.splice(0).forEach((unbind) => { unbind(); });
    interactionUnbinds.splice(0).forEach((unbind) => { unbind(); });
    boundInteractions = Object.create(null) as Record<string, ReturnType<
      typeof createOrchestrationHumanGateInteraction>>;
  };
  const refreshRequestLimits = (root?: Element | null): boolean => {
    const target = host(root);
    if (!target) return false;
    target.querySelectorAll('[data-tm-gate-input]').forEach((input) => {
      limitPolicy.applyHumanInput(input);
    });
    return true;
  };
  const project = (
    runId: unknown,
    gatesValue: Record<string, TaskModeGateEvent> = {},
  ) => {
    const gates = gatesValue && typeof gatesValue === 'object' ? gatesValue : {};
    const projected = presentation.project(gates);
    syncDrafts(runId, gates, projected.gateIds);
    return projected;
  };
  const bind = (
    root: Element | null | undefined,
    runId: unknown,
    gatesValue: Record<string, TaskModeGateEvent> = {},
    gateIdsValue?: string[],
  ): boolean => {
    const target = host(root);
    if (!target) return false;
    const gates = gatesValue && typeof gatesValue === 'object' ? gatesValue : {};
    const gateIds = Array.isArray(gateIdsValue)
      ? gateIdsValue : Object.keys(gates);
    refreshRequestLimits(target);
    gateIds.forEach((requestId, index) => {
      const card = target.querySelector(
        `[data-tm-gate-index="${index}"]`);
      if (!card) return;
      const gateKey = gateDraftKey(runId, requestId);
      const interaction = createOrchestrationHumanGateInteraction({
        root: card,
        translate,
      });
      boundInteractions[gateKey] = interaction;
      if (actions.pending(gateKey)) interaction.setBusy(true);
      const release = (owner: OrchestrationActionOwner): void => {
        if (!actions.release(owner)) return;
        boundInteractions[gateKey]?.setBusy(false);
      };
      const invoke = (callback: () => unknown): boolean => {
        const owner = actions.acquire(gateKey, 'gate');
        if (!owner) return false;
        const result = interaction.run(callback);
        if (!result || typeof result.then !== 'function') {
          actions.release(owner);
          return false;
        }
        Promise.resolve(result).then(
          () => { release(owner); },
          (error: unknown) => {
            report('gate action', error);
            release(owner);
          },
        );
        return true;
      };
      card.querySelectorAll('[data-tm-gate-decision]').forEach((button) => {
        interactionUnbinds.push(interaction.bindClick(button, () => {
          if (typeof options.onApprove === 'function') {
            invoke(() => options.onApprove?.(
              requestId,
              button.getAttribute('data-tm-gate-decision') === 'approve',
            ));
          }
        }));
      });
      const input = card.querySelector<HTMLTextAreaElement>('[data-tm-gate-input]');
      const send = card.querySelector('[data-tm-gate-send]');
      if (input) draftUnbinds.push(drafts.bind(input, gateKey));
      const submit = (): void => {
        if (typeof options.onInput === 'function') {
          invoke(() => Promise.resolve(options.onInput?.(
            requestId, input?.value ?? '')).then((accepted) => {
            if (accepted !== false) drafts.clear(gateKey);
            return accepted;
          }));
        }
      };
      interactionUnbinds.push(interaction.bindSubmit(input, send, submit));
    });
    return true;
  };
  return {
    project,
    bind,
    releaseBindings,
    gateCard: presentation.gateCard,
    refreshRequestLimits,
  };
}

(orchestrationRegistry as unknown as TaskModeGateViewWindow).createTaskModeGateView = createTaskModeGateView;
