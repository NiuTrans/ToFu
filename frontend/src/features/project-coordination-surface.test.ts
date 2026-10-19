// @vitest-environment jsdom
import { expect, it, vi } from 'vitest';
import { createProjectCoordinationSurface } from './project-coordination-surface';
import type { ProjectCoordinationPorts } from './project-coordination';
import type { CoordinatorReportState } from '../api/project-coordination.generated';
import type { ProjectDispatchPage } from '../api/project-dispatch.generated';
vi.mock('../i18n', () => ({ t: (key: string) => key }));
function fixture() {
  const sidebar = document.createElement('div'), canvas = document.createElement('div');
  document.body.replaceChildren(sidebar, canvas);
  let current = 'leader', path = '/project', wake = () => {};
  const unsubscribe = vi.fn(), details = vi.fn(), create = vi.fn();
  const ports: ProjectCoordinationPorts = {
    path, currentConversationId: current, currentPath: () => path, currentConversation: () => current,
    binding: vi.fn(async () => ({ projectPath: path, conversationId: 'leader', revision: 4,
      availability: 'available' as const, capabilities: { assign: false as const, sendMessage: false as const, steerSubagent: false } })),
    board: vi.fn(async () => ({ active: [], recentOutcomes: [{ id: 'result', conversationId: 'worker', status: 'completed' as const, resultSummary: 'Result evidence' }] })),
    bind: vi.fn(), navigate: vi.fn(), openEvidence: vi.fn(), subscribe: fn => { wake = fn; return unsubscribe; },
  };
  const surface = createProjectCoordinationSurface({ sidebar, canvas, scope: () => ({ ...ports, path, currentConversationId: current }),
    sidebarPath: () => path, details, create });
  return { surface, sidebar, canvas, ports, details, create, unsubscribe, wake: () => wake(),
    conversation: (id: string) => { current = id; }, path: (value: string) => { path = value; } };
}

function expandGraph(canvas: HTMLElement) {
  const topology = canvas.querySelector<HTMLDetailsElement>('.coordination-topology')!;
  topology.open = true; topology.dispatchEvent(new Event('toggle'));
}

function reportState(turnId = 'report-A', unread = true): CoordinatorReportState {
  return { projectPath: '/project', conversationId: 'leader', bindingRevision: 4, unread,
    latest: { turnId, ordinal: turnId === 'report-A' ? 1 : 2, createdAt: 1,
      title: `Decision ${turnId}`, kind: 'decision', content: 'A decision needs your attention.' } };
}

it('shows a real unread preview without acknowledgement and keeps it through read failures', async () => {
  const f = fixture(), read = vi.fn(async () => reportState()), markRead = vi.fn();
  f.ports.reports = { read, markRead }; await f.surface.refresh();
  expect(f.sidebar.querySelector('[data-unread="true"]')?.textContent).toContain('Decision report-A');
  expect(markRead).not.toHaveBeenCalled();
  const fold = f.canvas.querySelector<HTMLDetailsElement>('.coordination-summary')!;
  fold.open = false;
  const message = f.canvas.querySelector<HTMLDetailsElement>('.coordination-report')!;
  message.open = true; message.dispatchEvent(new Event('toggle'));
  read.mockRejectedValueOnce(new Error('offline'));
  await f.surface.refresh(true);
  expect(f.sidebar.querySelector('[data-unread="true"]')).not.toBeNull();
  expect(f.canvas.textContent).toContain('projectCoordination.report.unavailable');
  expect(f.canvas.querySelector<HTMLDetailsElement>('.coordination-summary')?.open).toBe(false);
  expect(f.canvas.querySelector<HTMLDetailsElement>('.coordination-report')?.open).toBe(true);
  expect(markRead).not.toHaveBeenCalled();
  // A stale cached report cannot follow a replacement binding into its chat.
  vi.mocked(f.ports.binding).mockResolvedValue({ ...(await f.ports.binding('/project'))!, revision: 5 });
  read.mockRejectedValueOnce(new Error('offline'));
  await f.surface.refresh(true);
  expect(f.sidebar.querySelector('[data-unread="true"]')).toBeNull();
  expect(f.canvas.querySelector('.coordination-report')).toBeNull(); f.surface.destroy();
});

