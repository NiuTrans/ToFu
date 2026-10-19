/** Bounded project execution graph projection. Assignment DTOs own execution,
 * dependency and delivery facts; Board records are a separate fallback, never
 * joined by conversation or ordered as a fabricated execution timeline.
 * Entry points: projectCoordinationGraph, layoutCoordinationGraph.
 */
import type { ProjectDispatchDependency, ProjectDispatchDependencyState, ProjectDispatchView } from '../api/project-dispatch.generated';
import type { CoordinationWork } from './project-coordination';

import type { CoordinationReference } from './project-coordination-reference';

export interface CoordinationGraphOptions {
  assignments?: readonly ProjectDispatchView[] | null;
  coordinatorId?: string;
  /** Conversation currently open, used for navigation context only. */
  activeConversationId?: string;
}
export type CoordinationGraphStatus = 'queued' | 'waiting' | 'running' | 'completed' | 'failed' | 'cancelled' | 'blocked' | 'unknown';
export interface CoordinationGraphNode {
  id: string;
  kind: 'task' | 'coordinator' | 'reference';
  title: string;
  conversationId?: string;
  status?: CoordinationGraphStatus;
  summary?: string;
  revoked?: boolean;
  delivered?: boolean;
  dependencyUnknown?: boolean;

  reference?: CoordinationReference;
}
export interface CoordinationGraphEdge {
  from: string;
  to: string;
  kind: 'dispatch' | 'dependency' | 'rework';
  status?: ProjectDispatchDependencyState['status'];
  reference?: ProjectDispatchDependency;
}
export interface CoordinationGraph {
  nodes: CoordinationGraphNode[];
  edges: CoordinationGraphEdge[];
  source: 'assignments' | 'work';
  assignmentsUnavailable: boolean;
  truncated: boolean;
  total: number | null;
  completed: number;
  delivered: number | null;
  counts: Record<CoordinationGraphStatus, number>;
}
export const COORDINATION_GRAPH_LIMITS = { assignments: 128, work: 200, references: 32, coordinators: 8, dependencies: 8, ranks: 8, columns: 3 } as const;
const excerpt = (value: string | undefined, limit: number) => value && value.length > limit ? `${value.slice(0, limit - 1)}…` : value || '';
/** Strip machine attachment references that can lead a captured title so the
 * list shows a readable line instead of raw `[image ref: /api/...]` markup. */
const cleanTitle = (value: string | undefined, limit: number) => {
  const stripped = (value || '')
    .replace(/\[(image|file|video|audio|attachment)[^\]]*\][^\s\]]*/gi, ' ')
    .replace(/\[image ref:[^\]]*\]/gi, ' ')
    .replace(/\s+/g, ' ').trim();
  return excerpt(stripped || value, limit);
};
const assignmentKey = (id: string) => `assignment:${id}`;
const coordinatorKey = (id: string) => `coordinator:${id}`;

function dependencyReferences(item: ProjectDispatchView): ProjectDispatchDependency[] {
  const references = new Map<string, ProjectDispatchDependency>();
  for (const ref of [...(item.request.dependencies || []).slice(0, 8), ...(item.dependencyStates || []).slice(0, 8).map(state => state.reference)]) {
    if (!references.has(ref.assignmentId)) references.set(ref.assignmentId, ref);
  }
  return [...references.values()].slice(0, COORDINATION_GRAPH_LIMITS.dependencies);
}
function dependencyState(item: ProjectDispatchView, reference: ProjectDispatchDependency) {
  return item.dependencyStates?.slice(0, COORDINATION_GRAPH_LIMITS.dependencies).find(state =>
    state.reference.assignmentId === reference.assignmentId && state.reference.grantRevision === reference.grantRevision
    && state.reference.resultRevision === reference.resultRevision && state.reference.checkpointSha === reference.checkpointSha)?.status;
}
function assignmentStatus(item: ProjectDispatchView): CoordinationGraphStatus {
  // Execution evidence outranks later revocation or changing prerequisites. A
  // withdrawn grant does not prove that an already running attempt has stopped.
  switch (item.execution?.status) {
    case 'pending': case 'queued': return 'queued';
    case 'running': return 'running';
    case 'completed': return 'completed';
    case 'failed': return 'failed';
    case 'cancelled': case 'aborted': return 'cancelled';
    case 'blocked': return 'blocked';
  }
  if (item.execution) return 'unknown';
  if (item.dispatch === 'aborted') return 'cancelled';
  // A null projection can also mean unavailable attempt evidence, not no birth.
  if (item.revoked) return 'unknown';
  const states = dependencyReferences(item).map(reference => dependencyState(item, reference));
  if (states.some(state => state === 'waiting_review' || state === 'waiting_delivery' || state === 'invalid')) return 'blocked';
  if (states.some(state => !state || state === 'unknown')) return 'unknown';
  // A deferred dispatch has not started. Acceptance without attempt evidence
  // must not be advertised as an execution queue or a running worker.
  return item.dispatch === 'deferred' ? 'waiting' : 'unknown';
}

