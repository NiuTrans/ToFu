import { orchestrationRegistry } from './registry';
export interface ActionStateInput {
  pending?: unknown;
  name?: unknown;
}

export interface ProjectedActionState {
  readonly pending: boolean;
  readonly name: string;
}

interface BusyTarget {
  setAttribute(name: string, value: string): void;
}

interface DisableTarget {
  disabled: boolean;
}

interface TextTarget {
  textContent: string | null;
}

interface VisibilityTarget {
  hidden: boolean;
}

export interface ActionStateViewOptions {
  busyTargets?: readonly (BusyTarget | null | undefined)[];
  controls?: ArrayLike<DisableTarget | null | undefined>;
  label?: TextTarget | null;
  status?: VisibilityTarget | null;
  statusText?: unknown;
}

type ActionStateWindow = Window & {
  projectOrchestrationActionState?: typeof projectOrchestrationActionState;
};

/** Atomically project an action-lock snapshot onto shared Studio/Task UI. */
export function projectOrchestrationActionState(
  options: ActionStateViewOptions = {},
  action: ActionStateInput | null | undefined = {},
): ProjectedActionState {
  const state = Object.freeze({
    pending: Boolean(action?.pending),
    name: String(action?.name || ''),
  });
  const busyTargets = Array.isArray(options.busyTargets)
    ? options.busyTargets : [];
  for (const target of busyTargets) {
    target?.setAttribute('aria-busy', state.pending ? 'true' : 'false');
  }
  for (const control of Array.from(options.controls ?? [])) {
    if (control) control.disabled = state.pending;
  }
  if (options.label) {
    options.label.textContent = state.pending
      ? String(options.statusText || '') : '';
  }
  if (options.status) options.status.hidden = !state.pending;
  return state;
}

(orchestrationRegistry as unknown as ActionStateWindow).projectOrchestrationActionState =
  projectOrchestrationActionState;
