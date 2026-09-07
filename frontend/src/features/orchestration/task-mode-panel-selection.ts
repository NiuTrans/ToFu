import { orchestrationRegistry } from './registry';
export interface TaskModePanelSelectionOptions {
  names?: readonly unknown[];
  initial?: unknown;
}

export interface TaskModePanelSelectionSnapshot {
  active: string;
  preferred: string;
  owner: string | null;
  presented: string | null;
}

type TaskModePanelSelectionWindow = Window & {
  createTaskModePanelSelection?: typeof createTaskModePanelSelection;
};

/** DOM-free user preference plus one owner-scoped transient presentation. */
export function createTaskModePanelSelection(
  options: TaskModePanelSelectionOptions = {},
) {
  const defaults = ['runs', 'run', 'inspector'];
  let names = Array.isArray(options.names)
    ? options.names.map(String).filter(
      (name, index, values) => Boolean(name) && values.indexOf(name) === index)
    : defaults.slice();
  if (!names.length) names = defaults.slice();
  let preferred = names.includes(String(options.initial ?? ''))
    ? String(options.initial) : names[0];
  let transient: { owner: string; panel: string } | null = null;
  const valid = (name: unknown): boolean => names.includes(String(name || ''));
  const active = (): string => transient?.panel ?? preferred;
  const snapshot = (): Readonly<TaskModePanelSelectionSnapshot> =>
    Object.freeze({
      active: active(),
      preferred,
      owner: transient?.owner ?? null,
      presented: transient?.panel ?? null,
    });
  const select = (name: unknown): string => {
    if (!valid(name)) return active();
    preferred = String(name);
    transient = null;
    return active();
  };
  const present = (name: unknown, owner: unknown): string => {
    const key = String(owner || '');
    if (!valid(name) || !key) return active();
    if (transient && transient.owner !== key) return active();
    transient = { owner: key, panel: String(name) };
    return active();
  };
  const release = (owner: unknown, fallback?: unknown): string => {
    if (!transient || transient.owner !== String(owner || '')) return active();
    transient = null;
    if (!valid(preferred)) {
      preferred = valid(fallback) ? String(fallback) : names[0];
    }
    return active();
  };
  return Object.freeze({ active, present, release, select, snapshot });
}

(orchestrationRegistry as unknown as TaskModePanelSelectionWindow).createTaskModePanelSelection =
  createTaskModePanelSelection;
