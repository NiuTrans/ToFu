// @vitest-environment jsdom
import { expect, it, vi } from 'vitest';
import { coordinatorProgressHeadline, coordinatorProgressTally, createCoordinatorProgress, type CoordinatorProgressPorts } from './coordinator-progress';
import type { CoordinatorProgressBoard } from '../api/project-coordination.generated';
vi.mock('../i18n', () => ({ t: (key: string, args?: Record<string, unknown>) => (args ? `${key}:${JSON.stringify(args)}` : key) }));

const board: CoordinatorProgressBoard = {
  revision: 2, updatedAt: Date.UTC(2026, 10, 2, 9, 5), summary: 'Two tasks moving; one finished.',
  tasks: [
    { title: 'Summarize the article', status: 'pending' },
    { title: 'Fix the folder dialog', status: 'completed', progress: 'Merged and verified in the browser.' },
    { title: 'Research homestays', status: 'in_progress', progress: 'Four platforms compared.' },
    { title: 'Old spike', status: 'cancelled', progress: 'Dropped at your request.' },
    { title: 'Stop the page flicker', status: 'in_progress' },
  ],
};

function ports(overrides: Partial<CoordinatorProgressPorts> = {}): CoordinatorProgressPorts {
  return { open: true, finishedOpen: false, toggle: vi.fn(), toggleFinished: vi.fn(), manage: vi.fn(), retry: vi.fn(),
    now: () => Date.UTC(2026, 10, 2, 12, 0), ...overrides };
}

it('renders the human summary first and lists in-progress work before upcoming, keeping finished work folded', () => {
  const card = createCoordinatorProgress({ kind: 'board', board }, ports());
  expect(card.dataset.state).toBe('board');
  expect(card.querySelector('.coordinator-progress-summary')?.textContent).toBe(board.summary);
  expect(card.querySelector('.coordinator-progress-tally')?.textContent).toBe('coordinatorProgress.sidebar.active:{"count":2}');
  const open = [...card.querySelectorAll(':scope > .coordinator-progress-body > .coordinator-progress-tasks > li')];
  expect(open.map(row => [row.getAttribute('data-status'), row.querySelector('strong')?.textContent])).toEqual([
    ['in_progress', 'Research homestays'], ['in_progress', 'Stop the page flicker'], ['pending', 'Summarize the article']]);
  expect(open[0].querySelector('small')?.textContent).toBe('Four platforms compared.');
  expect(open[1].querySelector('small')).toBeNull();
  const fold = card.querySelector<HTMLDetailsElement>('.coordinator-progress-finished')!;
  expect(fold.open).toBe(false);
  expect(fold.querySelector('summary')?.textContent).toBe('coordinatorProgress.finished:{"count":2}');
  expect([...fold.querySelectorAll('strong')].map(node => node.textContent)).toEqual(['Fix the folder dialog', 'Old spike']);
  expect(card.querySelector('time')?.getAttribute('datetime')).toBe(new Date(board.updatedAt).toISOString());
  // No session, worker, reference, usage or question vocabulary ever reaches the board.
  expect(card.textContent).not.toMatch(/projectCoordination\.(workList|dependencyGraph|report)|coordinatorBudget|coordinatorPlan/);
});

it('reports fold changes and offers only explicit management', () => {
  const p = ports();
  const card = createCoordinatorProgress({ kind: 'board', board }, p) as HTMLDetailsElement;
  card.open = false; card.dispatchEvent(new Event('toggle'));
  expect(p.toggle).toHaveBeenCalledWith(false);
  const fold = card.querySelector<HTMLDetailsElement>('.coordinator-progress-finished')!;
  fold.open = true; fold.dispatchEvent(new Event('toggle'));
  expect(p.toggleFinished).toHaveBeenCalledWith(true);
  const buttons = [...card.querySelectorAll('button')];
  expect(buttons.map(button => button.textContent)).toEqual(['projectCoordination.controls']);
  buttons[0].click();
  expect(p.manage).toHaveBeenCalledOnce();
});

it('distinguishes an empty board from an unavailable one and retries only on request', () => {
  const empty = createCoordinatorProgress({ kind: 'empty' }, ports());
  expect(empty.dataset.state).toBe('empty');
  expect(empty.textContent).toContain('coordinatorProgress.empty');
  const p = ports();
  const unavailable = createCoordinatorProgress({ kind: 'unavailable' }, p);
  expect(unavailable.querySelector('[role="status"]')?.textContent).toBe('coordinatorProgress.unavailable');
  expect(p.retry).not.toHaveBeenCalled();
  [...unavailable.querySelectorAll('button')].find(button => button.textContent === 'projectCoordination.retry')!.click();
  expect(p.retry).toHaveBeenCalledOnce();
});

it('summarizes the board for the sidebar without claiming progress the board does not state', () => {
  expect(coordinatorProgressTally(board)).toEqual({ active: 2, pending: 1, finished: 2, total: 5 });
  expect(coordinatorProgressHeadline(board)).toBe('coordinatorProgress.sidebar.active:{"count":2}');
  const done = { ...board, tasks: board.tasks.filter(task => task.status === 'completed' || task.status === 'cancelled') };
  expect(coordinatorProgressHeadline(done)).toBe('coordinatorProgress.sidebar.allDone');
  expect(coordinatorProgressHeadline({ ...board, tasks: [{ title: 'Next', status: 'pending' }] })).toBeNull();
  expect(coordinatorProgressHeadline({ ...board, tasks: [] })).toBeNull();
  expect(coordinatorProgressHeadline(null)).toBeNull();
});
