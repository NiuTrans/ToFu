/** Shared action identity below native intent headers. Pure DOM decoration;
 * evidence, disclosures, actions and reconciliation remain with their owners.
 * Used by Surface/default and classic rich rendering (including lazy hydration).
 */
import type { ConversationBlockViewModel } from '../presentation/conversation-view-model';
import { toolSummaryIdentity } from '../presentation/tool-summary-identity';
import { setStableData } from './renderers/stable-dom';

const INTENT_SELECTOR = '.ptool-text, .ptool-cmd-desc-inline, .conversation-tool-card__title, '
  + '.ptc-title, .hg-title, .sw-header-title';

/** The header's direct child that carries the native intent text. */
function intentSlot(header: HTMLElement): Element | null {
  let slot: Element | null = header.querySelector(INTENT_SELECTOR);
  while (slot && slot.parentElement !== header) slot = slot.parentElement;
  return slot;
}

/** One metadata slot per native outer summary, never a second card/control. */
export function decorateToolSummary(node: HTMLElement, block: ConversationBlockViewModel): void {
  const identity = toolSummaryIdentity(block);
  if (!identity.name) return;
  let header = node.querySelector<HTMLElement>(
    '.ptool-cmd-header, .ptool-result-header, .ptool-results-header, .ptool-line, '
    + '.ptc-head, .conversation-tool-card__summary, .sw-header, .hg-header, .ptool-todo-header, '
    + '.timer-watcher-header, .conversation-tool-summary');
  if (!header && node.hasChildNodes()) {
    // Structured/custom bodies without an action heading use this same outer
    // grammar. Never wrap/reparent their form, media or interactive evidence.
    header = node.ownerDocument.createElement('div');
    header.className = 'conversation-tool-summary';
    const title = node.ownerDocument.createElement('span'); title.className = 'ptool-text';
    header.append(title); node.prepend(header);
  }
  if (!header) return;
  if (header.classList.contains('conversation-tool-summary')) {
    const title = header.querySelector('.ptool-text')!;
    if (title.textContent !== identity.intent) title.textContent = identity.intent;
  }
  setStableData(header, 'actionSummary', 'true');
  let metadata = header.querySelector<HTMLElement>(':scope > .conversation-tool-identity');
  if (!metadata) {
    metadata = node.ownerDocument.createElement('span');
    metadata.className = 'conversation-tool-identity';
  }
  const value = identity.name + (identity.target ? `  ${identity.target}` : '');
  // The intent clamps to one line and identity waits for an open step, so the
  // tooltip carries both. A title the native renderer set is authoritative.
  const intent = header.querySelector<HTMLElement>(INTENT_SELECTOR);
  if (intent && (!intent.title || intent.dataset.intentTitle === 'true')) {
    const full = [intent.textContent?.trim() ?? '', value].filter(Boolean).join('\n');
    if (intent.title !== full) intent.title = full;
    setStableData(intent, 'intentTitle', 'true');
  }
  // Identity reads inline right after the intent, before the outcome and the
  // native controls; move it only when a native repaint displaced it.
  const slot = intentSlot(header);
  if (slot ? slot.nextElementSibling !== metadata : metadata.parentElement !== header) {
    if (slot) slot.after(metadata);
    else header.append(metadata);
  }
  if (metadata.textContent !== value) metadata.textContent = value;
  if (metadata.title !== value) metadata.title = value;
  // Routine terminal labels repeat the return itself. Preserve all meaningful
  // counts, exit codes, partial effects, warnings and unknown/custom badges.
  for (const badge of header.querySelectorAll<HTMLElement>(
    '.ptool-badge, .conversation-tool-card__chip, .conversation-tool-card__badge')) {
    const routine = /^(?:done|completed|success|ok|完成|已完成|成功)(?:\s*·\s*\d+(?:\.\d+)?s)?$/i
      .test(badge.textContent?.trim() ?? '');
    if (badge.hidden !== routine) badge.hidden = routine;
  }
}