it('refreshes after an exact read and preserves a newer unread report arriving meanwhile', async () => {
  const f = fixture(); let state = reportState();
  const markRead = vi.fn(async () => { state = reportState('report-B'); return state; });
  f.ports.reports = { read: vi.fn(async () => state), markRead }; await f.surface.refresh();
  f.canvas.querySelector<HTMLButtonElement>('.coordination-report button')!.click();
  await vi.waitFor(() => expect(f.canvas.querySelector('.coordination-report')?.getAttribute('data-report-id')).toBe('report-B'));
  expect(markRead).toHaveBeenCalledWith({ path: '/project', conversationId: 'leader', bindingRevision: 4, turnId: 'report-A' });
  expect(f.sidebar.querySelector('[data-unread="true"]')?.textContent).toContain('Decision report-B');
  state = reportState('report-B', false); f.wake();
  await vi.waitFor(() => expect(f.sidebar.querySelector('[data-unread="true"]')).toBeNull());
  f.surface.destroy();
});
it('opens the bound chat directly, defaults to a compact list, and does not mark ordinary chats as workers', async () => {
  const f = fixture(); await f.surface.refresh();
  expect(f.sidebar.querySelector('[data-coordinator-id]')?.getAttribute('data-coordinator-id')).toBe('leader');
  expect(f.canvas.querySelector('details')?.open).toBe(true);
  expect(f.canvas.querySelector('.coordination-work-list')).not.toBeNull();
  expect(f.canvas.querySelector('.coordination-graph-viewport')).toBeNull();
  expect(f.canvas.querySelector<HTMLDetailsElement>('.coordination-topology')?.open).toBe(false);
  expandGraph(f.canvas);
  expect(f.canvas.querySelector('[data-node-kind="coordinator"]')).not.toBeNull();
  expect(f.canvas.textContent).not.toContain('projectCoordination.deliveryCaveat');
  expect(f.sidebar.textContent).not.toContain('projectCoordination.attention');
  expect(f.sidebar.textContent).not.toContain('projectCoordination.active');
  // The sidebar row is one quiet navigation target, not a nested button.
  const row = f.sidebar.querySelector<HTMLElement>('.coordination-leader')!;
  expect(row.querySelector('button')).toBeNull();
  row.click();
  expect(f.details).not.toHaveBeenCalled();
  expect(f.ports.navigate).toHaveBeenCalledWith('leader');
  f.conversation('ordinary'); await f.surface.refresh();
  expect(f.canvas.textContent).not.toContain('returnCoordinator');
  expect(f.canvas.querySelector('details')).toBeNull();
  f.conversation('worker'); await f.surface.refresh();
  expect(f.canvas.textContent).toContain('returnCoordinator');
  f.canvas.querySelector('button')!.click(); expect(f.ports.navigate).toHaveBeenCalledWith('leader');
  f.surface.destroy();
});
it('offers explicit setup without a mutation and never navigates an unavailable binding', async () => {
  const f = fixture();
  vi.mocked(f.ports.binding).mockResolvedValue({ projectPath: '/project', conversationId: 'removed', revision: 5, availability: 'unavailable', capabilities: { assign: false, sendMessage: false, steerSubagent: false } });
  await f.surface.refresh();
  const setup = [...f.sidebar.querySelectorAll('button')].find(button => button.textContent === 'projectCoordination.setup')!;
  setup.click();
  expect(f.details).toHaveBeenCalledOnce(); expect(f.ports.navigate).not.toHaveBeenCalled(); expect(f.ports.bind).not.toHaveBeenCalled();
  expect(f.sidebar.textContent).toContain('projectCoordination.unavailable'); f.surface.destroy();
});
it('coalesces in-flight push reads, waits for all reads after failure, and fences disposal', async () => {
  const f = fixture(); let resolve!: (value: null) => void;
  vi.mocked(f.ports.board).mockImplementationOnce(() => new Promise(done => { resolve = done; }));
  vi.mocked(f.ports.binding).mockRejectedValueOnce(new Error('offline'));
  const flight = f.surface.refresh(); await Promise.resolve();
  f.wake(); f.wake();
  expect(f.ports.board).toHaveBeenCalledTimes(1);
  expect(f.ports.binding).toHaveBeenCalledTimes(1);
  f.surface.destroy(); resolve(null); await flight;
  expect(f.ports.board).toHaveBeenCalledTimes(1); expect(f.canvas.childElementCount).toBe(0); expect(f.unsubscribe).toHaveBeenCalledOnce();
});
it('fences a previous project response and refreshes the new scope once', async () => {
  const f = fixture(); let resolve!: (value: null) => void;
  vi.mocked(f.ports.board).mockImplementationOnce(() => new Promise(done => { resolve = done; }));
  const flight = f.surface.refresh(); f.path('/other'); void f.surface.refresh();
  expect(f.sidebar.childElementCount).toBe(0); resolve(null); await flight;
  await vi.waitFor(() => expect(f.ports.binding).toHaveBeenCalledTimes(2));
  expect(f.ports.binding).toHaveBeenLastCalledWith('/other'); f.surface.destroy();
});

