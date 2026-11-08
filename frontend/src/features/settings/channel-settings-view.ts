/**
 * Shared presentation for the channel Settings tabs (大象 and GitHub), in the
 * Database panel's card language.
 * Responsibility: the card scaffold (title plus a state chip or action),
 * hairline card sections, the fact grid, tone chips and the account status
 * chip, the thread table (one header row; each cell keeps its own label for
 * assistive tech and narrow panes), the outbox card of one channel, owner
 * commands whose control shows progress and keeps a failure beside it, the
 * account re-read while a session is connecting (a session reports each
 * transition once and nothing pushes it to the tab), the Global Router link,
 * the credential vault entries a channel may use as its secret, and the audit
 * of what Tofu recorded for other people (C11), page by page.
 * Entry points: element, card, cardSection, facts, hint, empty, chip, rowField,
 * statusLine, isConnecting, feedback, clearFeedback, runCommand, refreshWhileConnecting,
 * routerLink, threadTable, threadRow, selectCell, staticCell, outboxCard, vaultEntryNames,
 * createAuditList, auditCopy, timeText.
 * Dependencies: tofu-ui factories, i18n (the channel-independent audit copy), the
 * opening tab's lifecycle scope, the `Api.credentials` list and the runtime
 * `closeSettings` / `loadConversation` bindings.
 */
import { featureRegistry } from '../../feature-registry';
import { t } from '../../i18n';
import type { LifecycleScope } from '../../lifecycle';
import { createAlert, createButton, createField, createSpinner, type FieldHandle, type FieldOptions } from '../../ui';
import type {
  AccountState, AuditPage, OutboxAttachmentView, OutboxAuthority, OutboxDecision, OutboxItemView, OutboxState,
} from '../../api/channels.generated';

export type Tone = 'ok' | 'busy' | 'idle' | 'bad';

const STATE_TONE: Record<AccountState, Tone> = {
  connected: 'ok', starting: 'busy', authorizing: 'busy', disconnected: 'busy', disabled: 'idle',
  needs_login: 'bad', kicked: 'bad', error: 'bad',
};
// States an enabled account leaves by itself, so the tab keeps re-reading it.
const CONNECTING_STATES = new Set<AccountState>(['starting', 'authorizing', 'disconnected']);
const OUTBOX_TONE: Record<OutboxState, Tone> = {
  draft: 'busy', queued: 'idle', sending: 'busy', unknown: 'busy', failed: 'bad', sent: 'ok', discarded: 'idle',
};
const DISCARDABLE = new Set<OutboxState>(['draft', 'queued', 'failed']);
// Settings re-reads a live status at the cadence of its OAuth device login.
const STATUS_REFRESH_MS = 3000;

export interface StatusAccount {
  enabled: boolean;
  status: { state: AccountState; detail: string };
}

export interface CommandView {
  isCurrent: () => boolean;
  reload: () => Promise<void>;
  pendingLabel: string;
  failure: (error: unknown) => string;
}

export interface ThreadColumns {
  thread: string;
  mode: string;
  reply: string;
}

export interface OutboxCopy {
  title: string;
  empty: string;
  approve: string;
  discard: string;
  state: (state: OutboxState) => string;
  /** Channels with attachments (大象) name them; others never list any. */
  attachments?: AttachmentCopy;
  /** A draft's approval code, which the owner's own chat message names to decide it (C11). */
  code?: (code: string) => string;
}

export interface AuditCopy {
  title: string;
  hint: string;
  empty: string;
  more: string;
  loadFailed: (error: unknown) => string;
  state: (state: OutboxState) => string;
  /** Whose request recorded the item. */
  authority: (authority: OutboxAuthority) => string;
  /** The latest decision on the item, made at `time`; an undecided draft has none. */
  decision: (decision: OutboxDecision, time: string) => string;
  /** The names of the files an item carries. */
  attachments: (names: string) => string;
}

export interface AuditPorts {
  /** The page after `before` (a page's nextBefore). */
  more: (before: string) => Promise<AuditPage>;
  isCurrent: () => boolean;
}

