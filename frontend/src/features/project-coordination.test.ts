// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest';
vi.mock('../i18n', () => ({ t: (key: string) => key }));
import { openProjectCoordination, projectCoordinationWorkGroups, type ProjectCoordinationPorts } from './project-coordination';

beforeEach(() => {
  document.body.replaceChildren();
  HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new Event('close')); };
});
function ports(): ProjectCoordinationPorts {
  return {
    path: '/project', currentConversationId: 'worker', currentPath: () => '/project',
    binding: vi.fn(async () => ({ projectPath: '/project', conversationId: 'coord', revision: 1,
      availability: 'available' as const, capabilities: { assign: false as const, sendMessage: false as const, steerSubagent: false as const } })),
    board: vi.fn(async () => ({ active: [{ id: 'work', conversationId: 'worker', title: '<unsafe>', status: 'active' as const }],
      recentOutcomes: [{ id: 'done', title: 'Done', status: 'completed' as const }] })),
    bind: vi.fn(), navigate: vi.fn(), openEvidence: vi.fn(),
  };
}
async function rendered() {
  await vi.waitFor(() => expect(document.body.textContent).toContain('projectCoordination.openCoordinator'));
}
describe('project workspace', () => {
  it('projects actual status without treating completion as delivery', () => {
    const groups = projectCoordinationWorkGroups([{ id: 'bad', status: 'completed' }], [{ id: 'bad', status: 'active' }]);
    expect(groups).toEqual({ active: [], recent: [] });
  });
  it('escapes text and navigates in the ordinary canvas', async () => {
    const p = ports(); const run = openProjectCoordination(p);
    await rendered();
    expect(document.body.textContent).toContain('<unsafe>');
    expect(document.body.querySelector('unsafe')).toBeNull();
    expect(document.body.textContent).toContain('projectCoordination.deliveryCaveat');
    const button = [...document.querySelectorAll('button')].find(n => n.textContent === 'projectCoordination.openWorker')!;
    button.click(); await run;
    expect(p.navigate).toHaveBeenCalledWith('worker');
    expect(document.querySelector('dialog')).toBeNull();
  });
  it('closes a stale project response without rendering another scope', async () => {
    const p = ports(); let resolve!: (value: Awaited<ReturnType<typeof p.binding>>) => void;
    const value = await p.binding('/project');
    p.binding = () => new Promise(r => { resolve = r; });
    const run = openProjectCoordination(p);
    p.currentPath = () => '/other'; resolve(value); await run;
    expect(document.querySelector('dialog')).toBeNull();
    expect(p.navigate).not.toHaveBeenCalled();
  });
  it('shows an unavailable board instead of a fabricated empty roster', async () => {
    const p = ports(); p.board = async () => null;
    const run = openProjectCoordination(p); await rendered();
    expect(document.body.textContent).toContain('projectCoordination.boardUnavailable');
    expect(document.querySelector('[role=tabpanel]:not([hidden])')?.textContent).not.toContain('projectCoordination.empty');
    document.querySelector('dialog')!.close(); await run;
  });
});

it('shows project teammates even before their first work record and keeps controls demand-only', async () => {
  const p = ports();
  p.conversations = () => [{ id: 'idle-worker', title: 'Available teammate' }];
  p.messages = vi.fn(async () => ({ messages: [], next_before_id: null }));
  const dispose = vi.fn(); p.subscribe = vi.fn(() => dispose);
  const run = openProjectCoordination(p); await rendered();
  expect(p.messages).not.toHaveBeenCalled();
  const team = document.querySelectorAll<HTMLButtonElement>('[role=tab]')[1]; team.click();
  const visible = document.querySelector<HTMLElement>('[role=tabpanel]:not([hidden])')!;
  expect(visible.textContent).toContain('Available teammate');
  visible.querySelector('button')!.click(); await run;
  expect(p.navigate).toHaveBeenCalledWith('idle-worker');
  expect(dispose).toHaveBeenCalledTimes(1);
});

