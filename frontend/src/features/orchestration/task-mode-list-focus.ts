import { orchestrationRegistry } from './registry';
interface RovingPort { sync(preferred?: Element | null): unknown }

export interface TaskModeListFocusOptions { document?: Document }

type TaskModeListFocusWindow = Window & {
  createOrchestrationRovingItemsController?: (options: {
    root: Element;
    selector: string;
  }) => RovingPort;
  createTaskModeListFocusController?: typeof createTaskModeListFocusController;
};

export function createTaskModeListFocusController(
  options: TaskModeListFocusOptions = {},
) {
  let renderedIds: unknown[] = [];
  const doc = (): Document => options.document ?? document;
  const capture = (list: Element | null): unknown => {
    const active = doc().activeElement;
    if (!active || !list || !list.contains(active)) return null;
    const index = Number(active.getAttribute('data-tm-run-index'));
    return Number.isInteger(index) ? renderedIds[index] ?? null : null;
  };
  const restore = (
    list: Element,
    ids: unknown[],
    focusedId: unknown,
    activeButton?: Element | null,
  ): Element | null => {
    renderedIds = ids.slice();
    const index = renderedIds.indexOf(focusedId);
    const focusedButton = index < 0 ? null : list.querySelector(
      `[data-tm-run-index="${index}"]`);
    const factory = (orchestrationRegistry as unknown as TaskModeListFocusWindow)
      .createOrchestrationRovingItemsController;
    if (!factory) throw new Error('Task Mode list requires roving-items owner');
    factory({ root: list, selector: '[data-tm-run-index]' }).sync(
      focusedButton || activeButton || null);
    const focusable = focusedButton as HTMLElement | null;
    focusable?.focus({ preventScroll: true });
    return focusedButton;
  };
  const clear = (): void => { renderedIds = []; };
  return { capture, clear, restore };
}

(orchestrationRegistry as unknown as TaskModeListFocusWindow).createTaskModeListFocusController =
  createTaskModeListFocusController;
