// @vitest-environment jsdom
import { expect, it, vi } from 'vitest';
import { createProjectCoordinationSurface } from './project-coordination-surface';
import type { ProjectCoordinationPorts } from './project-coordination';
import type { CoordinatorProgressBoard, CoordinatorProgressState, CoordinatorReportState } from '../api/project-coordination.generated';
vi.mock('../i18n', () => ({ t: (key: string) => key }));
const BOARD: CoordinatorProgressBoard = { revision: 3, updatedAt: Date.UTC(2026, 10, 2, 8, 30), summary: 'Folder fix verified; trip research underway.',
  tasks: [{ title: 'Fix folder creation', status: 'completed', progress: 'Merged and verified.' },
    { title: 'Research New Year homestays', status: 'in_progress', progress: 'Comparing four platforms.' }] };
function progressState(board: CoordinatorProgressBoard | null = BOARD, revision = 4): CoordinatorProgressState {
  return { projectPath: '/project', conversationId: 'leader', bindingRevision: revision, board };
}
function fixture(mode?: 'sidebar' | 'canvas') {
  const sidebar = document.createElement('div'), canvas = document.createElement('div');
  document.body.replaceChildren(sidebar, canvas);
  let current = 'leader', path = '/project', wake = () => {};
  const unsubscribe = vi.fn(), details = vi.fn(), create = vi.fn();
  const ports: ProjectCoordinationPorts = {
    path, currentConversationId: current, currentPath: () => path, currentConversation: () => current,
    binding: vi.fn(async () => ({ projectPath: path, conversationId: 'leader', revision: 4,
      availability: 'available' as const, capabilities: { assign: false as const, sendMessage: false as const, steerSubagent: false } })),
    board: vi.fn(async () => ({ active: [], recentOutcomes: [] })),
    progress: { read: vi.fn(async () => progressState()) },
    conversations: () => [{ id: 'worker', title: 'Worker', turnCount: 2, coordinatorId: 'leader' }],
    bind: vi.fn(), navigate: vi.fn(), openEvidence: vi.fn(), subscribe: fn => { wake = fn; return unsubscribe; },
  };
  const surface = createProjectCoordinationSurface({ sidebar, canvas, mode, scope: () => ({ ...ports, path, currentConversationId: current }),
    sidebarPath: () => path, details, create });
  return { surface, sidebar, canvas, ports, details, create, unsubscribe, wake: () => wake(),
    conversation: (id: string) => { current = id; }, path: (value: string) => { path = value; } };
}

it('opens the sidebar Coordinator as soon as its binding resolves while details are stalled', async () => {
  const f = fixture('sidebar');
  let finish!: (value: CoordinatorProgressState) => void;
  vi.mocked(f.ports.progress!.read).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const flight = f.surface.refresh();
  try {
    await vi.waitFor(() => expect(f.sidebar.querySelector('[role="button"]')?.getAttribute('data-coordinator-id')).toBe('leader'));
    const row = f.sidebar.querySelector<HTMLElement>('.coordination-leader')!;
    expect(row.getAttribute('aria-busy')).toBeNull();
    row.click();
    expect(f.ports.navigate).toHaveBeenCalledWith('leader');
    expect(f.canvas.childElementCount).toBe(0);
  } finally { f.surface.destroy(); finish(progressState()); await flight; }
});

it('exposes binding failure and retry without waiting for a stalled work board', async () => {
  const f = fixture('sidebar');
  let finish!: (value: CoordinatorProgressState) => void;
  vi.mocked(f.ports.binding).mockRejectedValueOnce(new Error('offline'));
  vi.mocked(f.ports.progress!.read).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const flight = f.surface.refresh();
  try {
    await vi.waitFor(() => expect(f.sidebar.querySelector('.coordination-read-error')).not.toBeNull());
    expect(f.sidebar.querySelector('[aria-busy="true"]')).toBeNull();
    f.sidebar.querySelector<HTMLButtonElement>('.coordination-read-error button')!.click();
    await vi.waitFor(() => expect(f.sidebar.querySelector('[role="button"]')).not.toBeNull());
    expect(f.ports.binding).toHaveBeenCalledTimes(2);
  } finally { f.surface.destroy(); finish(progressState()); await flight; }
});

