/**
 * Shared fallback card for tool/program blocks the rich renderer declines.
 *
 * When the retained rich port returns empty HTML, both conversation
 * surfaces (classic renderers and the typed ConversationSurface default)
 * used to dump `JSON.stringify` into bare `<pre>` nodes with a single
 * border — the "ugly raw JSON" path named in
 * docs/TOOL_CALL_SUCCESS_RENDERING_DESIGN_PROPOSAL.md (Proposal C1/C3).
 * This builder gives the degraded path the same visual grammar as the rich
 * cards — title row, status chip, backend badge, child-call chips, and
 * syntax-highlighted code sections — while staying DOM-built and
 * textContent-safe (highlighted HTML is escaped at token emission; see
 * code-highlight.ts). All chrome values come from design tokens in
 * conversation-surface.css; nothing here hardcodes color or spacing.
 */

import { detectCodeLanguage, highlightCodeToHtml } from './code-highlight';

export type ToolFallbackStatusTone = 'ok' | 'err' | 'active' | 'muted';

const OK_STATUSES = new Set(['done', 'completed', 'ok', 'success']);
const ERR_STATUSES = new Set([
  'error', 'rejected', 'failed', 'aborted', 'unanswerable',
]);
const ACTIVE_STATUSES = new Set([
  'running', 'searching', 'in_progress', 'pending', 'awaiting',
  'awaiting_human', 'streaming',
]);

export function toolFallbackStatusTone(status: string): ToolFallbackStatusTone {
  const normalized = status.trim().toLowerCase();
  if (OK_STATUSES.has(normalized)) return 'ok';
  if (ERR_STATUSES.has(normalized)) return 'err';
  if (ACTIVE_STATUSES.has(normalized)) return 'active';
  return 'muted';
}

export interface ToolFallbackSectionSpec {
  /** Small caps label above the code block ('Input', 'Program', 'Result'). */
  label: string;
  text: string;
  /** Language hint for the highlighter; auto-detected when omitted. */
  language?: string;
}

export interface ToolFallbackCardSpec {
  title: string;
  status?: string;
  badges?: ReadonlyArray<string>;
  /** Child tool names for program orchestration cards. */
  childTools?: ReadonlyArray<string>;
  sections: ReadonlyArray<ToolFallbackSectionSpec>;
}

function appendCodeSection(
  document: Document,
  body: HTMLElement,
  section: ToolFallbackSectionSpec,
): void {
  const wrap = document.createElement('div');
  wrap.className = 'conversation-tool-card__section';
  const label = document.createElement('div');
  label.className = 'conversation-tool-card__section-label';
  label.textContent = section.label;
  const pre = document.createElement('pre');
  pre.className = 'conversation-tool-card__pre';
  const code = document.createElement('code');
  const language = section.language ?? detectCodeLanguage(section.text);
  code.innerHTML = highlightCodeToHtml(section.text, language);
  pre.appendChild(code);
  wrap.append(label, pre);
  body.appendChild(wrap);
}

/**
 * Build the fallback card. The root is a `<details>` so long bodies stay
 * collapsed behind the status row, matching the rich generic-result
 * viewer's disclosure discipline.
 */
export function buildToolFallbackCard(
  document: Document,
  spec: ToolFallbackCardSpec,
): HTMLDetailsElement {
  const details = document.createElement('details');
  details.className = 'conversation-tool-card';

  const summary = document.createElement('summary');
  summary.className = 'conversation-tool-card__summary';
  const title = document.createElement('span');
  title.className = 'conversation-tool-card__title';
  title.textContent = spec.title || 'Tool';
  summary.appendChild(title);
  for (const badgeText of spec.badges ?? []) {
    if (!badgeText) continue;
    const badge = document.createElement('span');
    badge.className = 'conversation-tool-card__badge';
    badge.textContent = badgeText;
    summary.appendChild(badge);
  }
  const status = (spec.status ?? '').trim();
  if (status) {
    const chip = document.createElement('span');
    const tone = toolFallbackStatusTone(status);
    chip.className = `conversation-tool-card__chip`
      + ` conversation-tool-card__chip--${tone}`;
    chip.dataset.tone = tone;
    chip.textContent = status;
    summary.appendChild(chip);
  }
  details.appendChild(summary);

  const children = (spec.childTools ?? []).filter(Boolean);
  if (children.length) {
    const row = document.createElement('div');
    row.className = 'conversation-tool-card__children';
    const label = document.createElement('span');
    label.className = 'conversation-tool-card__children-label';
    label.textContent = `${children.length} call${children.length === 1 ? '' : 's'}`;
    row.appendChild(label);
    for (const name of children) {
      const chip = document.createElement('span');
      chip.className = 'conversation-tool-card__child';
      chip.textContent = name;
      row.appendChild(chip);
    }
    details.appendChild(row);
  }

  const body = document.createElement('div');
  body.className = 'conversation-tool-card__body';
  for (const section of spec.sections) {
    if (!section.text) continue;
    appendCodeSection(document, body, section);
  }
  details.appendChild(body);
  return details;
}
