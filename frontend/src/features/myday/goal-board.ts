/**
 * My Day goal board: goals -> items -> progress, and the read-only day recap.
 *
 * Responsibility: render the owner's goals for today with their items grouped
 * by state, suggestions and linked Worker evidence apart, and each goal's
 * progress estimate as the server computed it; run owner commands through the
 * goal API (each answers with the goal's fresh view); render a past day as the
 * goal progress of that day plus its archived generated report, read-only.
 *
 * Bounds: one in-flight goal load per page (concurrent opens join it); a load
 * that started before a local change never overwrites that goal; commands of
 * one goal run in click order; a decomposition the server is already running
 * is joined, never restarted. Entry point: createMyDayGoalBoard.
 * Dependencies: injected goal API, translation, element, calendar and clock
 * ports; data is rendered with DOM text nodes only.
 */
import { MYDAY_PRESENTATION_ASSETS } from './presentation-assets';
import {
  calendarMarks,
  dayActivity,
  dayKey,
  groupGoalItems,
  todaySummary,
  type MyDayGoal,
  type MyDayGoalItem,
  type MyDayLinkableRequest,
} from './goal-model';

type Json = Record<string, unknown>;

export interface MyDayGoalApi {
  goals(): Promise<unknown>;
  goalCreate(text: string): Promise<unknown>;
  goalUpdate(goalId: string, body: Json): Promise<unknown>;
  goalItemAdd(goalId: string, text: string): Promise<unknown>;
  goalItemUpdate(goalId: string, itemId: string, body: Json): Promise<unknown>;
  goalItemRemove(goalId: string, itemId: string): Promise<unknown>;
  goalSuggestions(goalId: string, decision: 'accept' | 'dismiss'): Promise<unknown>;
  goalDecompose(goalId: string): Promise<unknown>;
  goalLink(goalId: string, body: Json): Promise<unknown>;
  goalUnlink(goalId: string): Promise<unknown>;
  goalRequests(): Promise<unknown>;
  report(date: string): Promise<unknown>;
}

export type MyDayBoardElement = 'mydayTasks' | 'mydayProgress' | 'mydayStatsBar';

export interface MyDayGoalBoardPorts {
  readonly api: MyDayGoalApi;
  readonly translate: (key: string, values?: Record<string, unknown>) => string;
  readonly element: (id: MyDayBoardElement) => HTMLElement | null;
  /** Repaint the calendar; it reads ``marks`` back from the board. */
  readonly renderCalendar: () => void;
  /** Select today in the retained calendar (which calls ``show`` again). */
  readonly showToday: () => void;
  readonly document?: Document;
  readonly now?: () => Date;
  readonly warn?: (message: string, detail?: unknown) => void;
}

export interface MyDayGoalBoard {
  show(date: string): Promise<void>;
  refresh(): Promise<void>;
  marks(year: number, month0: number): Record<number, number>;
}

interface Recap {
  readonly date: string;
  state: 'loading' | 'ready' | 'error';
  report: Json | null;
}

interface Picker {
  readonly goalId: string;
  state: 'loading' | 'ready' | 'error';
  requests: MyDayLinkableRequest[];
  error: string;
}

const REASON_KEYS: Record<string, string> = {
  queued: 'myday.reasonQueued', running: 'myday.reasonRunning',
  awaiting_review: 'myday.reasonAwaitingReview', accepted: 'myday.reasonAccepted',
  rework: 'myday.reasonRework', rejected: 'myday.reasonRejected',
  cancelled: 'myday.reasonCancelled', superseded: 'myday.reasonSuperseded',
};

function record(value: unknown): Json | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Json : null;
}

function errorText(error: unknown): string {
  const body = record(record(error)?.body);
  const detail = body && typeof body.error === 'string' ? body.error : '';
  if (detail) return detail;
  return error instanceof Error && error.message ? error.message : String(error);
}

function goalFrom(body: unknown): MyDayGoal {
  const goal = record(record(body)?.goal);
  if (!goal || typeof goal.id !== 'string' || !Array.isArray(goal.items)) {
    throw new Error('invalid goal response');
  }
  return goal as unknown as MyDayGoal;
}