export interface AuditList {
  el: HTMLElement;
  /** A (re-)read of the newest page: it replaces every item shown, including pages loaded since. */
  setPage: (page: AuditPage) => void;
  /** A failed read: replaces the loading state, or reports beside the title over a shown list. */
  showError: (text: string) => void;
}

export interface AttachmentCopy {
  label: string;
  previewOf: (name: string) => string;
  pending: string;
  /** The URL of an attached original image (its thumbnail). */
  imageUrl: (itemId: string, index: number) => string;
}

export interface OutboxDecisions {
  approve: (id: string) => Promise<unknown>;
  discard: (id: string) => Promise<unknown>;
}

type RuntimeBindings = { closeSettings?: () => void; loadConversation?: (id: string) => void };

type CredentialsFacade = { list: () => Promise<unknown> };

// Vault entries owned by other features are never offered as a channel's secret.
const RESERVED_ENTRY = /^(skill|channel)\./;

export function element<K extends keyof HTMLElementTagNameMap>(tag: K, className = '', text = ''): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

/** A card whose head carries its title and, on the right, a state chip or an action. */
export function card(title: string, aside: Node | null = null): HTMLElement {
  const section = element('section', 'channel-card');
  const head = element('div', 'channel-card-head');
  head.appendChild(element('div', 'channel-card-title', title));
  if (aside) head.appendChild(aside);
  section.appendChild(head);
  return section;
}

/** A hairline-separated group inside a card. */
export function cardSection(...children: Node[]): HTMLElement {
  const group = element('div', 'channel-section');
  group.append(...children);
  return group;
}

export function facts(rows: Array<[string, string | Node]>): HTMLDListElement {
  const list = element('dl', 'channel-facts');
  for (const [label, value] of rows) {
    const item = element('div', 'channel-fact');
    const detail = element('dd');
    detail.append(value);
    item.append(element('dt', '', label), detail);
    list.appendChild(item);
  }
  return list;
}

/** A date and time in the page's language. */
export function timeText(ms: number): string {
  return new Intl.DateTimeFormat(document.documentElement.lang || undefined, { dateStyle: 'medium', timeStyle: 'short' })
    .format(new Date(ms));
}

export function hint(text: string): HTMLElement {
  return element('p', 'channel-hint', text);
}

export function empty(text: string): HTMLElement {
  return element('p', 'channel-empty', text);
}

/** A field for a `.channel-actions` row: it takes the width its buttons leave. */
export function rowField(options: FieldOptions): FieldHandle {
  const field = createField(options);
  field.el.classList.add('channel-field');
  return field;
}

export function isConnecting(account: StatusAccount): boolean {
  return account.enabled && CONNECTING_STATES.has(account.status.state);
}

function quietSpinner(): HTMLElement {
  const spinner = createSpinner({ size: 'sm' });
  spinner.setAttribute('aria-hidden', 'true');
  return spinner;
}

/** A tone chip: a dot (or `lead`) and its text. */
export function chip(text: string, tone: Tone, lead: Node = element('span', 'channel-chip-dot')): HTMLElement {
  const node = element('span', 'channel-chip');
  node.dataset.tone = tone;
  node.append(lead, element('span', 'channel-chip-text', text));
  return node;
}

/** The account state chip; a connecting session shows a spinner instead of a dot. */
export function statusLine(account: StatusAccount, label: string): HTMLElement {
  const { state, detail } = account.status;
  const line = chip(detail ? `${label} — ${detail}` : label, STATE_TONE[state],
                    isConnecting(account) ? quietSpinner() : undefined);
  line.classList.add('channel-status');
  return line;
}

export function clearFeedback(anchor: Element): void {
  anchor.closest('.channel-card')?.querySelectorAll('.channel-feedback').forEach((node) => node.remove());
}

/** Show `text` as an error right after `anchor`, replacing its card's previous one. */
export function feedback(anchor: Element, text: string): void {
  clearFeedback(anchor);
  const alert = createAlert({ variant: 'error', title: text, dismissible: true }).el;
  alert.classList.add('channel-feedback');
  anchor.after(alert);
}

/**
 * Run one owner command. Its control (and the other controls of its row) wait
 * with a progress label; success re-reads the server, a failure stays beside
 * the control instead of at the top of a long panel.
 */
