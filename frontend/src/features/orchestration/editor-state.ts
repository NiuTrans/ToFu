import { orchestrationRegistry } from './registry';
type Port = Record<string, unknown>;
export interface OrchestrationEditorStateOptions {
  nodes?: unknown;
  edges?: unknown;
  selectedNodeId?: unknown;
  selectedEdgeId?: unknown;
  sequence?: unknown;
  name?: unknown;
  stack?: unknown;
}
type EditorStateWindow = Window & {
  createOrchestrationEditorState?: typeof createOrchestrationEditorState;
};

/** Current-level graph, selection, sequence and nested-stack state owner. */
export function createOrchestrationEditorState(
  options: OrchestrationEditorStateOptions = {},
) {
  const state: {
    nodes: unknown[];
    edges: unknown[];
    selectedNodeId: unknown;
    selectedEdgeId: unknown;
    sequence: number;
    name: unknown;
    stack: unknown[];
  } = {
    nodes: Array.isArray(options.nodes) ? options.nodes : [],
    edges: Array.isArray(options.edges) ? options.edges : [],
    selectedNodeId: options.selectedNodeId == null
      ? null : options.selectedNodeId,
    selectedEdgeId: options.selectedEdgeId == null
      ? null : options.selectedEdgeId,
    sequence: Number(options.sequence || 0),
    name: options.name == null ? 'Untitled Flow' : options.name,
    stack: Array.isArray(options.stack) ? options.stack : [],
  };
  const nodes = () => state.nodes;
  const edges = () => state.edges;
  const selectedNodeId = () => state.selectedNodeId;
  const selectedEdgeId = () => state.selectedEdgeId;
  const sequence = () => state.sequence;
  const name = () => state.name;
  const stack = () => state.stack;
  const setNodes = (value: unknown): unknown[] =>
    (state.nodes = Array.isArray(value) ? value : []);
  const setEdges = (value: unknown): unknown[] =>
    (state.edges = Array.isArray(value) ? value : []);
  const setSelectedNodeId = (value: unknown): unknown =>
    (state.selectedNodeId = value == null ? null : value);
  const setSelectedEdgeId = (value: unknown): unknown =>
    (state.selectedEdgeId = value == null ? null : value);
  const setSequence = (value: unknown): number => {
    const parsed = Number(value);
    state.sequence = Number.isFinite(parsed) ? parsed : 0;
    return state.sequence;
  };
  const setName = (value: unknown): unknown =>
    (state.name = value == null ? 'Untitled Flow' : value);
  const setStack = (value: unknown): unknown[] =>
    (state.stack = Array.isArray(value) ? value : []);
  const setGraph = (nextNodes: unknown, nextEdges: unknown) => {
    setNodes(nextNodes);
    setEdges(nextEdges);
    return { nodes: state.nodes, edges: state.edges };
  };
  const setSelection = (nodeId: unknown, edgeId: unknown) => {
    setSelectedNodeId(nodeId);
    setSelectedEdgeId(edgeId);
    return { nodeId: state.selectedNodeId, edgeId: state.selectedEdgeId };
  };
  const workspace = () => ({
    name: state.name,
    nodes: state.nodes,
    edges: state.edges,
    selected: state.selectedNodeId,
    sequence: state.sequence,
  });
  const adoptWorkspace = (value: Port = {}) => {
    setName(value.name);
    setGraph(value.nodes, value.edges);
    setSelectedNodeId(value.selected);
    setSelectedEdgeId(null);
    setSequence(value.sequence);
    return workspace();
  };
  const workspaceToken = (): string => state.stack.map((frame) =>
    frame && typeof frame === 'object' ? (frame as Port).groupId || '' : '',
  ).filter(Boolean).join('/');
  const findNode = (id: unknown): unknown => state.nodes.find((node) =>
    node && typeof node === 'object' && (node as Port).id === id) ?? null;
  const nextId = (prefix: unknown): string => {
    state.sequence += 1;
    return String(prefix || 'n') + state.sequence;
  };
  const applyPositions = (positionsValue: unknown): unknown[] => {
    const positions = positionsValue && typeof positionsValue === 'object'
      ? positionsValue as Port : {};
    state.nodes.forEach((nodeValue) => {
      if (!nodeValue || typeof nodeValue !== 'object') return;
      const node = nodeValue as Port;
      const position = node.id != null ? positions[String(node.id)] : null;
      if (position && typeof position === 'object') {
        node.x = (position as Port).x;
        node.y = (position as Port).y;
      }
    });
    return state.nodes;
  };
  const installLegacyGlobals = (target: object | null | undefined): boolean => {
    if (!target || typeof Object.defineProperty !== 'function') return false;
    const aliases: Record<string, { get: () => unknown; set: (value: unknown) => unknown }> = {
      _orchNodes: { get: nodes, set: setNodes },
      _orchEdges: { get: edges, set: setEdges },
      _orchSel: { get: selectedNodeId, set: setSelectedNodeId },
      _orchSelEdge: { get: selectedEdgeId, set: setSelectedEdgeId },
      _orchSeq: { get: sequence, set: setSequence },
      _orchName: { get: name, set: setName },
      _orchStack: { get: stack, set: setStack },
    };
    Object.entries(aliases).forEach(([key, alias]) => {
      Object.defineProperty(target, key, {
        configurable: true,
        enumerable: true,
        get: alias.get,
        set: alias.set,
      });
    });
    return true;
  };
  return {
    nodes, edges, selectedNodeId, selectedEdgeId, sequence, name, stack,
    setNodes, setEdges, setSelectedNodeId, setSelectedEdgeId,
    setSequence, setName, setStack, setGraph, setSelection,
    workspace, adoptWorkspace, workspaceToken,
    nodeCount: () => state.nodes.length,
    hasNodes: () => state.nodes.length > 0,
    findNode, nextId, applyPositions, installLegacyGlobals,
  };
}

(orchestrationRegistry as unknown as EditorStateWindow).createOrchestrationEditorState =
  createOrchestrationEditorState;