function clone(goal: MyDayGoal): MyDayGoal {
  return { ...goal, items: goal.items.map((item) => ({ ...item })) };
}

export function createMyDayGoalBoard(ports: MyDayGoalBoardPorts): Readonly<MyDayGoalBoard> {
  const t = ports.translate;
  const doc = ports.document ?? document;
  const now = ports.now ?? (() => new Date());
  const today = (): string => dayKey(now().getTime());

  let goals: MyDayGoal[] = [];
  let loaded = false;
  let loadError = '';
  let loading: Promise<void> | null = null;
  let generation = 0;
  let selected = '';
  let creating = false;
  let composeError = '';
  let editing = '';
  let archivedOpen = false;
  let picker: Picker | null = null;
  let recap: Recap | null = null;
  const touched = new Map<string, number>();
  const shown = new Set<string>();
  const decomposing = new Set<string>();
  const errors = new Map<string, string>();
  const drafts = new Map<string, string>();
  const chains = new Map<string, Promise<void>>();

  const el = <K extends keyof HTMLElementTagNameMap>(tag: K, className = '', text = '') => {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  };
  const button = (className: string, label: string, onClick: () => void, title = '') => {
    const node = el('button', className, label);
    node.type = 'button';
    if (title) { node.title = title; node.setAttribute('aria-label', title); }
    node.addEventListener('click', (event) => { event.stopPropagation(); onClick(); });
    return node;
  };
  const sectionLabel = (label: string, count?: number) => {
    const node = el('div', 'myday-section-label', label);
    if (count !== undefined) node.append(el('span', 'myday-section-count', String(count)));
    return node;
  };
  const note = (className: string, text: string) => el('div', className, text);
  const shortDate = (ms: number) => {
    const date = new Date(ms);
    return t('myday.monthDay', { m: date.getMonth() + 1, d: date.getDate() });
  };

  function setGoal(view: MyDayGoal): void {
    generation += 1;
    touched.set(view.id, generation);
    const index = goals.findIndex((goal) => goal.id === view.id);
    if (index >= 0) goals[index] = view; else goals.push(view);
  }

  function isToday(): boolean {
    return !selected || selected === today();
  }

  // ── Loading ─────────────────────────────────────────────────

  function refresh(): Promise<void> {
    if (loading) return loading;
    const started = generation;
    loading = (async () => {
      try {
        const list = record(await ports.api.goals())?.goals;
        if (!Array.isArray(list)) throw new Error('invalid goals response');
        const fresh = (list as MyDayGoal[]).map((goal) => ((touched.get(goal.id) ?? 0) > started
          ? goals.find((local) => local.id === goal.id) ?? goal : goal));
        for (const local of goals) {
          if ((touched.get(local.id) ?? 0) > started && !fresh.some((goal) => goal.id === local.id)) {
            fresh.push(local);
          }
        }
        goals = fresh;
        loaded = true;
        loadError = '';
      } catch (error) {
        loadError = errorText(error);
        ports.warn?.('[MyDay] goals load failed', error);
      } finally {
        loading = null;
      }
      render();
      ports.renderCalendar();
      for (const goal of goals) {
        if (goal.decomposing && !decomposing.has(goal.id)) void decompose(goal.id);
      }
    })();
    render();
    return loading;
  }

  async function show(date: string): Promise<void> {
    selected = date;
    if (!isToday() && (!recap || recap.date !== date)) loadRecap(date);
    render();
    await refresh();
  }

  function loadRecap(date: string): void {
    const current: Recap = { date, state: 'loading', report: null };
    recap = current;
    void ports.api.report(date).then((body) => {
      current.report = record(body);
      current.state = 'ready';
    }, (error: unknown) => {
      ports.warn?.('[MyDay] archived report load failed', error);
      current.state = 'error';
    }).then(() => {
      if (recap === current && selected === date) render();
    });
  }

  // ── Commands ────────────────────────────────────────────────

  function command(goalId: string, run: () => Promise<unknown>,
                   optimistic?: (draft: MyDayGoal) => MyDayGoal): Promise<void> {
    const current = goals.find((goal) => goal.id === goalId);
    if (optimistic && current) {
      setGoal(optimistic(clone(current)));
      renderGoal(goalId);
    }
    const next = (chains.get(goalId) ?? Promise.resolve()).then(async () => {
      try {
        setGoal(goalFrom(await run()));
        errors.delete(goalId);
      } catch (error) {
        errors.set(goalId, errorText(error));
        ports.warn?.('[MyDay] goal command failed', error);
        // The server is the authority: show what it holds instead of the optimistic guess.
        generation += 1;
        touched.delete(goalId);
        void refresh();
      }
      renderGoal(goalId);
      renderHeader();
      ports.renderCalendar();
    });
    chains.set(goalId, next);
    void next.finally(() => { if (chains.get(goalId) === next) chains.delete(goalId); });
    return next;
  }

  async function createGoal(text: string): Promise<void> {
    const value = text.trim();
    if (!value || creating) return;
    creating = true;
    composeError = '';
    render();
    try {
      const view = goalFrom(await ports.api.goalCreate(value));
      drafts.delete('');
      setGoal(view);
      creating = false;
      render();
      void decompose(view.id);
    } catch (error) {
      composeError = errorText(error);
      creating = false;
      render();
    }
  }

  async function decompose(goalId: string): Promise<void> {
    if (decomposing.has(goalId)) return;
    decomposing.add(goalId);
    errors.delete(goalId);
    renderGoal(goalId);
    try {
      setGoal(goalFrom(await ports.api.goalDecompose(goalId)));
    } catch (error) {
      errors.set(goalId, t('myday.goalDecomposeFailed', { reason: errorText(error) }));
    } finally {
      decomposing.delete(goalId);
      renderGoal(goalId);
      renderHeader();
    }
  }

  function setStatus(goal: MyDayGoal, item: MyDayGoalItem, status: 'pending' | 'in_progress' | 'completed') {
    void command(goal.id, () => ports.api.goalItemUpdate(goal.id, item.id, { status }), (draft) => {
      const target = draft.items.find((row) => row.id === item.id);
      if (target) {
        target.status = status;
        target.manual = true;
        target.completedAt = status === 'completed' ? now().getTime() : null;
      }
      return draft;
    });
  }

  async function openPicker(goal: MyDayGoal): Promise<void> {
    const current: Picker = { goalId: goal.id, state: 'loading', requests: [], error: '' };
    picker = current;
    renderGoal(goal.id);
    try {
      const requests = record(await ports.api.goalRequests())?.requests;
      current.requests = Array.isArray(requests) ? requests as MyDayLinkableRequest[] : [];
      current.state = 'ready';
    } catch (error) {
      current.state = 'error';
      current.error = errorText(error);
    }
    if (picker === current) renderGoal(goal.id);
  }

  // ── Rendering ───────────────────────────────────────────────

  function render(): void {
    const container = ports.element('mydayTasks');
    if (!container) return;
    const focused = doc.activeElement instanceof HTMLElement ? doc.activeElement.dataset.focusKey : undefined;
    container.replaceChildren(isToday() ? board() : recapView());
    if (focused) container.querySelector<HTMLElement>(`[data-focus-key="${focused}"]`)?.focus();
    renderHeader();
    renderFooter();
  }

  function renderGoal(goalId: string): void {
    const container = ports.element('mydayTasks');
    const goal = goals.find((row) => row.id === goalId);
    if (!container || !goal || !isToday()) return;
    const card = Array.from(container.querySelectorAll<HTMLElement>('.myday-goal'))
      .find((node) => node.dataset.goalId === goalId);
    if (!card || card.parentElement?.classList.contains('myday-goal-archived-list')) { render(); return; }
    const focused = doc.activeElement instanceof HTMLElement && card.contains(doc.activeElement)
      ? doc.activeElement.dataset.focusKey : undefined;
    const fresh = goal.state === 'active' ? goalCard(goal) : null;
    if (!fresh) { render(); return; }
    card.replaceWith(fresh);
    if (focused) fresh.querySelector<HTMLElement>(`[data-focus-key="${focused}"]`)?.focus();
  }

  function renderHeader(): void {
    const element = ports.element('mydayProgress');
    if (!element) return;
    if (!loaded) { element.replaceChildren(); return; }
    let text: string;
    if (isToday()) {
      const summary = todaySummary(goals, today());
      text = t('myday.summary', { goals: summary.activeGoals, done: summary.completedToday,
        active: summary.inProgress });
    } else {
      const done = dayActivity(goals, selected).reduce((total, entry) => total + entry.completed.length, 0);
      text = t('myday.recapDone', { n: done });
    }
    element.replaceChildren(el('span', 'myday-summary', text));
  }

  function renderFooter(): void {
    const element = ports.element('mydayStatsBar');
    if (!element) return;
    element.replaceChildren(el('span', 'myday-stat', t('myday.estimateHint')));
  }

  function composer(): HTMLElement {
    const box = el('div', 'myday-add-todo myday-goal-composer');
    const input = el('input', 'myday-todo-input');
    input.type = 'text';
    input.maxLength = 1000;
    input.placeholder = t('myday.goalPlaceholder');
    input.setAttribute('aria-label', t('myday.goalPlaceholder'));
    input.dataset.focusKey = 'compose';
    input.value = drafts.get('') ?? '';
    input.disabled = creating;
    input.addEventListener('input', () => drafts.set('', input.value));
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.isComposing) {
        event.preventDefault();
        void createGoal(input.value);
      }
    });
    box.append(button('myday-add-btn', '＋', () => { void createGoal(input.value); }, t('myday.goalAdd')), input);
    if (!composeError) return box;
    const wrap = el('div');
    wrap.append(box, note('myday-goal-error', composeError));
    return wrap;
  }

  function board(): DocumentFragment {
    const fragment = doc.createDocumentFragment();
    fragment.append(composer());
    if (!loaded) {
      if (loadError) {
        fragment.append(note('myday-goal-error', t('myday.goalsLoadFailed', { reason: loadError })),
          button('myday-goal-btn', t('myday.retry'), () => { void refresh(); }));
      } else {
        for (let index = 0; index < 3; index += 1) {
          const skeleton = el('div', 'myday-task-skel');
          const body = el('div', 'skel-body');
          body.append(el('div', 'skel-line w75'), el('div', 'skel-line w45'));
          skeleton.append(el('div', 'skel-check'), body);
          fragment.append(skeleton);
        }
      }
      return fragment;
    }
    const active = goals.filter((goal) => goal.state === 'active');
    if (!active.length) {
      const empty = el('div', 'myday-empty');
      empty.insertAdjacentHTML('afterbegin', MYDAY_PRESENTATION_ASSETS.emptyIllustration);
      empty.append(el('div', 'myday-empty-title', t('myday.goalsEmptyTitle')),
        el('div', 'myday-empty-hint', t('myday.goalsEmptyHint')));
      fragment.append(empty);
    } else {
      fragment.append(sectionLabel(t('myday.goalsActive'), active.length));
      for (const goal of active) fragment.append(goalCard(goal));
    }
    const archived = goals.filter((goal) => goal.state === 'archived');
    if (archived.length) fragment.append(archivedSection(archived));
    return fragment;
  }

  function percentBadge(goal: MyDayGoal): HTMLElement {
    const percent = goal.progress.percent;
    const badge = el('div', 'myday-goal-pct');
    badge.title = t('myday.estimateHint');
    badge.append(el('span', 'myday-goal-pct-value', percent === null ? '—' : t('myday.estimate', { p: percent })),
      el('span', 'myday-goal-pct-label', t('myday.estimateLabel')));
    return badge;
  }

  function progressBar(goal: MyDayGoal): HTMLElement {
    const track = el('div', 'myday-prog-track myday-goal-track');
    const fill = el('div', 'myday-prog-fill');
    fill.style.width = `${goal.progress.percent ?? 0}%`;
    track.append(fill);
    track.setAttribute('role', 'progressbar');
    track.setAttribute('aria-valuemin', '0');
    track.setAttribute('aria-valuemax', '100');
    if (goal.progress.percent !== null) track.setAttribute('aria-valuenow', String(goal.progress.percent));
    return track;
  }

  function basisLine(goal: MyDayGoal): HTMLElement {
    const progress = goal.progress;
    return note('myday-goal-basis', progress.counted
      ? t('myday.goalBasis', { done: progress.completed, counted: progress.counted,
        active: progress.inProgress, pending: progress.pending })
      : t('myday.goalBasisNone'));
  }

  function goalHead(goal: MyDayGoal): HTMLElement {
    const head = el('div', 'myday-goal-head');
    if (editing !== goal.id) {
      head.append(el('h3', 'myday-goal-title', goal.text), percentBadge(goal));
      return head;
    }
    const input = el('input', 'myday-todo-input myday-goal-edit');
    input.type = 'text';
    input.maxLength = 1000;
    input.value = drafts.get(`edit:${goal.id}`) ?? goal.text;
    input.dataset.focusKey = `edit:${goal.id}`;
    input.addEventListener('input', () => drafts.set(`edit:${goal.id}`, input.value));
    const save = () => {
      const text = input.value.trim();
      editing = '';
      drafts.delete(`edit:${goal.id}`);
      if (text && text !== goal.text) void command(goal.id, () => ports.api.goalUpdate(goal.id, { text }));
      else renderGoal(goal.id);
    };
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.isComposing) { event.preventDefault(); save(); }
      if (event.key === 'Escape') { event.stopPropagation(); editing = ''; renderGoal(goal.id); }
    });
    head.append(input, button('myday-goal-btn', t('myday.save'), save),
      button('myday-goal-btn', t('myday.cancel'), () => { editing = ''; renderGoal(goal.id); }));
    return head;
  }

  function badge(text: string, variant: string, title = ''): HTMLElement {
    const node = el('span', `myday-goal-badge ${variant}`, text);
    if (title) node.title = title;
    return node;
  }

  function itemRow(goal: MyDayGoal, item: MyDayGoalItem, kind: 'item' | 'suggested' | 'evidence'): HTMLElement {
    const completed = item.status === 'completed';
    const row = el('div', `myday-todo-item myday-goal-item s-${item.status}`);
    if (completed) row.classList.add('done');
    if (kind === 'suggested') row.classList.add('suggested');
    if (item.status === 'cancelled') row.classList.add('cancelled');
    if (kind === 'suggested' || item.status === 'cancelled') {
      row.append(el('span', 'myday-goal-bullet'));
    } else {
      const check = button(`myday-todo-check${completed ? ' checked' : ''}`, '',
        () => setStatus(goal, item, completed ? 'pending' : 'completed'),
        completed ? t('myday.markUndone') : t('myday.markDone'));
      check.setAttribute('aria-pressed', String(completed));
      check.insertAdjacentHTML('afterbegin', MYDAY_PRESENTATION_ASSETS.todoCheckIcon);
      row.append(check);
    }
    const text = el('span', 'myday-todo-text', item.text);
    if (item.evidence) text.title = item.evidence.conversationId;
    row.append(text);

    if (item.origin === 'agent' && kind !== 'suggested') row.append(badge(t('myday.badgeAgent'), 'agent'));
    if (item.evidence) {
      row.append(badge(t(REASON_KEYS[item.evidence.reason] ?? 'myday.reasonAwaitingReview'),
        `worker r-${item.evidence.reason}`));
    }
    if (kind === 'evidence' && item.manual) row.append(badge(t('myday.badgeManual'), 'manual'));
    if (completed && item.completedAt !== null) {
      const day = dayKey(item.completedAt);
      row.append(badge(day === today() ? t('myday.today') : shortDate(item.completedAt),
        day === today() ? 'today' : 'when'));
    } else if (!completed && kind !== 'suggested' && item.createdAt !== null
               && dayKey(item.createdAt) !== today()) {
      row.append(badge(t('myday.badgeSince', { date: shortDate(item.createdAt) }), 'since'));
    }

    if (kind === 'suggested') {
      row.append(button('myday-goal-btn', t('myday.accept'), () => {
        void command(goal.id, () => ports.api.goalItemUpdate(goal.id, item.id, { proposal: 'accepted' }));
      }), button('myday-goal-btn ghost', t('myday.dismiss'), () => {
        void command(goal.id, () => ports.api.goalItemRemove(goal.id, item.id));
      }));
      return row;
    }
    if (!completed && item.status !== 'cancelled') {
      const started = item.status === 'in_progress';
      const chip = button(`myday-goal-chip${started ? ' on' : ''}`,
        started ? t('myday.itemInProgress') : t('myday.itemStart'),
        () => setStatus(goal, item, started ? 'pending' : 'in_progress'));
      chip.setAttribute('aria-pressed', String(started));
      row.append(chip);
    }
    if (kind === 'evidence') {
      if (item.manual) {
        row.append(button('myday-goal-btn ghost', t('myday.followEvidence'), () => {
          void command(goal.id, () => ports.api.goalItemUpdate(goal.id, item.id, { followEvidence: true }));
        }));
      }
      return row;
    }
    const remove = button('myday-todo-del', '', () => {
      void command(goal.id, () => ports.api.goalItemRemove(goal.id, item.id));
    }, t('myday.deleteTodo'));
    remove.insertAdjacentHTML('afterbegin', MYDAY_PRESENTATION_ASSETS.todoDeleteIcon);
    row.append(remove);
    return row;
  }

  function group(card: HTMLElement, label: string, items: MyDayGoalItem[], goal: MyDayGoal,
                 kind: 'item' | 'suggested' | 'evidence', extra: HTMLElement[] = []): void {
    if (!items.length) return;
    const header = sectionLabel(label, items.length);
    header.classList.add('myday-goal-group');
    header.append(...extra);
    card.append(header);
    for (const item of items) card.append(itemRow(goal, item, kind));
  }

  function requestLine(goal: MyDayGoal): HTMLElement {
    const line = el('div', 'myday-goal-request');
    const link = goal.link;
    if (!link) return line;
    const label = el('span', 'myday-goal-request-label', t('myday.goalLinked', { label: link.label }));
    label.title = link.projectPath;
    line.append(label);
    if (goal.request?.settled) line.append(badge(t('myday.requestSettled'), 'settled'));
    line.append(button('myday-goal-btn ghost', t('myday.goalUnlink'), () => {
      void command(goal.id, () => ports.api.goalUnlink(goal.id));
    }));
    if (goal.request && !goal.request.available) {
      const wrap = el('div');
      wrap.append(line, note('myday-goal-error',
        t('myday.requestUnavailable', { reason: goal.request.reason ?? '' })));
      return wrap;
    }
    return line;
  }

  function linkPicker(goal: MyDayGoal, current: Picker): HTMLElement {
    const box = el('div', 'myday-goal-picker');
    const head = el('div', 'myday-goal-picker-head', t('myday.goalLink'));
    head.append(button('myday-goal-btn ghost', t('myday.close'), () => { picker = null; renderGoal(goal.id); }));
    box.append(head);
    if (current.state === 'loading') { box.append(note('myday-goal-basis', t('myday.requestsLoading'))); return box; }
    if (current.state === 'error') {
      box.append(note('myday-goal-error', t('myday.requestsFailed', { reason: current.error })));
      return box;
    }
    if (!current.requests.length) { box.append(note('myday-goal-basis', t('myday.requestsEmpty'))); return box; }
    for (const request of current.requests) {
      const project = request.projectPath.split('/').filter(Boolean).pop() ?? request.projectPath;
      const option = button('myday-goal-option', '', () => {
        picker = null;
        void command(goal.id, () => ports.api.goalLink(goal.id, {
          projectPath: request.projectPath, sourceAttemptId: request.sourceAttemptId }));
      }, request.label);
      option.append(el('span', 'myday-goal-option-label', request.label),
        el('span', 'myday-goal-option-meta', `${project} · ${t('myday.requestWorkers', { n: request.workers })}`
          + (request.createdAt ? ` · ${shortDate(request.createdAt)}` : '')));
      box.append(option);
    }
    return box;
  }

  function goalCard(goal: MyDayGoal): HTMLElement {
    const card = el('section', 'myday-goal');
    card.dataset.goalId = goal.id;
    // Only a goal's first appearance animates; re-renders after a click must not flicker.
    if (shown.has(goal.id)) card.classList.add('settled');
    shown.add(goal.id);
    card.append(goalHead(goal), progressBar(goal), basisLine(goal));
    if (goal.link) card.append(requestLine(goal));
    const running = decomposing.has(goal.id) || goal.decomposing;
    if (running) card.append(note('myday-goal-working', t('myday.goalDecomposing')));
    const error = errors.get(goal.id);
    if (error) card.append(note('myday-goal-error', error));

    const groups = groupGoalItems(goal);
    group(card, t('myday.itemsInProgress'), groups.inProgress, goal, 'item');
    group(card, t('myday.itemsPending'), groups.pending, goal, 'item');
    group(card, t('myday.itemsCompleted'), groups.completed, goal, 'item');
    group(card, t('myday.itemsEvidence'), groups.evidence, goal, 'evidence');
    group(card, t('myday.itemsSuggested'), groups.suggested, goal, 'suggested', [
      button('myday-goal-btn', t('myday.acceptAll'), () => {
        void command(goal.id, () => ports.api.goalSuggestions(goal.id, 'accept'));
      }),
      button('myday-goal-btn ghost', t('myday.dismissAll'), () => {
        void command(goal.id, () => ports.api.goalSuggestions(goal.id, 'dismiss'));
      }),
    ]);
    if (!goal.items.length && !running) card.append(note('myday-goal-basis', t('myday.goalNoItems')));

    const add = el('div', 'myday-add-todo myday-goal-add');
    const input = el('input', 'myday-todo-input');
    input.type = 'text';
    input.maxLength = 200;
    input.placeholder = t('myday.itemAddPlaceholder');
    input.setAttribute('aria-label', t('myday.itemAddPlaceholder'));
    input.dataset.focusKey = `add:${goal.id}`;
    input.value = drafts.get(goal.id) ?? '';
    const submit = () => {
      const text = input.value.trim();
      if (!text) return;
      drafts.delete(goal.id);
      input.value = '';
      void command(goal.id, () => ports.api.goalItemAdd(goal.id, text));
    };
    input.addEventListener('input', () => drafts.set(goal.id, input.value));
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.isComposing) { event.preventDefault(); submit(); }
    });
    add.append(button('myday-add-btn', '＋', submit, t('myday.itemAddPlaceholder')), input);
    card.append(add);

    const actions = el('div', 'myday-goal-actions');
    const decomposeButton = button('myday-generate-btn myday-goal-decompose',
      goal.decomposedAt ? t('myday.goalRedecompose') : t('myday.goalDecompose'),
      () => { void decompose(goal.id); });
    decomposeButton.disabled = running;
    actions.append(decomposeButton);
    if (!goal.link) actions.append(button('myday-goal-btn', t('myday.goalLink'), () => { void openPicker(goal); }));
    actions.append(
      button('myday-goal-btn ghost', t('myday.goalEdit'), () => { editing = goal.id; renderGoal(goal.id); }),
      button('myday-goal-btn ghost', t('myday.goalArchive'), () => {
        void command(goal.id, () => ports.api.goalUpdate(goal.id, { state: 'archived' }));
      }));
    card.append(actions);
    if (picker?.goalId === goal.id) card.append(linkPicker(goal, picker));
    return card;
  }

  function archivedSection(archived: MyDayGoal[]): HTMLElement {
    const section = el('div', 'myday-goal-archived');
    const toggle = button('myday-goal-btn ghost myday-goal-archived-toggle',
      `${archivedOpen ? '▾' : '▸'} ${t('myday.goalsArchived')} (${archived.length})`,
      () => { archivedOpen = !archivedOpen; render(); });
    toggle.setAttribute('aria-expanded', String(archivedOpen));
    section.append(toggle);
    if (!archivedOpen) return section;
    const list = el('div', 'myday-goal-archived-list');
    for (const goal of archived) {
      const row = el('div', 'myday-goal myday-goal-compact');
      row.dataset.goalId = goal.id;
      const head = el('div', 'myday-goal-head');
      head.append(el('h3', 'myday-goal-title', goal.text), percentBadge(goal));
      row.append(head, basisLine(goal), button('myday-goal-btn', t('myday.goalRestore'), () => {
        void command(goal.id, () => ports.api.goalUpdate(goal.id, { state: 'active' }));
      }));
      list.append(row);
    }
    section.append(list);
    return section;
  }

  function recapView(): DocumentFragment {
    const fragment = doc.createDocumentFragment();
    const activity = dayActivity(goals, selected);
    fragment.append(sectionLabel(t('myday.recapProgress'), activity.length));
    if (!loaded && !loadError) fragment.append(note('myday-goal-basis', t('myday.requestsLoading')));
    if (loaded && !activity.length) fragment.append(note('myday-goal-basis', t('myday.recapNothing')));
    for (const entry of activity) {
      const card = el('section', 'myday-goal myday-goal-compact');
      const head = el('div', 'myday-goal-head');
      head.append(el('h3', 'myday-goal-title', entry.goal.text));
      if (entry.created) head.append(badge(t('myday.recapCreated'), 'today'));
      card.append(head);
      for (const item of entry.completed) {
        const row = el('div', 'myday-todo-item myday-goal-item done');
        const mark = el('span', 'myday-todo-check checked');
        mark.insertAdjacentHTML('afterbegin', MYDAY_PRESENTATION_ASSETS.todoCheckIcon);
        row.append(mark, el('span', 'myday-todo-text', item.text));
        card.append(row);
      }
      for (const item of entry.started) {
        const row = el('div', 'myday-todo-item myday-goal-item s-in_progress');
        row.append(el('span', 'myday-goal-bullet'), el('span', 'myday-todo-text', item.text),
          badge(t('myday.recapStarted'), 'since'));
        card.append(row);
      }
      fragment.append(card);
    }
    fragment.append(archiveView());
    fragment.append(button('myday-goal-btn myday-goal-today', t('myday.backToToday'), () => ports.showToday()));
    return fragment;
  }

  function archiveView(): HTMLElement {
    const section = el('div', 'myday-goal-archive');
    section.append(sectionLabel(t('myday.archiveTitle')));
    const current = recap && recap.date === selected ? recap : null;
    if (!current || current.state === 'loading') {
      section.append(note('myday-goal-basis', t('myday.archiveLoading')));
      return section;
    }
    const report = current.report;
    const streams = Array.isArray(report?.streams) ? report.streams as Json[] : [];
    const todos = Array.isArray(report?.tomorrow) ? report.tomorrow as Json[] : [];
    if (current.state === 'error' || (!streams.length && !todos.length)) {
      section.append(note('myday-goal-basis', t('myday.archiveNone')));
      return section;
    }
    for (const stream of streams) {
      const status = typeof stream.status === 'string' ? stream.status : 'in_progress';
      const row = el('div', `myday-stream readonly s-${status}`);
      const body = el('div', 'myday-stream-body');
      body.append(el('div', 'myday-stream-title', String(stream.title ?? '')));
      if (typeof stream.summary === 'string' && stream.summary) {
        body.append(el('div', 'myday-stream-summary', stream.summary));
      }
      row.append(el('div', `myday-dot s-${status}`), body);
      section.append(row);
    }
    if (todos.length) section.append(sectionLabel(t('myday.archivePlan'), todos.length));
    for (const todo of todos) {
      const done = todo.done === true;
      const row = el('div', `myday-todo-item${done ? ' done' : ''}`);
      const mark = el('span', `myday-todo-check${done ? ' checked' : ''}`);
      mark.insertAdjacentHTML('afterbegin', MYDAY_PRESENTATION_ASSETS.todoCheckIcon);
      row.append(mark, el('span', 'myday-todo-text', String(todo.text ?? '')));
      section.append(row);
    }
    return section;
  }

  return Object.freeze({
    show,
    refresh,
    marks: (year: number, month0: number) => calendarMarks(goals, year, month0),
  });
}