it.each(['online', 'visible'])('recovers a failed binding when the browser becomes %s, without polling', async event => {
  const f = fixture('sidebar');
  vi.mocked(f.ports.binding).mockRejectedValueOnce(new Error('offline'));
  try {
    await f.surface.refresh();
    expect(f.sidebar.querySelector('.coordination-read-error')).not.toBeNull();
    await f.surface.refresh();
    expect(f.ports.binding).toHaveBeenCalledTimes(1);
    if (event === 'online') window.dispatchEvent(new Event('online'));
    else {
      const visibility = vi.spyOn(document, 'visibilityState', 'get');
      visibility.mockReturnValue('hidden');
      document.dispatchEvent(new Event('visibilitychange'));
      expect(f.ports.binding).toHaveBeenCalledTimes(1);
      visibility.mockRestore();
      document.dispatchEvent(new Event('visibilitychange'));
    }
    await vi.waitFor(() => expect(f.sidebar.querySelector('[role="button"]')).not.toBeNull());
    expect(f.sidebar.querySelector('.coordination-read-error')).toBeNull();
    expect(f.ports.binding).toHaveBeenCalledTimes(2);
    window.dispatchEvent(new Event('online'));
    document.dispatchEvent(new Event('visibilitychange'));
    expect(f.ports.binding).toHaveBeenCalledTimes(2);
  } finally { f.surface.destroy(); }
  window.dispatchEvent(new Event('online'));
  document.dispatchEvent(new Event('visibilitychange'));
  expect(f.ports.binding).toHaveBeenCalledTimes(2);
});

it('keeps a failed recovery explicit without spinning retries and reports a safe request witness', async () => {
  const f = fixture('sidebar');
  const warning = vi.spyOn(console, 'warn').mockImplementation(() => {});
  vi.mocked(f.ports.binding).mockRejectedValue(Object.assign(new Error('private transport details'), {
    code: 'timeout', status: 0, requestId: 'binding-request-17', body: { secret: 'must-not-log' },
  }));
  try {
    await f.surface.refresh();
    expect(f.sidebar.textContent).toContain('err.k.timeout.chip');
    expect(f.sidebar.textContent).not.toContain('private transport details');
    expect(warning).toHaveBeenCalledWith('[ProjectCoordination] Binding read failed: code=timeout status=0 requestId=binding-request-17');
    window.dispatchEvent(new Event('online'));
    await vi.waitFor(() => expect(warning).toHaveBeenCalledTimes(2));
    await f.surface.refresh();
    expect(f.ports.binding).toHaveBeenCalledTimes(2);
    expect(f.sidebar.querySelector('.coordination-read-error')).not.toBeNull();
    expect(f.sidebar.querySelector('[role="button"]')).toBeNull();
    expect(JSON.stringify(warning.mock.calls)).not.toContain('must-not-log');
  } finally { f.surface.destroy(); warning.mockRestore(); }
});

it('clears a recovered binding warning before optional detail reads finish', async () => {
  const f = fixture('sidebar');
  let finish!: (value: CoordinatorProgressState) => void;
  try {
    await f.surface.refresh();
    vi.mocked(f.ports.binding).mockRejectedValueOnce(new Error('offline'));
    await f.surface.refresh(true);
    expect(f.sidebar.querySelector('.coordination-read-error')).not.toBeNull();
    vi.mocked(f.ports.progress!.read).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    const retry = f.surface.refresh(true);
    await vi.waitFor(() => expect(f.ports.binding).toHaveBeenCalledTimes(3));
    await vi.waitFor(() => expect(f.sidebar.querySelector('.coordination-read-error')).toBeNull());
    f.sidebar.querySelector<HTMLElement>('[role="button"]')!.click();
    expect(f.ports.navigate).toHaveBeenCalledWith('leader');
    finish(progressState()); await retry;
  } finally { f.surface.destroy(); finish?.(progressState()); }
});

it('loads a new project without waiting for the previous binding and fences its late response', async () => {
  const f = fixture('sidebar');
  let finish!: (value: Awaited<ReturnType<ProjectCoordinationPorts['binding']>>) => void;
  const oldBinding = await f.ports.binding('/project');
  vi.mocked(f.ports.binding).mockClear().mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const first = f.surface.refresh();
  await Promise.resolve();
  f.path('/other');
  vi.mocked(f.ports.binding).mockResolvedValue({ ...oldBinding, projectPath: '/other', conversationId: 'other-leader' });
  await f.surface.refresh();
  try {
    expect(vi.mocked(f.ports.binding).mock.calls[0][1]?.aborted).toBe(true);
    expect(f.sidebar.querySelector('[role="button"]')?.getAttribute('data-coordinator-id')).toBe('other-leader');
    finish(oldBinding); await first;
    expect(f.sidebar.querySelector('[data-coordinator-id]')?.getAttribute('data-coordinator-id')).toBe('other-leader');
  } finally { f.surface.destroy(); finish(oldBinding); await first; }
});