it('keeps a coordinator entrance when the work endpoint fails', async () => {
  const p = ports(); p.board = vi.fn(async () => { throw new Error('offline'); });
  const run = openProjectCoordination(p); await rendered();
  expect(document.body.textContent).toContain('projectCoordination.boardUnavailable');
  document.querySelector('dialog')!.close(); await run;
});

it('does not replace another tab with a late background overview read', async () => {
  vi.useFakeTimers();
  try {
    const p = ports(); let refresh!: () => void;
    p.subscribe = callback => { refresh = callback; return vi.fn(); };
    const run = openProjectCoordination(p);
    await vi.advanceTimersByTimeAsync(0);
    let resolve!: (value: Awaited<ReturnType<typeof p.board>>) => void;
    p.board = () => new Promise(r => { resolve = r; });
    refresh(); await vi.advanceTimersByTimeAsync(300);
    document.querySelectorAll<HTMLButtonElement>('[role=tab]')[1].click();
    const originalTeam = document.querySelector('[role=tabpanel]:not([hidden])');
    resolve({ active: [], recentOutcomes: [] }); await vi.advanceTimersByTimeAsync(0);
    expect(document.querySelector('[role=tabpanel]:not([hidden])')).toBe(originalTeam);
    p.board = vi.fn(async () => ({ active: [{ id: 'latest', title: 'Latest work', status: 'active' as const }], recentOutcomes: [] }));
    document.querySelectorAll<HTMLButtonElement>('[role=tab]')[0].click();
    await vi.advanceTimersByTimeAsync(300);
    expect(p.board).toHaveBeenCalledTimes(1);
    expect(document.querySelector('[role=tabpanel]:not([hidden])')?.textContent).toContain('Latest work');
    document.querySelector('dialog')!.close(); await run;
  } finally { vi.useRealTimers(); }
});

it('remembers events outside the overview and preserves the mounted message view on return', async () => {
  vi.useFakeTimers();
  try {
    const p = ports(); let refresh!: () => void;
    p.subscribe = callback => { refresh = callback; return vi.fn(); };
    const run = openProjectCoordination(p); await vi.advanceTimersByTimeAsync(0);
    const tabs = document.querySelectorAll<HTMLButtonElement>('[role=tab]');
    tabs[2].click(); const messages = document.querySelector('[role=tabpanel]:not([hidden])');
    vi.mocked(p.board).mockResolvedValue({ active: [{ id: 'new', title: 'New work', status: 'active' }], recentOutcomes: [] });
    refresh(); refresh(); await vi.advanceTimersByTimeAsync(1000);
    expect(p.board).toHaveBeenCalledTimes(1);
    tabs[0].click(); await vi.advanceTimersByTimeAsync(300);
    expect(p.board).toHaveBeenCalledTimes(2);
    expect(document.querySelector('[role=tabpanel]:not([hidden])')?.textContent).toContain('New work');
    tabs[2].click(); expect(document.querySelector('[role=tabpanel]:not([hidden])')).toBe(messages);
    document.querySelector('dialog')!.close(); await run;
  } finally { vi.useRealTimers(); }
});

it('bounds refresh concurrency without starving a continuous event stream and disposes pending demand', async () => {
  vi.useFakeTimers();
  try {
    const p = ports(); let refresh!: () => void;
    const unsubscribe = vi.fn(); p.subscribe = callback => { refresh = callback; return unsubscribe; };
    const run = openProjectCoordination(p); await vi.advanceTimersByTimeAsync(0);
    let resolve!: (value: Awaited<ReturnType<typeof p.board>>) => void;
    vi.mocked(p.board).mockImplementationOnce(() => new Promise(r => { resolve = r; }));
    for (let i = 0; i < 10; i++) { refresh(); await vi.advanceTimersByTimeAsync(100); }
    expect(p.board).toHaveBeenCalledTimes(2); // One initial read plus one slow refresh.
    resolve({ active: [], recentOutcomes: [] }); await vi.advanceTimersByTimeAsync(0);
    await vi.advanceTimersByTimeAsync(300);
    expect(p.board).toHaveBeenCalledTimes(3); // All events during the read share one follow-up.
    refresh(); document.querySelector('dialog')!.close(); await run;
    await vi.advanceTimersByTimeAsync(1000); refresh(); await vi.advanceTimersByTimeAsync(1000);
    expect(p.board).toHaveBeenCalledTimes(3);
    expect(unsubscribe).toHaveBeenCalledTimes(1);
  } finally { vi.useRealTimers(); }
});

