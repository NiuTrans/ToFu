import { orchestrationRegistry } from './registry';
import { projectOrchestrationActionState, type ActionStateInput } from './action-state-view';
import { taskModeShellMarkup } from './task-mode-shell-template';
import { createOrchestrationDialogFocusManager } from './dialog';

interface DialogFocusManager {
  open(overlay: HTMLElement): unknown;
  close(overlay: HTMLElement): unknown;
  trapTab(event: KeyboardEvent | Record<string, unknown>, dialog: Element): unknown;
}
export interface TaskModeShellOptions {
  document?: Document;
  window?: Window;
  modalId?: string;
  translate?: (key: string, params?: unknown) => unknown;
  escape?: (value: unknown) => unknown;
  icon?: (name: string) => unknown;
  onOpenStudio?: () => unknown;
  onRefresh?: () => unknown;
  onPanelSelect?: (name: string | null) => unknown;
  onClosed?: () => unknown;
}
type TaskModeShellWindow = Window & {
  createTaskModeShell?: typeof createTaskModeShell;
};

/** Lazy modal mount, delegated actions and focus lifecycle. */
export function createTaskModeShell(options: TaskModeShellOptions = {}) {
  const doc = options.document ?? document;
  const win = options.window ?? window;
  let ready = false;
  let openGeneration = 0;
  const focusManager: DialogFocusManager =
    createOrchestrationDialogFocusManager({ document: doc, window: win });
  const translate = (key: string, params?: unknown): unknown =>
    options.translate ? options.translate(key, params) : key;
  const escape = (value: unknown): unknown => options.escape
    ? options.escape(value) : String(value == null ? '' : value);
  const icon = (name: string): unknown => options.icon ? options.icon(name) : '';
  const modal = (): HTMLElement | null =>
    doc.getElementById(options.modalId ?? 'taskModeModal');
  const isOpen = (): boolean => {
    const overlay = modal();
    return Boolean(overlay && overlay.style.display !== 'none');
  };
  const captureOpen = () => Object.freeze({ generation: openGeneration });
  const ownsOpen = (owner?: { generation?: unknown } | null): boolean =>
    isOpen() && Boolean(owner) && owner?.generation === openGeneration;

  const setActionState = (action: ActionStateInput): boolean => {
    const overlay = modal();
    if (!overlay) return false;
    const toolbar = overlay.querySelector<HTMLElement>('.tm-top-actions');
    const status = overlay.querySelector<HTMLElement>('[data-tm-top-state]');
    const label = overlay.querySelector<HTMLElement>('[data-tm-top-state-label]');
    const controls = overlay.querySelectorAll<HTMLButtonElement>(
      '[data-tm-action="refresh-runs"]');
    return projectOrchestrationActionState({
      busyTargets: [toolbar],
      controls,
      status,
      label,
      statusText: translate('tm.busy.refresh'),
    }, action).pending;
  };
  const close = (event?: Event | null): boolean => {
    const overlay = modal();
    if (!overlay || event && event.target !== overlay) return false;
    openGeneration += 1;
    focusManager.close(overlay);
    options.onClosed?.();
    return true;
  };
  const keyDown = (event: KeyboardEvent | Record<string, unknown>): void => {
    const overlay = modal();
    if (!overlay || overlay.style.display === 'none') return;
    const dialog = overlay.querySelector('[role="dialog"]');
    if (event.key === 'Tab' && dialog) {
      focusManager.trapTab(event, dialog);
      return;
    }
    if (event.key !== 'Escape') return;
    const preventDefault = event.preventDefault;
    if (typeof preventDefault === 'function') preventDefault.call(event);
    close();
  };
  const ensure = (): HTMLElement => {
    const existing = modal();
    if (ready && existing) return existing;
    const overlay = doc.createElement('div');
    overlay.className = 'tm-overlay';
    overlay.id = options.modalId ?? 'taskModeModal';
    overlay.style.display = 'none';
    overlay.innerHTML = taskModeShellMarkup({ translate, escape, icon });
    overlay.addEventListener('click', (event: MouseEvent) => {
      const target = event.target as Element | null;
      const panelControl = target?.closest?.('[data-tm-panel]') ?? null;
      if (panelControl && overlay.contains(panelControl)) {
        options.onPanelSelect?.(panelControl.getAttribute('data-tm-panel'));
        return;
      }
      const control = target?.closest?.('[data-tm-action]') ?? null;
      if (control && overlay.contains(control)) {
        const action = control.getAttribute('data-tm-action');
        if (action === 'open-studio') options.onOpenStudio?.();
        else if (action === 'refresh-runs') options.onRefresh?.();
        else if (action === 'close') close();
        return;
      }
      close(event);
    });
    overlay.addEventListener('keydown', keyDown as EventListener);
    doc.body.appendChild(overlay);
    ready = true;
    return overlay;
  };
  const open = (): HTMLElement => {
    const overlay = ensure();
    openGeneration += 1;
    focusManager.open(overlay);
    return overlay;
  };

  return {
    captureOpen,
    close,
    ensure,
    isOpen,
    isReady: () => ready,
    keyDown,
    open,
    ownsOpen,
    setActionState,
  };
}

(orchestrationRegistry as unknown as TaskModeShellWindow).createTaskModeShell = createTaskModeShell;
