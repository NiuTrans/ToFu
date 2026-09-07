import { orchestrationRegistry } from './registry';
import { createOrchestrationBoundedState } from './bounded-state';

export interface OrchestrationDisclosureStateOptions {
  maxEntries?: unknown;
}

export interface OrchestrationDisclosureBinding {
  selector?: string;
  attribute?: string;
}

export interface OrchestrationDisclosureState {
  bind(
    element: Element | null,
    owner: unknown,
    binding?: OrchestrationDisclosureBinding,
  ): number;
  reset(owner?: unknown): void;
}

type DisclosureStateWindow = Window & {
  createOrchestrationDisclosureState?:
    typeof createOrchestrationDisclosureState;
};

/** Bounded disclosure memory shared by the Studio and Task Mode inspectors. */
export function createOrchestrationDisclosureState(
  options: OrchestrationDisclosureStateOptions = {},
): OrchestrationDisclosureState {
  const values = createOrchestrationBoundedState<boolean>({
    maxEntries: options.maxEntries,
    fallbackMaxEntries: 256,
  });
  const bind = (
    element: Element | null,
    owner: unknown,
    binding: OrchestrationDisclosureBinding = {},
  ): number => {
    if (!element) return 0;
    const selector = binding.selector
      || 'details[data-orch-disclosure-key]';
    const attribute = binding.attribute || 'data-orch-disclosure-key';
    const prefix = `${String(owner == null ? '' : owner)}\u0000`;
    const sections = element.querySelectorAll<HTMLDetailsElement>(selector);
    sections.forEach((section) => {
      const key = prefix + (section.getAttribute(attribute) || '');
      if (values.has(key)) {
        section.open = Boolean(values.get(key));
      }
      section.addEventListener('toggle', () => values.set(key, section.open));
    });
    return sections.length;
  };
  const reset = (...owners: readonly unknown[]): void => {
    if (!owners.length) {
      values.clear();
      return;
    }
    const prefix = `${String(owners[0] == null ? '' : owners[0])}\u0000`;
    values.keys().forEach((key) => {
      if (key.startsWith(prefix)) values.remove(key);
    });
  };
  return { bind, reset };
}

(orchestrationRegistry as unknown as DisclosureStateWindow).createOrchestrationDisclosureState =
  createOrchestrationDisclosureState;