it('retains usable content and keyboard focus across background failures, retries and changed work', async () => {
  vi.useFakeTimers();
  try {
    const p = ports(); let refresh!: () => void;
    p.subscribe = callback => { refresh = callback; return vi.fn(); };
    const run = openProjectCoordination(p); await vi.advanceTimersByTimeAsync(0);
    const worker = document.querySelector<HTMLButtonElement>('[data-coordination-focus]')!;
    vi.mocked(p.board).mockResolvedValueOnce({
      active: [Object.assign({ id: 'work', conversationId: 'worker', title: '<unsafe>', status: 'active' as const }, { observedAt: 123 })],
      recentOutcomes: [{ id: 'done', title: 'Done', status: 'completed' }],
    });
    worker.focus(); refresh(); await vi.advanceTimersByTimeAsync(300);
    expect(document.activeElement).toBe(worker); // Identical facts do not replace DOM.
    const tabs = document.querySelector('[role=tablist]');
    vi.mocked(p.binding).mockRejectedValueOnce(new Error('temporarily offline'));
    refresh(); await vi.advanceTimersByTimeAsync(300);
    expect(document.querySelector('[role=tablist]')).toBe(tabs);
    expect(document.activeElement).toBe(worker);
    expect(document.body.textContent).toContain('temporarily offline');
    const retry = [...document.querySelectorAll('button')].find(button => button.textContent === 'projectCoordination.retry')!;
    retry.click(); await vi.advanceTimersByTimeAsync(0);
    expect(document.body.textContent).not.toContain('temporarily offline');
    vi.mocked(p.board).mockResolvedValue({ active: [{ id: 'work', conversationId: 'worker', title: 'Updated', status: 'active' }], recentOutcomes: [] });
    worker.focus(); refresh(); await vi.advanceTimersByTimeAsync(300);
    expect(document.activeElement?.getAttribute('data-coordination-focus')).toBe(worker.dataset.coordinationFocus);
    expect(document.body.textContent).toContain('Updated');
    document.querySelector('dialog')!.close(); await run;
  } finally { vi.useRealTimers(); }
});

it('does not release an in-flight refresh when binding rejects before the other reads settle', async () => {
  vi.useFakeTimers();
  try {
    const p = ports(); let refresh!: () => void;
    p.subscribe = callback => { refresh = callback; return vi.fn(); };
    const run = openProjectCoordination(p); await vi.advanceTimersByTimeAsync(0);
    let resolve!: (value: Awaited<ReturnType<typeof p.board>>) => void;
    vi.mocked(p.board).mockImplementationOnce(() => new Promise(r => { resolve = r; }));
    vi.mocked(p.binding).mockRejectedValueOnce(new Error('early failure'));
    refresh(); await vi.advanceTimersByTimeAsync(300);
    refresh(); await vi.advanceTimersByTimeAsync(1000);
    expect(p.board).toHaveBeenCalledTimes(2);
    resolve(null); await vi.advanceTimersByTimeAsync(0);
    expect(document.body.textContent).toContain('early failure');
    await vi.advanceTimersByTimeAsync(300);
    expect(p.board).toHaveBeenCalledTimes(3);
    expect(document.body.textContent).not.toContain('early failure');
    document.querySelector('dialog')!.close(); await run;
  } finally { vi.useRealTimers(); }
});

