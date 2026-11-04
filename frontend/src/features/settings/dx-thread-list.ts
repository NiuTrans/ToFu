/**
 * 大象 Settings thread list: every thread of the account in one list that
 * scales to many groups (docs/CHANNELS.md C2, C6).
 * Responsibility: search by name or key, a mode filter with counts, a
 * one-click mode choice and a reply-policy select per thread (none for an
 * ignored thread, which gets no replies), selection with bulk mode / reply
 * changes, and per-thread write ordering: a thread's writes
 * run in order, its controls show the last acknowledged view once they
 * settle, and a failure restores that view and stays beside its row.
 * Choosing a thread kind's default stores null (follow the default). Search,
 * filter and selection are presentation state of one opening; the server
 * stays the authority for every thread, and a re-read keeps them.
 * Entry points: createDxThreadList; compareThreads and threadTitle (shared
 * with the command chat picker).
 * Dependencies: tofu-ui factories, the shared channel view, i18n and the
 * generated wire DTOs.
 */
import { t } from '../../i18n';
import {
  createAlert, createButton, createChoice, createDropdown, createField, createSpinner, type MenuEntry,
} from '../../ui';
import { CHANNEL_DEFAULT_MODE_BY_KIND, CHANNEL_DEFAULT_REPLY_POLICY } from '../../api/channels.generated';
import type {
  ChannelThreadUpdate, ChannelThreadView, ConfigurableThreadMode, ReplyPolicy, ThreadKind, ThreadMode,
} from '../../api/channels.generated';
import { card, chip, element, empty, feedback, hint } from './channel-settings-view';

export interface DxThreadListPorts {
  update: (key: string, update: ChannelThreadUpdate) => Promise<ChannelThreadView>;
  isCurrent: () => boolean;
  failure: (error: unknown) => string;
  pendingLabel: string;
}

export interface DxThreadList {
  el: HTMLElement;
  setThreads: (threads: ChannelThreadView[]) => void;
  /** A failed read: replaces the loading state, or reports beside the title over a shown list. */
  showError: (text: string) => void;
}

type Filter = 'all' | ConfigurableThreadMode;

interface Row {
  thread: ChannelThreadView;
  el: HTMLElement;
  pick: HTMLInputElement | null;
  modes: Map<ConfigurableThreadMode, HTMLInputElement>;
  reply: HTMLSelectElement | null;
  replyField: HTMLElement | null;
  meta: HTMLElement;
  pending: number;
  chain: Promise<unknown>;
}

const MODES: ConfigurableThreadMode[] = ['watch', 'mentions', 'ignore'];
const REPLY_POLICIES: ReplyPolicy[] = ['draft', 'auto', 'never'];
const FILTERS: Filter[] = ['all', ...MODES];
const DEFAULT = 'default';

let listSeq = 0;
let rowSeq = 0;

function modeLabel(mode: ThreadMode): string {
  return t(`settings.dx.mode.${mode}` as const);
}

function replyLabel(policy: ReplyPolicy): string {
  return t(`settings.dx.reply.${policy}` as const);
}

function modeValue(value: string): string {
  return t('settings.dx.modeValue', { value });
}

function replyValue(value: string): string {
  return t('settings.dx.replyValue', { value });
}

function modeUpdate(thread: ChannelThreadView, mode: ConfigurableThreadMode | typeof DEFAULT): ChannelThreadUpdate {
  return { mode: mode === DEFAULT || mode === CHANNEL_DEFAULT_MODE_BY_KIND[thread.kind] ? null : mode };
}

function replyUpdate(policy: ReplyPolicy | typeof DEFAULT): ChannelThreadUpdate {
  return { replyPolicy: policy === DEFAULT || policy === CHANNEL_DEFAULT_REPLY_POLICY ? null : policy };
}

function kindLabel(kind: ThreadKind): string {
  return kind === 'issue' ? '' : t(`settings.dx.kind.${kind}` as const);
}

export function threadTitle(thread: ChannelThreadView): string {
  return thread.title || t('settings.dx.untitled');
}

/** Pinned command chat, then the most recently active, then by name. */
export function compareThreads(a: ChannelThreadView, b: ChannelThreadView): number {
  if (a.isCommandChat !== b.isCommandChat) return a.isCommandChat ? -1 : 1;
  const recent = (b.lastEventAtMs ?? -1) - (a.lastEventAtMs ?? -1);
  if (recent) return recent;
  return threadTitle(a).localeCompare(threadTitle(b)) || a.key.localeCompare(b.key);
}

