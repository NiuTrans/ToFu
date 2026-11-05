/**
 * Shared presentation for the channel Settings tabs (大象 and GitHub).
 * Responsibility: the account status line, owner commands whose control shows
 * progress and keeps a failure beside it, the account re-read while a session
 * is connecting (a session reports each transition once and nothing pushes it
 * to the tab), and the Global Router link.
 * Entry points: element, card, facts, hint, rowField, statusLine, isConnecting,
 * feedback, runCommand, refreshWhileConnecting, routerLink.
 * Dependencies: tofu-ui factories, the opening tab's lifecycle scope and the
 * runtime `closeSettings` / `loadConversation` bindings.
 */
import { featureRegistry } from '../../feature-registry';
import type { LifecycleScope } from '../../lifecycle';
import { createAlert, createButton, createField, createSpinner, type FieldHandle, type FieldOptions } from '../../ui';
import type { AccountState } from '../../api/channels.generated';

type Tone = 'ok' | 'busy' | 'idle' | 'bad';

const STATE_TONE: Record<AccountState, Tone> = {
  connected: 'ok', starting: 'busy', disconnected: 'busy', disabled: 'idle',
  needs_login: 'bad', kicked: 'bad', error: 'bad',
};
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

type RuntimeBindings = { closeSettings?: () => void; loadConversation?: (id: string) => void };

export function element<K extends keyof HTMLElementTagNameMap>(tag: K, className = '', text = ''): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

export function card(title: string): HTMLElement {
  const section = element('section', 'channel-card');
  section.appendChild(element('div', 'channel-card-title', title));
  return section;
}

export function facts(rows: Array<[string, string | Node]>): HTMLDListElement {
  const list = element('dl', 'channel-facts');
  for (const [label, value] of rows) {
    const detail = element('dd');
    detail.append(value);
    list.append(element('dt', '', label), detail);
  }
  return list;
}

export function hint(text: string): HTMLElement {
  return element('div', 'channel-hint', text);
}

/** A field for a `.channel-actions` row: it takes the width its buttons leave. */
export function rowField(options: FieldOptions): FieldHandle {
  const field = createField(options);
  field.el.classList.add('channel-field');
  return field;
}

export function isConnecting(account: StatusAccount): boolean {
  return account.enabled && (account.status.state === 'starting' || account.status.state === 'disconnected');
}

function quietSpinner(): HTMLElement {
  const spinner = createSpinner({ size: 'sm' });
  spinner.setAttribute('aria-hidden', 'true');
  return spinner;
}

/** The colored state line; a connecting session shows a spinner instead of a dot. */
export function statusLine(account: StatusAccount, label: string): HTMLElement {
  const line = element('div', 'channel-status');
  line.dataset.tone = STATE_TONE[account.status.state];
  line.append(isConnecting(account) ? quietSpinner() : element('span', 'channel-status-dot'),
              element('span', '', account.status.detail ? `${label} — ${account.status.detail}` : label));
  return line;
}

function clearFeedback(anchor: Element): void {
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
  const anchor = control.closest('.channel-actions, .channel-row') ?? control;
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
    label: open, variant: 'ghost', size: 'sm', title: conversationId,
    onClick: () => {
      runtime.closeSettings?.();
      runtime.loadConversation?.(conversationId);
    },
  });
}