it('keeps the resolved sidebar row usable through conversation switches and cancels reads on teardown', async () => {
  const f = fixture('sidebar'); await f.surface.refresh();
  const row = f.sidebar.querySelector<HTMLElement>('.coordination-leader')!;
  let finish!: (value: Awaited<ReturnType<ProjectCoordinationPorts['binding']>>) => void;
  const binding = await f.ports.binding('/project');
  vi.mocked(f.ports.binding).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  f.conversation('ordinary'); const flight = f.surface.refresh();
  expect(f.sidebar.querySelector('.coordination-leader')).toBe(row);
  row.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }));
  expect(f.ports.navigate).toHaveBeenCalledWith('leader');
  const signal = vi.mocked(f.ports.binding).mock.lastCall![1]!;
  expect(signal.aborted).toBe(false);
  f.surface.destroy();
  expect(signal.aborted).toBe(true);
  finish(binding); await flight;
  expect(f.sidebar.childElementCount).toBe(0);
});

function reportState(turnId = 'report-A', unread = true): CoordinatorReportState {
  return { projectPath: '/project', conversationId: 'leader', bindingRevision: 4, unread,
    latest: { turnId, ordinal: turnId === 'report-A' ? 1 : 2, createdAt: 1,
      title: `Delivered ${turnId}`, kind: 'delivery', content: 'The fix is merged.' } };
}

it('previews an unread message in the sidebar without reading it there', async () => {
  const f = fixture('sidebar'), read = vi.fn(async () => reportState()), markRead = vi.fn();
  f.ports.reports = { read, markRead }; await f.surface.refresh();
  expect(f.sidebar.querySelector('[data-unread="true"]')?.textContent).toContain('Delivered report-A');
  expect(f.sidebar.querySelector('.coordination-leader-unread')?.getAttribute('aria-label')).toBe('projectCoordination.report.unread');
  read.mockRejectedValueOnce(new Error('offline'));
  await f.surface.refresh(true);
  expect(f.sidebar.querySelector('[data-unread="true"]')).not.toBeNull();
  expect(markRead).not.toHaveBeenCalled();
  // A stale cached report cannot follow a replacement binding.
  vi.mocked(f.ports.binding).mockResolvedValue({ ...(await f.ports.binding('/project'))!, revision: 5 });
  read.mockRejectedValueOnce(new Error('offline'));
  await f.surface.refresh(true);
  expect(f.sidebar.querySelector('[data-unread="true"]')).toBeNull(); f.surface.destroy();
});

it('reads the exact latest message when the Coordinator chat is open and visible, like a messaging app', async () => {
  const f = fixture(); let state = reportState();
  const markRead = vi.fn(async (request: { turnId: string }) => { state = reportState(request.turnId, false); return state; });
  f.ports.reports = { read: vi.fn(async () => state), markRead };
  await f.surface.refresh();
  expect(markRead).toHaveBeenCalledWith({ path: '/project', conversationId: 'leader', bindingRevision: 4, turnId: 'report-A' });
  await vi.waitFor(() => expect(f.sidebar.querySelector('[data-unread="true"]')).toBeNull());
  // A newer message arriving while the chat stays open is read on its own Turn ID.
  state = reportState('report-B'); f.wake();
  await vi.waitFor(() => expect(markRead).toHaveBeenLastCalledWith(expect.objectContaining({ turnId: 'report-B' })));
  expect(markRead).toHaveBeenCalledTimes(2);
  // Reading never happens from another conversation of the project.
  state = reportState('report-C'); f.conversation('ordinary'); await f.surface.refresh();
  expect(markRead).toHaveBeenCalledTimes(2);
  f.surface.destroy();
});

it('waits for a hidden tab to become visible before reading and retries a failed read', async () => {
  const f = fixture(); const state = reportState();
  const markRead = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue(reportState('report-A', false));
  f.ports.reports = { read: vi.fn(async () => state), markRead };
  const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
  try {
    await f.surface.refresh();
    expect(markRead).not.toHaveBeenCalled();
    visibility.mockReturnValue('visible');
    document.dispatchEvent(new Event('visibilitychange'));
    expect(markRead).toHaveBeenCalledTimes(1);
    await new Promise(resolve => setTimeout(resolve, 0));
    document.dispatchEvent(new Event('visibilitychange'));
    await vi.waitFor(() => expect(markRead).toHaveBeenCalledTimes(2));
  } finally { visibility.mockRestore(); f.surface.destroy(); }
});

