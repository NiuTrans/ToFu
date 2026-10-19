/** Text selection actions and quote previews. Owns ephemeral DOM/listeners only;
 * composer quotes and authoritative branch creation remain injected ports.
 * Entry points: createSelectionPopup, renderReplyQuotePreviews.
 */
import { createButton, createIconButton } from './ui';
import { iconHtml } from './icons';
import { createLifecycleScope } from './lifecycle';
import type { Translator } from './i18n';

export interface SelectedPassage {
  conversationId: string;
  turnId: string;
  text: string;
}
interface SelectionPopupOptions {
  t: Translator;
  conversationId(): string | null;
  quote(passage: SelectedPassage): void;
  branch(passage: SelectedPassage): Promise<boolean>;
}

const editable = 'input, textarea, select, button, [contenteditable]:not([contenteditable="false"]), [role="textbox"]';
function parentElement(node: Node | null): Element | null {
  return node instanceof Element ? node : node?.parentElement ?? null;
}
function glyph(name: string): HTMLElement {
  const span = document.createElement('span');
  span.className = 'selection-action-icon';
  span.setAttribute('aria-hidden', 'true');
  span.innerHTML = iconHtml(name);
  return span;
}

export function createSelectionPopup(options: SelectionPopupOptions) {
  const scope = createLifecycleScope();
  const popup = document.createElement('div');
  popup.className = 'selection-popup';
  popup.hidden = true;
  const toolbar = document.createElement('div');
  toolbar.className = 'selection-popup-actions';
  toolbar.setAttribute('role', 'toolbar');
  const hint = document.createElement('div');
  hint.className = 'selection-popup-hint';
  hint.id = 'selection-popup-hint';
  hint.setAttribute('role', 'status');
  popup.append(toolbar, hint);
  document.body.append(popup);
  let captured: (SelectedPassage & { range: Range; rangeText: string; message: HTMLElement }) | null = null;
  let pending = false;
  let frame = 0;
  let dragging = false;
  let dismissedSelection = '';

  function hide() {
    popup.hidden = true;
    captured = null;
  }
  function valid() {
    return captured && captured.conversationId === options.conversationId()
      && captured.message.isConnected && captured.range.toString() === captured.rangeText;
  }
  const quote = createButton({ label: '', variant: 'ghost', onClick: () => {
    if (!valid() || pending) { hide(); return; }
    const passage = captured!;
    dismissedSelection = passage.text;
    hide();
    window.getSelection()?.removeAllRanges();
    options.quote(passage);
  } });
  const branch = createButton({ label: '', variant: 'ghost', onClick: () => { void startBranch(); } });
  quote.dataset.action = 'reply';
  branch.dataset.action = 'branch';
  for (const button of [quote, branch]) {
    button.setAttribute('aria-describedby', hint.id);
    toolbar.append(button);
  }
  function labels() {
    const quoteLabel = options.t('selection.quote');
    const branchLabel = options.t('selection.branch');
    // Locale data loads asynchronously. Never show internal keys while it loads.
    if (quoteLabel === 'selection.quote' || branchLabel === 'selection.branch') return false;
    quote.replaceChildren(glyph('quote'), document.createTextNode(quoteLabel));
    branch.replaceChildren(glyph('gitBranch'), document.createTextNode(branchLabel));
    quote.title = options.t('selection.quoteHint');
    const running = ['pending', 'running'].includes(captured?.message.dataset.turnStatus ?? '');
    branch.disabled = pending || running;
    branch.title = options.t(running ? 'selection.branchPending' : 'selection.branchHint');
    toolbar.setAttribute('aria-label', options.t('selection.actions'));
    hint.textContent = pending ? options.t('selection.creating') : quote.title;
    return true;
  }
  async function startBranch() {
    if (!valid() || pending) return;
    const passage = captured!;
    pending = true;
    quote.setAttribute('aria-disabled', 'true');
    branch.setAttribute('aria-disabled', 'true');
    popup.setAttribute('aria-busy', 'true');
    hint.textContent = options.t('selection.creating');
    let succeeded = false;
    try { succeeded = await options.branch(passage); }
    catch { /* The inline failure retains the passage and offers retry. */ }
    finally {
      pending = false;
      quote.removeAttribute('aria-disabled');
      branch.removeAttribute('aria-disabled');
      popup.removeAttribute('aria-busy');
    }
    if (scope.signal.aborted) return;
    if (captured !== passage || !valid()) { hide(); return; }
    if (succeeded) {
      dismissedSelection = passage.text;
      hide();
      window.getSelection()?.removeAllRanges();
    } else {
      hint.textContent = options.t('selection.failed');
    }
  }
  function position(range: Range) {
    const viewport = window.visualViewport;
    const left = viewport?.offsetLeft ?? 0, top = viewport?.offsetTop ?? 0;
    const width = viewport?.width ?? window.innerWidth;
    const height = viewport?.height ?? window.innerHeight;
    const gap = parseFloat(getComputedStyle(popup).getPropertyValue('--space-2')) || 8;
    const rects = Array.from(range.getClientRects()).filter(rect => rect.bottom >= top && rect.top <= top + height);
    const selection = window.getSelection();
    const backwards = selection?.focusNode === range.startContainer && selection.focusOffset === range.startOffset;
    const rect = (backwards ? rects[0] : rects.at(-1)) ?? range.getBoundingClientRect();
    if (rect.bottom < top || rect.top > top + height) { hide(); return; }
    const box = popup.getBoundingClientRect();
    popup.style.left = `${Math.max(left + gap, Math.min(rect.left + rect.width / 2 - box.width / 2, left + width - box.width - gap))}px`;
    const selectionBounds = range.getBoundingClientRect();
    const above = selectionBounds.top - box.height - gap;
    popup.style.top = `${Math.max(top + gap, Math.min(above >= top + gap ? above : selectionBounds.bottom + gap, top + height - box.height - gap))}px`;
  }
  function refresh() {
    frame = 0;
    if (pending || toolbar.contains(document.activeElement)) return;
    const selection = window.getSelection();
    const text = selection?.toString().trim() ?? '';
    if (!text) dismissedSelection = '';
    if (!selection?.rangeCount || !text || text === dismissedSelection || selection.isCollapsed) { hide(); return; }
    const range = selection.getRangeAt(0);
    const start = parentElement(range.startContainer), end = parentElement(range.endContainer);
    const message = start?.closest<HTMLElement>('.message[data-turn-id]');
    const conversationId = options.conversationId();
    if (!conversationId || !message || end?.closest('.message[data-turn-id]') !== message
      || !start?.closest('.message-content') || !end?.closest('.message-content')
      || start.closest(editable) || end.closest(editable)
      || (message.closest<HTMLElement>('[data-conversation-id]')?.dataset.conversationId ?? conversationId) !== conversationId) {
      hide(); return;
    }
    captured = { conversationId, turnId: message.dataset.turnId!, text, range: range.cloneRange(), rangeText: range.toString(), message };
    if (!labels()) { hide(); return; }
    popup.hidden = false;
    position(range);
  }
  function schedule() {
    scope.cancelFrame(frame);
    frame = scope.frame(refresh);
  }
  scope.listen(document, 'pointerdown', event => {
    if (popup.contains(event.target as Node)) return;
    dragging = true;
    dismissedSelection = '';
    hide();
  });
  scope.listen(document, 'pointerup', () => { dragging = false; schedule(); });
  scope.listen(document, 'pointercancel', () => { dragging = false; hide(); });
  // Prevent mouse activation from collapsing the native selection before click.
  scope.listen(popup, 'mousedown', event => { event.preventDefault(); });
  scope.listen(document, 'selectionchange', () => {
    if (!dragging) schedule();
  });
  scope.listen(document, 'keydown', event => {
    const key = event as KeyboardEvent;
    if (popup.hidden) return;
    if (key.key === 'Escape') {
      dismissedSelection = captured?.text ?? '';
      const focused = toolbar.contains(document.activeElement);
      hide();
      if (focused) window.getSelection()?.removeAllRanges();
      return;
    }
    if (key.key === 'Tab' && !key.shiftKey && !toolbar.contains(document.activeElement)
      && !parentElement(document.activeElement)?.closest(editable)) {
      key.preventDefault(); quote.focus();
    } else if (toolbar.contains(document.activeElement) && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(key.key)) {
      key.preventDefault();
      (key.key === 'Home' ? quote : key.key === 'End' ? branch : document.activeElement === quote ? branch : quote).focus();
    }
  });
  for (const button of [quote, branch]) {
    for (const event of ['pointerenter', 'focus']) scope.listen(button, event, () => {
      if (!pending) hint.textContent = button.title;
    });
  }
  scope.listen(popup, 'focusout', () => { scope.frame(() => { if (!pending && !popup.contains(document.activeElement)) hide(); }); });
  scope.listen(window, 'scroll', event => { if (!(event.target instanceof Node) || !popup.contains(event.target)) hide(); }, { capture: true, passive: true });
  scope.listen(window, 'resize', hide);
  if (window.visualViewport) {
    scope.listen(window.visualViewport, 'resize', hide);
    scope.listen(window.visualViewport, 'scroll', hide);
  }
  scope.listen(window, 'tofu:language-change', () => {
    if (!popup.hidden && captured) { labels(); position(captured.range); }
    else schedule();
  });
  scope.add(() => popup.remove());
  return { hide, destroy: () => scope.destroy() };
}

