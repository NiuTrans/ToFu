// @vitest-environment jsdom
import { expect, it, vi } from 'vitest';
vi.mock('../i18n', () => ({ t: (key: string, values?: Record<string, unknown>) => key + (values ? JSON.stringify(values) : '') }));
import { coordinationTaskDigest, createCoordinationBoard as createBoard, projectCoordinationWorkGroups } from './project-coordination-board';
// Graph specifications explicitly request topology; the default-list behavior is
// asserted separately, including that no graph DOM exists before demand.
const createCoordinationBoard: typeof createBoard = (...args) => {
  const board = createBoard(...args);
  const fold = board.querySelector<HTMLDetailsElement>('.coordination-topology')!;
  fold.open = true; fold.dispatchEvent(new Event('toggle')); return board;
};
import type { ProjectDispatchView } from '../api/project-dispatch.generated';

function assignment(overrides: Partial<ProjectDispatchView> = {}): ProjectDispatchView {
  return { assignmentId: 'a', request: { projectPath: '/p', coordinatorId: 'c', recipientId: 'w',
    authorizationSource: { conversationId: 'c', turnId: 't', projectionRevision: 1 }, brief: 'Deliver work', model: 'test',
    budget: { maxTokens: 100, maxModelRequests: 1, deadline: 10 },
    profile: { mode: 'files', tools: ['read_files'], readPaths: ['.'], writePaths: [], maxToolCalls: 1 } },
    grantRevision: 1, revoked: false, canonicalDelivery: false,
    execution: { turnId: 't', attemptId: 'attempt', taskId: 'task', status: 'completed', resultRevision: 2, result: {} },
    review: null, ...overrides };
}

it('surfaces the human message from a serialized error envelope as the digest', () => {
  const summary = JSON.stringify({ kind: 'server_busy', severity: 'warning', retryable: true,
    message: 'Server is temporarily busy.\nServer is temporarily out of capacity.' });
  const digest = coordinationTaskDigest({ id: 'x', kind: 'task', title: 'T', status: 'failed', summary });
  expect(digest).toBe('Server is temporarily busy. Server is temporarily out of capacity.');
  expect(coordinationTaskDigest({ id: 'y', kind: 'task', title: 'T', summary: 'plain summary' })).toBe('plain summary');
  // Truncated stored envelopes still yield the message, not the raw JSON tail.
  const truncated = '{"kind":"server_busy","severity":"warning","retryable":true,"message":"Server is temporarily busy.\\nRetry shortly","hint":"…';
  expect(coordinationTaskDigest({ id: 'z', kind: 'task', title: 'T', summary: truncated })).toContain('Server is temporarily busy.');
});


const running = () => assignment({ execution: { turnId: 't', attemptId: 'x', taskId: 'task', status: 'running', resultRevision: 0, result: {} } });

it('draws connected Coordinator and Worker nodes while the Coordinator conversation is open', () => {
  const board = createCoordinationBoard({ active: [], recent: [] }, vi.fn(),
    { assignments: [running()], coordinatorId: 'c', activeConversationId: 'c' });
  expect(board.querySelector('svg [data-edge-kind="dispatch"]')).not.toBeNull();
  expect(board.querySelector('[data-node-kind="coordinator"]')).not.toBeNull();
  expect(board.querySelector('[data-node-kind="task"]')?.textContent).toContain('Deliver work');
  expect(board.querySelector('.coordination-group')).toBeNull();
  expect(board.querySelector('[role="progressbar"]')).toBeNull();
});

it('selects a node to inspect its outcome and open the ordinary worker conversation', () => {
  const item = running(); item.request.brief = 'Implement UI';
  const navigate = vi.fn(), state = {};
  const board = createCoordinationBoard({ active: [], recent: [] }, navigate, { assignments: [item] }, { viewState: state });
  const button = board.querySelector<HTMLButtonElement>('[data-node-kind="task"] button')!;
  button.click(); expect(navigate).not.toHaveBeenCalled();
  expect(button.getAttribute('aria-pressed')).toBe('true');
  const inspector = board.querySelector<HTMLElement>('.coordination-node-inspector')!;
  expect(inspector.hidden).toBe(false);
  expect(inspector.textContent).toContain('projectCoordination.graph.deliveryUnconfirmed');
  inspector.querySelector<HTMLButtonElement>('[data-coordination-focus="graph-open:assignment:a"]')!.click(); expect(navigate).toHaveBeenCalledWith('w');
  const refreshed = createCoordinationBoard({ active: [], recent: [] }, navigate, { assignments: [item] }, { viewState: state });
  expect(refreshed.querySelector('[data-node-kind="task"] button')?.getAttribute('aria-pressed')).toBe('true');
});

