import { orchestrationRegistry } from './registry';
import { projectOrchestrationActionState } from './action-state-view';
import { record } from './contracts';
import {
  createOrchestrationPanelFocusReturn,
  setOrchestrationPanelState,
} from './panel-state';

export interface RunDrawerViewOptions {
  document?: Document;
  startSeed?: (definition?: unknown) => unknown;
  translate?: (key: string) => string;
  onVisibilityChange?: (opened: boolean) => unknown;
}

type RunDrawerViewWindow = Window & {
  createOrchestrationRunDrawerView?: typeof createOrchestrationRunDrawerView;
};

/** Studio Run Drawer accessibility, focus, input, log and action projection. */
export function createOrchestrationRunDrawerView(
  options: RunDrawerViewOptions = {},
) {
  let opened = false;
  const focusReturn = createOrchestrationPanelFocusReturn();
  const doc = (): Document => options.document ?? document;
  const startSeed = (definition?: unknown): string => String(
    typeof options.startSeed === 'function'
      ? options.startSeed(definition) || '' : '');
  const translate = (key: string): string => options.translate
    ? options.translate(key) : key;
  const inputFor = (definition?: unknown): string => {
    const input = doc().getElementById(
      'orchRunInput') as HTMLInputElement | HTMLTextAreaElement | null;
    const value = input?.value || '';
    return !value.trim() ? startSeed(definition) : value;
  };
  const open = (): boolean => {
    const owner = doc();
    const drawer = owner.getElementById('orchRunDrawer');
    const trigger = owner.getElementById('orchOpenRunBtn');
    if (drawer) {
      focusReturn.capture(owner);
      opened = setOrchestrationPanelState(drawer, true, {
        document: owner,
        openClass: 'is-open',
        trigger,
      });
      options.onVisibilityChange?.(opened);
    }
    const input = owner.getElementById(
      'orchRunInput') as HTMLInputElement | HTMLTextAreaElement | null;
    if (input && !input.value) input.value = startSeed();
    input?.focus?.();
    return opened;
  };
  const close = (): boolean => {
    const owner = doc();
    const drawer = owner.getElementById('orchRunDrawer');
    const trigger = owner.getElementById('orchOpenRunBtn');
    focusReturn.prepare(owner, drawer);
    opened = setOrchestrationPanelState(drawer, false, {
      document: owner,
      openClass: 'is-open',
      trigger,
      focusTarget: trigger as HTMLElement | null,
    });
    options.onVisibilityChange?.(opened);
    focusReturn.restore(owner);
    return opened;
  };
  const clearLog = (): void => {
    const target = doc().getElementById('orchRunLog');
    if (target) target.innerHTML = '';
  };
  const log = (html: string, className?: string): boolean => {
    const owner = doc();
    const target = owner.getElementById('orchRunLog');
    if (!target) return false;
    const row = owner.createElement('div');
    row.className = `orch-run-line${className ? ` ${className}` : ''}`;
    row.innerHTML = html;
    target.appendChild(row);
    target.scrollTop = target.scrollHeight;
    return true;
  };
  const setActionState = (actionValue: unknown): boolean => {
    const action = record(actionValue) ?? {};
    const actionName = String(action.name || '');
    const owner = doc();
    const plan = owner.getElementById('orchRunPlanBtn') as HTMLButtonElement | null;
    const run = owner.getElementById('orchRunBtn') as HTMLButtonElement | null;
    const durable = owner.getElementById('orchRunTaskBtn') as HTMLButtonElement | null;
    const abort = owner.getElementById('orchRunAbort') as HTMLElement | null;
    const input = owner.getElementById('orchRunInput') as HTMLInputElement | null;
    const logTarget = owner.getElementById('orchRunLog');
    const drawer = owner.getElementById('orchRunDrawer');
    const state = owner.getElementById('orchRunState') as
      (HTMLElement & { hidden: boolean }) | null;
    const stateLabel = owner.getElementById('orchRunStateLabel');
    const keys: Record<string, string> = {
      plan: 'orch.run.busyPlan',
      run: 'orch.run.busyRun',
      durable: 'orch.run.busyTask',
    };
    const projected = projectOrchestrationActionState({
      busyTargets: [drawer, logTarget],
      controls: [plan, run, durable, input],
      status: state,
      label: stateLabel,
      statusText: translate(keys[actionName] || 'orch.run.busy'),
    }, action);
    if (abort) {
      abort.style.display = projected.pending && actionName === 'run'
        ? '' : 'none';
    }
    return projected.pending;
  };

  return {
    inputFor,
    isOpen: () => opened,
    open,
    close,
    clearLog,
    log,
    setActionState,
  };
}

(orchestrationRegistry as unknown as RunDrawerViewWindow).createOrchestrationRunDrawerView =
  createOrchestrationRunDrawerView;