it('pins only the task board atop the Coordinator chat: no sessions, references, graphs or usage', async () => {
  const f = fixture(); await f.surface.refresh();
  expect(f.sidebar.querySelector('[data-coordinator-id]')?.getAttribute('data-coordinator-id')).toBe('leader');
  const card = f.canvas.querySelector<HTMLDetailsElement>('.coordinator-progress')!;
  expect(f.canvas.firstElementChild).toBe(card);
  // Folded to its one-line head by default: the sidebar already lists the work.
  expect(card.open).toBe(false);
  expect(card.querySelector('.coordinator-progress-summary')?.textContent).toBe(BOARD.summary);
  expect([...card.querySelectorAll('.coordinator-progress-task strong')].map(node => node.textContent))
    .toEqual(['Research New Year homestays', 'Fix folder creation']);
  for (const retired of ['.coordination-work-list', '.coordination-topology', '.coordination-reference',
    '.coordinator-budget', '.coordinator-plan', '.coordination-report']) expect(f.canvas.querySelector(retired)).toBeNull();
  expect(f.ports.board).not.toHaveBeenCalled();
  // The sidebar row is one quiet navigation target that summarizes the board.
  const row = f.sidebar.querySelector<HTMLElement>('.coordination-leader')!;
  expect(row.querySelector('button')).toBeNull();
  expect(row.querySelector('.coordination-leader-meta')?.textContent).toBe('coordinatorProgress.sidebar.active');
  expect(row.title).toContain(BOARD.summary);
  row.click();
  expect(f.details).not.toHaveBeenCalled();
  expect(f.ports.navigate).toHaveBeenCalledWith('leader');
  f.conversation('ordinary'); await f.surface.refresh();
  expect(f.canvas.childElementCount).toBe(0);
  f.conversation('worker'); await f.surface.refresh();
  expect(f.canvas.textContent).toContain('returnCoordinator');
  f.canvas.querySelector('button')!.click(); expect(f.ports.navigate).toHaveBeenCalledWith('leader');
  f.surface.destroy();
});

it('keeps fold state and focus across live board updates and opens management only explicitly', async () => {
  const f = fixture(); await f.surface.refresh();
  const card = f.canvas.querySelector<HTMLDetailsElement>('.coordinator-progress')!;
  card.open = true; card.dispatchEvent(new Event('toggle'));
  const finished = f.canvas.querySelector<HTMLDetailsElement>('.coordinator-progress-finished')!;
  finished.open = true; finished.dispatchEvent(new Event('toggle'));
  const manage = [...f.canvas.querySelectorAll('button')].find(button => button.textContent === 'projectCoordination.controls')!;
  manage.focus();
  vi.mocked(f.ports.progress!.read).mockResolvedValue(progressState({ ...BOARD, revision: 4, summary: 'Research done.',
    tasks: BOARD.tasks.map(task => ({ ...task, status: 'completed' as const })) }));
  f.wake();
  await vi.waitFor(() => expect(f.canvas.querySelector('.coordinator-progress-summary')?.textContent).toBe('Research done.'));
  expect(f.canvas.querySelector<HTMLDetailsElement>('.coordinator-progress')?.open).toBe(true);
  expect(f.canvas.querySelector<HTMLDetailsElement>('.coordinator-progress-finished')?.open).toBe(true);
  expect(document.activeElement?.textContent).toBe('projectCoordination.controls');
  expect(f.sidebar.querySelector('.coordination-leader-meta')?.textContent).toBe('coordinatorProgress.sidebar.allDone');
  (document.activeElement as HTMLButtonElement).click();
  expect(f.details).toHaveBeenCalledWith(expect.objectContaining({ path: '/project' }));
  expect(f.ports.navigate).not.toHaveBeenCalled();
  f.surface.destroy();
});