it('never caches a late roster from a previous project when the new roster read fails', async () => {
  const f = fixture();
  let resolve!: (page: { conversations: { conversationId: string; title: string; turnCount: number; coordinatorId: null }[]; nextCursor: null; coverage: 'complete' }) => void;
  f.ports.workerConversations = vi.fn()
    .mockImplementationOnce(() => new Promise(done => { resolve = done; }))
    .mockRejectedValueOnce(new Error('new project roster unavailable'));
  const flight = f.surface.refresh();
  await vi.waitFor(() => expect(f.ports.workerConversations).toHaveBeenCalledOnce());
  f.path('/other'); void f.surface.refresh();
  resolve({ conversations: [{ conversationId: 'old-worker', title: 'Old project private shell', turnCount: 0, coordinatorId: null }], nextCursor: null, coverage: 'complete' });
  await flight;
  await vi.waitFor(() => expect(f.canvas.textContent).toContain('projectCoordination.roster.unavailable'));
  expect(f.ports.workerConversations).toHaveBeenLastCalledWith('/other');
  expect(f.canvas.textContent).not.toContain('Old project private shell');
  expect(f.canvas.querySelector('[data-work-id="conversation:old-worker"]')).toBeNull();
  f.surface.destroy();
});

it('retains unchanged DOM/focus and expanded summary on changed push facts', async () => {
  const f = fixture(); await f.surface.refresh();
  expandGraph(f.canvas);
  f.canvas.querySelector<HTMLButtonElement>('[data-coordination-focus="graph-history"]')!.click();
  const fold = f.canvas.querySelector('details')!; fold.open = true;
  const button = f.canvas.querySelector('button')!; button.focus();
  await f.surface.refresh(true);
  expect(f.canvas.querySelector('details')).toBe(fold); expect(document.activeElement).toBe(button);
  vi.mocked(f.ports.board).mockResolvedValue({ active: [], recentOutcomes: [
    { id: 'result', conversationId: 'worker', status: 'completed', resultSummary: 'New evidence' },
  ] });
  await f.surface.refresh(true);
  expect(f.canvas.querySelector('details')?.open).toBe(true);
  expect(document.activeElement?.textContent).toBe(button.textContent);
  expect(f.canvas.textContent).toContain('New evidence'); f.surface.destroy();
});

