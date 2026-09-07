/**
 * Owns the chat composer's Send/Stop DOM projection.
 *
 * The retained runtime supplies authoritative busy state and command ports;
 * this module projects them onto the single shared control (Stop while busy,
 * Send when idle) and keeps the text-draft listener scoped to its input.
 */

export interface ComposerSendControlLabels {
  readonly send: string;
  readonly sendTitle: string;
  readonly stop: string;
  readonly stopping: string;
}

export interface ComposerSendControlsInput {
  readonly document: Document;
  readonly translating: boolean;
  readonly startupConnecting: boolean;
  readonly turnBusy: boolean;
  readonly stopping: boolean;
  readonly hasAttachmentDraft: boolean;
  readonly queueCount: number;
  readonly labels: ComposerSendControlLabels;
  readonly onSend: () => void;
  readonly onStop: () => void;
  readonly requestRefresh: () => void;
}

export interface ComposerSendControlsState {
  readonly busy: boolean;
  readonly hasDraft: boolean;
  readonly splitControls: boolean;
}

const SEND_BUTTON_ID = 'sendBtn';
const STOP_BUTTON_ID = 'composerStopBtn';
const SPLIT_CLASS = 'composer-dual-actions';

const SEND_ICON = '<svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 12L20 12"/><path d="M13 5l7 7-7 7"/></svg>';
const STOP_ICON = '<svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>';
const STOPPING_ICON = '<svg aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><path d="M12 3a9 9 0 1 0 9 9"><animateTransform attributeName="transform" type="rotate" from="0 12 12" to="360 12 12" dur="0.8s" repeatCount="indefinite"/></path></svg>';

const boundDraftInputs = new WeakSet<HTMLTextAreaElement>();

function buttonById(document: Document, id: string): HTMLButtonElement | null {
  const element = document.getElementById(id);
  return element?.tagName === 'BUTTON' ? element as HTMLButtonElement : null;
}

function composerTextarea(document: Document): HTMLTextAreaElement | null {
  const element = document.getElementById('userInput');
  return element?.tagName === 'TEXTAREA' ? element as HTMLTextAreaElement : null;
}

function composerHasTextDraft(document: Document): boolean {
  return Boolean(composerTextarea(document)?.value.trim());
}

function bindDraftRefresh(input: ComposerSendControlsInput): void {
  const textarea = composerTextarea(input.document);
  if (!textarea || boundDraftInputs.has(textarea)) return;
  textarea.addEventListener('input', input.requestRefresh);
  boundDraftInputs.add(textarea);
}

function queueBadge(queueCount: number): string {
  const count = Number.isFinite(queueCount)
    ? Math.max(0, Math.floor(queueCount)) : 0;
  return count > 0 ? `<span class="queue-badge">${count}</span>` : '';
}

function setAccessibleLabel(
  button: HTMLButtonElement,
  title: string,
  ariaLabel: string,
): void {
  button.title = title;
  button.setAttribute('aria-label', ariaLabel);
}

function showSend(
  button: HTMLButtonElement,
  input: ComposerSendControlsInput,
): void {
  button.hidden = false;
  button.className = 'send-btn';
  button.style.cursor = '';
  button.removeAttribute('aria-busy');
  setAccessibleLabel(button, input.labels.sendTitle, input.labels.send);
  button.innerHTML = SEND_ICON;
  button.onclick = input.onSend;
}

function showStop(
  button: HTMLButtonElement,
  input: ComposerSendControlsInput,
): void {
  const label = input.stopping ? input.labels.stopping : input.labels.stop;
  const separateClass = button.id === STOP_BUTTON_ID ? ' composer-stop-btn' : '';
  button.hidden = false;
  button.className = input.stopping
    ? 'send-btn stop-btn is-stopping' + separateClass
    : 'send-btn stop-btn' + separateClass;
  button.style.cursor = input.stopping ? 'progress' : '';
  button.setAttribute('aria-busy', String(input.stopping));
  setAccessibleLabel(button, label, label);
  button.innerHTML = (input.stopping ? STOPPING_ICON : STOP_ICON)
    + queueBadge(input.queueCount);
  button.onclick = input.onStop;
}

function hideSeparateStop(
  document: Document,
  sendButton: HTMLButtonElement,
): void {
  const stopButton = buttonById(document, STOP_BUTTON_ID);
  if (stopButton) {
    stopButton.hidden = true;
    stopButton.onclick = null;
  }
  sendButton.parentElement?.classList.remove(SPLIT_CLASS);
}

/** Project authoritative composer state onto its click and touch controls. */
export function updateComposerSendControls(
  input: ComposerSendControlsInput,
): ComposerSendControlsState {
  const busy = input.translating || input.startupConnecting
    || input.turnBusy || input.stopping;
  const hasDraft = composerHasTextDraft(input.document)
    || input.hasAttachmentDraft;
  const sendButton = buttonById(input.document, SEND_BUTTON_ID);
  bindDraftRefresh(input);
  if (!sendButton) return { busy, hasDraft, splitControls: false };

  /* One control, one state: a live turn never coexists with a Send affordance
   * (queueing a follow-up mid-turn stays on the Enter key path). Any separate
   * stop button left by an older runtime is always retired. */
  hideSeparateStop(input.document, sendButton);
  if (busy) showStop(sendButton, input);
  else showSend(sendButton, input);
  return { busy, hasDraft, splitControls: false };
}
