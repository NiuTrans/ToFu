/**
 * System-note and grouped context-note DOM projections. Generated reason
 * taxonomy remains the source of presentation labels and glyphs.
 * Entry points: renderSystemNoteBlock, renderSystemNoteGroupBlock.
 * Dependencies: conversation-view-model, system-note-presentation.generated,
 * context-note-presentation, context-note-grouping, shared icons, ports,
 * stable-dom, localized-copy.
 */
import { iconHtml } from '../../../icons';
import type { ConversationBlockViewModel } from '../../presentation/conversation-view-model';
import {
  contextNoteFacet,
  contextNoteTextRuns,
  type ContextNoteFacet,
} from '../../presentation/context-note-presentation';
import { isContextInjectionNote } from '../../presentation/context-note-grouping';
import type { ContextProvenanceFact } from '../../presentation/turn-provenance';
import {
  SYSTEM_NOTE_REASON_PRESENTATION as SYSTEM_NOTE_REASON_CHIP,
} from '../system-note-presentation.generated';
import type { ClassicConversationRendererPorts } from './ports';
import { preserveDetailsOpen } from './stable-dom';
import { textFor } from './localized-copy';

const SYSTEM_NOTE_STALL_ICON_HTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em"><path d="M3 2v6h6"/><path d="M3 8a9 9 0 1 0 3-5.7L3 8"/></svg>';

const SYSTEM_NOTE_TODO_ICON_HTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em"><path d="M9 11l3 3L22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>';

const SYSTEM_NOTE_CONTEXT_ICON_HTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em"><path d="M12 5v14"/><path d="M5 12h14"/></svg>';

const SYSTEM_NOTE_SESSION_ICON_HTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>';

const SYSTEM_NOTE_MODIFIED_FILES_ICON_HTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>';

/* Data-driven noteKind presentation. Unknown kinds fall back to the generic
 * intervention entry (RENDER_CONTRACT: unknown note kinds render as a generic
 * system note), so a newer backend never breaks an older client. */
const SYSTEM_NOTE_PRESENTATION = {
  'intent-stall': { icon: SYSTEM_NOTE_STALL_ICON_HTML, labelKey: 'systemNote.intentStallLabel' },
  'todo-continuation': { icon: SYSTEM_NOTE_TODO_ICON_HTML, labelKey: 'systemNote.todoContinuationLabel' },
  'system-intervention': { icon: SYSTEM_NOTE_STALL_ICON_HTML, labelKey: 'systemNote.interventionLabel' },
  'context-injection': { icon: SYSTEM_NOTE_CONTEXT_ICON_HTML, labelKey: 'systemNote.contextInjectionLabel' },
  'session-context': { icon: SYSTEM_NOTE_SESSION_ICON_HTML, labelKey: 'systemNote.sessionContextLabel' },
} as const;

const SYSTEM_NOTE_GENERIC_PRESENTATION = {
  icon: SYSTEM_NOTE_STALL_ICON_HTML, labelKey: 'systemNote.interventionLabel',
} as const;

/* The noteKind alone cannot tell one generic context-injection from another;
 * the durable `reason` (registry id) distinguishes the active-checklist
 * snapshot so it gets its own first-class title + task-list icon instead of
 * hiding behind the card's buried "原因: active_checklist" line. Unknown
 * reasons keep the noteKind presentation, preserving the generic fallback. */
const SYSTEM_NOTE_REASON_PRESENTATION = {
  active_checklist: { icon: SYSTEM_NOTE_TODO_ICON_HTML, labelKey: 'systemNote.activeChecklistLabel' },
  modified_files: { icon: SYSTEM_NOTE_MODIFIED_FILES_ICON_HTML, labelKey: 'systemNote.modifiedFilesLabel' },
} as const;

/* Per-reason title chip shown INSIDE the ptool-text title line (the reason no
 * longer lives in the card head). The map is GENERATED from the backend
 * registry's InjectionSpec.presentation (system-note-presentation.generated.ts)
 * — the single source of truth — so this vocabulary is never hand-duplicated.
 * Unknown reasons keep a neutral chip with the raw id so a newer backend never
 * renders blank. The runtime surface embeds the same generated contract
 * (_SEG_NOTE_REASON_CHIP, tool_rounds.js). */
function systemNoteReasonChip(
  document: Document,
  ports: ClassicConversationRendererPorts,
  reason: string,
): HTMLElement {
  const chip = Object.prototype.hasOwnProperty.call(SYSTEM_NOTE_REASON_CHIP, reason)
    ? SYSTEM_NOTE_REASON_CHIP[reason] : undefined;
  const span = document.createElement('span');
  span.className = `sw-note-reason sw-note-reason-${chip ? chip.tone : 'info'}`;
  span.textContent = chip ? textFor(ports, chip.labelKey, chip.fallback) : reason;
  span.title = `${textFor(ports, 'systemNote.reasonLabel', 'Reason')}: ${reason}`;
  return span;
}