it('does not tear down the sidebar coordinator row when switching conversations in one project', async () => {
  const f = fixture(); await f.surface.refresh();
  /* The row must stay mounted through the refresh cycle — the bug was that a
   * same-project conversation switch cleared the container and left it empty
   * until the async read resolved, so the conversation list shifted up and
   * back down on every click. Assert the row never disappears mid-refresh. */
  expect(f.sidebar.querySelector('[data-coordinator-id]')).not.toBeNull();
  const heightBefore = f.sidebar.childElementCount;
  f.conversation('worker');
  const pending = f.surface.refresh();
  expect(f.canvas.childElementCount).toBe(0);
  expect(f.sidebar.querySelector('[data-coordinator-id]')).not.toBeNull();
  expect(f.sidebar.childElementCount).toBe(heightBefore);
  await pending;
  expect(f.sidebar.querySelector('[data-coordinator-id]')?.getAttribute('data-coordinator-id')).toBe('leader');
  expect(f.sidebar.childElementCount).toBe(heightBefore);
  f.conversation('ordinary'); await f.surface.refresh();
  expect(f.sidebar.querySelector('[data-coordinator-id]')).not.toBeNull();
  expect(f.sidebar.childElementCount).toBe(heightBefore);
  f.surface.destroy();
});
it('keeps folder navigation separate from the active conversation canvas', async () => {
  const f = fixture(); await f.surface.refresh();
  const otherSidebar = document.createElement('div'), unusedCanvas = document.createElement('div');
  const other = createProjectCoordinationSurface({ sidebar: otherSidebar, canvas: unusedCanvas, mode: 'sidebar',
    scope: () => ({ ...f.ports, path: '/other', currentPath: () => '/other', currentConversationId: '', currentConversation: () => '',
      binding: async () => ({ projectPath: '/other', conversationId: 'other-leader', revision: 1, availability: 'available', capabilities: { assign: false, sendMessage: false, steerSubagent: false } }) }),
    sidebarPath: () => '/other', details: vi.fn(), create: vi.fn() });
  await other.refresh();
  expect(otherSidebar.querySelector('[data-coordinator-id]')?.getAttribute('data-coordinator-id')).toBe('other-leader');
  expect(unusedCanvas.childElementCount).toBe(0);
  expect(f.canvas.querySelector('details')).not.toBeNull();
  other.destroy(); f.surface.destroy();
});

it('creates only on explicit action, coalesces clicks, and exposes failure for retry', async () => {
  const f = fixture();
  vi.mocked(f.ports.binding).mockResolvedValue({ projectPath: '/project', conversationId: '', revision: 0,
    availability: 'unbound', capabilities: { assign: false, sendMessage: false, steerSubagent: false } });
  let reject!: (error: Error) => void;
  f.create.mockImplementationOnce(() => new Promise((_done, fail) => { reject = fail; }));
  await f.surface.refresh();
  expect(f.create).not.toHaveBeenCalled(); expect(f.ports.bind).not.toHaveBeenCalled();
  const button = [...f.sidebar.querySelectorAll('button')].find(node => node.textContent === 'projectCoordination.create')!;
  button.click(); button.click();
  expect(f.create).toHaveBeenCalledTimes(1); expect(button.disabled).toBe(true);
  reject(new Error('creation offline'));
  await vi.waitFor(() => expect(f.sidebar.querySelector('[role=alert]')?.textContent).toBe('creation offline'));
  expect(button.disabled).toBe(false);
  button.click(); await vi.waitFor(() => expect(f.create).toHaveBeenCalledTimes(2));
  expect(f.ports.bind).not.toHaveBeenCalled(); f.surface.destroy();
});

it('leaves an ordinary conversation canvas free of project entry and Worker labels', async () => {
  const f = fixture(); f.conversation('ordinary'); await f.surface.refresh();
  expect(f.canvas.childElementCount).toBe(0); expect(f.canvas.textContent).not.toContain('Worker');
  expect(f.sidebar.querySelector('[data-coordinator-id="leader"]')).not.toBeNull();
  f.surface.destroy();
});

