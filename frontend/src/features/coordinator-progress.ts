/** Session-free task board pinned atop the bound Coordinator conversation.
 *
 * Reads as a todo list of the Coordinator-authored board (contract
 * `progressBoard` in contracts/project_coordination_v1.schema.json): one line
 * per task behind a status glyph, in-progress work first, upcoming work in the
 * Coordinator's order, finished work folded. Notes are secondary: only an
 * in-progress task shows its note (one line); every other note is its row's
 * tooltip and stays in the accessibility tree. A display translation
 * (`progressTranslation`) replaces each text it covers, and an "Original"
 * toggle shows the board as written. Sessions, Workers, references, graphs,
 * usage and questions never appear here; questions are ask_human cards in the
 * chat. Rendering reads nothing, acknowledges nothing and starts no work.
 *
 * The card persists across renders: the owner's footer control (a Router's
 * Worker model) stays attached, and the fold, scroll and focus survive live
 * board updates.
 */
import { createButton } from '../ui';
import { t } from '../i18n';
import type {
  CoordinatorProgressBoard, CoordinatorProgressTask, CoordinatorProgressTranslation,
} from '../api/project-coordination.generated';

export type CoordinatorProgressView =
  | { kind: 'board'; board: CoordinatorProgressBoard; translation?: CoordinatorProgressTranslation }
  | { kind: 'empty' }
  | { kind: 'unavailable' };

export interface CoordinatorProgressPorts {
  /** Initial fold of the card and of its finished tasks; the owner keeps both across cards. */
  open: boolean;
  finishedOpen: boolean;
  toggle(open: boolean): void;
  toggleFinished(open: boolean): void;
  /** Owner control kept attached at the start of the footer for the card's lifetime. */
  footer?: HTMLElement;
  now?(): number;
}

/** The owner's current callbacks, bound again on every render. */
export interface CoordinatorProgressActions {
  /** Explicit way to the existing management view; never opened implicitly. */
  manage(): void;
  retry(): void;
}

export interface CoordinatorProgressCard {
  el: HTMLDetailsElement;
  render(view: CoordinatorProgressView, actions: CoordinatorProgressActions): void;
}

type Status = CoordinatorProgressTask['status'];

const STATUS_LABEL = {
  in_progress: 'coordinatorProgress.status.in_progress',
  pending: 'coordinatorProgress.status.pending',
  completed: 'coordinatorProgress.status.completed',
  cancelled: 'coordinatorProgress.status.cancelled',
} as const satisfies Record<Status, string>;

const OPEN: ReadonlySet<Status> = new Set(['in_progress', 'pending']);
const SVG_NS = 'http://www.w3.org/2000/svg';

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

/** A board text as shown: its display translation when the board carries one. */
export function coordinatorProgressText(translation: CoordinatorProgressTranslation | undefined, text: string): string {
  return translation?.texts.find(entry => entry.source === text)?.text ?? text;
}

function updatedLabel(updatedAt: number, now: number): string {
  const when = new Date(updatedAt);
  const sameDay = when.toDateString() === new Date(now).toDateString();
  const time = new Intl.DateTimeFormat(document.documentElement.lang || undefined, sameDay
    ? { hour: '2-digit', minute: '2-digit' }
    : { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }).format(when);
  return t('coordinatorProgress.updated', { time });
}

/* Open work is a ring, half filled while in progress; finished work is a
 * filled disc with its mark cut out, a check when done and a cross when dropped. */