it('creates an empty replacement explicitly instead of offering historical conversation binding', async () => {
  const p = ports();
  p.conversations = () => [{ id: 'old-worker', title: 'Old worker conversation' }];
  p.create = vi.fn(async () => ({ projectPath: '/project', conversationId: 'fresh', revision: 2,
    availability: 'available' as const, capabilities: { assign: false as const, sendMessage: false as const, steerSubagent: false as const } }));
  const run = openProjectCoordination(p); await rendered();
  expect(p.create).not.toHaveBeenCalled();
  expect(document.querySelector('.coordination-entry')?.textContent).not.toContain('Old worker conversation');
  const create = [...document.querySelectorAll('button')].find(button => button.textContent === 'projectCoordination.replace')!;
  document.querySelectorAll<HTMLButtonElement>('[role=tab]')[4].click();
  create.click();
  expect(p.create).not.toHaveBeenCalled();
  const confirmation = document.querySelectorAll('dialog')[1];
  expect(confirmation.textContent).toContain('projectCoordination.replacement.impact');
  [...confirmation.querySelectorAll('button')].find(button => button.textContent === 'projectCoordination.replacement.confirm')!.click();
  await vi.waitFor(() => expect(p.create).toHaveBeenCalledWith('/project', 1));
  expect(p.bind).not.toHaveBeenCalled();
  document.querySelector('dialog')!.close(); await run;
});

const buttonNamed = (label: string, root: ParentNode = document) => [...root.querySelectorAll<HTMLButtonElement>('button')].find(button => button.textContent === label)!;

it('keeps replacement in Manage, cancels safely, and fences a binding changed during confirmation', async () => {
  const p = ports(); p.create = vi.fn();
  const run = openProjectCoordination(p); await rendered();
  expect(document.querySelector('[role=tabpanel]:not([hidden])')?.textContent).not.toContain('projectCoordination.replace');
  document.querySelectorAll<HTMLButtonElement>('[role=tab]')[4].click();
  buttonNamed('projectCoordination.replace').click();
  buttonNamed('projectCoordination.replacement.cancel').click();
  await vi.waitFor(() => expect(document.querySelectorAll('dialog')).toHaveLength(1));
  expect(p.create).not.toHaveBeenCalled();
  buttonNamed('projectCoordination.replace').click();
  const old = await p.binding('/project');
  vi.mocked(p.binding).mockResolvedValue({ ...old, revision: 2, conversationId: 'someone-else' });
  buttonNamed('projectCoordination.replacement.confirm').click();
  await vi.waitFor(() => expect(document.body.textContent).toContain('projectCoordination.replacement.changed'));
  expect(p.create).not.toHaveBeenCalled();
  expect(buttonNamed('projectCoordination.replace').disabled).toBe(true);
  document.querySelector('dialog')!.close(); await run;
});

it('reads replacement status after an uncertain response instead of repeating the write', async () => {
  const p = ports(); p.create = vi.fn(async () => { throw new Error('response lost'); });
  const run = openProjectCoordination(p); await rendered();
  document.querySelectorAll<HTMLButtonElement>('[role=tab]')[4].click();
  buttonNamed('projectCoordination.replace').click(); buttonNamed('projectCoordination.replacement.confirm').click();
  await vi.waitFor(() => expect(document.body.textContent).toContain('response lost'));
  expect(p.create).toHaveBeenCalledTimes(1);
  buttonNamed('projectCoordination.retry').click();
  await vi.waitFor(() => expect(buttonNamed('projectCoordination.replace').disabled).toBe(false));
  expect(p.create).toHaveBeenCalledTimes(1); expect(p.bind).not.toHaveBeenCalled();
  expect(document.body.textContent).toContain('projectCoordination.context.unavailable');
  document.querySelector('dialog')!.close(); await run;
});

