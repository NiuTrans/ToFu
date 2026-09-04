/**
 * Pure presentation policy for synthetic context-injection tool rows.
 *
 * Responsibility: render the four display-only injection lanes (sub-agent
 * inbox, peer conversation, operator steer, and intent-stall nudge) through
 * one bounded projection. Entry point: `createToolInjectionPresentation`.
 * Dependencies are explicit translation, Markdown, icon, and catalog-title
 * ports. This owner reads no DOM or browser globals and never mutates input.
 */

import { escapeHtmlText } from '../../html-safety';
import type { I18nArgs, I18nKey, Translator } from '../../i18n';

type UnknownRecord = Readonly<Record<string, unknown>>;

type BoundedText = Readonly<{
  value: string;
  truncated: boolean;
}>;

type ParsedSwarmUpdate = Readonly<{
  agentId: BoundedText;
  role: BoundedText;
  status: BoundedText;
  elapsed: BoundedText;
  tokens: BoundedText;
  outputFile: BoundedText;
  error: BoundedText;
  preview: BoundedText;
  running: number | null;
  pending: number | null;
}>;

const PREVIEW_ITEMS = 16;
const AGENT_IDENTITIES = 4;
const SENDER_BUBBLES = 3;
const IDENTIFIER_UNITS = 512;
const TITLE_UNITS = 512;
const XML_INPUT_UNITS = 65_536;
const MARKDOWN_UNITS = 16_384;
const RAW_TEXT_UNITS = 16_384;
const ERROR_UNITS = 4_096;
const OUTPUT_PATH_UNITS = 4_096;
const ROLE_UNITS = 512;
const STATUS_UNITS = 128;
const META_UNITS = 128;
const STALL_TOOL_UNITS = 512;
const STALL_PROMPT_UNITS = 32_768;

export const TOOL_INJECTION_PRESENTATION_LIMITS = Object.freeze({
  previewItems: PREVIEW_ITEMS,
  agentIdentities: AGENT_IDENTITIES,
  senderBubbles: SENDER_BUBBLES,
  identifierUnits: IDENTIFIER_UNITS,
  titleUnits: TITLE_UNITS,
  xmlInputUnits: XML_INPUT_UNITS,
  markdownUnits: MARKDOWN_UNITS,
  rawTextUnits: RAW_TEXT_UNITS,
  errorUnits: ERROR_UNITS,
  outputPathUnits: OUTPUT_PATH_UNITS,
  roleUnits: ROLE_UNITS,
  statusUnits: STATUS_UNITS,
  metaUnits: META_UNITS,
  stallToolUnits: STALL_TOOL_UNITS,
  stallPromptUnits: STALL_PROMPT_UNITS,
});

export type ToolInjectionPresentation = Readonly<{
  renderInjectionHtml(round: unknown): string;
}>;

export type ToolInjectionPresentationDependencies = Readonly<{
  translate: Translator;
  renderMarkdown: (source: string) => string;
  iconHtml: (name: string, size?: number) => string;
  resolveConversationTitle: (conversationId: string) => unknown;
}>;

const EMPTY_RECORD: UnknownRecord = Object.freeze({});
const EMPTY_ARRAY: readonly unknown[] = Object.freeze([]);

const INBOX_ICON_HTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em"><polyline points="22 12 16 12 14 15 10 15 8 12 2 12"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/></svg>';
const PEER_ICON_HTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>';
const PEER_JUMP_ICON_HTML = '<svg class="sw-peer-jump" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:.85em;height:.85em"><path d="M7 17 17 7"/><path d="M8 7h9v9"/></svg>';
const STEER_ICON_HTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>';
const STALL_ICON_HTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:1em;height:1em"><path d="M3 2v6h6"/><path d="M3 8a9 9 0 1 0 3-5.7L3 8"/></svg>';

function record(value: unknown): UnknownRecord {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord
    : EMPTY_RECORD;
}

function field(value: unknown, name: string): unknown {
  try {
    return record(value)[name];
  } catch {
    return undefined;
  }
}

function arrayField(value: unknown, name: string): readonly unknown[] {
  const candidate = field(value, name);
  if (!Array.isArray(candidate)) return EMPTY_ARRAY;
  try {
    void candidate.length;
    return candidate;
  } catch {
    return EMPTY_ARRAY;
  }
}

