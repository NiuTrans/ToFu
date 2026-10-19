import { describe, expect, it } from 'vitest';
import type { ProjectDispatchView } from '../api/project-dispatch.generated';
import { COORDINATION_GRAPH_GEOMETRY, COORDINATION_GRAPH_LIMITS, layoutCoordinationGraph, projectCoordinationGraph } from './project-coordination-graph';

const empty = { active: [], recent: [] };
function assignment(id: string, overrides: Partial<ProjectDispatchView> = {}): ProjectDispatchView {
  return { assignmentId: id, request: { projectPath: '/p', coordinatorId: 'coordinator', recipientId: `worker-${id}`,
    authorizationSource: { conversationId: 'coordinator', turnId: 'human', projectionRevision: 1 }, brief: `Task ${id}`, model: 'test',
    budget: { maxTokens: 100, maxModelRequests: 1, deadline: 10 } }, grantRevision: 1, revoked: false,
    canonicalDelivery: false, execution: null, review: null, ...overrides };
}
function execution(status: string): NonNullable<ProjectDispatchView['execution']> {
  return { status, turnId: 'turn', attemptId: 'attempt', taskId: 'task', resultRevision: status === 'completed' ? 1 : null, result: {} };
}
const ref = (assignmentId: string) => ({ assignmentId, grantRevision: 1, resultRevision: 1 });
const status = (item: ProjectDispatchView) => projectCoordinationGraph(empty, { assignments: [item] }).nodes.find(node => node.kind === 'task')!.status;

describe('assignment execution evidence', () => {
  it.each([
    ['pending', 'queued'], ['running', 'running'], ['completed', 'completed'], ['failed', 'failed'],
    ['cancelled', 'cancelled'], ['aborted', 'cancelled'], ['blocked', 'blocked'], ['new-server-status', 'unknown'],
  ])('projects execution %s as %s', (input, expected) => {
    expect(status(assignment('a', { execution: execution(input) }))).toBe(expected);
  });

  it('does not infer queued execution from accepted or deferred dispatch', () => {
    expect(status(assignment('a'))).toBe('unknown');
    expect(status(assignment('a', { dispatch: 'accepted' }))).toBe('unknown');
    expect(status(assignment('a', { dispatch: 'deferred' }))).toBe('waiting');
    expect(status(assignment('a', { revoked: true }))).toBe('unknown');
    expect(status(assignment('a', { dispatch: 'aborted' }))).toBe('cancelled');
    expect(status(assignment('a', { dependencyStates: [{ reference: ref('old'), status: 'waiting_review', reason: 'review_required' }] }))).toBe('blocked');
    expect(status(assignment('a', { dependencyStates: [{ reference: ref('old'), status: 'unknown', reason: 'evidence_unavailable' }] }))).toBe('unknown');
    const missing = assignment('a', { dispatch: 'deferred' }); missing.request.dependencies = [ref('old')];
    expect(status(missing)).toBe('unknown');
  });

  it('does not turn completed/running execution into cancelled or blocked after authority changes', () => {
    for (const value of ['completed', 'running']) expect(status(assignment('a', { execution: execution(value), revoked: true,
      dependencyStates: [{ reference: ref('old'), status: 'invalid', reason: 'revoked' }] }))).toBe(value);
  });

  it('requires exact version-pinned dependency state, not another result revision', () => {
    const item = assignment('a', { dispatch: 'deferred', dependencyStates: [{ reference: { ...ref('old'), resultRevision: 2 }, status: 'ready', reason: 'ready' }] });
    item.request.dependencies = [ref('old')];
    expect(status(item)).toBe('unknown');
    const graph = projectCoordinationGraph(empty, { assignments: [item] });
    expect(graph.edges.find(edge => edge.kind === 'dependency')).toMatchObject({ status: 'unknown', reference: ref('old') });
  });

  it('uses actual dependencies/rework, not list order or shared worker conversation', () => {
    const a = assignment('a', { execution: execution('completed') }), b = assignment('b'), independent = assignment('independent');
    b.request.dependencies = [ref('a')]; b.request.reworkOf = ref('a');
    b.dependencyStates = [{ reference: ref('a'), status: 'waiting_review', reason: 'review_required' }];
    independent.request.recipientId = b.request.recipientId;
    const graph = projectCoordinationGraph(empty, { assignments: [b, independent, a] });
    expect(graph.edges.filter(edge => edge.kind === 'dependency')).toEqual([
      { from: 'assignment:a', to: 'assignment:b', kind: 'dependency', status: 'waiting_review', reference: ref('a') },
    ]);
    expect(graph.edges.filter(edge => edge.kind === 'rework')).toHaveLength(1);
    expect(graph.edges.filter(edge => edge.kind === 'dispatch')).toHaveLength(3);
    expect(graph.edges.some(edge => edge.from === 'assignment:b' && edge.to === 'assignment:independent')).toBe(false);
    const layout = layoutCoordinationGraph(graph);
    expect(layout.positions.get('assignment:a')!.y).toBeLessThan(layout.positions.get('assignment:b')!.y);
  });

  it('keeps missing referenced tasks explicit without adding them to completion totals', () => {
    const item = assignment('a'); item.request.dependencies = [ref('not-loaded')];
    const graph = projectCoordinationGraph(empty, { assignments: [item] });
    expect(graph.nodes.find(node => node.id === 'assignment:not-loaded')).toMatchObject({ kind: 'reference', status: 'unknown' });
    expect(graph.total).toBe(1); expect(graph.counts.unknown).toBe(1);
  });

  it('counts execution completion separately from confirmed canonical delivery', () => {
    const a = assignment('a', { execution: execution('completed'), canonicalDelivery: true });
    const b = assignment('b', { execution: execution('completed'), canonicalDelivery: false,
      delivery: { taskId: 'task', status: 'delivered', reason: '', checkpointSha: 'sha', targetSha: 'target' } });
    const c = assignment('c', { execution: execution('failed') });
    const d = assignment('d', { ...b, assignmentId: 'd', canonicalDelivery: true });
    const graph = projectCoordinationGraph(empty, { assignments: [a, b, c, d] });
    expect(graph.total).toBe(4); expect(graph.completed).toBe(3); expect(graph.delivered).toBe(1);
    expect(graph.counts.failed).toBe(1);
  });
});