function glyph(status: Status): HTMLSpanElement {
  const mark = document.createElement('span');
  mark.className = 'coordinator-progress-glyph';
  mark.setAttribute('role', 'img');
  mark.setAttribute('aria-label', t(STATUS_LABEL[status]));
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 16 16');
  svg.setAttribute('aria-hidden', 'true');
  svg.setAttribute('focusable', 'false');
  const shape = (name: string, attributes: Record<string, string>) => {
    const node = document.createElementNS(SVG_NS, name);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
    svg.append(node);
  };
  if (OPEN.has(status)) {
    shape('circle', { cx: '8', cy: '8', r: '6.25', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.5' });
    if (status === 'in_progress') shape('path', { d: 'M8 4.25a3.75 3.75 0 0 1 0 7.5z', fill: 'currentColor' });
  } else {
    shape('circle', { cx: '8', cy: '8', r: '7', fill: 'currentColor' });
    shape('path', {
      class: 'coordinator-progress-glyph-cut', fill: 'none', 'stroke-width': '1.6',
      'stroke-linecap': 'round', 'stroke-linejoin': 'round',
      d: status === 'completed' ? 'M5 8.25l2 2 4-4.25' : 'M5.75 5.75l4.5 4.5m0-4.5-4.5 4.5',
    });
  }
  mark.append(svg);
  return mark;
}

function taskRow(task: CoordinatorProgressTask, say: (text: string) => string): HTMLLIElement {
  const row = document.createElement('li');
  row.className = 'coordinator-progress-task';
  row.dataset.status = task.status;
  const text = document.createElement('span');
  text.className = 'coordinator-progress-text';
  const title = document.createElement('span');
  title.className = 'coordinator-progress-task-title';
  title.textContent = say(task.title);
  text.append(title);
  if (task.progress) {
    const note = document.createElement('span');
    note.className = 'coordinator-progress-task-note';
    note.textContent = say(task.progress);
    // The shown note is cut to one line; any other note is only the row's tooltip.
    if (task.status === 'in_progress') note.title = note.textContent;
    else row.title = note.textContent;
    text.append(note);
  }
  row.append(glyph(task.status), text);
  return row;
}

function taskList(tasks: readonly CoordinatorProgressTask[], say: (text: string) => string): HTMLOListElement {
  const list = document.createElement('ol');
  list.className = 'coordinator-progress-tasks';
  list.append(...tasks.map(task => taskRow(task, say)));
  return list;
}

export function createCoordinatorProgress(ports: CoordinatorProgressPorts): CoordinatorProgressCard {
  let finishedOpen = ports.finishedOpen, original = false;
  let last: [CoordinatorProgressView, CoordinatorProgressActions] | null = null;

  const card = document.createElement('details');
  card.className = 'coordinator-progress';
  card.open = ports.open;
  card.addEventListener('toggle', () => ports.toggle(card.open));
  const head = document.createElement('summary');
  head.className = 'coordinator-progress-head';
  const body = document.createElement('div');
  body.className = 'coordinator-progress-body';
  const content = document.createElement('div');
  content.className = 'coordinator-progress-content';
  // Footer nodes are never moved by a render, so a focused or open control stays live.
  const actions = document.createElement('div');
  actions.className = 'coordinator-progress-actions';
  const commands = document.createElement('span');
  commands.className = 'coordinator-progress-commands';
  const originalToggle = createButton({ label: t('chat.bilingualOriginal'), size: 'sm', variant: 'ghost', onClick: () => {
    original = !original;
    if (last) render(...last);
  } });
  const commandSlot = document.createElement('span');
  commandSlot.className = 'coordinator-progress-command-slot';
  commands.append(originalToggle, commandSlot);
  if (ports.footer) actions.append(ports.footer);
  actions.append(commands);
  body.append(content, actions);
  card.append(head, body);

  function render(view: CoordinatorProgressView, actionPorts: CoordinatorProgressActions) {
    last = [view, actionPorts];
    card.dataset.state = view.kind;
    const titleRow = document.createElement('span');
    titleRow.className = 'coordinator-progress-title-row';
    const title = document.createElement('span');
    title.className = 'coordinator-progress-title';
    title.textContent = t('coordinatorProgress.title');
    titleRow.append(title);
    const headNodes: Node[] = [titleRow];
    const contentNodes: Node[] = [];
    const commandNodes: Node[] = [];
    let translated = false;

    if (view.kind === 'unavailable') {
      const notice = document.createElement('p');
      notice.className = 'coordinator-progress-note';
      notice.setAttribute('role', 'status');
      notice.textContent = t('coordinatorProgress.unavailable');
      contentNodes.push(notice);
      commandNodes.push(createButton({ label: t('projectCoordination.retry'), size: 'sm', variant: 'ghost', onClick: actionPorts.retry }));
    } else if (view.kind === 'empty') {
      const notice = document.createElement('p');
      notice.className = 'coordinator-progress-note';
      notice.textContent = t('coordinatorProgress.empty');
      contentNodes.push(notice);
    } else {
      const { board, translation } = view;
      translated = !!translation?.texts.some(entry => entry.text !== entry.source);
      const shown = translated && !original && translation
        ? new Map(translation.texts.map(entry => [entry.source, entry.text])) : null;
      const say = (text: string) => shown?.get(text) ?? text;
      const headline = coordinatorProgressHeadline(board);
      if (headline) {
        const tally = document.createElement('span');
        tally.className = 'coordinator-progress-tally';
        tally.textContent = headline;
        titleRow.append(tally);
      }
      const updated = document.createElement('time');
      updated.className = 'coordinator-progress-updated';
      updated.dateTime = new Date(board.updatedAt).toISOString();
      updated.textContent = updatedLabel(board.updatedAt, ports.now?.() ?? Date.now());
      titleRow.append(updated);
      const summary = document.createElement('span');
      summary.className = 'coordinator-progress-summary';
      summary.textContent = say(board.summary);
      headNodes.push(summary);

      const open = board.tasks.filter(task => OPEN.has(task.status));
      const finished = board.tasks.filter(task => !OPEN.has(task.status));
      // In-progress work leads; otherwise the Coordinator's own order is kept.
      open.sort((left, right) => Number(right.status === 'in_progress') - Number(left.status === 'in_progress'));
      if (open.length) contentNodes.push(taskList(open, say));
      if (finished.length) {
        const fold = document.createElement('details');
        fold.className = 'coordinator-progress-finished';
        fold.open = finishedOpen;
        fold.addEventListener('toggle', () => { finishedOpen = fold.open; ports.toggleFinished(fold.open); });
        const label = document.createElement('summary');
        label.textContent = t('coordinatorProgress.finished', { count: finished.length });
        fold.append(label, taskList(finished, say));
        contentNodes.push(fold);
      }
    }
    commandNodes.push(createButton({ label: t('projectCoordination.controls'), size: 'sm', variant: 'ghost', onClick: actionPorts.manage }));
    originalToggle.hidden = !translated;
    originalToggle.setAttribute('aria-pressed', String(translated && original));
    head.replaceChildren(...headNodes);
    content.replaceChildren(...contentNodes);
    commandSlot.replaceChildren(...commandNodes);
  }

  return { el: card, render };
}