it("shows an empty or unavailable board explicitly and never a previous binding's board", async () => {
  const f = fixture();
  vi.mocked(f.ports.progress!.read).mockResolvedValue(progressState(null));
  await f.surface.refresh();
  expect(f.canvas.querySelector('.coordinator-progress')?.getAttribute('data-state')).toBe('empty');
  expect(f.sidebar.querySelector('.coordination-leader-meta')?.textContent).toBe('projectCoordination.sidebarIdle');
  vi.mocked(f.ports.progress!.read).mockResolvedValue(progressState(BOARD, 3));
  await f.surface.refresh(true);
  expect(f.canvas.querySelector('.coordinator-progress')?.getAttribute('data-state')).toBe('empty');
  vi.mocked(f.ports.progress!.read).mockRejectedValueOnce(new Error('offline'));
  await f.surface.refresh(true);
  expect(f.canvas.querySelector('.coordinator-progress')?.getAttribute('data-state')).toBe('unavailable');
  expect(f.sidebar.querySelector('.coordination-leader-meta')?.textContent).toBe('projectCoordination.boardUnavailable');
  vi.mocked(f.ports.progress!.read).mockResolvedValue(progressState());
  [...f.canvas.querySelectorAll('button')].find(button => button.textContent === 'projectCoordination.retry')!.click();
  await vi.waitFor(() => expect(f.canvas.querySelector('.coordinator-progress')?.getAttribute('data-state')).toBe('board'));
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
  const f = fixture(); let resolve!: (value: CoordinatorProgressState) => void;
  vi.mocked(f.ports.progress!.read).mockImplementationOnce(() => new Promise(done => { resolve = done; }));
  vi.mocked(f.ports.binding).mockRejectedValueOnce(new Error('offline'));
  const flight = f.surface.refresh(); await Promise.resolve();
  f.wake(); f.wake();
  expect(f.ports.progress!.read).toHaveBeenCalledTimes(1);
  expect(f.ports.binding).toHaveBeenCalledTimes(1);
  f.surface.destroy(); resolve(progressState()); await flight;
  expect(f.ports.progress!.read).toHaveBeenCalledTimes(1); expect(f.canvas.childElementCount).toBe(0); expect(f.unsubscribe).toHaveBeenCalledOnce();
  expect(f.ports.board).not.toHaveBeenCalled();
});
it('fences a previous project response and refreshes the new scope once', async () => {
  const f = fixture(); let resolve!: (value: CoordinatorProgressState) => void;
  vi.mocked(f.ports.progress!.read).mockImplementationOnce(() => new Promise(done => { resolve = done; }));
  const flight = f.surface.refresh(); f.path('/other'); void f.surface.refresh();
  /* The stale identity is gone, but the row itself stays: only its identity
   * is withdrawn until the new scope's binding read settles. */
  expect(f.sidebar.childElementCount).toBe(1);
  expect(f.sidebar.querySelector('[data-coordinator-id="leader"]')).toBeNull();
  expect(f.sidebar.querySelector('.coordination-leader')?.getAttribute('aria-busy')).toBe('true');
  resolve(progressState()); await flight;
  await vi.waitFor(() => expect(f.ports.binding).toHaveBeenCalledTimes(2));
  expect(f.ports.binding).toHaveBeenLastCalledWith('/other', expect.any(AbortSignal)); f.surface.destroy();
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
  // The Coordinator's task board never follows into a worker chat.
  expect(f.canvas.querySelector('.coordinator-progress')).toBeNull();
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
  expect(f.canvas.querySelector('.coordinator-progress')).not.toBeNull();
  other.destroy(); f.surface.destroy();
});

it('paints the coordinator row synchronously on a folder switch and reuses the cached binding', async () => {
  const f = fixture(); await f.surface.refresh();
  expect(f.sidebar.querySelector('[data-coordinator-id="leader"]')).not.toBeNull();
  /* A folder switch used to clear the container and wait for the binding
   * read, so the row appeared late and pushed the list down. The shell must
   * be in the DOM on the first frame of the new scope. */
  f.path('/other');
  const pending = f.surface.refresh();
  const row = f.sidebar.querySelector<HTMLElement>('.coordination-leader')!;
  expect(row).not.toBeNull();
  expect(row.getAttribute('aria-busy')).toBe('true');
  expect(row.dataset.coordinatorId).toBe('');
  await pending;
  expect(f.sidebar.querySelector('[data-coordinator-id]')).not.toBeNull();
  expect(f.sidebar.querySelector('.coordination-leader')?.getAttribute('aria-busy')).toBeNull();
  f.path('/project');
  vi.mocked(f.ports.binding).mockImplementationOnce(() => new Promise(() => {}));
  void f.surface.refresh();
  /* Returning to a known project restores the identity before the read lands,
   * so the conversation list filters the same chat on the first render. */
  expect(f.sidebar.querySelector('[data-coordinator-id="leader"]')?.getAttribute('aria-busy')).toBe('true');
  f.surface.destroy();
});