function safeText(value: unknown): string {
  if (typeof value === 'string') return value;
  if (
    typeof value === 'number'
    || typeof value === 'boolean'
    || typeof value === 'bigint'
  ) return String(value);
  return '';
}

function boundedText(value: unknown, limit: number): BoundedText {
  const text = safeText(value);
  const truncated = text.length > limit;
  return {
    value: truncated ? `${text.slice(0, limit)}…` : text,
    truncated,
  };
}

function safeCount(value: unknown, fallback: number): number {
  return typeof value === 'number'
    && Number.isSafeInteger(value)
    && value >= 0
    ? value
    : fallback;
}

function boundedArray(value: readonly unknown[]): readonly unknown[] {
  const retained: unknown[] = [];
  const count = Math.min(value.length, PREVIEW_ITEMS);
  for (let index = 0; index < count; index += 1) {
    try {
      retained.push(value[index]);
    } catch {
      retained.push(undefined);
    }
  }
  return retained;
}

function unescapeMinimalXml(value: string): string {
  return value
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&amp;/g, '&');
}

function parseSwarmUpdateXml(value: unknown): ParsedSwarmUpdate | null {
  const raw = safeText(value);
  if (
    raw.length > XML_INPUT_UNITS
    || !/<swarm-update>|<task-notification>/.test(raw)
  ) return null;

  function pick(tag: string, limit: number): BoundedText {
    const match = raw.match(new RegExp(`<${tag}>([\\s\\S]*?)</${tag}>`));
    return boundedText(
      match ? unescapeMinimalXml(match[1]).trim() : '',
      limit,
    );
  }

  const remaining = raw.match(
    /<remaining\s+running="(\d+)"\s+pending="(\d+)"\s*\/>/,
  );
  return {
    agentId: pick('agent-id', IDENTIFIER_UNITS),
    role: pick('role', ROLE_UNITS),
    status: pick('status', STATUS_UNITS),
    elapsed: pick('elapsed-seconds', META_UNITS),
    tokens: pick('tokens', META_UNITS),
    outputFile: pick('output-file', OUTPUT_PATH_UNITS),
    error: pick('error', ERROR_UNITS),
    preview: pick('preview', MARKDOWN_UNITS),
    running: remaining ? safeCount(Number(remaining[1]), 0) : null,
    pending: remaining ? safeCount(Number(remaining[2]), 0) : null,
  };
}

function statusClass(status: string): string {
  const normalized = status.toLowerCase();
  if (
    normalized === 'completed'
    || normalized === 'done'
    || normalized === 'success'
  ) return 'ptool-badge-ok';
  if (normalized === 'failed' || normalized === 'error') {
    return 'ptool-badge-err';
  }
  return 'ptool-badge-info';
}