export function projectCoordinationGraph(
  groups: { active: readonly CoordinationWork[]; recent: readonly CoordinationWork[] },
  options: CoordinationGraphOptions = {},
): CoordinationGraph {
  const graph: CoordinationGraph = {
    nodes: [], edges: [], source: options.assignments?.length ? 'assignments' : 'work',
    assignmentsUnavailable: options.assignments === null, truncated: false,
    total: 0, completed: 0, delivered: options.assignments === undefined || options.assignments === null ? null : 0,
    counts: { queued: 0, waiting: 0, running: 0, completed: 0, failed: 0, cancelled: 0, blocked: 0, unknown: 0 },
  };
  const nodes = new Map<string, CoordinationGraphNode>();
  const coordinatorIds = new Set<string>();
  if (options.coordinatorId) coordinatorIds.add(options.coordinatorId);
  const addCoordinator = (id: string) => {
    if (!id || nodes.has(coordinatorKey(id))) return;
    if ([...nodes.values()].filter(node => node.kind === 'coordinator').length >= COORDINATION_GRAPH_LIMITS.coordinators) {
      graph.truncated = true; return;
    }
    nodes.set(coordinatorKey(id), { id: coordinatorKey(id), kind: 'coordinator', title: '', conversationId: id });
  };
  if (options.coordinatorId) addCoordinator(options.coordinatorId);
  if (graph.source === 'assignments') {
    const assignments = options.assignments!.slice(0, COORDINATION_GRAPH_LIMITS.assignments);
    graph.truncated = options.assignments!.length > assignments.length;
    for (const item of assignments) coordinatorIds.add(item.request.coordinatorId);
    const visible: ProjectDispatchView[] = [];
    for (const item of assignments) {
      if (coordinatorIds.has(item.request.recipientId) || nodes.has(assignmentKey(item.assignmentId))) continue;
      visible.push(item);
      const references = dependencyReferences(item);
      const delivered = item.canonicalDelivery === true && item.delivery?.status === 'delivered';
      nodes.set(assignmentKey(item.assignmentId), {
        id: assignmentKey(item.assignmentId), kind: 'task', title: cleanTitle(item.request.brief || item.assignmentId, 240),
        conversationId: item.request.recipientId || undefined, status: assignmentStatus(item), revoked: item.revoked,

        reference: { conversationId: item.request.recipientId, assignmentId: item.assignmentId,
          grantRevision: item.grantRevision, resultRevision: item.execution?.resultRevision },
        summary: excerpt(typeof item.execution?.result?.content === 'string' ? item.execution.result.content : '', 320),
        delivered: (item.request.profile?.mode === 'files' || item.request.profile?.mode === 'project') || delivered ? delivered : undefined,
        dependencyUnknown: references.some(reference => !dependencyState(item, reference) || dependencyState(item, reference) === 'unknown'),
      });
      addCoordinator(item.request.coordinatorId);
      if ((item.request.dependencies?.length || 0) > COORDINATION_GRAPH_LIMITS.dependencies
        || (item.dependencyStates?.length || 0) > COORDINATION_GRAPH_LIMITS.dependencies) graph.truncated = true;
    }
    let references = 0;
    const addReference = (reference: ProjectDispatchDependency) => {
      const key = assignmentKey(reference.assignmentId);
      if (nodes.has(key)) return true;
      if (references >= COORDINATION_GRAPH_LIMITS.references) { graph.truncated = true; return false; }
      references++;
      nodes.set(key, { id: key, kind: 'reference', title: excerpt(reference.assignmentId, 120), status: 'unknown' });
      return true;
    };
    for (const item of visible) {
      const to = assignmentKey(item.assignmentId), from = coordinatorKey(item.request.coordinatorId);
      if (nodes.has(from)) graph.edges.push({ from, to, kind: 'dispatch' });
      for (const reference of dependencyReferences(item)) {
        if (addReference(reference)) graph.edges.push({ from: assignmentKey(reference.assignmentId), to, kind: 'dependency',
          status: dependencyState(item, reference) || 'unknown', reference: { ...reference } });
      }
      if (item.request.reworkOf && addReference(item.request.reworkOf)) graph.edges.push({
        from: assignmentKey(item.request.reworkOf.assignmentId), to, kind: 'rework', reference: { ...item.request.reworkOf },
      });
    }
  } else {
    // Work ids are not assignment ids. Without an explicit identity join, mixing
    // these projections would count the same execution twice or merge retries.
    graph.delivered = null;
    const work = [...groups.active.slice(0, 100), ...groups.recent.slice(0, 100)].slice(0, COORDINATION_GRAPH_LIMITS.work);
    graph.truncated = groups.active.length > 100 || groups.recent.length > 100;
    for (const item of work) {
      if (item.conversationId && coordinatorIds.has(item.conversationId) || nodes.has(`work:${item.id}`)) continue;
      nodes.set(`work:${item.id}`, {
        id: `work:${item.id}`, kind: 'task', title: cleanTitle(item.title || item.id, 240),
        conversationId: item.conversationId, status: item.status === 'active' ? 'running' : item.status,

        reference: { conversationId: item.conversationId, workId: item.id },
        summary: excerpt(item.resultSummary, 320),
      });
    }
  }
  graph.nodes = [...nodes.values()];
  const tasks = graph.nodes.filter(node => node.kind === 'task');
  for (const node of tasks) graph.counts[node.status || 'unknown']++;
  graph.total = !tasks.length && graph.assignmentsUnavailable ? null : tasks.length;
  graph.completed = graph.counts.completed;
  if (graph.delivered !== null) graph.delivered = tasks.filter(node => node.delivered).length;
  return graph;
}