/* Engine-authored intervention (stall nudge / todo reminder) carried as a
 * durable system_note segment. Renders in the injection-chip visual family,
 * collapsed by default; action details are plain text, never markdown.
 * Appending context is not an acknowledgement of model consumption. */
type SystemNoteBlock = Extract<ConversationBlockViewModel, { kind: 'system-note' }>;

const SYSTEM_NOTE_ACTION_LABELS = {
  prompt: ['systemNote.actionPrompt', 'Context appended'],
  stop: ['systemNote.actionStop', 'Stopped'],
  'tool-policy': ['systemNote.actionToolPolicy', 'Tool policy changed'],
  continue: ['systemNote.actionContinue', 'Continuation requested'],
} as const;

function systemNotePresentation(
  block: SystemNoteBlock,
): { icon: string; labelKey: string } {
  const reasonPresentation = block.reason && Object.prototype.hasOwnProperty.call(SYSTEM_NOTE_REASON_PRESENTATION, block.reason)
    ? SYSTEM_NOTE_REASON_PRESENTATION[block.reason as keyof typeof SYSTEM_NOTE_REASON_PRESENTATION] : undefined;
  return reasonPresentation
    ?? (Object.prototype.hasOwnProperty.call(SYSTEM_NOTE_PRESENTATION, block.noteKind)
      ? SYSTEM_NOTE_PRESENTATION[block.noteKind as keyof typeof SYSTEM_NOTE_PRESENTATION] : undefined)
    ?? SYSTEM_NOTE_GENERIC_PRESENTATION;
}

function systemNoteHeader(
  document: Document,
  ports: ClassicConversationRendererPorts,
  block: SystemNoteBlock,
): HTMLElement {
  const presentation = systemNotePresentation(block);
  const header = document.createElement('div');
  const icon = document.createElement('span');
  icon.className = 'ptool-icon';
  icon.innerHTML = presentation.icon;
  const label = document.createElement('span');
  label.className = 'ptool-text';
  label.textContent = block.injectionId
    ? `${textFor(ports, presentation.labelKey, 'System note')} · ${block.injectionId}`
    : textFor(ports, presentation.labelKey, 'System note');
  const badge = document.createElement('span');
  badge.className = 'ptool-badge ptool-badge-info';
  const actionLabel = Object.prototype.hasOwnProperty.call(SYSTEM_NOTE_ACTION_LABELS, block.action ?? 'prompt')
    ? SYSTEM_NOTE_ACTION_LABELS[block.action ?? 'prompt'] : SYSTEM_NOTE_ACTION_LABELS.prompt;
  badge.textContent = textFor(ports, actionLabel[0], actionLabel[1]);
  if (block.reason) {
    header.append(icon, label, systemNoteReasonChip(document, ports, block.reason), badge);
  } else {
    header.append(icon, label, badge);
  }
  return header;
}

function systemNoteBodyCard(
  document: Document,
  ports: ClassicConversationRendererPorts,
  block: SystemNoteBlock,
): HTMLElement {
  const card = document.createElement('div');
  card.className = 'sw-card sw-stall-card-item';
  const head = document.createElement('div');
  head.className = 'sw-card-head';
  const role = document.createElement('span');
  role.className = 'sw-card-role';
  role.textContent = textFor(ports, 'systemNote.detailsLabel', 'Recorded intervention');
  head.appendChild(role);
  const pre = document.createElement('pre');
  pre.className = 'sw-card-raw-pre';
  pre.textContent = block.text;
  card.append(head, pre);
  return card;
}

/* ── Context notes ─────────────────────────────────────────────────────
 * A context note reads as one row of a list: a glyph sticker tinted by the
 * block's authority, its registry label, a quiet size or count, and a
 * one-line glimpse. The row's title already says what was injected, so the
 * repeated kind title, raw reason id and "appended" badge stay out of view
 * (the id remains the title's tooltip). Multi-line text opens in place;
 * a single line is shown whole and needs no disclosure. */