export async function runCommand(view: CommandView, control: HTMLButtonElement | HTMLSelectElement,
                                 action: () => Promise<unknown>, failure = view.failure): Promise<void> {
  const anchor = control.closest('.channel-actions, .channel-row, .channel-outbox-item') ?? control;
  clearFeedback(anchor);
  const locked = [control, ...anchor.querySelectorAll<HTMLButtonElement | HTMLSelectElement>('button, select')]
    .filter((node, index, all) => !node.disabled && all.indexOf(node) === index);
  const label = [...control.childNodes];
  for (const node of locked) node.disabled = true;
  control.setAttribute('aria-busy', 'true');
  if (control instanceof HTMLButtonElement) control.replaceChildren(quietSpinner(), view.pendingLabel);
  try {
    await action();
  } catch (error) {
    if (!view.isCurrent()) return;
    for (const node of locked) node.disabled = false;
    control.removeAttribute('aria-busy');
    if (control instanceof HTMLButtonElement) control.replaceChildren(...label);
    feedback(anchor, failure(error));
    return;
  }
  if (view.isCurrent()) await view.reload();
}

/** Re-read a connecting account until its status moves, then reload the tab once. */
export function refreshWhileConnecting<T extends StatusAccount>(
  scope: LifecycleScope, account: T, read: (signal: AbortSignal) => Promise<T>,
  isCurrent: () => boolean, reload: () => Promise<void>,
): void {
  if (!isConnecting(account)) return;
  const key = (value: StatusAccount) => `${value.enabled}\n${value.status.state}\n${value.status.detail}`;
  const seen = key(account);
  const tick = (): void => {
    void read(scope.signal).then((fresh) => {
      if (!isCurrent()) return;
      if (key(fresh) === seen) scope.timeout(tick, STATUS_REFRESH_MS);
      else void reload();
    }, () => {
      if (isCurrent()) scope.timeout(tick, STATUS_REFRESH_MS);
    });
  };
  scope.timeout(tick, STATUS_REFRESH_MS);
}

/** The Global Router: a button that opens its conversation, or `missing`. */
export function routerLink(conversationId: string | null, open: string, missing: string): string | HTMLElement {
  if (!conversationId) return missing;
  const runtime = featureRegistry as unknown as RuntimeBindings;
  return createButton({
    label: open, variant: 'secondary', size: 'sm', title: conversationId,
    onClick: () => {
      runtime.closeSettings?.();
      runtime.loadConversation?.(conversationId);
    },
  });
}

/** Names of the owner's vault entries a channel may use as its secret; none when the vault is not listed. */
export async function vaultEntryNames(): Promise<string[]> {
  const credentials = (featureRegistry.Api as { credentials?: CredentialsFacade } | undefined)?.credentials;
  if (!credentials) return [];
  const listing = await credentials.list().catch(() => null) as { credentials?: Array<{ name?: unknown }> } | null;
  return (listing?.credentials ?? [])
    .map((entry) => (typeof entry.name === 'string' ? entry.name : ''))
    .filter((name) => name && !RESERVED_ENTRY.test(name));
}

/** Threads under one header row; the header is presentation only, each cell names itself. */
export function threadTable(columns: ThreadColumns, rows: HTMLElement[]): HTMLElement {
  const table = element('div', 'channel-table');
  const head = element('div', 'channel-table-head');
  head.setAttribute('aria-hidden', 'true');
  head.append(element('span', '', columns.thread), element('span', '', columns.mode),
              element('span', '', columns.reply));
  table.append(head, ...rows);
  return table;
}

export function threadRow(title: string, key: string | Node, mode: HTMLElement, reply: HTMLElement): HTMLElement {
  const row = element('div', 'channel-row');
  const name = element('div', 'channel-row-title');
  name.append(element('div', 'channel-row-name', title),
              typeof key === 'string' ? element('div', 'channel-key', key) : key);
  row.append(name, mode, reply);
  return row;
}

/** A select cell of the thread table; `onChange` receives the select after each change. */
export function selectCell(label: string, value: string, choices: Array<{ value: string; label: string }>,
                           onChange: (select: HTMLSelectElement) => void): HTMLElement {
  const field = createField({ label, value, choices });
  field.el.classList.add('channel-cell');
  const select = field.control as HTMLSelectElement;
  select.addEventListener('change', () => onChange(select));
  return field.el;
}

