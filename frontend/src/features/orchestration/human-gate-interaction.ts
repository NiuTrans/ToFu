import { orchestrationRegistry } from './registry';
import { projectOrchestrationActionState } from './action-state-view';

interface GateRoot extends Element {
  querySelectorAll(selectors: string): NodeListOf<Element>;
}

export interface HumanGateInteractionOptions {
  root?: GateRoot | null | (() => GateRoot | null);
  translate?: (key: string) => string;
}

type HumanGateInteractionWindow = Window & {
  createOrchestrationHumanGateInteraction?:
    typeof createOrchestrationHumanGateInteraction;
};

/** Pending projection and removable click/Enter bindings for human gates. */
export function createOrchestrationHumanGateInteraction(
  options: HumanGateInteractionOptions = {},
) {
  let pending = false;
  const root = (): GateRoot | null => typeof options.root === 'function'
    ? options.root() : options.root ?? null;
  const translate = (key: string): string => options.translate
    ? options.translate(key) : key;

  const setBusy = (next: unknown): boolean => {
    const target = root();
    if (!target || (next && pending)) return false;
    pending = Boolean(next);
    projectOrchestrationActionState({
      busyTargets: [target],
      controls: target.querySelectorAll(
        'button,input,textarea') as unknown as ArrayLike<{ disabled: boolean }>,
      status: target.querySelector('[data-orch-gate-state]') as
        (Element & { hidden: boolean }) | null,
      label: target.querySelector('[data-orch-gate-state-label]'),
      statusText: translate('orch.gate.busy'),
    }, { pending, name: 'gate' });
    return true;
  };

  const run = <T>(
    callback?: (() => T | PromiseLike<T>) | null,
  ): false | Promise<T> => {
    if (typeof callback !== 'function' || !setBusy(true)) return false;
    let result: T | PromiseLike<T>;
    try {
      result = callback();
    } catch (error: unknown) {
      result = Promise.reject(error);
    }
    return Promise.resolve(result).then(
      (value) => {
        setBusy(false);
        return value;
      },
      (error: unknown) => {
        setBusy(false);
        throw error;
      },
    );
  };

  const bindClick = (
    control: EventTarget | null | undefined,
    callback?: (() => unknown) | null,
  ): (() => void) => {
    if (!control || typeof callback !== 'function') return () => {};
    const onClick = (): void => { callback(); };
    control.addEventListener('click', onClick);
    return () => { control.removeEventListener('click', onClick); };
  };

  const bindSubmit = (
    input: EventTarget | null | undefined,
    control: EventTarget | null | undefined,
    callback?: (() => unknown) | null,
  ): (() => void) => {
    const unbindClick = bindClick(control, callback);
    if (!input || typeof callback !== 'function') return unbindClick;
    const onKeyDown = (event: Event): void => {
      const keyboard = event as KeyboardEvent;
      if (keyboard.key !== 'Enter' || keyboard.shiftKey) return;
      event.preventDefault();
      callback();
    };
    input.addEventListener('keydown', onKeyDown);
    return () => {
      unbindClick();
      input.removeEventListener('keydown', onKeyDown);
    };
  };

  return Object.freeze({
    setBusy,
    run,
    bindClick,
    bindSubmit,
    isPending: () => pending,
  });
}

(orchestrationRegistry as unknown as HumanGateInteractionWindow)
  .createOrchestrationHumanGateInteraction =
    createOrchestrationHumanGateInteraction;