it('keeps authorization form drafts, disclosures and focus mounted during manual refresh', async () => {
  const p = ports(); p.currentConversationId = 'coord';
  p.dispatch = { recipients: () => [], humanTurns: () => [], commandId: () => 'command',
    list: vi.fn(async () => ({ assignments: [], olderCursor: null, newerCursor: null })) } as unknown as NonNullable<ProjectCoordinationPorts['dispatch']>;
  const run = openProjectCoordination(p); await rendered();
  document.querySelectorAll<HTMLButtonElement>('[role=tab]')[4].click();
  const field = document.querySelector<HTMLInputElement>('input[name="brief"]')!;
  field.value = 'Keep this human draft'; field.focus();
  const disclosure = field.closest('details')!; disclosure.open = true;
  const tabs = document.querySelector('[role=tablist]');
  buttonNamed('projectCoordination.refresh').click();
  await vi.waitFor(() => expect(p.board).toHaveBeenCalledTimes(2));
  expect(document.querySelector('[role=tablist]')).toBe(tabs);
  expect(document.querySelector('input[name="brief"]')).toBe(field);
  expect(field.value).toBe('Keep this human draft'); expect(disclosure.open).toBe(true);
  expect(document.activeElement).toBe(field);
  document.querySelector('dialog')!.close(); await run;
});

it('pages an empty matching roster page without hiding its next-page action and includes assignment recipients', async () => {
  const p = ports();
  p.workerConversations = vi.fn(async (_path, cursor) => cursor
    ? { conversations: [{ conversationId: 'new-shell', title: 'Not yet started', turnCount: 0, coordinatorId: 'coord' }], nextCursor: null, coverage: 'complete' as const }
    : { conversations: [], nextCursor: 'next-page', coverage: 'page' as const });
  const run = openProjectCoordination(p); await rendered();
  document.querySelectorAll<HTMLButtonElement>('[role=tab]')[1].click();
  const panel = document.querySelector<HTMLElement>('[role=tabpanel]:not([hidden])')!;
  expect(panel.textContent).toContain('<unsafe>'); // Board-only conversation remains reachable.
  buttonNamed('projectCoordination.roster.more', panel).click();
  await vi.waitFor(() => expect(p.workerConversations).toHaveBeenLastCalledWith('/project', 'next-page'));
  await vi.waitFor(() => expect(panel.textContent).toContain('Not yet started'));
  expect(panel.textContent).toContain('projectCoordination.shell.not_started');
  expect(panel.textContent).not.toContain('projectCoordination.graph.status.queued');
  document.querySelector('dialog')!.close(); await run;
});

it('keeps Overview and hidden Team reference disclosure and focus independent after refresh', async () => {
  vi.useFakeTimers();
  try {
    const p = ports(); let refresh!: () => void;
    p.conversations = () => [{ id: 'worker', title: 'Worker conversation' }];
    p.subscribe = callback => { refresh = callback; return vi.fn(); };
    const run = openProjectCoordination(p); await vi.advanceTimersByTimeAsync(0);
    const panels = document.querySelectorAll<HTMLElement>('[role=tabpanel]');
    const overview = panels[0], team = panels[1];
    const reference = overview.querySelector<HTMLDetailsElement>('article details')!;
    const teamReference = team.querySelector<HTMLDetailsElement>('article details')!;
    reference.open = true;
    reference.querySelector('summary')!.focus();
    const focus = (document.activeElement as HTMLElement).dataset.coordinationFocus;
    expect(teamReference.open).toBe(false);
    vi.mocked(p.board).mockResolvedValue({ active: [{ id: 'work', conversationId: 'worker', title: 'Updated work', status: 'active' }], recentOutcomes: [] });
    refresh(); await vi.advanceTimersByTimeAsync(300);
    expect(overview.querySelector<HTMLDetailsElement>('article details')!.open).toBe(true);
    expect(team.querySelector<HTMLDetailsElement>('article details')!.open).toBe(false);
    expect((document.activeElement as HTMLElement).dataset.coordinationFocus).toBe(focus);
    expect(overview.contains(document.activeElement)).toBe(true);
    document.querySelector('dialog')!.close(); await run;
  } finally { vi.useRealTimers(); }
});