function element<K extends keyof HTMLElementTagNameMap>(
  document: Document, tag: K, className: string, text?: string,
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function contextNoteLabel(ports: ClassicConversationRendererPorts, facet: ContextNoteFacet): string {
  return facet.labelKey ? textFor(ports, facet.labelKey, facet.fallbackLabel) : facet.fallbackLabel;
}

function noteSticker(document: Document, glyph: string): HTMLElement {
  const sticker = element(document, 'span', 'sw-note-sticker');
  sticker.dataset.glyph = glyph;
  sticker.setAttribute('aria-hidden', 'true');
  sticker.innerHTML = iconHtml(glyph, 14);
  return sticker;
}

/* Verbatim text, byte for byte: wrapper-tag lines are quieted and Markdown
 * headings emphasised by wrapping them, never by rewriting them. */
function verbatimNoteText(document: Document, text: string, inline: boolean): HTMLElement {
  const pre = element(document, 'pre', inline ? 'sw-note-text sw-note-text--inline' : 'sw-note-text');
  for (const run of contextNoteTextRuns(text)) {
    if (run.kind === 'text') pre.append(run.text);
    else pre.append(element(document, 'span', `sw-note-text-${run.kind}`, run.text));
  }
  return pre;
}

function pairedProvenance(
  ports: ClassicConversationRendererPorts, item: SystemNoteBlock,
): ContextProvenanceFact | undefined {
  return item.provenance ? ports.contextProvenanceFacts?.(item.provenance)[0] : undefined;
}

/* The row title already names the kind ("Relevant memories"), so a counted
 * fact shows only its number. */
function pairedMeta(
  ports: ClassicConversationRendererPorts, fact: ContextProvenanceFact | undefined,
): string {
  if (!fact) return '';
  return fact.count > 0
    ? textFor(ports, 'systemNote.groupItemsTitle', '{n} items', { n: fact.count })
    : fact.label;
}

/* Acquisition metadata the note witnesses: its sentence and facts, then the
 * escaped list. Without structured facts, fall back to the receipt markup or,
 * port-less, to the raw record so nothing is silently dropped. */
function pairedProvenanceBody(
  document: Document, ports: ClassicConversationRendererPorts,
  item: SystemNoteBlock, fact: ContextProvenanceFact | undefined,
): HTMLElement {
  const body = element(document, 'div', 'sw-note-item-body sw-note-provenance');
  if (fact) {
    body.dataset.provenanceField = fact.field;
    const caption = [fact.headline, fact.detail].filter(Boolean).join(' · ');
    if (caption) body.append(element(document, 'p', 'sw-note-caption', caption));
    if (fact.bodyHtml) body.insertAdjacentHTML('beforeend', fact.bodyHtml);
    return body;
  }
  const html = ports.renderContextProvenanceHtml?.(item.provenance);
  if (html) body.innerHTML = html;
  else body.append(element(document, 'pre', 'sw-note-text', JSON.stringify(item.provenance, null, 2)));
  return body;
}

function contextNoteItem(
  document: Document,
  ports: ClassicConversationRendererPorts,
  item: SystemNoteBlock,
  options: Readonly<{ collapsible: boolean; open: boolean }>,
): HTMLElement {
  const facet = contextNoteFacet(item);
  const fact = pairedProvenance(ports, item);
  const multiline = Boolean(item.provenance) || facet.readableLines > 1;
  const disclosure = multiline && options.collapsible;
  const row = document.createElement(disclosure ? 'details' : 'div');
  row.className = 'sw-note-group-item';
  row.dataset.blockId = item.blockId;
  row.dataset.noteKind = item.noteKind;
  row.dataset.glyph = facet.glyph;
  if (item.reason) row.dataset.reason = item.reason;
  if (facet.authority) row.dataset.authority = facet.authority;
  if (disclosure) (row as HTMLDetailsElement).open = options.open;

  const head = element(document, disclosure ? 'summary' : 'div', 'sw-note-item-head');
  const title = element(document, 'span', 'sw-note-item-title', contextNoteLabel(ports, facet));
  if (facet.reason) {
    title.title = `${textFor(ports, 'systemNote.reasonLabel', 'Reason')}: ${facet.reason}`;
  }
  head.append(noteSticker(document, facet.glyph), title);
  const meta = item.provenance ? pairedMeta(ports, fact)
    : facet.readableLines > 1
      ? textFor(ports, 'systemNote.lineCount', '{n} lines', { n: facet.readableLines }) : '';
  if (meta) head.append(element(document, 'span', 'sw-note-item-meta', meta));
  const peek = !disclosure ? '' : item.provenance ? fact?.peek ?? '' : facet.peek;
  if (peek) head.append(element(document, 'span', 'sw-note-item-peek', peek));
  if (!multiline && item.text) head.append(verbatimNoteText(document, item.text, true));
  row.append(head);

  if (item.provenance) {
    row.append(pairedProvenanceBody(document, ports, item, fact));
  } else if (multiline) {
    const body = element(document, 'div', 'sw-note-item-body');
    body.append(verbatimNoteText(document, item.text, false));
    row.append(body);
  }
  return row;
}

/* Unmatched acquisition receipts lead the fold, visibly apart from the notes
 * whose text was injected. */
function contextReceipts(
  document: Document, ports: ClassicConversationRendererPorts, value: unknown,
): HTMLElement {
  const receipts = element(document, 'div', 'sw-note-receipts');
  const html = ports.renderContextProvenanceHtml?.(value);
  if (html) receipts.innerHTML = html;
  else receipts.append(element(document, 'pre', 'sw-note-text', JSON.stringify(value, null, 2)));
  return receipts;
}

function contextSummary(
  document: Document, ports: ClassicConversationRendererPorts,
  names: readonly string[], count: number | null,
): HTMLElement {
  const summary = element(document, 'summary', 'ptool-line sw-inbox-row-header');
  const icon = element(document, 'span', 'ptool-icon');
  icon.innerHTML = iconHtml('layers', 14);
  summary.append(icon, element(document, 'span', 'ptool-text',
    textFor(ports, 'systemNote.groupLabel', 'Context notes')));
  if (names.length) {
    summary.append(element(document, 'span', 'sw-note-group-names', names.join(' · ')));
  }
  if (count !== null) {
    summary.append(element(document, 'span', 'sw-note-group-count',
      textFor(ports, 'systemNote.groupItemsTitle', '{n} items', { n: count })));
  }
  return summary;
}

function openContextItems(node: HTMLElement): ReadonlySet<string> {
  return new Set(Array.from(
    node.querySelectorAll<HTMLElement>('details.sw-note-group-item[open]'),
    (item) => item.dataset.blockId ?? '',
  ));
}

function renderContextNoteBlock(
  node: HTMLElement,
  block: SystemNoteBlock,
  ports: ClassicConversationRendererPorts,
): void {
  const document = node.ownerDocument;
  const details = element(document, 'details', 'sw-inbox-row sw-system-note-row sw-note-solo');
  details.open = preserveDetailsOpen(node, false);
  details.dataset.noteKind = block.noteKind;
  details.dataset.contextNote = 'true';
  if (block.injectionId) details.dataset.injectionId = block.injectionId;
  if (block.reason) details.dataset.reason = block.reason;
  const body = element(document, 'div', 'sw-inbox-row-body sw-note-group-body');
  const list = element(document, 'div', 'sw-note-list');
  list.append(contextNoteItem(document, ports, block, { collapsible: false, open: true }));
  body.append(list);
  details.append(
    contextSummary(document, ports, [contextNoteLabel(ports, contextNoteFacet(block))], null),
    body,
  );
  node.replaceChildren(details);
}

export function renderSystemNoteBlock(
  node: HTMLElement,
  block: SystemNoteBlock,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-block conversation-block--system-note';
  if (isContextInjectionNote(block)) {
    renderContextNoteBlock(node, block, ports);
    return;
  }
  const document = node.ownerDocument;
  const details = document.createElement('details');
  details.className = 'sw-inbox-row sw-system-note-row';
  details.open = preserveDetailsOpen(node, false);
  details.dataset.noteKind = block.noteKind;
  if (block.injectionId) details.dataset.injectionId = block.injectionId;
  if (block.reason === 'active_checklist') details.dataset.checklist = 'true';
  if (block.reason) details.dataset.reason = block.reason;
  const summary = document.createElement('summary');
  summary.className = 'ptool-line sw-inbox-row-header';
  const header = systemNoteHeader(document, ports, block);
  summary.append(...Array.from(header.childNodes));
  const body = document.createElement('div');
  body.className = 'sw-inbox-row-body';
  body.appendChild(systemNoteBodyCard(document, ports, block));
  details.append(summary, body);
  node.replaceChildren(details);
}

/* A merged run of adjacent context-injection notes: ONE disclosure whose
 * summary names what was injected, and whose body lists each member note as
 * one row (unmatched acquisition receipts first). Member positions are
 * unchanged; each row keeps the reader's open state across live growth. */
export function renderSystemNoteGroupBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'system-note-group' }>,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-block conversation-block--system-note conversation-block--system-note-group';
  const document = node.ownerDocument;
  const openItems = openContextItems(node);
  const details = element(document, 'details', 'sw-inbox-row sw-system-note-row sw-note-group');
  /* A live-growing group defeats the sameBlock short-circuit (items.length
   * changes), so without this read-back a reader's open state would snap
   * back to collapsed every time a new injection lands mid-turn. */
  details.open = preserveDetailsOpen(node, false);
  details.dataset.noteGroup = 'context-injection';
  const body = element(document, 'div', 'sw-inbox-row-body sw-note-group-body');
  if (block.provenance) body.append(contextReceipts(document, ports, block.provenance));
  const list = element(document, 'div', 'sw-note-list');
  for (const item of block.items) {
    list.append(contextNoteItem(document, ports, item, {
      collapsible: true, open: openItems.has(item.blockId),
    }));
  }
  body.append(list);
  const names = block.items.map((item) => contextNoteLabel(ports, contextNoteFacet(item)));
  details.append(contextSummary(document, ports, names, block.items.length), body);
  node.replaceChildren(details);
}
