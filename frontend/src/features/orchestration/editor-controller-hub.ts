import { orchestrationRegistry } from './registry';
import { createOrchestrationSelectionFocus } from './selection-focus';
import { createOrchestrationGraphActions } from './graph-actions';

type Port = Record<string, unknown>;
export interface OrchestrationEditorControllerHubOptions extends Port {
  editorState?: Port;
  document?: Document;
}
type EditorControllerHubWindow = Window & {
  createOrchestrationBreadcrumbView?: (options: Port) => Port;
  createOrchestrationNavigationController?: (options: Port) => Port;
  createOrchestrationEditorControllerHub?:
    typeof createOrchestrationEditorControllerHub;
};

/** Composition root for current-level graph actions and nested navigation. */
export function createOrchestrationEditorControllerHub(
  options: OrchestrationEditorControllerHubOptions = {},
) {
  const state = options.editorState ?? {};
  const reader = (name: string): (() => unknown) => {
    const value = state[name];
    return typeof value === 'function'
      ? (value as () => unknown).bind(state) : () => null;
  };
  const selectionFocus = createOrchestrationSelectionFocus({
    document: options.document,
    selectedNodeId: reader('selectedNodeId'),
    selectedEdgeId: reader('selectedEdgeId'),
  });
  const graphActions = createOrchestrationGraphActions({
    graph: options.graph,
    nodes: state.nodes,
    edges: state.edges,
    setGraph: state.setGraph,
    selectedNodeId: state.selectedNodeId,
    selectedEdgeId: state.selectedEdgeId,
    setSelection: state.setSelection,
    controls: options.controls,
    limitPolicy: options.limitPolicy,
    subflowDepth: () => {
      const frames = reader('stack')();
      return Array.isArray(frames) ? frames.length : 0;
    },
    nextId: state.nextId,
    defaultParams: options.defaultParams,
    isDragging: options.isDragging,
    markDirty: options.markDirty,
    render: options.render,
    renderNodes: options.renderNodes,
    renderEdges: options.renderEdges,
    renderInspector: options.renderInspector,
    focusSelection: selectionFocus.focus,
    translate: options.translate,
    toast: options.toast,
  });
  const bridge = orchestrationRegistry as unknown as EditorControllerHubWindow;
  if (!bridge.createOrchestrationBreadcrumbView
      || !bridge.createOrchestrationNavigationController) {
    throw new Error('Orchestration editor controller dependency unavailable');
  }
  const breadcrumb = bridge.createOrchestrationBreadcrumbView({
    document: options.document,
    graph: options.graph,
    fallbackName: options.fallbackName,
    nodeLabel: options.nodeLabel,
    translate: options.translate,
  });
  const navigation = bridge.createOrchestrationNavigationController({
    graph: options.graph,
    workspace: state.workspace,
    stack: state.stack,
    setStack: state.setStack,
    adopt: state.adoptWorkspace,
    blankGroupDefinition: options.blankGroupDefinition,
    fallbackName: options.fallbackName,
    breadcrumb,
    render: options.render,
    onNavigate: options.onNavigate,
    tidy: options.tidy,
  });
  return Object.freeze({ selectionFocus, graphActions, breadcrumb, navigation });
}

(orchestrationRegistry as unknown as EditorControllerHubWindow).createOrchestrationEditorControllerHub =
  createOrchestrationEditorControllerHub;
