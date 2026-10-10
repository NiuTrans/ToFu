import { orchestrationRegistry } from './registry';
import { createOrchestrationGraphSelectionActions } from './graph-selection-actions';

type Port = Record<string, unknown>;
type GraphActionsWindow = Window & {
  createOrchestrationGraphActionContext?: (options: Port) => Port;
  createOrchestrationGraphMutationActions?: (context: Port) => Port;
  createOrchestrationGraphActions?: typeof createOrchestrationGraphActions;
};

/** Stable Studio graph action facade over mutation and selection owners. */
export function createOrchestrationGraphActions(options: Port = {}) {
  const bridge = orchestrationRegistry as unknown as GraphActionsWindow;
  const contextFactory = bridge.createOrchestrationGraphActionContext;
  const mutationFactory = bridge.createOrchestrationGraphMutationActions;
  if (!contextFactory || !mutationFactory) {
    throw new Error('Orchestration graph action dependency unavailable');
  }
  const context = contextFactory(options);
  const mutations = mutationFactory(context);
  const selection = createOrchestrationGraphSelectionActions(
    context as Parameters<typeof createOrchestrationGraphSelectionActions>[0]);
  return Object.freeze({
    findNode: mutations.findNode,
    addNode: mutations.addNode,
    connectNodes: mutations.connectNodes,
    deleteNode: mutations.deleteNode,
    deleteEdge: mutations.deleteEdge,
    reverseEdge: mutations.reverseEdge,
    selectNode: selection.selectNode,
    selectEdge: selection.selectEdge,
    selectNodeForDrag: selection.selectNodeForDrag,
    clearSelection: selection.clearSelection,
    clearSelectedEdge: selection.clearSelectedEdge,
    nodeKeyDown: selection.nodeKeyDown,
  });
}

(orchestrationRegistry as unknown as GraphActionsWindow).createOrchestrationGraphActions =
  createOrchestrationGraphActions;
