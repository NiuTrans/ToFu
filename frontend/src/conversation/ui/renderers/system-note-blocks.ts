/**
 * System-note and grouped context-note DOM projections. Generated reason
 * taxonomy remains the source of presentation labels.
 * Entry points: renderSystemNoteBlock, renderSystemNoteGroupBlock.
 * Dependencies: conversation-view-model, system-note-presentation.generated,
 * ports, stable-dom, localized-copy.
 */
import type { ConversationBlockViewModel } from '../../presentation/conversation-view-model';
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
  header.className = 'sw-note-group-item-head';
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

/* The verbatim wire text alone — the group member card already carries its
 * own header row, so the repeated "介入记录" card head would be pure noise. */
function systemNoteItemBody(
  document: Document,
  block: SystemNoteBlock,
): HTMLElement {
  const pre = document.createElement('pre');
  pre.className = 'sw-card-raw-pre';
  pre.textContent = block.text;
  return pre;
}

function contextProvenanceBody(
  document: Document, ports: ClassicConversationRendererPorts, value: unknown,
): HTMLElement {
  const body = document.createElement('div');
  body.className = 'sw-note-provenance';
  const html = ports.renderContextProvenanceHtml?.(value);
  if (html) body.innerHTML = html;
  else {
    const pre = document.createElement('pre');
    pre.className = 'sw-card-raw-pre';
    pre.textContent = JSON.stringify(value, null, 2);
    body.appendChild(pre);
  }
  return body;
}

export function renderSystemNoteBlock(
  node: HTMLElement,
  block: SystemNoteBlock,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-block conversation-block--system-note';
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
  if (block.provenance) {
    // Same dedup as grouped members: provenance is the readable summary, the
    // verbose raw body only repeats it.
    body.appendChild(contextProvenanceBody(document, ports, block.provenance));
  } else {
    body.appendChild(systemNoteBodyCard(document, ports, block));
  }
  details.append(summary, body);
  node.replaceChildren(details);
}

/* The card reused to hold one note's content. A single consistent box (the
 * header row + any provenance meta + the verbatim wire text) so an expanded
 * group reads as a stack of uniform cards, not two mismatched sub-boxes. */
const NOTE_ITEM_CARD = 'sw-note-group-item';

function systemNoteItemCard(
  document: Document,
  ports: ClassicConversationRendererPorts,
  item: SystemNoteBlock,
): HTMLElement {
  const card = document.createElement('div');
  card.className = `sw-card sw-stall-card-item ${NOTE_ITEM_CARD}`;
  card.dataset.noteKind = item.noteKind;
  if (item.reason) card.dataset.reason = item.reason;
  if (item.reason === 'active_checklist') card.dataset.checklist = 'true';
  card.appendChild(systemNoteHeader(document, ports, item));
  if (item.provenance) {
    // The provenance summary is the curated form of this note's content; the
    // raw injected block (e.g. the full <relevant_memories> dump) repeats the
    // same memory names/descriptions, so omit it to avoid duplicate noise.
    card.appendChild(contextProvenanceBody(document, ports, item.provenance));
  } else {
    card.appendChild(systemNoteItemBody(document, item));
  }
  return card;
}

/* A merged run of adjacent context-injection notes: ONE disclosure whose
 * summary is the shared "context injections" title + count, and whose body
 * lists each member note as one uniform card (header + provenance meta +
 * verbatim text). Member positions are unchanged. */
export function renderSystemNoteGroupBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'system-note-group' }>,
  ports: ClassicConversationRendererPorts,
): void {
  node.className = 'conversation-block conversation-block--system-note conversation-block--system-note-group';
  const document = node.ownerDocument;
  const details = document.createElement('details');
  details.className = 'sw-inbox-row sw-system-note-row sw-note-group';
  /* A live-growing group defeats the sameBlock short-circuit (items.length
   * changes), so without this read-back a reader's open state would snap
   * back to collapsed every time a new injection lands mid-turn. */
  details.open = preserveDetailsOpen(node, false);
  details.dataset.noteGroup = 'context-injection';
  const summary = document.createElement('summary');
  summary.className = 'ptool-line sw-inbox-row-header';
  const icon = document.createElement('span');
  icon.className = 'ptool-icon';
  icon.innerHTML = SYSTEM_NOTE_CONTEXT_ICON_HTML;
  const label = document.createElement('span');
  label.className = 'ptool-text';
  label.textContent = textFor(ports, 'systemNote.groupLabel', 'Context notes');
  const count = document.createElement('span');
  count.className = 'sw-note-group-count';
  count.textContent = textFor(
    ports, 'systemNote.groupItemsTitle', '{n} items',
    { n: block.items.length },
  );
  summary.append(icon, label, count);
  const body = document.createElement('div');
  body.className = 'sw-inbox-row-body sw-note-group-body';

  if (block.provenance) body.appendChild(contextProvenanceBody(document, ports, block.provenance));
  for (const item of block.items) {
    body.appendChild(systemNoteItemCard(document, ports, item));
  }
  details.append(summary, body);
  node.replaceChildren(details);
}
