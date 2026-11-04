// @vitest-environment jsdom
import { expect, it, vi } from 'vitest';
import {
  coordinatorProgressHeadline, coordinatorProgressTally, coordinatorProgressText, createCoordinatorProgress,
  type CoordinatorProgressActions, type CoordinatorProgressPorts,
} from './coordinator-progress';
import type { CoordinatorProgressBoard, CoordinatorProgressTranslation } from '../api/project-coordination.generated';
vi.mock('../i18n', () => ({ t: (key: string, args?: Record<string, unknown>) => (args ? `${key}:${JSON.stringify(args)}` : key) }));

const board: CoordinatorProgressBoard = {
  revision: 2, updatedAt: Date.UTC(2026, 10, 2, 9, 5), summary: 'Two tasks moving; one finished.',
  tasks: [
    { title: 'Summarize the article', status: 'pending', progress: 'Waiting for the link.' },
    { title: 'Fix the folder dialog', status: 'completed', progress: 'Merged and verified in the browser.' },
    { title: 'Research homestays', status: 'in_progress', progress: 'Four platforms compared.' },
    { title: 'Old spike', status: 'cancelled', progress: 'Dropped at your request.' },
    { title: 'Stop the page flicker', status: 'in_progress' },
  ],
};

const translation: CoordinatorProgressTranslation = { target: 'Chinese', texts: [
  { source: 'Two tasks moving; one finished.', text: '两项进行中，一项已完成。' },
  { source: 'Research homestays', text: '调研民宿' },
  { source: 'Four platforms compared.', text: '已比较四个平台。' },
  { source: 'Waiting for the link.', text: '等待链接。' },
] };

function ports(overrides: Partial<CoordinatorProgressPorts> = {}): CoordinatorProgressPorts {
  return { open: true, finishedOpen: false, toggle: vi.fn(), toggleFinished: vi.fn(),
    now: () => Date.UTC(2026, 10, 2, 12, 0), ...overrides };
}
function actions(): CoordinatorProgressActions { return { manage: vi.fn(), retry: vi.fn() }; }
const titles = (root: ParentNode) => [...root.querySelectorAll('.coordinator-progress-task-title')].map(node => node.textContent);
const visibleButtons = (root: ParentNode) => [...root.querySelectorAll('button')].filter(button => !button.hidden);

it('lists one line per task behind a status glyph: in-progress work first, finished work folded', () => {
  const { el: card, render } = createCoordinatorProgress(ports());
  render({ kind: 'board', board }, actions());
  expect(card.dataset.state).toBe('board');
  expect(card.querySelector('.coordinator-progress-summary')?.textContent).toBe(board.summary);
  expect(card.querySelector('.coordinator-progress-tally')?.textContent).toBe('coordinatorProgress.sidebar.active:{"count":2}');
  const open = [...card.querySelectorAll(':scope > .coordinator-progress-body > .coordinator-progress-content > .coordinator-progress-tasks > li')];
  expect(open.map(row => [row.getAttribute('data-status'), row.querySelector('.coordinator-progress-task-title')?.textContent])).toEqual([
    ['in_progress', 'Research homestays'], ['in_progress', 'Stop the page flicker'], ['pending', 'Summarize the article']]);
  // Status is a glyph with an accessible name, never a text label in the line.
  expect(open.map(row => row.querySelector('.coordinator-progress-glyph[role="img"]')?.getAttribute('aria-label'))).toEqual([
    'coordinatorProgress.status.in_progress', 'coordinatorProgress.status.in_progress', 'coordinatorProgress.status.pending']);
  expect(open.map(row => row.textContent)).toEqual(['Research homestaysFour platforms compared.', 'Stop the page flicker',
    'Summarize the articleWaiting for the link.']);
  const fold = card.querySelector<HTMLDetailsElement>('.coordinator-progress-finished')!;
  expect(fold.open).toBe(false);
  expect(fold.querySelector('summary')?.textContent).toBe('coordinatorProgress.finished:{"count":2}');
  expect(titles(fold)).toEqual(['Fix the folder dialog', 'Old spike']);
  expect([...fold.querySelectorAll('.coordinator-progress-glyph')].map(glyph => glyph.getAttribute('aria-label')))
    .toEqual(['coordinatorProgress.status.completed', 'coordinatorProgress.status.cancelled']);
  expect(card.querySelector('time')?.getAttribute('datetime')).toBe(new Date(board.updatedAt).toISOString());
  // No session, worker, reference, usage or question vocabulary ever reaches the board.
  expect(card.textContent).not.toMatch(/projectCoordination\.(workList|dependencyGraph|report)|coordinatorBudget|coordinatorPlan/);
});

it('shows only current work\'s note; any other note is its row\'s tooltip', () => {
  const { el: card, render } = createCoordinatorProgress(ports({ finishedOpen: true }));
  render({ kind: 'board', board }, actions());
  const row = (title: string) => [...card.querySelectorAll<HTMLElement>('.coordinator-progress-task')]
    .find(item => item.querySelector('.coordinator-progress-task-title')?.textContent === title)!;
  const current = row('Research homestays');
  expect(current.title).toBe('');
  expect(current.querySelector<HTMLElement>('.coordinator-progress-task-note')?.title).toBe('Four platforms compared.');
  expect(row('Stop the page flicker').querySelector('.coordinator-progress-task-note')).toBeNull();
  for (const [title, note] of [['Summarize the article', 'Waiting for the link.'], ['Fix the folder dialog', 'Merged and verified in the browser.'],
    ['Old spike', 'Dropped at your request.']]) {
    expect(row(title).title).toBe(note);
    expect(row(title).querySelector('.coordinator-progress-task-note')?.textContent).toBe(note);
  }
});

