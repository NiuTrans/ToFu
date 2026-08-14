import { orchestrationRegistry } from './registry';
type Port = Record<string, unknown>;
export interface OrchestrationDefinitionSnapshotOptions {
  graph?: Port | null;
  workspace?: () => Port;
  stack?: () => unknown;
}
type DefinitionSnapshotWindow = Window & {
  createOrchestrationDefinitionSnapshotPort?:
    typeof createOrchestrationDefinitionSnapshotPort;
};

/** Canonical live definition reads for save, export, validation and runs. */
export function createOrchestrationDefinitionSnapshotPort(
  options: OrchestrationDefinitionSnapshotOptions = {},
) {
  const graph = options.graph;
  if (!graph || typeof graph.definitionFromState !== 'function'
      || typeof graph.rootSnapshot !== 'function') {
    throw new TypeError('definition snapshot port requires graph tools');
  }
  if (typeof options.workspace !== 'function'
      || typeof options.stack !== 'function') {
    throw new TypeError(
      'definition snapshot port requires workspace and stack readers');
  }
  const fromState = (name: unknown, nodes: unknown, edges: unknown): unknown =>
    (graph.definitionFromState as (...values: unknown[]) => unknown)(
      name, nodes, edges);
  const currentLevel = (): unknown => {
    const workspace = options.workspace?.() ?? {};
    return fromState(workspace.name, workspace.nodes, workspace.edges);
  };
  const root = (): unknown => {
    const workspace = options.workspace?.() ?? {};
    return (graph.rootSnapshot as (...values: unknown[]) => unknown)(
      workspace.name, workspace.nodes, workspace.edges, options.stack?.());
  };
  return Object.freeze({ fromState, currentLevel, root });
}

(orchestrationRegistry as unknown as DefinitionSnapshotWindow).createOrchestrationDefinitionSnapshotPort =
  createOrchestrationDefinitionSnapshotPort;
