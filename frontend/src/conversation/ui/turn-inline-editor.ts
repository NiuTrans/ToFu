/**
 * In-place editor for one rendered conversation turn.
 *
 * Replaces the detached prompt modal: the turn's rendered blocks are hidden
 * and a textarea session is mounted where the content was. The surface
 * re-keys and re-orders turn DOM on every authoritative commit, so the
 * session owns a persistent host element that reconcileTurnInlineEditors()
 * re-attaches and re-positions after each commit — draft, undo stack, and
 * focus survive repaints. Store authority stays with the caller; this module
 * only reports submit/cancel intents and never mutates conversation state.
 */

export interface TurnInlineEditorSubmit {
  readonly text: string;
  readonly resend: boolean;
}

export interface TurnInlineEditorOptions {
  readonly conversationId: string;
  readonly turnId: string;
  readonly text: string;
  /** Attachment-carrying turns may save an empty draft (modal parity). */
  readonly allowEmpty?: boolean;
  /** Human turns followed by a generated turn expose Save & Resend. */
  readonly canResend?: boolean;
  readonly findTurnNode: (turnId: string) => HTMLElement | null;
  readonly translate?: (key: string) => string;
  /** Resolve true to close the editor; false keeps the draft (failure was
   * already reported by the caller). */
  readonly onSubmit: (submit: TurnInlineEditorSubmit) => Promise<boolean> | boolean;
  readonly onCancel?: () => void;
}

export interface TurnInlineEditorSession {
  readonly conversationId: string;
  readonly turnId: string;
  draft(): string;
  close(): void;
}

interface TurnInlineEditorRecord {
  readonly options: TurnInlineEditorOptions;
  host: HTMLElement;
  textarea: HTMLTextAreaElement;
  saveButton: HTMLButtonElement;
  resendButton: HTMLButtonElement;
  cancelButton: HTMLButtonElement;
  hadFocus: boolean;
  busy: boolean;
  closed: boolean;
}

const activeEditors = new Map<string, TurnInlineEditorRecord>();

function childPart(parent: HTMLElement, part: string): HTMLElement | null {
  for (const child of Array.from(parent.children)) {
    if ((child as HTMLElement).dataset?.conversationPart === part) {
      return child as HTMLElement;
    }
  }
  return null;
}

function localized(
  options: TurnInlineEditorOptions,
  key: string,
  fallback: string,
): string {
  const value = options.translate?.(key);
  return value && value !== key ? value : fallback;
}

function fitEditorTextarea(textarea: HTMLTextAreaElement): void {
  textarea.style.height = 'auto';
  const viewHeight = textarea.ownerDocument.defaultView?.innerHeight || 800;
  const maxHeight = Math.max(160, Math.floor(viewHeight * 0.45));
  const contentHeight = textarea.scrollHeight || 0;
  if (contentHeight > 0) {
    textarea.style.height = `${Math.min(contentHeight, maxHeight)}px`;
  }
  textarea.style.overflowY = contentHeight > maxHeight ? 'auto' : 'hidden';
}

function refreshControls(record: TurnInlineEditorRecord): void {
  const emptyForbidden = !record.options.allowEmpty && !record.textarea.value.trim();
  record.saveButton.disabled = record.busy || emptyForbidden;
  record.resendButton.disabled = record.busy || emptyForbidden;
  record.cancelButton.disabled = record.busy;
}

function setBusy(record: TurnInlineEditorRecord, busy: boolean): void {
  record.host.dataset.submitting = String(busy);
  record.textarea.readOnly = busy;
  refreshControls(record);
}

function closeEditor(
  record: TurnInlineEditorRecord,
  reason: 'cancel' | 'submitted' | 'superseded',
): void {
  if (record.closed) return;
  record.closed = true;
  activeEditors.delete(record.options.turnId);
  const turnNode = record.options.findTurnNode(record.options.turnId);
  if (turnNode?.dataset.inlineEditing) delete turnNode.dataset.inlineEditing;
  record.host.remove();
  if (reason === 'cancel') record.options.onCancel?.();
}

async function submitEditor(
  record: TurnInlineEditorRecord,
  resend: boolean,
): Promise<void> {
  if (record.busy || record.closed) return;
  const text = record.textarea.value.trim();
  if (!text && !record.options.allowEmpty) {
    refreshControls(record);
    return;
  }
  record.busy = true;
  setBusy(record, true);
  try {
    const accepted = await record.options.onSubmit({ text, resend });
    if (!record.closed && accepted) closeEditor(record, 'submitted');
  } catch (_error) {
    /* The caller reports submission failures; the draft simply stays open. */
  } finally {
    if (!record.closed) {
      record.busy = false;
      setBusy(record, false);
    }
  }
}

/* Focus leaves the textarea both when the user clicks away and when a commit
 * detaches the host. Only the click-away case clears hadFocus: at focusout
 * time a detached host is already disconnected, so the intent to keep typing
 * survives the remount. */
function trackFocus(record: TurnInlineEditorRecord): void {
  record.textarea.addEventListener('focusin', () => {
    record.hadFocus = true;
  });
  record.textarea.addEventListener('focusout', () => {
    queueMicrotask(() => {
      if (record.closed) return;
      if (!record.host.isConnected) return;
      const active = record.host.ownerDocument.activeElement;
      if (active && record.host.contains(active)) return;
      record.hadFocus = false;
    });
  });
}