function assignment(status: string) {
  return { assignmentId: 'assignment-a', grantRevision: 1, revoked: false, canonicalDelivery: false,
    request: { projectPath: '/project', coordinatorId: 'leader', recipientId: 'worker', brief: 'Build the worker task',
      authorizationSource: { conversationId: 'leader', turnId: 'human-1', projectionRevision: 1 },
      model: 'test', budget: { maxTokens: 1000, maxModelRequests: 2, deadline: 9999999999 } },
    execution: { taskId: 'task-a', turnId: 'result-a', attemptId: 'attempt-a', status, resultRevision: 1, result: {} },
    review: null, publication: null, delivery: null };
}
it('refreshes graph execution facts even when board and review decisions are unchanged', async () => {
  const f = fixture();
  const list = vi.fn(async () => ({ olderCursor: null, newerCursor: null, assignments: [assignment('queued')] }));
  f.ports.dispatch = { list } as unknown as NonNullable<ProjectCoordinationPorts['dispatch']>;
  await f.surface.refresh();
  expect(f.canvas.querySelector('[data-work-id="assignment:assignment-a"]')?.getAttribute('data-status')).toBe('queued');
  expandGraph(f.canvas);

  const viewport = f.canvas.querySelector<HTMLElement>('.coordination-graph-viewport')!;
  viewport.scrollLeft = 80; viewport.scrollTop = 120;
  viewport.focus();
  list.mockResolvedValue({ olderCursor: null, newerCursor: null, assignments: [assignment('running')] });
  await f.surface.refresh(true);
  expect(f.canvas.querySelector('[data-node-kind="task"]')?.getAttribute('data-status')).toBe('running');
  expect(f.canvas.textContent).not.toContain('projectCoordination.graph.status.queued');

  const updated = f.canvas.querySelector<HTMLElement>('.coordination-graph-viewport')!;
  expect(updated.scrollLeft).toBe(80); expect(updated.scrollTop).toBe(120);
  expect(document.activeElement).toBe(updated);
  f.surface.destroy();
});
it('keeps assignment graph evidence when the observed-work endpoint is unavailable', async () => {
  const f = fixture(); f.ports.board = vi.fn(async () => null);
  f.ports.dispatch = { list: vi.fn(async () => ({ olderCursor: null, newerCursor: null, assignments: [assignment('completed')] })) } as unknown as NonNullable<ProjectCoordinationPorts['dispatch']>;
  await f.surface.refresh();
  expect(f.canvas.textContent).toContain('projectCoordination.boardUnavailable');
  expandGraph(f.canvas);
  f.canvas.querySelector<HTMLButtonElement>('[data-coordination-focus="graph-history"]')!.click();
  expect(f.canvas.textContent).toContain('Build the worker task');
  expect(f.canvas.querySelector('[data-node-kind="coordinator"]')).not.toBeNull();
  f.surface.destroy();
});
it('keeps a user-collapsed graph collapsed after live updates and only opens management explicitly', async () => {
  const f = fixture(); await f.surface.refresh();
  f.canvas.querySelector('details')!.open = false;
  f.ports.board = vi.fn(async () => ({ active: [], recentOutcomes: [] }));
  await f.surface.refresh(true);
  expect(f.canvas.querySelector('details')?.open).toBe(false);
  [...f.canvas.querySelectorAll('button')].find(button => button.textContent === 'projectCoordination.controls')!.click();
  expect(f.details).toHaveBeenCalledWith(f.ports);
  expect(f.ports.navigate).not.toHaveBeenCalled();
  f.surface.destroy();
});
it('does not navigate through a stale Coordinator entry after switching projects', async () => {
  const f = fixture(); await f.surface.refresh();
  f.path('/other');
  f.sidebar.querySelector<HTMLElement>('.coordination-leader')!.click();
  expect(f.ports.navigate).not.toHaveBeenCalled();
  expect(f.details).not.toHaveBeenCalled();
  f.surface.destroy();
});

it('reaches history beyond 128 tasks while keeping only one page of graph nodes', async () => {
  const f = fixture();
  const list = vi.fn(async (_path: string, _command?: string, options?: { cursor?: string }): Promise<ProjectDispatchPage> => {
    const offset = Number(options?.cursor || 0);
    return { olderCursor: offset < 128 ? String(offset + 32) : null, newerCursor: offset ? String(offset - 32) : null,
      assignments: Array.from({ length: 32 }, (_, index) => ({ ...assignment(offset ? 'completed' : 'running'),
        assignmentId: String(160 - offset - index), request: { ...assignment('running').request, brief: `Work ${160 - offset - index}` } })) };
  });
  f.ports.dispatch = { list } as unknown as NonNullable<ProjectCoordinationPorts['dispatch']>;
  await f.surface.refresh();
  expandGraph(f.canvas);
  for (let page = 0; page < 5; page++) {
    expect(f.canvas.querySelectorAll('[data-node-kind="task"]')).toHaveLength(32);
    expect(f.canvas.querySelector(`[data-node-id="assignment:${160 - page * 32}"]`)).not.toBeNull();
    if (page < 4) {
      [...f.canvas.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent === 'projectCoordination.history.older')!.click();
      await vi.waitFor(() => expect(f.canvas.querySelector(`[data-node-id="assignment:${128 - page * 32}"]`)).not.toBeNull());
    }
  }
  [...f.canvas.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent === 'projectCoordination.history.latest')!.click();
  await vi.waitFor(() => expect(f.canvas.querySelector('[data-node-id="assignment:160"]')).not.toBeNull());
  expect(f.ports.bind).not.toHaveBeenCalled(); expect(f.ports.navigate).not.toHaveBeenCalled();
  f.surface.destroy();
});

