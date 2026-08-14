import { orchestrationRegistry } from './registry';
interface PanelTarget extends Element {
  ownerDocument: Document;
}

export interface PanelStateOptions {
  document?: Document;
  focusTarget?: HTMLElement | null;
  trigger?: Element | null;
  openClass?: string;
  triggerExpanded?: boolean;
  triggerActiveClass?: string;
}

type PanelStateWindow = Window & {
  setOrchestrationPanelState?: typeof setOrchestrationPanelState;
  focusOrchestrationPanel?: typeof focusOrchestrationPanel;
  createOrchestrationPanelFocusReturn?:
    typeof createOrchestrationPanelFocusReturn;
};

/** One accessible expanded/collapsed projection for every Studio panel. */
export function setOrchestrationPanelState(
  panel: PanelTarget | null,
  expandedValue: unknown,
  options: PanelStateOptions = {},
): boolean {
  const expanded = Boolean(expandedValue);
  if (!panel) return expanded;
  const doc = options.document ?? panel.ownerDocument
    ?? (typeof document !== 'undefined' ? document : null);
  const focusTarget = options.focusTarget
    ?? options.trigger as HTMLElement | null ?? null;
  const active = doc?.activeElement;
  const restoreFocus = !expanded && focusTarget && active
    && typeof panel.contains === 'function' && panel.contains(active);
  if (restoreFocus && typeof focusTarget.focus === 'function') {
    focusTarget.focus();
  }
  if (options.openClass && panel.classList) {
    panel.classList.toggle(options.openClass, expanded);
  }
  panel.setAttribute('aria-hidden', expanded ? 'false' : 'true');
  if (expanded) panel.removeAttribute('inert');
  else panel.setAttribute('inert', '');
  const trigger = options.trigger;
  if (trigger) {
    const triggerExpanded = typeof options.triggerExpanded === 'boolean'
      ? options.triggerExpanded : expanded;
    trigger.setAttribute(
      'aria-expanded', triggerExpanded ? 'true' : 'false');
    if (options.triggerActiveClass && trigger.classList) {
      trigger.classList.toggle(options.triggerActiveClass, expanded);
    }
  }
  return expanded;
}

export function focusOrchestrationPanel(
  panel: Element | null,
  preferredSelector?: string,
): boolean {
  if (!panel) return false;
  let target = preferredSelector
    ? panel.querySelector(preferredSelector) : null;
  target ??= panel.querySelector(
    'button:not([disabled]),input:not([disabled]),select:not([disabled]),'
      + 'textarea:not([disabled]),[tabindex]:not([tabindex="-1"])');
  const focusable = target as (Element & { focus?: () => void }) | null;
  if (!focusable || typeof focusable.focus !== 'function') return false;
  focusable.focus();
  return true;
}

export function createOrchestrationPanelFocusReturn() {
  let target: Element | null = null;
  let eligible = false;
  const capture = (doc: Document): Element | null => {
    target ??= doc.activeElement;
    return target;
  };
  const prepare = (doc: Document, panel: Element | null): boolean => {
    eligible = Boolean(panel && target
      && (!doc.contains || doc.contains(target))
      && panel.contains(doc.activeElement));
    return eligible;
  };
  const restore = (doc: Document): boolean => {
    const focusTarget = target as HTMLElement | null;
    const shouldRestore = eligible;
    target = null;
    eligible = false;
    if (!shouldRestore || !focusTarget
        || (doc.contains && !doc.contains(focusTarget))
        || focusTarget.closest?.('[inert],[aria-hidden="true"]')
        || typeof focusTarget.focus !== 'function') return false;
    if (doc.activeElement !== focusTarget) focusTarget.focus();
    return true;
  };
  const clear = (): void => { target = null; eligible = false; };
  return Object.freeze({ capture, prepare, restore, clear });
}

Object.assign(orchestrationRegistry as unknown as PanelStateWindow, {
  setOrchestrationPanelState,
  focusOrchestrationPanel,
  createOrchestrationPanelFocusReturn,
});
