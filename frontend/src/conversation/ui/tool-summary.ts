/** Shared action identity below native intent headers. Pure DOM decoration;
 * evidence, disclosures, actions and reconciliation remain with their owners.
 * Used by Surface/default and classic rich rendering (including lazy hydration).
 */
import type { ConversationBlockViewModel } from '../presentation/conversation-view-model';
import { setStableData } from './renderers/stable-dom';

const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object'
  && !Array.isArray(value) ? value as Record<string, unknown> : {};
const text = (value: unknown): string => typeof value === 'string' ? value : '';

export function toolSummaryIdentity(block: ConversationBlockViewModel): { name: string; target: string; intent: string } {
  if (block.kind !== 'tool' && block.kind !== 'program') return { name: '', target: '', intent: '' };
  const source = record(block.source);
  const round = record(block.round);
  let input = round.toolArgs ?? source.input;
  if (typeof input === 'string' && input.length <= 120_000) {
    try { input = JSON.parse(input); } catch { input = null; }
  }
  const args = record(input);
  const meta = record(Array.isArray(round.results) ? round.results[0] : null);
  const name = text(round.toolName) || text(source.name) || text(source.toolName);
  const paths = Array.isArray(args.paths) ? args.paths.slice(0, 3).map(text).join(', ') : '';
  const target = [args.path, args.file_path, paths, args.command, args.url, args.query, args.pattern,
    args.cwd, args.root, meta.command].map(text).find(Boolean) ?? '';
  const intent = text(args.description) || text(meta.description) || text(round.query) || name;
  return { name, target: target.slice(0, 2000), intent: intent.slice(0, 2000) };
}

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