it('shows the board\'s display translation and toggles to the original as written', () => {
  const { el: card, render } = createCoordinatorProgress(ports());
  document.body.replaceChildren(card);
  render({ kind: 'board', board, translation }, actions());
  expect(card.querySelector('.coordinator-progress-summary')?.textContent).toBe('两项进行中，一项已完成。');
  expect(titles(card)).toEqual(['调研民宿', 'Stop the page flicker', 'Summarize the article', 'Fix the folder dialog', 'Old spike']);
  expect(card.querySelector('.coordinator-progress-task-note')?.textContent).toBe('已比较四个平台。');
  const pending = [...card.querySelectorAll<HTMLElement>('.coordinator-progress-task')].find(item => item.dataset.status === 'pending')!;
  expect(pending.title).toBe('等待链接。');
  const toggle = visibleButtons(card).find(button => button.textContent === 'chat.bilingualOriginal')!;
  expect(toggle.getAttribute('aria-pressed')).toBe('false');
  toggle.focus();
  toggle.click();
  expect(card.querySelector('.coordinator-progress-summary')?.textContent).toBe(board.summary);
  expect(titles(card)[0]).toBe('Research homestays');
  expect(toggle.getAttribute('aria-pressed')).toBe('true');
  // The toggle is one stable control: its focus survives its own repaint.
  expect(document.activeElement).toBe(toggle);
  toggle.click();
  expect(titles(card)[0]).toBe('调研民宿');
  // Nothing to switch when the translation changes no text, or there is none.
  render({ kind: 'board', board, translation: { target: 'Chinese', texts: [{ source: 'Old spike', text: 'Old spike' }] } }, actions());
  expect(toggle.hidden).toBe(true);
  render({ kind: 'board', board }, actions());
  expect(toggle.hidden).toBe(true);
  card.remove();
  expect(coordinatorProgressText(translation, 'Research homestays')).toBe('调研民宿');
  expect(coordinatorProgressText(translation, 'Old spike')).toBe('Old spike');
  expect(coordinatorProgressText(undefined, 'Old spike')).toBe('Old spike');
});

it('keeps one card and its footer control attached across renders, binding the latest actions', () => {
  const footer = document.createElement('details');
  footer.className = 'router-worker-model';
  const p = ports({ footer });
  const { el: card, render } = createCoordinatorProgress(p);
  document.body.replaceChildren(card);
  const first = actions();
  render({ kind: 'board', board }, first);
  const commands = card.querySelector('.coordinator-progress-actions')!;
  expect(commands.firstElementChild).toBe(footer);
  const observer = new MutationObserver(() => undefined);
  observer.observe(card, { childList: true, subtree: true });
  const latest = actions();
  render({ kind: 'board', board: { ...board, revision: 3, summary: 'Research done.' } }, latest);
  render({ kind: 'unavailable' }, latest);
  const moved = observer.takeRecords().flatMap(record => [...record.removedNodes, ...record.addedNodes]);
  observer.disconnect();
  expect(moved.some(node => node === footer || node.contains(footer))).toBe(false);
  expect(commands.firstElementChild).toBe(footer);
  visibleButtons(card).find(button => button.textContent === 'projectCoordination.controls')!.click();
  expect(first.manage).not.toHaveBeenCalled();
  expect(latest.manage).toHaveBeenCalledOnce();
  card.remove();
});

it('reports fold changes and offers only explicit management', () => {
  const p = ports();
  const a = actions();
  const { el: card, render } = createCoordinatorProgress(p);
  render({ kind: 'board', board }, a);
  card.open = false; card.dispatchEvent(new Event('toggle'));
  expect(p.toggle).toHaveBeenCalledWith(false);
  const fold = card.querySelector<HTMLDetailsElement>('.coordinator-progress-finished')!;
  fold.open = true; fold.dispatchEvent(new Event('toggle'));
  expect(p.toggleFinished).toHaveBeenCalledWith(true);
  // The finished fold stays as the reader left it across live updates.
  render({ kind: 'board', board: { ...board, revision: 3 } }, a);
  expect(card.querySelector<HTMLDetailsElement>('.coordinator-progress-finished')?.open).toBe(true);
  const buttons = visibleButtons(card);
  expect(buttons.map(button => button.textContent)).toEqual(['projectCoordination.controls']);
  buttons[0].click();
  expect(a.manage).toHaveBeenCalledOnce();
});

it('distinguishes an empty board from an unavailable one and retries only on request', () => {
  const empty = createCoordinatorProgress(ports());
  empty.render({ kind: 'empty' }, actions());
  expect(empty.el.dataset.state).toBe('empty');
  expect(empty.el.textContent).toContain('coordinatorProgress.empty');
  const a = actions();
  const unavailable = createCoordinatorProgress(ports());
  unavailable.render({ kind: 'unavailable' }, a);
  expect(unavailable.el.querySelector('[role="status"]')?.textContent).toBe('coordinatorProgress.unavailable');
  expect(a.retry).not.toHaveBeenCalled();
  visibleButtons(unavailable.el).find(button => button.textContent === 'projectCoordination.retry')!.click();
  expect(a.retry).toHaveBeenCalledOnce();
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
