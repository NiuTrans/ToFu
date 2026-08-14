import { orchestrationRegistry } from './registry';
type Port = Record<string, unknown>;
export interface OrchestrationGraphToolsOptions extends Port {
  schemaId?: unknown;
}
type GraphToolsWindow = Window & {
  createOrchestrationGraphTopology?: () => Port;
  createOrchestrationGraphWorkspace?: (options: Port) => Port;
  createOrchestrationGraphTools?: typeof createOrchestrationGraphTools;
};

/** Stable composition facade for topology edits and nested snapshots. */
export function createOrchestrationGraphTools(
  options: OrchestrationGraphToolsOptions = {},
) {
  const bridge = orchestrationRegistry as unknown as GraphToolsWindow;
  if (!bridge.createOrchestrationGraphTopology
      || !bridge.createOrchestrationGraphWorkspace) {
    throw new Error('Orchestration graph tools dependency unavailable');
  }
  const topology = bridge.createOrchestrationGraphTopology();
  const workspace = bridge.createOrchestrationGraphWorkspace({
    topology,
    schemaId: options.schemaId,
  });
  return {
    connections: topology.connections,
    findNode: topology.findNode,
    connect: topology.connect,
    deleteNode: topology.deleteNode,
    deleteEdge: topology.deleteEdge,
    reverseEdge: topology.reverseEdge,
    workspaceFromDefinitionResult: workspace.workspaceFromDefinitionResult,
    workspaceFromDefinition: workspace.workspaceFromDefinition,
    enterGroup: workspace.enterGroup,
    exitGroup: workspace.exitGroup,
    definitionFromState: workspace.definitionFromState,
    rootSnapshot: workspace.rootSnapshot,
  };
}

(orchestrationRegistry as unknown as GraphToolsWindow).createOrchestrationGraphTools =
  createOrchestrationGraphTools;