export function createToolInjectionPresentation(
  dependencies: ToolInjectionPresentationDependencies,
): ToolInjectionPresentation {
  const {
    translate,
    renderMarkdown,
    iconHtml,
    resolveConversationTitle,
  } = dependencies;

  function translatedHtml<K extends I18nKey>(
    key: K,
    ...args: I18nArgs<K>
  ): string {
    return escapeHtmlText(translate(key, ...args));
  }

  function trustedMarkdown(source: string): string {
    try {
      const html = renderMarkdown(source);
      return typeof html === 'string' ? html : escapeHtmlText(source);
    } catch {
      return escapeHtmlText(source);
    }
  }

  function trustedIcon(name: string, size: number): string {
    try {
      const html = iconHtml(name, size);
      return typeof html === 'string' ? html : '';
    } catch {
      return '';
    }
  }

  function contentLimitHtml(limit: number): string {
    return `<div class="ptool-result-trunc">${translatedHtml(
      'toolInjection.contentLimit',
      { n: limit },
    )}</div>`;
  }

  function itemsLimitHtml(total: number): string {
    if (total <= PREVIEW_ITEMS) return '';
    return `<div class="ptool-result-trunc">${translatedHtml(
      'toolInjection.itemsLimit',
      { shown: PREVIEW_ITEMS, total },
    )}</div>`;
  }

  function renderSwarmUpdateCard(fields: ParsedSwarmUpdate): string {
    const aid = escapeHtmlText(fields.agentId.value);
    const role = fields.role.value
      ? `<span class="sw-card-role">${escapeHtmlText(fields.role.value)}</span>`
      : '';
    const status = fields.status.value;
    const statusChip = status
      ? `<span class="ptool-badge ${statusClass(status)} sw-card-status">${escapeHtmlText(status)}</span>`
      : '';
    const metaBits: string[] = [];
    if (fields.elapsed.value) {
      metaBits.push(`${escapeHtmlText(fields.elapsed.value)}s`);
    }
    if (fields.tokens.value) {
      metaBits.push(`${escapeHtmlText(fields.tokens.value)} tok`);
    }
    if (
      fields.running !== null
      && (fields.running !== 0 || fields.pending !== 0)
    ) {
      metaBits.push(translatedHtml('swarmCard.remaining', {
        r: fields.running,
        p: fields.pending ?? 0,
      }));
    }
    const metaHtml = metaBits.length > 0
      ? `<span class="sw-card-meta">${metaBits.join(' · ')}</span>`
      : '';
    const errorHtml = fields.error.value
      ? `<div class="sw-card-error">${escapeHtmlText(fields.error.value)}</div>`
      : '';
    const previewHtml = fields.preview.value
      ? `<div class="sw-card-preview md-content">${trustedMarkdown(fields.preview.value)}</div>${fields.preview.truncated ? contentLimitHtml(MARKDOWN_UNITS) : ''}`
      : '';
    const fileHtml = fields.outputFile.value
      ? `<div class="sw-card-file" title="${escapeHtmlText(fields.outputFile.value)}">${trustedIcon('file', 11)}<span>${escapeHtmlText(fields.outputFile.value)}</span></div>`
      : '';
    return `<div class="sw-card">
       <div class="sw-card-head">
         ${aid ? `<span class="sw-card-agent">${aid}</span>` : ''}
         ${role}
         ${statusChip}
         ${metaHtml}
       </div>
       ${errorHtml}
       ${previewHtml}
       ${fileHtml}
     </div>`;
  }

  function conversationTitle(conversationId: string): string {
    let candidate: unknown = '';
    try {
      candidate = resolveConversationTitle(conversationId);
    } catch {
      candidate = '';
    }
    const title = boundedText(candidate, TITLE_UNITS).value;
    return title || conversationId;
  }

  function peerBubble(conversationIdValue: unknown): string {
    const conversationId = boundedText(
      conversationIdValue,
      IDENTIFIER_UNITS,
    ).value;
    if (!conversationId) return '';
    const title = conversationTitle(conversationId);
    const tip = `${title} · conv ${conversationId} — ${translate(
      'peer.jumpToConv',
    )}`;
    return `<button type="button" class="sw-peer-from-bubble" data-conv-jump="${escapeHtmlText(conversationId)}" title="${escapeHtmlText(tip)}">${PEER_ICON_HTML}<span>${escapeHtmlText(title)}</span>${PEER_JUMP_ICON_HTML}</button>`;
  }

  function distinctSenderIds(
    previews: readonly unknown[],
  ): readonly string[] {
    const seen = new Set<string>();
    const ids: string[] = [];
    for (const previewValue of previews) {
      const id = boundedText(
        field(previewValue, 'fromConv'),
        IDENTIFIER_UNITS,
      ).value;
      if (!id || seen.has(id)) continue;
      seen.add(id);
      ids.push(id);
    }
    return ids;
  }

  function peerBubbleGroup(previews: readonly unknown[]): string {
    const ids = distinctSenderIds(previews);
    if (ids.length === 0) return '';
    const shown = ids
      .slice(0, SENDER_BUBBLES)
      .map(peerBubble)
      .join('');
    const overflow = ids.length > SENDER_BUBBLES
      ? `<span class="sw-peer-from-more">+${ids.length - SENDER_BUBBLES}</span>`
      : '';
    return `<span class="sw-peer-from-group">${shown}${overflow}</span>`;
  }

  function roundNumberHtml(round: UnknownRecord): string {
    return escapeHtmlText(boundedText(field(round, 'roundNum'), META_UNITS).value);
  }

  function renderInboxRow(round: UnknownRecord): string {
    const allPreviews = arrayField(round, 'inboxPreviews');
    const previews = boundedArray(allPreviews);
    const count = safeCount(field(round, 'inboxCount'), allPreviews.length);
    const allIdentities = arrayField(round, 'inboxAgentIds');
    const identities: string[] = [];
    const identityScan = Math.min(allIdentities.length, PREVIEW_ITEMS);
    for (let index = 0; index < identityScan; index += 1) {
      const identity = boundedText(
        allIdentities[index],
        IDENTIFIER_UNITS,
      ).value;
      if (identity) identities.push(identity);
      if (identities.length >= AGENT_IDENTITIES) break;
    }
    const identitiesLabel = identities.length > 0
      ? `<span class="sw-inbox-row-ids">[${identities.map(escapeHtmlText).join(', ')}${allIdentities.length > identities.length ? ` +${allIdentities.length - identities.length}` : ''}]</span>`
      : '';
    const word = count === 1
      ? translatedHtml('swarmCard.updateOne')
      : translatedHtml('swarmCard.updateMany');
    let bodyHtml = '';
    for (const previewValue of previews) {
      const parsed = parseSwarmUpdateXml(field(previewValue, 'text'));
      if (parsed) {
        const fallbackAgentId = boundedText(
          field(previewValue, 'agentId'),
          IDENTIFIER_UNITS,
        );
        bodyHtml += renderSwarmUpdateCard(
          parsed.agentId.value
            ? parsed
            : { ...parsed, agentId: fallbackAgentId },
        );
        continue;
      }
      const agentId = boundedText(
        field(previewValue, 'agentId'),
        IDENTIFIER_UNITS,
      ).value;
      const rawText = boundedText(
        field(previewValue, 'text'),
        RAW_TEXT_UNITS,
      );
      bodyHtml += `<div class="sw-card sw-card-rawonly">${agentId ? `<div class="sw-card-head"><span class="sw-card-agent">${escapeHtmlText(agentId)}</span></div>` : ''}<pre class="sw-card-raw-pre">${escapeHtmlText(rawText.value)}</pre>${rawText.truncated ? contentLimitHtml(RAW_TEXT_UNITS) : ''}</div>`;
    }
    if (previews.length === 0) {
      bodyHtml = `<div class="sw-inbox-row-empty">${translatedHtml(
        'swarmCard.noPayload',
      )}</div>`;
    } else {
      bodyHtml += itemsLimitHtml(allPreviews.length);
    }
    const badge = translatedHtml('peer.injectRowBadge');
    const label = translatedHtml('swarmCard.received');
    return `<details class="sw-inbox-row" data-rn="${roundNumberHtml(round)}">
       <summary class="ptool-line sw-inbox-row-header">
         <span class="ptool-icon">${INBOX_ICON_HTML}</span>
         <span class="ptool-text">${label} <b>${count}</b> ${word}</span>
         ${identitiesLabel}
         <span class="ptool-badge ptool-badge-info">${badge}</span>
       </summary>
       <div class="sw-inbox-row-body">${bodyHtml}</div>
     </details>`;
  }

  function renderPeerRow(round: UnknownRecord): string {
    const allPreviews = arrayField(round, 'peerPreviews');
    const previews = boundedArray(allPreviews);
    const count = safeCount(field(round, 'peerCount'), allPreviews.length);
    const word = count === 1
      ? translatedHtml('peer.injectRowOne')
      : translatedHtml('peer.injectRowMany');
    const label = translatedHtml('peer.injectRowLabel');
    const badge = translatedHtml('peer.injectRowBadge');
    const distinctSenders = distinctSenderIds(previews);
    const perCardAttribution = distinctSenders.length > 1;
    let bodyHtml = '';
    for (const previewValue of previews) {
      const text = boundedText(
        field(previewValue, 'text'),
        MARKDOWN_UNITS,
      );
      const fromBubble = perCardAttribution
        ? peerBubble(field(previewValue, 'fromConv'))
        : '';
      const markdownHtml = text.value.trim()
        ? `<div class="sw-card-preview md-content">${trustedMarkdown(text.value)}</div>${text.truncated ? contentLimitHtml(MARKDOWN_UNITS) : ''}`
        : '';
      bodyHtml += `<div class="sw-card sw-peer-card-item">${fromBubble ? `<div class="sw-card-head">${fromBubble}</div>` : ''}${markdownHtml}</div>`;
    }
    if (previews.length === 0) {
      bodyHtml = `<div class="sw-inbox-row-empty">${translatedHtml(
        'peerCard.noPayload',
      )}</div>`;
    } else {
      bodyHtml += itemsLimitHtml(allPreviews.length);
    }
    const headBubble = peerBubbleGroup(previews);
    return `<details class="sw-inbox-row sw-peer-row" data-rn="${roundNumberHtml(round)}">
       <summary class="ptool-line sw-inbox-row-header">
         <span class="ptool-icon">${PEER_ICON_HTML}</span>
         <span class="ptool-text">${label} <b>${count}</b> ${word}</span>
         ${headBubble}
         <span class="ptool-badge ptool-badge-info">${badge}</span>
       </summary>
       <div class="sw-inbox-row-body">${bodyHtml}</div>
     </details>`;
  }

  function renderSteerRow(round: UnknownRecord): string {
    const allPreviews = arrayField(round, 'steerPreviews');
    const previews = boundedArray(allPreviews);
    const count = safeCount(field(round, 'steerCount'), allPreviews.length);
    const word = count === 1
      ? translatedHtml('steer.injectRowOne')
      : translatedHtml('steer.injectRowMany');
    const label = translatedHtml('steer.injectRowLabel');
    const badge = translatedHtml('peer.injectRowBadge');
    let bodyHtml = '';
    for (const previewValue of previews) {
      const text = boundedText(
        field(previewValue, 'text'),
        MARKDOWN_UNITS,
      );
      const markdownHtml = text.value.trim()
        ? `<div class="sw-card-preview md-content">${trustedMarkdown(text.value)}</div>${text.truncated ? contentLimitHtml(MARKDOWN_UNITS) : ''}`
        : '';
      bodyHtml += `<div class="sw-card sw-steer-card-item">${markdownHtml}</div>`;
    }
    if (previews.length === 0) {
      bodyHtml = `<div class="sw-inbox-row-empty">${translatedHtml(
        'steer.noPayload',
      )}</div>`;
    } else {
      bodyHtml += itemsLimitHtml(allPreviews.length);
    }
    return `<details class="sw-inbox-row sw-steer-row" data-rn="${roundNumberHtml(round)}">
       <summary class="ptool-line sw-inbox-row-header">
         <span class="ptool-icon">${STEER_ICON_HTML}</span>
         <span class="ptool-text">${label} <b>${count}</b> ${word}</span>
         <span class="ptool-badge ptool-badge-info">${badge}</span>
       </summary>
       <div class="sw-inbox-row-body">${bodyHtml}</div>
     </details>`;
  }

  function renderStallRow(round: UnknownRecord): string {
    const label = translatedHtml('stall.injectRowLabel');
    const badge = translatedHtml('peer.injectRowBadge');
    const tool = boundedText(
      field(round, 'stallTool'),
      STALL_TOOL_UNITS,
    ).value;
    const reason = tool
      ? translatedHtml('stall.reasonWithTool', { tool })
      : translatedHtml('stall.reasonGeneric');
    const bound = translatedHtml('stall.bound');
    const prompt = boundedText(
      field(round, 'stallPrompt'),
      STALL_PROMPT_UNITS,
    );
    const promptHtml = prompt.value
      ? `<div class="sw-card sw-stall-card-item"><div class="sw-card-head"><span class="sw-card-role">${translatedHtml('stall.promptLabel')}</span></div><pre class="sw-card-raw-pre">${escapeHtmlText(prompt.value)}</pre>${prompt.truncated ? contentLimitHtml(STALL_PROMPT_UNITS) : ''}</div>`
      : '';
    return `<details class="sw-inbox-row sw-stall-row" data-rn="${roundNumberHtml(round)}">
       <summary class="ptool-line sw-inbox-row-header">
         <span class="ptool-icon">${STALL_ICON_HTML}</span>
         <span class="ptool-text">${label}</span>
         <span class="ptool-badge ptool-badge-info">${badge}</span>
       </summary>
       <div class="sw-inbox-row-body">
         <div class="sw-stall-reason">${reason}</div>
         <div class="sw-stall-bound">${bound}</div>
         ${promptHtml}
       </div>
     </details>`;
  }

  function renderInjectionHtml(roundValue: unknown): string {
    const round = record(roundValue);
    if (Boolean(field(round, '_inboxInject'))) return renderInboxRow(round);
    if (Boolean(field(round, '_peerInject'))) return renderPeerRow(round);
    if (Boolean(field(round, '_userSteerInject'))) return renderSteerRow(round);
    if (Boolean(field(round, '_stallNudge'))) return renderStallRow(round);
    return '';
  }

  return Object.freeze({ renderInjectionHtml });
}
