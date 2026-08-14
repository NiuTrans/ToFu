import { orchestrationRegistry } from './registry';
type Port = Record<string, unknown>;
export interface OrchestrationGraphSelectionContext extends Port {
  render(name: string): unknown;
  isDragging(): unknown;
  setSelection(nodeId: unknown, edgeId: unknown): unknown;
  selectedNodeId(): unknown;
  focusSelection(): unknown;
}
type GraphSelectionWindow = Window & {
  createOrchestrationGraphSelectionActions?:
    typeof createOrchestrationGraphSelectionActions;
};

/** Mutually exclusive graph selection commands. */
export function createOrchestrationGraphSelectionActions(
  context: OrchestrationGraphSelectionContext,
) {
  const renderSelection = (): void => {
    context.render('renderNodes');
    context.render('renderEdges');
    context.render('renderInspector');
  };
  const selectNode = (id: unknown): boolean => {
    if (context.isDragging()) return false;
    context.setSelection(id, null);
    renderSelection();
    return true;
  };
  const selectEdge = (id: unknown): boolean => {
    context.setSelection(null, id);
    renderSelection();
    return true;
  };
  const selectNodeForDrag = (id: unknown): boolean => {
    context.setSelection(id, null);
    return true;
  };
  const clearSelection = (): boolean => {
    context.setSelection(null, null);
    renderSelection();
    return true;
  };
  const clearSelectedEdge = (): boolean => {
    context.setSelection(context.selectedNodeId(), null);
    return true;
  };
  const nodeKeyDown = (
    event: KeyboardEvent | Record<string, unknown>, id: unknown,
  ): boolean => {
    if (event.target !== event.currentTarget) return false;
    if (event.key !== 'Enter' && event.key !== ' ') return false;
    const preventDefault = event.preventDefault;
    if (typeof preventDefault === 'function') preventDefault.call(event);
    if (!selectNode(id)) return false;
    context.focusSelection();
    return true;
  };
  return Object.freeze({
    selectNode,
    selectEdge,
    selectNodeForDrag,
    clearSelection,
    clearSelectedEdge,
    nodeKeyDown,
  });
}

(orchestrationRegistry as unknown as GraphSelectionWindow).createOrchestrationGraphSelectionActions =
  createOrchestrationGraphSelectionActions;
