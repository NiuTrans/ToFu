import { orchestrationRegistry } from './registry';
export interface OrchestrationSelectionFocusOptions {
  document?: Document;
  selectedNodeId?: () => unknown;
  selectedEdgeId?: () => unknown;
}
type SelectionFocusWindow = Window & {
  createOrchestrationSelectionFocus?: typeof createOrchestrationSelectionFocus;
};

/** Focus projection for the currently selected Canvas subject. */
export function createOrchestrationSelectionFocus(
  options: OrchestrationSelectionFocusOptions = {},
) {
  const doc = options.document ?? document;
  const selectedNodeId = (): unknown => options.selectedNodeId?.() ?? null;
  const selectedEdgeId = (): unknown => options.selectedEdgeId?.() ?? null;
  const target = (): HTMLElement | SVGElement | null => {
    const nodeId = selectedNodeId();
    const card = nodeId ? doc.getElementById(`orch-node-${String(nodeId)}`) : null;
    if (card) {
      return card.querySelector<HTMLElement>('.orch-node-select') || card;
    }
    if (selectedEdgeId()) {
      const edge = doc.getElementById('orchEdges')
        ?.querySelector<SVGElement>('.orch-edge-path.is-selected');
      if (edge) return edge;
    }
    return doc.getElementById('orchCanvas');
  };
  const focus = (): boolean => {
    const element = target();
    if (!element || typeof element.focus !== 'function') return false;
    try { element.focus({ preventScroll: true }); }
    catch { element.focus(); }
    return true;
  };
  return { focus };
}

(orchestrationRegistry as unknown as SelectionFocusWindow).createOrchestrationSelectionFocus =
  createOrchestrationSelectionFocus;
