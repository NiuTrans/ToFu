// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest';
import { createMyDayGoalBoard, type MyDayGoalApi } from './goal-board';
import type { MyDayGoal, MyDayGoalItem } from './goal-model';

const NOW = new Date(2026, 10, 8, 15, 0, 0);
const TODAY_MS = NOW.getTime();
const YESTERDAY_MS = TODAY_MS - 24 * 60 * 60 * 1000;

function item(id: string, text: string, overrides: Partial<MyDayGoalItem> = {}): MyDayGoalItem {
  return {
    id, text, origin: 'owner', status: 'pending', manual: false, counted: true,
    createdAt: YESTERDAY_MS, statusChangedAt: YESTERDAY_MS, startedAt: null, completedAt: null,
    ...overrides,
  };
}

function goal(overrides: Partial<MyDayGoal> = {}): MyDayGoal {
  return {
    id: 'g-0000000000000001', text: '把 My Day 改成目标追踪', origin: 'owner', state: 'active',
    createdAt: YESTERDAY_MS, updatedAt: YESTERDAY_MS, archivedAt: null, decomposedAt: YESTERDAY_MS,
    decomposing: false, link: null, request: null,
    items: [
      item('i-0000000000000001', '写设计', { status: 'completed', manual: true, completedAt: TODAY_MS }),
      item('i-0000000000000002', '补测试', { status: 'in_progress', manual: true, startedAt: YESTERDAY_MS }),
      item('a-0000000000000003', '浏览器验证', { origin: 'agent', proposal: 'pending', counted: false }),
      item('w-0000000000000004', '[DB] measure load', {
        origin: 'evidence', status: 'in_progress', createdAt: null, statusChangedAt: null,
        evidence: { assignmentId: 'a4', conversationId: 'worker-a4', review: null, state: 'in_progress',
          reason: 'running' },
      }),
    ],
    progress: { counted: 3, completed: 1, inProgress: 2, pending: 0, suggested: 1, percent: 33 },
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((ok, fail) => { resolve = ok; reject = fail; });
  return { promise, resolve, reject };
}

function setup(api: Partial<MyDayGoalApi>) {
  document.body.innerHTML = '<div id="mydayProgress"></div><div id="mydayTasks"></div><div id="mydayStatsBar"></div>';
  const calls = { calendar: 0, today: 0 };
  const full: MyDayGoalApi = {
    goals: vi.fn(async () => ({ goals: [goal()] })),
    goalCreate: vi.fn(), goalUpdate: vi.fn(), goalItemAdd: vi.fn(), goalItemUpdate: vi.fn(),
    goalItemRemove: vi.fn(), goalSuggestions: vi.fn(), goalDecompose: vi.fn(), goalLink: vi.fn(),
    goalUnlink: vi.fn(), goalRequests: vi.fn(async () => ({ requests: [] })),
    report: vi.fn(async () => null),
    ...api,
  } as MyDayGoalApi;
  const board = createMyDayGoalBoard({
    api: full,
    translate: (key, values) => (values ? `${key} ${JSON.stringify(values)}` : key),
    element: (id) => document.getElementById(id),
    renderCalendar: () => { calls.calendar += 1; },
    showToday: () => { calls.today += 1; },
    now: () => NOW,
  });
  const tasks = () => document.getElementById('mydayTasks') as HTMLElement;
  return { board, api: full, calls, tasks };
}

const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('My Day goal board', () => {
  it('renders goals -> items -> progress with provenance kept apart', async () => {
    const { board, tasks } = setup({});
    await board.show('2026-11-08');
    const card = tasks().querySelector('.myday-goal') as HTMLElement;
    expect(card.querySelector('.myday-goal-title')?.textContent).toBe('把 My Day 改成目标追踪');
    expect(card.querySelector('.myday-goal-pct-value')?.textContent).toBe('myday.estimate {"p":33}');
    const groups = Array.from(card.querySelectorAll('.myday-goal-group')).map((node) => node.firstChild?.textContent);
    expect(groups).toEqual(['myday.itemsInProgress', 'myday.itemsCompleted', 'myday.itemsEvidence',
      'myday.itemsSuggested']);
    const suggestion = card.querySelector('.myday-goal-item.suggested') as HTMLElement;
    expect(suggestion.querySelector('.myday-todo-check')).toBeNull();
    expect(suggestion.textContent).toContain('myday.accept');
    expect(card.querySelector('.myday-goal-badge.worker')?.textContent).toBe('myday.reasonRunning');
    expect(card.querySelector('.myday-goal-badge.today')?.textContent).toBe('myday.today');
    expect(card.querySelector('.s-in_progress .myday-goal-badge.since')).not.toBeNull();
    expect(document.getElementById('mydayProgress')?.textContent)
      .toBe('myday.summary {"goals":1,"done":1,"active":2}');
  });

  it('checks an item off optimistically and adopts the server view', async () => {
    const answer = deferred<unknown>();
    const goalItemUpdate = vi.fn(() => answer.promise);
    const { board, api, tasks } = setup({ goalItemUpdate });
    await board.show('2026-11-08');
    const row = Array.from(tasks().querySelectorAll('.myday-goal-item'))
      .find((node) => node.textContent?.includes('补测试')) as HTMLElement;
    (row.querySelector('.myday-todo-check') as HTMLButtonElement).click();
    await flush();
    expect(goalItemUpdate).toHaveBeenCalledWith('g-0000000000000001', 'i-0000000000000002', { status: 'completed' });
    const optimistic = Array.from(tasks().querySelectorAll('.myday-goal-item.done')).map((node) => node.textContent);
    expect(optimistic.some((text) => text?.includes('补测试'))).toBe(true);
    const server = goal();
    server.items[1] = { ...server.items[1], status: 'completed', completedAt: TODAY_MS };
    answer.resolve({ goal: { ...server, progress: { ...server.progress, completed: 2, inProgress: 1, percent: 66 } } });
    await flush();
    expect(tasks().querySelector('.myday-goal-pct-value')?.textContent).toBe('myday.estimate {"p":66}');
    expect(api.goals).toHaveBeenCalledTimes(1);
  });

  it('shares one load between opens and never lets a stale load undo a later change', async () => {
    const load = deferred<unknown>();
    const goals = vi.fn(() => load.promise);
    const goalItemUpdate = vi.fn(async () => {
      const changed = goal();
      changed.items[1] = { ...changed.items[1], status: 'completed', completedAt: TODAY_MS };
      return { goal: changed };
    });
    const { board, tasks } = setup({ goals, goalItemUpdate });
    const first = board.show('2026-11-08');
    const second = board.refresh();
    expect(goals).toHaveBeenCalledTimes(1);
    load.resolve({ goals: [goal()] });
    await Promise.all([first, second]);

    const stale = deferred<unknown>();
    goals.mockImplementation(() => stale.promise);
    const refreshing = board.refresh();
    const row = Array.from(tasks().querySelectorAll('.myday-goal-item'))
      .find((node) => node.textContent?.includes('补测试')) as HTMLElement;
    (row.querySelector('.myday-todo-check') as HTMLButtonElement).click();
    await flush();
    await flush();
    stale.resolve({ goals: [goal()] });  // read before the check-off
    await refreshing;
    const done = Array.from(tasks().querySelectorAll('.myday-goal-item.done')).map((node) => node.textContent);
    expect(done.some((text) => text?.includes('补测试'))).toBe(true);
  });

  it('joins a decomposition the server is already running, once', async () => {
    const join = deferred<unknown>();
    const goalDecompose = vi.fn(() => join.promise);
    const goals = vi.fn(async () => ({ goals: [goal({ decomposing: true })] }));
    const { board, tasks } = setup({ goals, goalDecompose });
    await board.show('2026-11-08');
    await board.refresh();  // a reload while the run continues joins nothing new
    expect(goalDecompose).toHaveBeenCalledTimes(1);
    expect(tasks().querySelector('.myday-goal-working')).not.toBeNull();
    join.resolve({ goal: goal({ decomposing: false }) });
    await flush();
    expect(tasks().querySelector('.myday-goal-working')).toBeNull();
  });

  it('creates a goal from the composer and decomposes it', async () => {
    const created = goal({ id: 'g-0000000000000002', text: 'Run three times', items: [],
      progress: { counted: 0, completed: 0, inProgress: 0, pending: 0, suggested: 0, percent: null } });
    const goalCreate = vi.fn(async () => ({ goal: created }));
    const goalDecompose = vi.fn(async () => ({ goal: created }));
    const { board, tasks } = setup({ goals: vi.fn(async () => ({ goals: [] })), goalCreate, goalDecompose });
    await board.show('2026-11-08');
    expect(tasks().querySelector('.myday-empty')).not.toBeNull();
    const input = tasks().querySelector('.myday-goal-composer input') as HTMLInputElement;
    input.value = '  Run three times  ';
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }));
    await flush();
    await flush();
    expect(goalCreate).toHaveBeenCalledWith('Run three times');
    expect(goalDecompose).toHaveBeenCalledWith('g-0000000000000002');
    expect(tasks().querySelector('.myday-goal-pct-value')?.textContent).toBe('—');
  });

  it('shows a past day as its goal progress plus the archived report, read-only', async () => {
    const report = vi.fn(async () => ({ streams: [{ title: 'Old stream', status: 'done', summary: 'shipped' }],
      tomorrow: [{ text: 'Old todo', done: false }] }));
    const yesterday = new Date(YESTERDAY_MS);
    const date = `${yesterday.getFullYear()}-${String(yesterday.getMonth() + 1).padStart(2, '0')}-${String(yesterday.getDate()).padStart(2, '0')}`;
    const withYesterday = goal();
    withYesterday.items[0] = { ...withYesterday.items[0], completedAt: YESTERDAY_MS };
    const { board, api, calls, tasks } = setup({ goals: vi.fn(async () => ({ goals: [withYesterday] })), report });
    await board.show(date);
    await flush();
    expect(report).toHaveBeenCalledWith(date);
    const text = tasks().textContent ?? '';
    expect(text).toContain('写设计');
    expect(text).toContain('Old stream');
    expect(text).toContain('Old todo');
    expect(tasks().querySelectorAll('button.myday-todo-check')).toHaveLength(0);
    expect(document.getElementById('mydayProgress')?.textContent).toBe('myday.recapDone {"n":1}');
    (tasks().querySelector('.myday-goal-today') as HTMLButtonElement).click();
    expect(calls.today).toBe(1);
    expect(api.goalItemUpdate).not.toHaveBeenCalled();
  });
});