it('discards an old page response and resets history when the project changes', async () => {
  const f = fixture(); let resolve!: (value: ProjectDispatchPage) => void;
  const first: ProjectDispatchPage = { assignments: [assignment('running')], olderCursor: 'old-page', newerCursor: null };
  const list = vi.fn(async (_path: string, _command?: string, options?: { cursor?: string }): Promise<ProjectDispatchPage> => {
    if (options?.cursor) return new Promise(done => { resolve = done; });
    return first;
  });
  f.ports.dispatch = { list } as unknown as NonNullable<ProjectCoordinationPorts['dispatch']>;
  await f.surface.refresh();
  [...f.canvas.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent === 'projectCoordination.history.older')!.click();
  await vi.waitFor(() => expect(resolve).toBeDefined());
  f.path('/other'); await f.surface.refresh();
  resolve({ assignments: [{ ...assignment('completed'), assignmentId: 'stale' }], olderCursor: null, newerCursor: 'back' });
  await vi.waitFor(() => expect(list).toHaveBeenLastCalledWith('/other', undefined, undefined));
  expect(f.canvas.querySelector('[data-node-id="assignment:stale"]')).toBeNull();
  f.surface.destroy();
});

it('shows a known empty worker shell and provides the same return route before work begins', async () => {
  const f = fixture();
  f.ports.conversations = () => [{ id: 'shell', title: 'New worker', turnCount: 0, coordinatorId: 'leader' }];
  f.ports.workerConversations = vi.fn(async () => ({ conversations: [{ conversationId: 'shell', title: 'New worker', turnCount: 0, coordinatorId: 'leader' }], nextCursor: null, coverage: 'complete' as const }));
  await f.surface.refresh();
  expect(f.canvas.querySelector('[data-work-id="conversation:shell"]')?.getAttribute('data-status')).toBe('not_started');
  f.conversation('shell'); await f.surface.refresh();
  expect(f.canvas.textContent).toContain('projectCoordination.returnCoordinator');
  f.canvas.querySelector('button')!.click(); expect(f.ports.navigate).toHaveBeenCalledWith('leader');
  expect(f.ports.workerConversations).toHaveBeenCalledTimes(1); f.surface.destroy();
});

it('retains usable work and reference disclosure on binding failure; retry only reads status', async () => {
  const f = fixture(); await f.surface.refresh();
  const list = f.canvas.querySelector('.coordination-work-list');
  const reference = f.canvas.querySelector<HTMLDetailsElement>('.coordination-reference details')!;
  reference.open = true;
  vi.mocked(f.ports.binding).mockRejectedValueOnce(new Error('offline'));
  await f.surface.refresh(true);
  expect(f.canvas.querySelector('.coordination-work-list')).toBe(list); expect(reference.open).toBe(true);
  expect(f.canvas.querySelector('.coordination-read-error[role=status]')?.textContent).toContain('projectCoordination.error');
  f.canvas.querySelector<HTMLButtonElement>('.coordination-read-error button')!.click();
  await vi.waitFor(() => expect(f.canvas.querySelector('.coordination-read-error')).toBeNull());
  expect(f.ports.bind).not.toHaveBeenCalled(); expect(f.create).not.toHaveBeenCalled(); f.surface.destroy();
});