describe('bounded source coverage and layout', () => {
  it('never counts the coordinator as a worker, or adds overlapping Board executions', () => {
    const self = assignment('self'); self.request.recipientId = 'coordinator';
    const graph = projectCoordinationGraph({ active: [{ id: 'board-work', conversationId: 'worker-a', status: 'active' }], recent: [] },
      { assignments: [assignment('a'), self], coordinatorId: 'coordinator' });
    expect(graph.total).toBe(1); expect(graph.source).toBe('assignments');
    expect(graph.nodes.filter(node => node.kind === 'coordinator')).toHaveLength(1);
    expect(graph.nodes.some(node => node.id === 'work:board-work')).toBe(false);
    const fallback = projectCoordinationGraph({ active: [{ id: 'coordinator-work', conversationId: 'coordinator', status: 'active' }], recent: [] },
      { coordinatorId: 'coordinator', assignments: [] });
    expect(fallback.total).toBe(0);
  });

  it('does not guess dependencies or delivery for Board work; preserves unknown vs zero', () => {
    const graph = projectCoordinationGraph({ active: [{ id: 'a', status: 'active' }], recent: [{ id: 'b', status: 'cancelled' }] }, { assignments: null });
    expect(graph.edges).toEqual([]); expect(graph.delivered).toBeNull(); expect(graph.assignmentsUnavailable).toBe(true);
    expect(graph.total).toBe(2); expect(graph.completed).toBe(0);
    expect(projectCoordinationGraph(empty, { assignments: null }).total).toBeNull();
    expect(projectCoordinationGraph(empty, { assignments: [] }).total).toBe(0);
  });

  it('bounds task/reference counts, text and long-chain geometry with no overlapping nodes', () => {
    const assignments = Array.from({ length: 500 }, (_, index) => {
      const item = assignment(String(index)); item.request.brief = 'Long localized title / '.repeat(400);
      item.request.dependencies = [ref(index ? String(index - 1) : 'missing')]; return item;
    });
    const graph = projectCoordinationGraph(empty, { assignments });
    expect(graph.total).toBe(COORDINATION_GRAPH_LIMITS.assignments); expect(graph.truncated).toBe(true);
    expect(Math.max(...graph.nodes.map(node => node.title.length))).toBeLessThanOrEqual(240);
    const layout = layoutCoordinationGraph(graph);
    expect(layout.width).toBeLessThanOrEqual(COORDINATION_GRAPH_LIMITS.columns * COORDINATION_GRAPH_GEOMETRY.column);
    expect(new Set([...layout.positions.values()].map(position => `${position.x},${position.y}`)).size).toBe(graph.nodes.length);
    expect([...layout.positions.values()].every(position => Number.isFinite(position.x) && Number.isFinite(position.y))).toBe(true);
  });

  it('packs independent work across compact rows and keeps the first worker below the coordinator on narrow screens', () => {
    const graph = projectCoordinationGraph(empty, { assignments: ['a', 'b', 'c', 'd', 'e'].map(id => assignment(id)) });
    const { positions } = layoutCoordinationGraph(graph);
    const coordinator = positions.get('coordinator:coordinator')!, first = positions.get('assignment:a')!;
    expect(first.x).toBe(coordinator.x);
    expect(first.y).toBeGreaterThan(coordinator.y);
    expect((first.y + COORDINATION_GRAPH_GEOMETRY.height) * 16).toBeLessThanOrEqual(844 * 0.6);
    expect(positions.get('assignment:c')!.y).toBe(first.y);
    expect(positions.get('assignment:d')!.y).toBeGreaterThan(first.y);
    expect(graph.edges.every(edge => edge.kind === 'dispatch')).toBe(true);
  });

  it('caps missing prerequisites and tolerates cycles without inventing a sequence', () => {
    const items = Array.from({ length: 128 }, (_, index) => {
      const item = assignment(String(index)); item.request.dependencies = [ref(`missing-${index}`)]; return item;
    });
    const graph = projectCoordinationGraph(empty, { assignments: items });
    expect(graph.nodes.filter(node => node.kind === 'reference')).toHaveLength(COORDINATION_GRAPH_LIMITS.references);
    expect(graph.truncated).toBe(true);
    const a = assignment('a'), b = assignment('b'); a.request.dependencies = [ref('b')]; b.request.dependencies = [ref('a')];
    const cycle = projectCoordinationGraph(empty, { assignments: [a, b] });
    expect(layoutCoordinationGraph(cycle).positions.size).toBe(3);
    expect(cycle.edges.filter(edge => edge.kind === 'dependency')).toHaveLength(2);
  });

  it('has a bounded serializable signature that changes when execution facts change, not opaque result bodies', () => {
    const item = assignment('a', { execution: execution('pending') });
    const before = JSON.stringify(projectCoordinationGraph(empty, { assignments: [item] }));
    item.execution!.result = { toolRounds: ['large unrelated tool output'.repeat(5000)] };
    expect(JSON.stringify(projectCoordinationGraph(empty, { assignments: [item] }))).toBe(before);
    item.execution!.status = 'running';
    expect(JSON.stringify(projectCoordinationGraph(empty, { assignments: [item] }))).not.toBe(before);
  });
});


it('projects bounded assistant outcomes but never opaque tool state into node summaries', () => {
  const item = assignment('a', { execution: execution('completed') });
  const before = projectCoordinationGraph(empty, { assignments: [item] });
  item.execution!.result = { content: 'Verified worker result. '.repeat(5000), toolRounds: ['private raw output'] };
  const graph = projectCoordinationGraph(empty, { assignments: [item] });
  expect(graph.nodes.find(node => node.kind === 'task')?.summary).toHaveLength(320);
  expect(JSON.stringify(graph)).not.toContain('private raw output');
  expect(JSON.stringify(graph)).not.toBe(JSON.stringify(before));
});
