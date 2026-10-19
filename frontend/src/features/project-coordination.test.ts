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
  create.click();
  await vi.waitFor(() => expect(p.create).toHaveBeenCalledWith('/project', 1));
  expect(p.bind).not.toHaveBeenCalled();
  document.querySelector('dialog')!.close(); await run;
});