function lastEventText(thread: ChannelThreadView): string {
  if (!thread.lastEventAtMs) return '';
  const time = new Intl.DateTimeFormat(document.documentElement.lang || undefined,
                                       { dateStyle: 'medium', timeStyle: 'short' })
    .format(new Date(thread.lastEventAtMs));
  return t('settings.dx.lastEvent', { time });
}

function busy(control: HTMLButtonElement, label: string): () => void {
  const saved = [...control.childNodes];
  const spinner = createSpinner({ size: 'sm' });
  spinner.setAttribute('aria-hidden', 'true');
  control.disabled = true;
  control.setAttribute('aria-busy', 'true');
  control.replaceChildren(spinner, label);
  return () => {
    control.disabled = false;
    control.removeAttribute('aria-busy');
    control.replaceChildren(...saved);
  };
}

export function createDxThreadList(ports: DxThreadListPorts): DxThreadList {
  const seq = ++listSeq;
  const rows = new Map<string, Row>();
  const selected = new Set<string>();
  let query = '';
  let filter: Filter = 'all';
  let bulkRunning = false;

  const section = card(t('settings.dx.threads'));
  section.classList.add('dx-threads');
  const body = element('div', 'dx-thread-body');
  body.appendChild(createSpinner());
  section.append(hint(t('settings.dx.threadsHint')), body);

  // ── toolbar: search and mode filter ───────────────────────────────
  const search = createField({ label: t('settings.dx.threadsSearch') });
  search.el.classList.add('dx-thread-search');
  const searchInput = search.control as HTMLInputElement;
  searchInput.type = 'search';
  searchInput.placeholder = t('settings.dx.threadsSearchPlaceholder');
  searchInput.autocomplete = 'off';
  searchInput.addEventListener('input', () => {
    query = searchInput.value.trim().toLowerCase();
    selected.clear();
    applyView();
  });

  const filterGroup = element('div', 'dx-thread-filter');
  filterGroup.setAttribute('role', 'radiogroup');
  filterGroup.setAttribute('aria-label', t('settings.dx.filterLabel'));
  const filterLabels = new Map<Filter, HTMLElement>();
  for (const value of FILTERS) {
    const choice = createChoice({
      type: 'radio', name: `dx-thread-filter-${seq}`, value, label: '', checked: value === filter,
      onChange: (checked) => {
        if (!checked) return;
        filter = value;
        selected.clear();
        applyView();
      },
    });
    filterLabels.set(value, choice.el.lastElementChild as HTMLElement);
    filterGroup.appendChild(choice.el);
  }
  const tools = element('div', 'channel-section dx-thread-tools');
  tools.append(search.el, filterGroup);

  // ── selection bar: select all visible, bulk changes ───────────────
  const all = createChoice({
    label: t('settings.dx.selectAll'),
    onChange: (checked) => {
      for (const row of visibleRows()) {
        if (!row.pick) continue;
        if (checked) selected.add(row.thread.key);
        else selected.delete(row.thread.key);
      }
      syncSelection();
    },
  });
  const count = element('span', 'dx-thread-selected');
  const bulkItems: MenuEntry[] = [
    ...MODES.map((mode) => ({ id: `mode:${mode}`, label: modeValue(modeLabel(mode)) })),
    { id: `mode:${DEFAULT}`, label: modeValue(t('settings.dx.useDefault')) },
    'separator',
    ...REPLY_POLICIES.map((policy) => ({ id: `reply:${policy}`, label: replyValue(replyLabel(policy)) })),
    { id: `reply:${DEFAULT}`, label: replyValue(t('settings.dx.useDefault')) },
  ];
  const bulk = createDropdown({
    label: t('settings.dx.bulk'), items: bulkItems, size: 'sm',
    onSelect: (id) => { void applyBulk(id); },
  });
  const bulkTrigger = bulk.el.querySelector('button')!;
  const clear = createButton({
    label: t('settings.dx.clearSelection'), variant: 'ghost', size: 'sm',
    onClick: () => {
      selected.clear();
      syncSelection();
    },
  });
  const bulkActions = element('div', 'dx-thread-bulk-actions');
  bulkActions.append(count, bulk.el, clear);
  const selectionBar = element('div', 'dx-thread-bulk');
  selectionBar.append(all.el, bulkActions);

  const list = element('div', 'dx-thread-rows');
  const noMatch = empty(t('settings.dx.noMatch'));

  function visibleRows(): Row[] {
    return [...rows.values()].filter((row) => !row.el.hidden);
  }

  function matches(thread: ChannelThreadView): boolean {
    if (filter !== 'all' && thread.mode !== filter) return false;
    return !query || thread.key.toLowerCase().includes(query) || thread.title.toLowerCase().includes(query);
  }

  function syncCounts(): void {
    const threads = [...rows.values()].map((row) => row.thread);
    for (const [value, label] of filterLabels) {
      const name = value === 'all' ? t('settings.dx.filter.all') : modeLabel(value);
      const total = value === 'all' ? threads.length : threads.filter((thread) => thread.mode === value).length;
      label.textContent = t('settings.dx.filterCount', { label: name, count: String(total) });
    }
  }

  function syncSelection(): void {
    for (const key of [...selected]) {
      if (!rows.get(key)?.pick) selected.delete(key);
    }
    const pickable = visibleRows().filter((row) => row.pick);
    for (const row of rows.values()) {
      if (row.pick) row.pick.checked = selected.has(row.thread.key);
    }
    const chosen = pickable.filter((row) => selected.has(row.thread.key)).length;
    all.input.checked = pickable.length > 0 && chosen === pickable.length;
    all.input.indeterminate = chosen > 0 && chosen < pickable.length;
    all.input.disabled = pickable.length === 0;
    bulkActions.hidden = selected.size === 0;
    count.textContent = t('settings.dx.selected', { count: String(selected.size) });
    if (!bulkRunning) bulkTrigger.disabled = selected.size === 0;
  }

  function applyView(): void {
    let shown = 0;
    for (const row of rows.values()) {
      row.el.hidden = !matches(row.thread);
      if (!row.el.hidden) shown += 1;
    }
    noMatch.hidden = shown > 0;
    syncSelection();
  }

  function syncRow(row: Row): void {
    for (const [mode, input] of row.modes) input.checked = row.thread.mode === mode;
    if (row.reply) row.reply.value = row.thread.replyPolicy;
    // An ignored thread delivers nothing, so there is no reply to choose a policy for.
    if (row.replyField) row.replyField.hidden = row.thread.mode === 'ignore';
    row.meta.replaceChildren(...meta(row.thread));
  }

  function meta(thread: ChannelThreadView): Node[] {
    const parts: Node[] = [];
    const kind = kindLabel(thread.kind);
    if (kind) parts.push(element('span', 'ui-badge', kind));
    parts.push(element('span', 'channel-key', thread.key));
    const last = lastEventText(thread);
    if (last) parts.push(element('span', 'dx-thread-last', last));
    return parts;
  }

  function settle(row: Row): void {
    if (row.pending > 0) return;
    syncRow(row);
    syncCounts();
  }

  /** Queue one write behind the thread's earlier ones; resolves to its error, or null. */
  function write(row: Row, update: ChannelThreadUpdate): Promise<unknown> {
    row.pending += 1;
    const step = row.chain.then(() => ports.update(row.thread.key, update));
    row.chain = step.catch(() => undefined);
    return step.then((fresh) => {
      row.pending -= 1;
      if (ports.isCurrent()) {
        row.thread = fresh;
        settle(row);
      }
      return null;
    }, (error: unknown) => {
      row.pending -= 1;
      if (ports.isCurrent()) settle(row);
      return error ?? new Error('update failed');
    });
  }

  function writeFromRow(row: Row, update: ChannelThreadUpdate): void {
    void write(row, update).then((error) => {
      if (error && ports.isCurrent()) feedback(row.el, ports.failure(error));
    });
  }

  async function applyBulk(id: string): Promise<void> {
    const [kind, value] = id.split(':') as ['mode' | 'reply', string];
    const targets = [...selected].map((key) => rows.get(key)).filter((row): row is Row => Boolean(row?.pick));
    if (!targets.length) return;
    bulkRunning = true;
    const restore = busy(bulkTrigger, ports.pendingLabel);
    let failed = 0;
    let first: unknown = null;
    for (const row of targets) {
      const update = kind === 'mode'
        ? modeUpdate(row.thread, value as ConfigurableThreadMode | typeof DEFAULT)
        : replyUpdate(value as ReplyPolicy | typeof DEFAULT);
      const error = await write(row, update);
      if (!ports.isCurrent()) return;
      if (error) {
        failed += 1;
        first ??= error;
      }
    }
    bulkRunning = false;
    restore();
    syncSelection();
    if (failed) {
      feedback(selectionBar, t('settings.dx.bulkFailed', { count: String(failed), error: ports.failure(first) }));
    }
  }

  /** Two lines: the name with its facts, then the thread's controls under the name. */
  function buildRow(thread: ChannelThreadView): Row {
    const el = element('div', 'dx-thread-row');
    const head = element('div', 'dx-thread-head');
    const metaLine = element('div', 'dx-thread-meta');
    const controls = element('div', 'dx-thread-controls');
    const row: Row = { thread, el, pick: null, modes: new Map(), reply: null, replyField: null, meta: metaLine,
                       pending: 0, chain: Promise.resolve() };
    if (thread.isCommandChat) {
      el.classList.add('dx-thread-row--command');
      head.append(element('div', 'dx-thread-name', threadTitle(thread)), metaLine);
      controls.appendChild(chip(modeLabel('command'), 'ok'));
      el.append(head, controls);
      syncRow(row);
      return row;
    }
    const pick = createChoice({
      label: threadTitle(thread),
      onChange: (checked) => {
        if (checked) selected.add(row.thread.key);
        else selected.delete(row.thread.key);
        syncSelection();
      },
    });
    pick.el.classList.add('dx-thread-name');
    row.pick = pick.input;
    head.append(pick.el, metaLine);

    const mode = element('div', 'dx-thread-mode');
    mode.setAttribute('role', 'radiogroup');
    mode.setAttribute('aria-label', `${t('settings.dx.column.mode')} · ${threadTitle(thread)}`);
    const name = `dx-thread-mode-${seq}-${++rowSeq}`;
    for (const value of MODES) {
      const choice = createChoice({
        type: 'radio', name, value, label: modeLabel(value), checked: thread.mode === value,
        onChange: (checked) => {
          if (checked) writeFromRow(row, modeUpdate(row.thread, value));
        },
      });
      row.modes.set(value, choice.input);
      mode.appendChild(choice.el);
    }

    // Each option names its setting, so the select needs no visible label beside the mode choice.
    const reply = createField({
      label: t('settings.dx.column.reply'), value: thread.replyPolicy,
      choices: REPLY_POLICIES.map((policy) => ({ value: policy, label: replyValue(replyLabel(policy)) })),
    });
    reply.el.classList.add('dx-thread-reply');
    row.reply = reply.control as HTMLSelectElement;
    row.replyField = reply.el;
    row.reply.addEventListener('change', () => {
      writeFromRow(row, replyUpdate(row.reply!.value as ReplyPolicy));
    });
    controls.append(mode, reply.el);
    el.append(head, controls);
    syncRow(row);
    return row;
  }

  function setThreads(threads: ChannelThreadView[]): void {
    const fresh = new Map(threads.map((thread) => [thread.key, thread]));
    for (const [key, row] of rows) {
      if (!fresh.has(key) && row.pending === 0) {
        rows.delete(key);
        selected.delete(key);
      }
    }
    for (const thread of threads) {
      const row = rows.get(thread.key);
      if (!row || row.thread.isCommandChat !== thread.isCommandChat) {
        rows.set(thread.key, buildRow(thread));
      } else if (row.pending === 0) {
        row.thread = thread;
        syncRow(row);
      }
    }
    const ordered = [...rows.values()].sort((a, b) => compareThreads(a.thread, b.thread)).map((row) => row.el);
    if (ordered.some((node, index) => list.children[index] !== node) || list.children.length !== ordered.length) {
      list.replaceChildren(...ordered);
    }
    if (!rows.size) {
      body.replaceChildren(empty(t('settings.dx.threadsEmpty')));
      return;
    }
    if (!body.contains(list)) body.replaceChildren(tools, selectionBar, list, noMatch);
    syncCounts();
    applyView();
  }

  function showError(text: string): void {
    if (rows.size) feedback(section.firstElementChild!, text);
    else body.replaceChildren(createAlert({ variant: 'error', title: text }).el);
  }

  return { el: section, setThreads, showError };
}