it('keeps completed prerequisites connected while unrelated history is disclosed on demand', () => {
  const a = assignment(), b = running(); b.assignmentId = 'b';
  b.request.dependencies = [{ assignmentId: 'a', grantRevision: 1, resultRevision: 2 }];
  const old = assignment({ assignmentId: 'unrelated' });
  const state = {};
  const board = createCoordinationBoard({ active: [], recent: [] }, vi.fn(), { assignments: [a, b, old] }, { viewState: state });
  expect(board.querySelector('[data-node-id="assignment:a"]')).not.toBeNull();
  expect(board.querySelector('[data-node-id="assignment:unrelated"]')).toBeNull();
  expect(board.querySelector('svg [data-edge-kind="dependency"]')).not.toBeNull();
  board.querySelector<HTMLButtonElement>('[data-coordination-focus="graph-history"]')!.click();
  expect(board.querySelectorAll('[data-node-kind="task"]')).toHaveLength(3);
  const refreshed = createCoordinationBoard({ active: [], recent: [] }, vi.fn(), { assignments: [a, b, old] }, { viewState: state });
  expect(refreshed.querySelectorAll('[data-node-kind="task"]')).toHaveLength(3);
});

it('leaves observed tasks unconnected when there is no assignment evidence', () => {
  const board = createCoordinationBoard(projectCoordinationWorkGroups([
    { id: 'work', conversationId: 'worker', title: 'Implement UI', status: 'active' },
  ], [{ id: 'old', status: 'completed' }]), vi.fn());
  expect(board.querySelectorAll('[data-node-kind="task"]')).toHaveLength(1);
  expect(board.querySelectorAll('[data-edge-kind]')).toHaveLength(0);
  expect(board.textContent).toContain('projectCoordination.graph.workCoverage');
  expect(board.textContent).not.toContain('projectCoordination.graph.delivery{');
});

it('distinguishes unavailable evidence, empty projects, and finished workers', () => {
  const unknown = createCoordinationBoard({ active: [], recent: [] }, vi.fn(), { assignments: null });
  expect(unknown.textContent).toContain('projectCoordination.graph.progressUnknown');
  expect(unknown.textContent).toContain('projectCoordination.graph.assignmentsUnavailable');
  const empty = createCoordinationBoard({ active: [], recent: [] }, vi.fn(), { assignments: [] });
  expect(empty.textContent).toContain('projectCoordination.graph.empty');
  const done = createCoordinationBoard({ active: [], recent: [] }, vi.fn(), { assignments: [assignment()] });
  expect(done.textContent).toContain('projectCoordination.graph.noActiveWork');
});

it('keeps untrusted long titles escaped, node controls keyboard accessible and zoom bounded', () => {
  const item = running(); item.request.brief = '<img src=x onerror=alert(1)>'.repeat(500);
  const board = createCoordinationBoard({ active: [], recent: [] }, vi.fn(), { assignments: [item] });
  const viewport = board.querySelector<HTMLElement>('.coordination-graph-viewport')!;
  expect(viewport.tabIndex).toBe(0); expect(viewport.getAttribute('role')).toBe('region');
  expect(board.querySelector('img')).toBeNull();
  const button = board.querySelector<HTMLButtonElement>('[data-node-kind="task"] button')!;
  expect(button.textContent!.length).toBeLessThanOrEqual(240);
  document.body.append(board); button.focus(); expect(document.activeElement).toBe(button);
  const zoom = board.querySelector<HTMLButtonElement>('[data-coordination-focus="graph-zoom-out"]')!;
  for (let i=0; i<10; i++) zoom.click();
  expect(zoom.disabled).toBe(true); expect(board.textContent).toContain('50%'); board.remove();
});

it('strips attachment references from node titles', () => {
  const item = running(); item.request.brief = '[image ref: /api/images/1.png — call inspect_image] Fix login';
  const board = createCoordinationBoard({ active: [], recent: [] }, vi.fn(), { assignments: [item] });
  expect(board.querySelector('[data-node-kind="task"] button')?.textContent).toBe('Fix login');
});


it('keeps a selected worker visible as it finishes without opening all archived work', () => {
  const state = { selectedId: 'assignment:a' };
  const board = createCoordinationBoard({ active: [], recent: [] }, vi.fn(),
    { assignments: [assignment(), assignment({ assignmentId: 'old' })] }, { viewState: state });
  expect(board.querySelector('[data-node-id="assignment:a"]')).not.toBeNull();
  expect(board.querySelector('[data-node-id="assignment:old"]')).toBeNull();
  expect(board.querySelector<HTMLElement>('.coordination-node-inspector')!.hidden).toBe(false);
});
