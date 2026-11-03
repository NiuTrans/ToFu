/** Session-free task board pinned atop the bound Coordinator conversation.
 *
 * Its head is one line (title, state, summary, update time) that stays
 * readable while the card is folded. Renders only the Coordinator-authored board (contract `progressBoard` in
 * contracts/project_coordination_v1.schema.json): what the human asked for and
 * how far each task is. Sessions, Workers, references, graphs, usage and
 * questions never appear here; questions are ask_human cards in the chat.
 * Rendering reads nothing, acknowledges nothing and starts no work.
 */
import { createButton } from '../ui';
import { t } from '../i18n';
import type { CoordinatorProgressBoard, CoordinatorProgressTask } from '../api/project-coordination.generated';

export type CoordinatorProgressView =
  | { kind: 'board'; board: CoordinatorProgressBoard }
  | { kind: 'empty' }
  | { kind: 'unavailable' };

export interface CoordinatorProgressPorts {
  /** Whether the card and its finished-task fold are open; the owner keeps them across repaints. */
  open: boolean;
  finishedOpen: boolean;
  toggle(open: boolean): void;
  toggleFinished(open: boolean): void;
  /** Explicit way to the existing management view; never opened implicitly. */
  manage(): void;
  retry(): void;
  now?(): number;
}

type Status = CoordinatorProgressTask['status'];

const STATUS_LABEL = {
  in_progress: 'coordinatorProgress.status.in_progress',
  pending: 'coordinatorProgress.status.pending',
  completed: 'coordinatorProgress.status.completed',
  cancelled: 'coordinatorProgress.status.cancelled',
} as const satisfies Record<Status, string>;

const OPEN: ReadonlySet<Status> = new Set(['in_progress', 'pending']);

export function coordinatorProgressTally(board: CoordinatorProgressBoard) {
  const count = (status: Status) => board.tasks.filter(task => task.status === status).length;
  return { active: count('in_progress'), pending: count('pending'),
    finished: count('completed') + count('cancelled'), total: board.tasks.length };
}

/** One-line state for the sidebar entry; null when the board says nothing yet. */
export function coordinatorProgressHeadline(board: CoordinatorProgressBoard | null): string | null {
  if (!board || board.tasks.length === 0) return null;
  const tally = coordinatorProgressTally(board);
  if (tally.active > 0) return t('coordinatorProgress.sidebar.active', { count: tally.active });
  if (tally.pending === 0) return t('coordinatorProgress.sidebar.allDone');
  return null;
}

function updatedLabel(updatedAt: number, now: number): string {
  const when = new Date(updatedAt);
  const sameDay = when.toDateString() === new Date(now).toDateString();
  const time = new Intl.DateTimeFormat(document.documentElement.lang || undefined, sameDay
    ? { hour: '2-digit', minute: '2-digit' }
    : { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(when);
  return t('coordinatorProgress.updated', { time });
}

function taskRow(task: CoordinatorProgressTask): HTMLLIElement {
  const row = document.createElement('li');
  row.className = 'coordinator-progress-task';
  row.dataset.status = task.status;
  const status = document.createElement('span');
  status.className = 'coordinator-progress-status';
  status.textContent = t(STATUS_LABEL[task.status]);
  const body = document.createElement('span');
  body.className = 'coordinator-progress-text';
  const title = document.createElement('strong');
  title.textContent = task.title;
  body.append(title);
  if (task.progress) {
    const progress = document.createElement('small');
    progress.textContent = task.progress;
    body.append(progress);
  }
  row.append(status, body);
  return row;
}

function taskList(tasks: readonly CoordinatorProgressTask[]): HTMLOListElement {
  const list = document.createElement('ol');
  list.className = 'coordinator-progress-tasks';
  list.append(...tasks.map(taskRow));
  return list;
}

export function createCoordinatorProgress(view: CoordinatorProgressView, ports: CoordinatorProgressPorts): HTMLElement {
  const card = document.createElement('details');
  card.className = 'coordinator-progress';
  card.dataset.state = view.kind;
  card.open = ports.open;
  card.addEventListener('toggle', () => ports.toggle(card.open));

  const head = document.createElement('summary');
  head.className = 'coordinator-progress-head';
  const title = document.createElement('span');
  title.className = 'coordinator-progress-title';
  title.textContent = t('coordinatorProgress.title');
  head.append(title);
  card.append(head);
  const headline = (text: string) => {
    const line = document.createElement('span');
    line.className = 'coordinator-progress-summary';
    line.textContent = text;
    head.append(line);
    return line;
  };

  const body = document.createElement('div');
  body.className = 'coordinator-progress-body';
  const actions = document.createElement('div');
  actions.className = 'coordinator-progress-actions';

  if (view.kind === 'unavailable') {
    headline(t('coordinatorProgress.unavailable')).setAttribute('role', 'status');
    actions.append(createButton({ label: t('projectCoordination.retry'), size: 'sm', variant: 'ghost', onClick: ports.retry }));
  } else if (view.kind === 'empty') {
    headline(t('coordinatorProgress.empty'));
  } else {
    const { board } = view;
    const state = coordinatorProgressHeadline(board);
    if (state) {
      const tally = document.createElement('span');
      tally.className = 'coordinator-progress-tally';
      tally.textContent = state;
      head.append(tally);
    }
    headline(board.summary);
    const updated = document.createElement('time');
    updated.className = 'coordinator-progress-updated';
    updated.dateTime = new Date(board.updatedAt).toISOString();
    updated.textContent = updatedLabel(board.updatedAt, ports.now?.() ?? Date.now());
    head.append(updated);

    const open = board.tasks.filter(task => OPEN.has(task.status));
    const finished = board.tasks.filter(task => !OPEN.has(task.status));
    // In-progress work leads; otherwise the Coordinator's own order is kept.
    open.sort((left, right) => Number(right.status === 'in_progress') - Number(left.status === 'in_progress'));
    if (open.length) body.append(taskList(open));
    if (finished.length) {
      const fold = document.createElement('details');
      fold.className = 'coordinator-progress-finished';
      fold.open = ports.finishedOpen;
      fold.addEventListener('toggle', () => ports.toggleFinished(fold.open));
      const label = document.createElement('summary');
      label.textContent = t('coordinatorProgress.finished', { count: finished.length });
      fold.append(label, taskList(finished));
      body.append(fold);
    }
  }
  actions.append(createButton({ label: t('projectCoordination.controls'), size: 'sm', variant: 'ghost', onClick: ports.manage }));
  body.append(actions);
  card.append(body);
  return card;
}