it('keeps the pre-painted row and reports the read failure instead of collapsing the sidebar', async () => {
  const sidebar = document.createElement('div'), canvas = document.createElement('div');
  let path = '/project';
  const surface = createProjectCoordinationSurface({ sidebar, canvas, mode: 'sidebar',
    scope: () => ({ path, currentConversationId: '', currentPath: () => path, currentConversation: () => '',
      binding: vi.fn(async () => { throw new Error('offline'); }), board: vi.fn(async () => null),
      bind: vi.fn(), navigate: vi.fn(), openEvidence: vi.fn() }),
    sidebarPath: () => path, details: vi.fn(), create: vi.fn() });
  await surface.refresh();
  const row = sidebar.querySelector<HTMLElement>('.coordination-leader')!;
  expect(row.getAttribute('aria-busy')).toBeNull();
  expect(row.dataset.coordinatorId).toBe('');
  expect(row.textContent).toContain('projectCoordination.error');
  expect(sidebar.querySelector('.coordination-read-error')).not.toBeNull();
  surface.destroy();
});
it('keeps the pre-painted row and reports the read failure instead of collapsing the sidebar', async () => {
  const sidebar = document.createElement('div'), canvas = document.createElement('div');
  let path = '/project';
  const binding = vi.fn(async () => { throw new Error('offline'); }), changed = vi.fn();
  let surface!: ReturnType<typeof createProjectCoordinationSurface>;
  surface = createProjectCoordinationSurface({ sidebar, canvas, mode: 'sidebar',
    scope: () => ({ path, currentConversationId: '', currentPath: () => path, currentConversation: () => '',
      binding, board: vi.fn(async () => null),
      bind: vi.fn(), navigate: vi.fn(), openEvidence: vi.fn() }),
    sidebarPath: () => path, details: vi.fn(), create: vi.fn(),
    /* Production wiring: re-rendering the conversation list refreshes this very
     * surface through presenceRefresh. */
    changed: () => { changed(); void surface.refresh(); } });
  await surface.refresh();
  const row = sidebar.querySelector<HTMLElement>('.coordination-leader')!;
  expect(row.getAttribute('aria-busy')).toBeNull();
  expect(row.dataset.coordinatorId).toBe('');
  expect(row.textContent).toContain('projectCoordination.error');
  expect(sidebar.querySelector('.coordination-read-error')).not.toBeNull();
  await Promise.resolve();
  /* Dropping the cached identity must re-render the conversation list, but the
   * re-render may not queue another read: a persistent failure would then
   * retry forever. */
  expect(changed).toHaveBeenCalledTimes(1);
  expect(binding).toHaveBeenCalledTimes(1);
  surface.destroy();
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

it('re-evaluates Create when the folder of the painted project is selected, without another read', async () => {
  const sidebar = document.createElement('div'), canvas = document.createElement('div');
  document.body.replaceChildren(sidebar, canvas);
  let folderSelected = false;
  const binding = vi.fn(async () => ({ projectPath: '/project', conversationId: '', revision: 0,
    availability: 'unbound' as const, capabilities: { assign: false as const, sendMessage: false as const, steerSubagent: false } }));
  const surface = createProjectCoordinationSurface({ sidebar, canvas, mode: 'sidebar',
    scope: () => ({ path: '/project', currentConversationId: 'ordinary', currentPath: () => '/project',
      currentConversation: () => 'ordinary', binding, board: vi.fn(async () => ({ active: [], recentOutcomes: [] })),
      bind: vi.fn(), navigate: vi.fn(), openEvidence: vi.fn() }),
    sidebarPath: () => '/project', details: vi.fn(), create: vi.fn(), canCreate: () => folderSelected });
  try {
    await surface.refresh();
    const create = [...sidebar.querySelectorAll('button')].find(node => node.textContent === 'projectCoordination.create')!;
    const hint = [...sidebar.querySelectorAll('small')].find(node => node.textContent === 'projectCoordination.selectFolder')!;
    expect([create.disabled, hint.hidden]).toEqual([true, false]);
    /* The conversation already showed this project, so selecting its folder
     * changes neither the read key nor the painted facts. */
    folderSelected = true; await surface.refresh();
    expect([create.isConnected, create.disabled, hint.hidden]).toEqual([true, false, true]);
    folderSelected = false; await surface.refresh();
    expect([create.disabled, hint.hidden]).toEqual([true, false]);
    expect(binding).toHaveBeenCalledTimes(1);
  } finally { surface.destroy(); }
});

it('leaves an ordinary conversation canvas free of project entry and Worker labels', async () => {
  const f = fixture(); f.conversation('ordinary'); await f.surface.refresh();
  expect(f.canvas.childElementCount).toBe(0); expect(f.canvas.textContent).not.toContain('Worker');
  expect(f.sidebar.querySelector('[data-coordinator-id="leader"]')).not.toBeNull();
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

it('gives an unstarted Worker shell the same return route without reading its roster', async () => {
  const f = fixture();
  f.ports.conversations = () => [{ id: 'shell', title: 'New worker', turnCount: 0, coordinatorId: 'leader' }];
  f.ports.workerConversations = vi.fn();
  f.conversation('shell'); await f.surface.refresh();
  expect(f.canvas.textContent).toContain('projectCoordination.returnCoordinator');
  f.canvas.querySelector('button')!.click(); expect(f.ports.navigate).toHaveBeenCalledWith('leader');
  expect(f.ports.workerConversations).not.toHaveBeenCalled(); f.surface.destroy();
});

it('retains the painted board on binding failure; retry only reads status', async () => {
  const f = fixture(); await f.surface.refresh();
  const board = f.canvas.querySelector('.coordinator-progress');
  vi.mocked(f.ports.binding).mockRejectedValueOnce(new Error('offline'));
  await f.surface.refresh(true);
  expect(f.canvas.querySelector('.coordinator-progress')).toBe(board);
  expect(f.canvas.querySelector('.coordination-read-error[role=status]')?.textContent).toContain('projectCoordination.error');
  f.canvas.querySelector<HTMLButtonElement>('.coordination-read-error button')!.click();
  await vi.waitFor(() => expect(f.canvas.querySelector('.coordination-read-error')).toBeNull());
  expect(f.ports.bind).not.toHaveBeenCalled(); expect(f.create).not.toHaveBeenCalled(); f.surface.destroy();
});


it('keeps a confirmed sidebar Coordinator through a transient re-read failure until a recovery signal re-reads it', async () => {
  const f = fixture('sidebar');
  const warning = vi.spyOn(console, 'warn').mockImplementation(() => {});
  let finish!: (value: CoordinatorProgressState) => void;
  try {
    await f.surface.refresh();
    const row = f.sidebar.querySelector<HTMLElement>('.coordination-leader')!;
    const meta = row.querySelector('.coordination-leader-meta')!.textContent;
    expect(meta).toBe('coordinatorProgress.sidebar.active');
    /* A stalled read settles as code=timeout status=0: the server never
     * answered, so nothing says the confirmed binding changed. */
    vi.mocked(f.ports.binding).mockRejectedValueOnce(Object.assign(new Error('timeout'),
      { code: 'timeout', status: 0, requestId: 'binding-request-9' }));
    await f.surface.refresh(true);
    expect(warning).toHaveBeenCalledWith('[ProjectCoordination] Binding read failed: code=timeout status=0 requestId=binding-request-9');
    expect(f.sidebar.querySelector('.coordination-read-error')).toBeNull();
    expect(f.sidebar.querySelector('.coordination-leader')).toBe(row);
    expect(row.dataset.coordinatorId).toBe('leader');
    expect(row.querySelector('.coordination-leader-meta')!.textContent).toBe(meta);
    row.click(); expect(f.ports.navigate).toHaveBeenCalledWith('leader');
    await f.surface.refresh();
    expect(f.ports.binding).toHaveBeenCalledTimes(2);
    // Recovery re-reads without flashing the intact row back to a loading state.
    vi.mocked(f.ports.progress!.read).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
    window.dispatchEvent(new Event('online'));
    await vi.waitFor(() => expect(f.ports.progress!.read).toHaveBeenCalledTimes(3));
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(f.ports.binding).toHaveBeenCalledTimes(3);
    expect(f.sidebar.querySelector('.coordination-leader')).toBe(row);
    expect(row.querySelector('.coordination-leader-meta')!.textContent).toBe(meta);
  } finally { warning.mockRestore(); f.surface.destroy(); finish?.(progressState()); }
});

it('keeps the confirmed Coordinator canvas on a transport failure but surfaces a definitive rejection', async () => {
  const f = fixture();
  const warning = vi.spyOn(console, 'warn').mockImplementation(() => {});
  try {
    await f.surface.refresh();
    const board = f.canvas.querySelector('.coordinator-progress');
    vi.mocked(f.ports.binding).mockRejectedValueOnce(Object.assign(new Error('Failed to fetch'), { code: 'network', status: 0 }));
    await f.surface.refresh(true);
    expect(f.canvas.querySelector('.coordinator-progress')).toBe(board);
    expect(f.canvas.querySelector('.coordination-read-error')).toBeNull();
    vi.mocked(f.ports.binding).mockRejectedValueOnce(Object.assign(new Error('Forbidden'), { code: 'forbidden', status: 403 }));
    await f.surface.refresh(true);
    expect(f.canvas.querySelector('.coordinator-progress')).toBe(board);
    expect(f.canvas.querySelector('.coordination-read-error[role=status]')?.textContent).toContain('projectCoordination.error · Forbidden');
  } finally { warning.mockRestore(); f.surface.destroy(); }
});
it('repaints the canvas from settled project reads on a same-project switch, never leaving it blank meanwhile', async () => {
  const f = fixture('canvas');
  const changed = vi.fn();
  const surface = createProjectCoordinationSurface({ sidebar: f.sidebar, canvas: f.canvas, mode: 'canvas',
    scope: () => ({ ...f.ports, currentConversationId: f.ports.currentConversation!() }),
    sidebarPath: () => '/project', details: f.details, create: f.create, changed });
  f.surface.destroy();
  f.ports.conversations = () => [{ id: 'worker', title: 'Worker', turnCount: 2, coordinatorId: 'leader' },
    { id: 'worker-2', title: 'Second worker', turnCount: 1, coordinatorId: 'leader' }];
  f.conversation('worker'); await surface.refresh();
  expect(f.canvas.textContent).toContain('projectCoordination.returnCoordinator');
  changed.mockClear();
  // The strip above the transcript keeps its geometry while fresh reads stall.
  vi.mocked(f.ports.binding).mockImplementationOnce(() => new Promise(() => {}));
  f.conversation('worker-2'); void surface.refresh();
  expect(f.canvas.textContent).toContain('projectCoordination.returnCoordinator');
  f.canvas.querySelector('button')!.click();
  expect(f.ports.navigate).toHaveBeenCalledWith('leader');
  expect(changed).not.toHaveBeenCalled();
  // An unrelated chat drops the route at once rather than after its reads.
  f.conversation('ordinary'); void surface.refresh();
  expect(f.canvas.childElementCount).toBe(0);
  await vi.waitFor(() => expect(f.ports.binding).toHaveBeenCalledTimes(3));
  // The board belongs to the binding: arriving at the Coordinator shows it at
  // once, never a worker's route, and identical fresh reads keep the same node.
  f.conversation('worker'); await surface.refresh();
  f.conversation('leader'); const coordinator = surface.refresh();
  const board = f.canvas.querySelector('.coordinator-progress');
  expect(board?.textContent).toContain('Research New Year homestays');
  expect(f.canvas.textContent).not.toContain('projectCoordination.returnCoordinator');
  await coordinator;
  expect(f.canvas.querySelector('.coordinator-progress')).toBe(board);
  // The board never follows back into a worker chat.
  f.conversation('worker'); void surface.refresh();
  expect(f.canvas.querySelector('.coordinator-progress')).toBeNull();
  surface.destroy();
});
it("names the shown project's bound Coordinator only from that project's settled binding read", async () => {
  const f = fixture('canvas');
  expect(f.surface.coordinatorId()).toBe('');
  await f.surface.refresh();
  expect(f.surface.coordinatorId()).toBe('leader');
  // The binding is a project fact: a chat switch inside the project keeps it.
  f.conversation('worker'); await f.surface.refresh();
  expect(f.surface.coordinatorId()).toBe('leader');
  const binding = await f.ports.binding('/project');
  let finish!: (value: typeof binding) => void;
  vi.mocked(f.ports.binding).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  f.path('/other'); const flight = f.surface.refresh();
  expect(f.surface.coordinatorId()).toBe('');
  finish({ ...binding, projectPath: '/other', conversationId: 'other-leader' }); await flight;
  expect(f.surface.coordinatorId()).toBe('other-leader');
  vi.mocked(f.ports.binding).mockResolvedValueOnce({ ...binding, projectPath: '/other', conversationId: '', availability: 'unbound' });
  await f.surface.refresh(true);
  expect(f.surface.coordinatorId()).toBe('');
  f.surface.destroy();
  expect(f.surface.coordinatorId()).toBe('');
});