export interface CoordinationGraphPosition { x: number; y: number; }
/* Geometry units are design-token --space-4 cells shared with the stylesheet
 * (`.coordination-graph-node`). Height 12 fits head + digest + details fold + action. */
export const COORDINATION_GRAPH_GEOMETRY = { width: 14, height: 7, column: 17, row: 9, padding: 1 } as const;
/** Uniform token-sized cells keep SVG edges aligned without a layout observer.
 * A bounded topological pass ranks only declared edges; cycles/long chains remain
 * visible, never recursively walked. Positions convey layout, not start time.
 */
export function layoutCoordinationGraph(graph: CoordinationGraph, columns: number = COORDINATION_GRAPH_LIMITS.columns) {
  columns = Math.max(1, Math.min(COORDINATION_GRAPH_LIMITS.columns, Math.floor(columns) || 1));
  const { width, height, column, row, padding } = COORDINATION_GRAPH_GEOMETRY;
  const positions = new Map<string, CoordinationGraphPosition>();
  const depth = new Map(graph.nodes.map(node => [node.id, node.kind === 'coordinator' ? 0 : 1]));
  const outgoing = new Map<string, string[]>();
  const indegree = new Map(graph.nodes.map(node => [node.id, 0]));
  for (const edge of graph.edges) {
    if (edge.from === edge.to || !indegree.has(edge.from) || !indegree.has(edge.to)) continue;
    outgoing.set(edge.from, [...(outgoing.get(edge.from) || []), edge.to]);
    indegree.set(edge.to, indegree.get(edge.to)! + 1);
  }
  const queue = graph.nodes.filter(node => !indegree.get(node.id)).map(node => node.id);
  for (let index = 0; index < queue.length; index++) {
    const id = queue[index];
    for (const child of outgoing.get(id) || []) {
      depth.set(child, Math.min(COORDINATION_GRAPH_LIMITS.ranks - 1, Math.max(depth.get(child)!, depth.get(id)! + 1)));
      indegree.set(child, indegree.get(child)! - 1);
      if (!indegree.get(child)) queue.push(child);
    }
  }
  // Dependencies rank rows, not timestamps. Independent tasks share a compact
  // row band; the first worker stays below the coordinator on narrow screens.
  const ranks = new Map<number, CoordinationGraphNode[]>();
  for (const node of graph.nodes) {
    const rank = depth.get(node.id)!;
    ranks.set(rank, [...(ranks.get(rank) || []), node]);
  }
  let rowOffset = 0;
  for (const [, nodes] of [...ranks].sort(([a], [b]) => a - b)) {
    nodes.forEach((node, index) => positions.set(node.id, {
      x: padding + index % columns * column,
      y: padding + (rowOffset + Math.floor(index / columns)) * row,
    }));
    rowOffset += Math.ceil(nodes.length / columns);
  }
  return {
    positions,
    width: Math.max(width + padding * 2, ...[...positions.values()].map(position => position.x + width + padding)),
    height: Math.max(height + padding * 2, ...[...positions.values()].map(position => position.y + height + padding)),
  };
}