function buildEditorDom(
  record: TurnInlineEditorRecord,
  document: Document,
): void {
  const { options } = record;
  const host = document.createElement('div');
  host.className = 'turn-inline-editor';
  host.dataset.turnInlineEditor = '';
  host.dataset.submitting = 'false';

  const textarea = document.createElement('textarea');
  textarea.className = 'turn-inline-editor-input';
  textarea.rows = 3;
  textarea.spellcheck = true;
  const hint = localized(options, 'editMsg.hintInPlace', 'Edit this message in place');
  textarea.setAttribute('aria-label', hint);
  textarea.value = options.text;

  const footer = document.createElement('div');
  footer.className = 'turn-inline-editor-footer';
  const hintNode = document.createElement('span');
  hintNode.className = 'turn-inline-editor-hint';
  hintNode.dataset.i18n = 'editMsg.hintInPlace';
  hintNode.textContent = hint;

  const buttons = document.createElement('div');
  buttons.className = 'turn-inline-editor-buttons';
  const makeButton = (
    className: string,
    key: string,
    fallback: string,
  ): HTMLButtonElement => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = `turn-inline-editor-btn turn-inline-editor-btn--${className}`;
    button.dataset.i18n = key;
    button.textContent = localized(options, key, fallback);
    return button;
  };
  const cancelButton = makeButton('cancel', 'editMsg.cancel', 'Cancel');
  const resendButton = makeButton('resend', 'editMsg.resend', 'Save & Resend');
  const saveButton = makeButton('save', 'editMsg.save', 'Save');
  resendButton.hidden = !options.canResend;
  buttons.append(cancelButton, resendButton, saveButton);
  footer.append(hintNode, buttons);
  host.append(textarea, footer);

  record.host = host;
  record.textarea = textarea;
  record.saveButton = saveButton;
  record.resendButton = resendButton;
  record.cancelButton = cancelButton;

  cancelButton.addEventListener('click', () => closeEditor(record, 'cancel'));
  saveButton.addEventListener('click', () => void submitEditor(record, false));
  resendButton.addEventListener('click', () => void submitEditor(record, true));
  textarea.addEventListener('input', () => {
    fitEditorTextarea(textarea);
    refreshControls(record);
  });
  textarea.addEventListener('keydown', (event) => {
    if (event.isComposing) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      closeEditor(record, 'cancel');
    } else if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      event.stopPropagation();
      void submitEditor(record, false);
    }
  });
  trackFocus(record);
  refreshControls(record);
}

function positionHost(
  record: TurnInlineEditorRecord,
  turnNode: HTMLElement,
): boolean {
  const content = childPart(turnNode, 'turn-content');
  const blocks = content ? childPart(content, 'turn-blocks') : null;
  if (!content || !blocks) return false;
  turnNode.dataset.inlineEditing = 'true';
  if (record.host.parentElement !== content
      || record.host.previousElementSibling !== blocks) {
    blocks.insertAdjacentElement('afterend', record.host);
  }
  return true;
}

function focusDraftEnd(record: TurnInlineEditorRecord): void {
  const end = record.textarea.value.length;
  record.textarea.focus({ preventScroll: true });
  record.textarea.setSelectionRange(end, end);
}

function sessionFacade(
  record: TurnInlineEditorRecord,
): TurnInlineEditorSession {
  return {
    conversationId: record.options.conversationId,
    turnId: record.options.turnId,
    draft: () => record.textarea.value,
    close: () => closeEditor(record, 'cancel'),
  };
}

/** Mount an inline editor on a rendered turn; null means the caller should
 * use its detached fallback (the turn is windowed out of the DOM). */
export function openTurnInlineEditor(
  options: TurnInlineEditorOptions,
): TurnInlineEditorSession | null {
  const turnNode = options.findTurnNode(options.turnId);
  if (!turnNode) return null;
  for (const record of Array.from(activeEditors.values())) {
    if (record.options.turnId !== options.turnId) {
      closeEditor(record, 'superseded');
    }
  }
  const existing = activeEditors.get(options.turnId);
  if (existing && !existing.closed) {
    focusDraftEnd(existing);
    return sessionFacade(existing);
  }
  const record: TurnInlineEditorRecord = {
    options,
    host: null as unknown as HTMLElement,
    textarea: null as unknown as HTMLTextAreaElement,
    saveButton: null as unknown as HTMLButtonElement,
    resendButton: null as unknown as HTMLButtonElement,
    cancelButton: null as unknown as HTMLButtonElement,
    hadFocus: false,
    busy: false,
    closed: false,
  };
  buildEditorDom(record, turnNode.ownerDocument);
  activeEditors.set(options.turnId, record);
  if (!positionHost(record, turnNode)) {
    activeEditors.delete(options.turnId);
    return null;
  }
  fitEditorTextarea(record.textarea);
  focusDraftEnd(record);
  return sessionFacade(record);
}

/** Re-attach editors after an authoritative surface commit. Commits reuse
 * turn nodes but re-order their parts and may rebuild nodes outright; the
 * persistent host keeps the draft and is simply moved back into place. */
export function reconcileTurnInlineEditors(): void {
  for (const record of activeEditors.values()) {
    if (record.closed) continue;
    const turnNode = record.options.findTurnNode(record.options.turnId);
    /* A missing node means the turn is windowed out or its conversation is
     * no longer rendered; the session stays dormant with its draft. */
    if (!turnNode) continue;
    const wasConnected = record.host.isConnected;
    if (!positionHost(record, turnNode)) continue;
    if (!wasConnected) {
      fitEditorTextarea(record.textarea);
      if (record.hadFocus) focusDraftEnd(record);
    }
  }
}