/** Render caller-owned quotes without altering their exact submission text. */
export function renderReplyQuotePreviews(container: HTMLElement, quotes: readonly string[], t: Translator, remove: (index: number) => void) {
  container.hidden = quotes.length === 0;
  const focusedIndex = Array.from(container.querySelectorAll('button')).indexOf(document.activeElement as HTMLButtonElement);
  const expanded = new Set(Array.from(container.querySelectorAll<HTMLDetailsElement>('details[open]')).map(node => node.dataset.quote));
  container.replaceChildren(...quotes.map((text, index) => {
    const row = document.createElement('div');
    row.className = 'reply-quote-preview';
    const details = document.createElement('details');
    details.dataset.quote = text;
    details.open = expanded.has(text);
    const summary = document.createElement('summary');
    const label = document.createElement('span');
    label.className = 'reply-quote-preview-label';
    label.textContent = t('selection.quoted');
    const preview = document.createElement('span');
    preview.className = 'reply-quote-preview-text';
    preview.textContent = text.replace(/\s+/g, ' ');
    summary.title = t('selection.preview');
    summary.append(label, preview);
    const full = document.createElement('blockquote');
    full.textContent = text;
    details.append(summary, full);
    row.append(details, createIconButton({ icon: glyph('x'), label: t('selection.removeQuote'), onClick: () => remove(index) }));
    return row;
  }));
  if (focusedIndex >= 0) {
    const buttons = container.querySelectorAll<HTMLButtonElement>('button');
    (buttons[Math.min(focusedIndex, buttons.length - 1)] ?? document.getElementById('userInput'))?.focus();
  }
}