/** A fixed value in a select column (a command chat's mode). */
export function staticCell(label: string, value: string): HTMLElement {
  const cell = element('div', 'channel-cell channel-cell-static');
  cell.append(element('span', 'channel-cell-caption', label), chip(value, 'ok'));
  return cell;
}

function byteSize(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * An item's attachments: each name, type and size with its delivery state; an
 * original image also shows its thumbnail, and a preview copy names its original.
 */
function attachmentList(item: OutboxItemView, copy: OutboxCopy, attachments: AttachmentCopy): HTMLElement {
  const list = element('ul', 'channel-outbox-attachments');
  list.setAttribute('aria-label', attachments.label);
  const byIndex = new Map<number, OutboxAttachmentView>(item.attachments.map((entry) => [entry.index, entry]));
  for (const attachment of item.attachments) {
    const row = element('li', 'channel-outbox-attachment');
    if (attachment.kind === 'image' && attachment.renditionOf === null) {
      const thumbnail = element('img', 'channel-outbox-thumbnail');
      thumbnail.src = attachments.imageUrl(item.id, attachment.index);
      thumbnail.alt = attachment.name;
      thumbnail.loading = 'lazy';
      thumbnail.decoding = 'async';
      row.appendChild(thumbnail);
    }
    const text = element('div', 'channel-outbox-attachment-text');
    text.appendChild(element('span', 'channel-outbox-attachment-name', attachment.name));
    const source = attachment.renditionOf === null ? undefined : byIndex.get(attachment.renditionOf);
    const facts = [attachment.type, byteSize(attachment.size)];
    if (source) facts.unshift(attachments.previewOf(source.name));
    text.appendChild(element('span', 'channel-outbox-attachment-meta', facts.join(' · ')));
    if (attachment.error) text.appendChild(element('span', 'channel-outbox-error', attachment.error));
    row.appendChild(text);
    const state = attachment.state === 'pending'
      ? chip(attachments.pending, 'idle')
      : chip(copy.state(attachment.state), attachment.state === 'sent' ? 'ok' : 'bad');
    row.appendChild(state);
    list.appendChild(row);
  }
  return list;
}

/** One channel's outbox: each item's state, thread, text and attachments, with its owner decisions. */
export function outboxCard(items: OutboxItemView[], copy: OutboxCopy, view: CommandView,
                           decide: OutboxDecisions): HTMLElement {
  const section = card(copy.title);
  if (!items.length) {
    section.appendChild(empty(copy.empty));
    return section;
  }
  const list = element('div', 'channel-outbox');
  for (const item of items) {
    const row = element('div', 'channel-outbox-item');
    const head = element('div', 'channel-outbox-head');
    const meta = element('div', 'channel-outbox-meta');
    meta.append(chip(copy.state(item.state), OUTBOX_TONE[item.state]),
                element('span', 'channel-outbox-thread', item.threadTitle || item.thread),
                element('span', 'channel-key', item.thread));
    if (item.state === 'draft' && copy.code) {
      meta.appendChild(element('span', 'channel-outbox-code', copy.code(item.approvalCode)));
    }
    head.appendChild(meta);
    const actions = element('div', 'channel-outbox-actions');
    if (item.state === 'draft') {
      const approve = createButton({
        label: copy.approve, variant: 'primary', size: 'sm',
        onClick: () => { void runCommand(view, approve, () => decide.approve(item.id)); },
      });
      actions.appendChild(approve);
    }
    if (DISCARDABLE.has(item.state)) {
      const discard = createButton({
        label: copy.discard, variant: 'danger', size: 'sm',
        onClick: () => { void runCommand(view, discard, () => decide.discard(item.id)); },
      });
      actions.appendChild(discard);
    }
    if (actions.childElementCount) head.appendChild(actions);
    row.append(head, element('div', 'channel-outbox-text', item.text));
    if (copy.attachments && item.attachments.length) row.appendChild(attachmentList(item, copy, copy.attachments));
    if (item.error) row.appendChild(element('div', 'channel-outbox-error', item.error));
    list.appendChild(row);
  }
  section.appendChild(list);
  return section;
}

function auditItem(item: OutboxItemView, copy: AuditCopy): HTMLElement {
  const row = element('div', 'channel-audit-item');
  const meta = element('div', 'channel-outbox-meta');
  meta.append(chip(copy.state(item.state), OUTBOX_TONE[item.state]),
              element('span', 'channel-outbox-thread', item.threadTitle || item.thread),
              element('span', 'channel-key', item.thread),
              element('span', 'channel-audit-time', timeText(item.createdAtMs)));
  row.append(meta, element('div', 'channel-outbox-text', item.text));
  if (item.attachments.length) {
    const names = new Intl.ListFormat(document.documentElement.lang || undefined, { type: 'conjunction' })
      .format(item.attachments.map((attachment) => attachment.name));
    row.appendChild(element('div', 'channel-audit-fact', copy.attachments(names)));
  }
  row.appendChild(element('div', 'channel-audit-fact', copy.authority(item.authority)));
  if (item.decision) {
    row.appendChild(element('div', 'channel-audit-fact', copy.decision(item.decision, timeText(item.decision.atMs))));
  }
  if (item.error) row.appendChild(element('div', 'channel-outbox-error', item.error));
  return row;
}

/** The audit card's copy; `state` names an item's state in the tab's own words. */
export function auditCopy(state: (state: OutboxState) => string): AuditCopy {
  return {
    title: t('settings.channels.audit.title'), hint: t('settings.channels.audit.hint'),
    empty: t('settings.channels.audit.empty'), more: t('settings.channels.audit.more'),
    loadFailed: (error) => t('settings.channels.audit.loadFailed',
                             { error: error instanceof Error ? error.message : String(error) }),
    state,
    authority: (authority) => t(`settings.channels.audit.authority.${authority}` as const),
    decision: (decision, time) => t('settings.channels.audit.decided', {
      decision: t(`settings.channels.audit.kind.${decision.kind}` as const),
      by: t(`settings.channels.audit.by.${decision.by}` as const), time,
    }),
    attachments: (names) => t('settings.channels.audit.attachments', { names }),
  };
}

/**
 * The audit card (C11): every item Tofu recorded for other people, newest
 * first, whatever became of it — its state, thread, time, exact text and
 * files, whose request recorded it and the owner's latest decision. It is
 * read-only (drafts are decided in the outbox card); older pages load on
 * request, and a re-read of the newest page replaces them.
 */
export function createAuditList(copy: AuditCopy, ports: AuditPorts): AuditList {
  const section = card(copy.title);
  section.classList.add('channel-audit');
  const body = element('div', 'channel-audit-body');
  body.appendChild(createSpinner());
  section.append(hint(copy.hint), body);
  const list = element('div', 'channel-audit-items');
  let before: string | null = null;
  let pages = 0;
  const more = createButton({
    label: copy.more, variant: 'ghost', size: 'sm',
    onClick: () => { void readMore(); },
  });
  more.classList.add('channel-audit-more');

  function append(page: AuditPage): void {
    list.append(...page.items.map((item) => auditItem(item, copy)));
    before = page.nextBefore;
    more.hidden = !before;
    more.disabled = false;
  }

  async function readMore(): Promise<void> {
    if (!before) return;
    const shown = pages;
    more.disabled = true;
    clearFeedback(more);
    try {
      const page = await ports.more(before);
      if (ports.isCurrent() && shown === pages) append(page);
    } catch (error) {
      if (!ports.isCurrent() || shown !== pages) return;
      more.disabled = false;
      feedback(more, copy.loadFailed(error));
    }
  }

  function setPage(page: AuditPage): void {
    pages += 1;
    list.replaceChildren();
    if (!page.items.length) {
      before = null;
      body.replaceChildren(empty(copy.empty));
      return;
    }
    append(page);
    body.replaceChildren(list, more);
  }

  function showError(text: string): void {
    if (body.contains(list)) feedback(section.firstElementChild!, text);
    else body.replaceChildren(createAlert({ variant: 'error', title: text }).el);
  }

  return { el: section, setPage, showError };
}
