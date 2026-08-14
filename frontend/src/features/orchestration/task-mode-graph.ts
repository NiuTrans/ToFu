import { orchestrationRegistry } from './registry';
import {
  createTaskModeGraphProjection,
  type TaskModeGraphProjectionOptions,
  type TaskModeGraphProjectionResult,
  type TaskModeGraphState,
} from './task-mode-graph-projection';
import type { OrchestrationNode } from './node-summary';

interface RovingController {
  sync(preferred?: Element | null): unknown;
}

export interface TaskModeGraphViewOptions extends TaskModeGraphProjectionOptions {
  document?: Document;
  hostId?: string;
  sectionId?: string;
  projection?: {
    project(state: TaskModeGraphState): TaskModeGraphProjectionResult | null;
  };
  bindImageFallbacks?: (root: Element) => unknown;
  nodeAccent?: (node: OrchestrationNode) => unknown;
  onSelect?: (nodeId: unknown) => unknown;
}

type TaskModeGraphWindow = Window & {
  createOrchestrationRovingItemsController?: (options: {
    root: Element;
    selector: string;
  }) => RovingController;
  createTaskModeGraphView?: typeof createTaskModeGraphView;
};

export function createTaskModeGraphView(options: TaskModeGraphViewOptions = {}) {
  let renderedNodeIds: unknown[] = [];
  let lastActiveNode: unknown = null;
  let locateControl: Element | null = null;
  const projection = options.projection ?? createTaskModeGraphProjection(options);
  const doc = (): Document => options.document ?? document;
  const host = (): HTMLElement | null => doc().getElementById(options.hostId
    || 'tmGraph');
  const revealActive = (): boolean => {
    const active = host()?.querySelector<HTMLElement>('.tm-gnode.is-active');
    if (!active || typeof active.scrollIntoView !== 'function') return false;
    active.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    return true;
  };
  const syncLocateControl = (enabled: boolean): void => {
    const control = doc().querySelector<HTMLButtonElement>(
      '[data-tm-graph-action="reveal-active"]');
    if (!control) return;
    control.disabled = !enabled;
    if (control === locateControl) return;
    locateControl = control;
    control.addEventListener('click', revealActive);
  };
  const clear = (): { nodes: number; width: number; height: number } => {
    const surface = host();
    const section = doc().getElementById(options.sectionId || 'tmGraphSection');
    if (!surface) return { nodes: 0, width: 0, height: 0 };
    if (section) section.style.display = 'none';
    else surface.style.display = 'none';
    renderedNodeIds = [];
    lastActiveNode = null;
    surface.innerHTML = '';
    syncLocateControl(false);
    return { nodes: 0, width: 0, height: 0 };
  };
  const render = (
    stateValue: TaskModeGraphState = {},
  ): { nodes: number; width: number; height: number } | null => {
    const state = stateValue ?? {};
    const surface = host();
    if (!surface) return null;
    let focusedNodeId: unknown = null;
    const focused = doc().activeElement;
    if (focused && surface.contains(focused)
        && typeof focused.getAttribute === 'function') {
      const focusedIndex = Number(focused.getAttribute('data-tm-node-index'));
      if (Number.isInteger(focusedIndex)) {
        focusedNodeId = renderedNodeIds[focusedIndex] ?? null;
      }
    }
    const projected = projection.project(state);
    if (!projected) return clear();
    const section = doc().getElementById(options.sectionId || 'tmGraphSection');
    if (section) section.style.display = '';
    surface.style.display = '';
    surface.innerHTML = projected.html;
    renderedNodeIds = projected.nodeIds;
    options.bindImageFallbacks?.(surface);
    const cards = Array.from(surface.querySelectorAll<HTMLElement>(
      '[data-tm-node-index]'));
    cards.forEach((card) => {
      const node = projected.nodes[Number(card.getAttribute('data-tm-node-index'))];
      if (!node) return;
      if (typeof options.nodeAccent === 'function') {
        card.style.setProperty('--tm-accent', String(options.nodeAccent(node)));
      }
      const select = (): void => { options.onSelect?.(node.id); };
      card.addEventListener('click', select);
      card.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault();
        select();
      });
    });
    const rovingFactory = (orchestrationRegistry as unknown as TaskModeGraphWindow)
      .createOrchestrationRovingItemsController;
    if (!rovingFactory) {
      throw new Error('Task Mode graph requires the shared roving-items owner');
    }
    const keyboard = rovingFactory({
      root: surface,
      selector: '[data-tm-node-index]',
    });
    const focusedCardIndex = renderedNodeIds.indexOf(focusedNodeId);
    const focusedCard = focusedCardIndex < 0 ? null
      : surface.querySelector<HTMLElement>(
        `[data-tm-node-index="${focusedCardIndex}"]`);
    keyboard.sync(focusedCard
      || surface.querySelector('.tm-gnode.is-selected')
      || surface.querySelector('.tm-gnode.is-active'));
    focusedCard?.focus({ preventScroll: true });
    const activeChanged = Boolean(state.activeNode)
      && state.activeNode !== lastActiveNode;
    lastActiveNode = state.activeNode || null;
    const activeCard = surface.querySelector('.tm-gnode.is-active');
    syncLocateControl(Boolean(activeCard));
    if (activeChanged && !focusedNodeId) revealActive();
    return {
      nodes: projected.nodes.length,
      width: projected.width,
      height: projected.height,
    };
  };
  return Object.freeze({ render, clear, revealActive });
}

(orchestrationRegistry as unknown as TaskModeGraphWindow).createTaskModeGraphView = createTaskModeGraphView;
