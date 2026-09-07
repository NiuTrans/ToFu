import { orchestrationRegistry } from './registry';
import {
  createOrchestrationKeyedActionLock,
  type OrchestrationActionOwner,
} from './action-lock';
import {
  createOrchestrationDraftState,
  type OrchestrationDraftState,
} from './draft-state';
import {
  createOrchestrationHumanGateInteraction,
} from './human-gate-interaction';
import {
  createOrchestrationHumanGatePresentation,
  type HumanGatePresentationOptions,
  type HumanGateProjection,
} from './human-gate-presentation';

interface HumanGateViewEntry {
  row: HTMLDivElement;
  input: HTMLTextAreaElement | null;
  interaction: ReturnType<typeof createOrchestrationHumanGateInteraction>;
  unbind(): void;
}

export interface HumanGateViewOptions extends HumanGatePresentationOptions {
  targetId?: string;
  draftState?: OrchestrationDraftState;
  presentation?: {
    build(event: unknown): HumanGateProjection;
  };
  onApprove?: (requestId: string, approved: boolean) => unknown;
  onInput?: (requestId: string) => unknown;
}

export interface OrchestrationHumanGateView {
  render(event: unknown): boolean;
  clear(requestId: unknown): void;
  clearAll(): void;
  begin(requestId: unknown): OrchestrationActionOwner | null;
  end(requestId: unknown, owner: OrchestrationActionOwner): boolean;
  inputValue(requestId: unknown): string;
}

type HumanGateViewWindow = Window & {
  createOrchestrationHumanGateView?: typeof createOrchestrationHumanGateView;
};

/** Request-scoped gate DOM, draft and pending ownership. */
export function createOrchestrationHumanGateView(
  options: HumanGateViewOptions = {},
): OrchestrationHumanGateView {
  const views = new Map<string, HumanGateViewEntry>();
  let actionLocks = createOrchestrationKeyedActionLock();
  const drafts = options.draftState ?? createOrchestrationDraftState();
  const presentation = options.presentation
    ?? createOrchestrationHumanGatePresentation(options);
  const doc = (): Document => options.document ?? document;
  const translate = (key: string): string => options.translate
    ? options.translate(key) : key;
  const target = (): HTMLElement | null => doc().getElementById(
    options.targetId || 'orchRunLog');
  const draftKey = (requestId: unknown): string => JSON.stringify(
    ['studio-human-gate', String(requestId || '')]);

  const remove = (requestId: unknown, forgetDraft: boolean): void => {
    const key = String(requestId || '');
    const view = views.get(key);
    const row = view?.row ?? doc().getElementById(`orchHumanGate-${key}`);
    view?.unbind();
    row?.remove();
    views.delete(key);
    if (forgetDraft) {
      const owner = actionLocks.current(key);
      if (owner) actionLocks.release(owner);
      drafts.clear(draftKey(key));
    }
  };
  const clear = (requestId: unknown): void => { remove(requestId, true); };
  const clearAll = (): void => {
    Array.from(views.keys()).forEach((key) => { remove(key, false); });
    actionLocks = createOrchestrationKeyedActionLock();
    drafts.clearAll();
  };
  const begin = (requestId: unknown): OrchestrationActionOwner | null => {
    const key = String(requestId || '');
    const view = views.get(key);
    if (!view?.row) return null;
    const owner = actionLocks.acquire(key, 'gate');
    if (!owner) return null;
    if (view.interaction.setBusy(true)) return owner;
    actionLocks.release(owner);
    return null;
  };
  const end = (
    requestId: unknown,
    owner: OrchestrationActionOwner,
  ): boolean => {
    const key = String(requestId || '');
    if (!actionLocks.release(owner)) return false;
    const view = views.get(key);
    if (view?.row) view.interaction.setBusy(false);
    return true;
  };
  const inputValue = (requestId: unknown): string => {
    const key = String(requestId || '');
    const view = views.get(key);
    let input: HTMLTextAreaElement | null = view?.input ?? null;
    if (!input) {
      input = doc().getElementById(
        `orchHumanInput-${key}`) as HTMLTextAreaElement | null;
    }
    return input ? input.value : drafts.read(draftKey(key), '');
  };
  const render = (event: unknown): boolean => {
    const logTarget = target();
    if (!logTarget) return false;
    const projected = presentation.build(event);
    const { requestId, row, input } = projected;
    remove(requestId, false);
    const interaction = createOrchestrationHumanGateInteraction({
      root: row,
      translate,
    });
    const interactionUnbinds: Array<() => void> = [];
    if ((event as Record<string, unknown> | null)?.mode === 'approve') {
      interactionUnbinds.push(interaction.bindClick(
        projected.approve, () => options.onApprove?.(requestId, true)));
      interactionUnbinds.push(interaction.bindClick(
        projected.reject, () => options.onApprove?.(requestId, false)));
    } else {
      interactionUnbinds.push(interaction.bindSubmit(
        input, projected.send, () => options.onInput?.(requestId)));
    }
    logTarget.appendChild(row);
    const draftUnbind = input ? drafts.bind(input, draftKey(requestId)) : null;
    views.set(requestId, {
      row,
      input,
      interaction,
      unbind: () => {
        interactionUnbinds.splice(0).forEach((unbind) => { unbind(); });
        draftUnbind?.();
      },
    });
    if (actionLocks.pending(requestId)) interaction.setBusy(true);
    logTarget.scrollTop = logTarget.scrollHeight;
    return true;
  };

  return { render, clear, clearAll, begin, end, inputValue };
}

(orchestrationRegistry as unknown as HumanGateViewWindow).createOrchestrationHumanGateView =
  createOrchestrationHumanGateView;
