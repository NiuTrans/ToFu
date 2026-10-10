/**
 * Activity-event DOM projection and chronology-local failure evidence. Labels,
 * glyphs and receipt markup have explicit dependencies.
 * Entry points: renderActivityEventBlock.
 * Dependencies: conversation-sync.generated, conversation-view-model, ports,
 * compaction-block, icons, localized-copy, failure-receipt.
 */
import type { TurnActivityEntry } from '../../../api/conversation-sync.generated';
import {
  type ConversationBlockViewModel,
  type ConversationTurnViewModel,
} from '../../presentation/conversation-view-model';
import type { ClassicConversationRendererPorts } from './ports';
import {
  compactionGlyph,
  buildCompactionTokenFlow,
} from './compaction-block';
import {
  disclosureChevron,
  lineSvgIcon,
} from './icons';
import { textFor } from './localized-copy';
import { buildFailureReceipt } from './failure-receipt';

function activityInterpolationArgs(
  value: Readonly<Record<string, unknown>> | undefined,
): Readonly<Record<string, string | number>> | undefined {
  if (!value) return undefined;
  const entries = Object.entries(value).flatMap(([key, item]) => (
    typeof item === 'string' || typeof item === 'number'
      ? [[key, item] as const] : []
  ));
  return entries.length ? Object.fromEntries(entries) : undefined;
}

function activityDuration(value: number | undefined): string {
  if (value == null || value < 0) return '';
  if (value < 1000) return `${Math.round(value)}ms`;
  if (value < 60_000) return `${(value / 1000).toFixed(value < 10_000 ? 1 : 0)}s`;
  const minutes = Math.floor(value / 60_000);
  const seconds = Math.floor((value % 60_000) / 1000);
  return `${minutes}m ${seconds}s`;
}

function activitySummaryWithoutDecorativeEmoji(value: string): string {
  const cleaned = value.replace(
    /^(?:(?:\p{Extended_Pictographic}|\uFE0F|\u200D|\u20E3)+\s*)+/u,
    '',
  ).trimStart();
  return cleaned || value;
}

function activityMarkerIcon(
  document: Document,
  entry: TurnActivityEntry,
): SVGSVGElement {
  if (['context_compaction', 'compacting'].includes(entry.reasonCode ?? '')) {
    return compactionGlyph(document);
  }
  if (entry.severity === 'error' || entry.status === 'failed') {
    return lineSvgIcon(document, [
      'M8 2.5a5.5 5.5 0 1 1 0 11 5.5 5.5 0 0 1 0-11Z',
      'm6 6 4 4M10 6l-4 4',
    ]);
  }
  if (entry.severity === 'warning'
      || ['switched', 'skipped', 'aborted'].includes(entry.status)) {
    return lineSvgIcon(document, [
      'M7.1 2.8 2.2 11.7a1 1 0 0 0 .88 1.5h9.84a1 1 0 0 0 .88-1.5L8.9 2.8a1.03 1.03 0 0 0-1.8 0Z',
      'M8 6v3.1M8 11.35h.01',
    ]);
  }
  if (entry.status === 'succeeded') {
    return lineSvgIcon(document, [
      'M8 2.5a5.5 5.5 0 1 1 0 11 5.5 5.5 0 0 1 0-11Z',
      'm5.5 8 1.65 1.65L10.8 6',
    ]);
  }
  if (entry.status === 'running' || entry.status === 'waiting') {
    return lineSvgIcon(document, [
      'M13 8a5 5 0 1 1-1.46-3.54',
      'M11.5 2.75v2.2H9.3',
    ]);
  }
  return lineSvgIcon(document, [
    'M8 3a5 5 0 1 1 0 10A5 5 0 0 1 8 3Z',
    'M8 6.25v3.25M8 11.25h.01',
  ]);
}

function buildActivityEventRow(
  document: Document,
  entry: TurnActivityEntry,
  ports: ClassicConversationRendererPorts,
  terminalError?: Extract<ConversationBlockViewModel, {
    kind: 'activity-event'
  }>['terminalError'],
  previousOpen?: boolean,
  turn?: ConversationTurnViewModel,
): HTMLElement {
  const isCompaction = ['context_compaction', 'compacting'].includes(entry.reasonCode ?? '');
  const row = document.createElement(terminalError ? 'details' : 'div');
  row.className = `activity-event activity-event--${entry.kind} `
    + `activity-event--${entry.status} activity-event--${entry.severity}`
    + (entry.parentSpanId ? ' activity-event--nested' : '')
    + (isCompaction ? ' activity-event--compaction' : '')
    + (terminalError ? ' activity-event--terminal-error' : '');
  if (row.tagName === 'DETAILS') {
    (row as HTMLDetailsElement).open = previousOpen ?? true;
  }
  row.dataset.activityId = entry.id;
  row.dataset.activitySpanId = entry.spanId;
  const marker = document.createElement('span');
  marker.className = 'activity-event__marker';
  marker.setAttribute('aria-hidden', 'true');
  marker.appendChild(activityMarkerIcon(document, entry));

  const body = document.createElement('div');
  body.className = 'activity-event__body';
  const line = document.createElement('div');
  line.className = 'activity-event__line';
  const occurredAtDate = new Date(entry.occurredAt);
  const occurredAtText = ports.formatTimestamp?.(entry.occurredAt)
    || (Number.isFinite(occurredAtDate.getTime())
      ? occurredAtDate.toLocaleString() : '');
  if (occurredAtText) line.title = occurredAtText;
  const summary = document.createElement('span');
  summary.className = 'activity-event__summary';
  const localizedSummary = entry.summaryKey
    ? textFor(
      ports, entry.summaryKey, entry.summary || `${entry.kind} · ${entry.status}`,
      activityInterpolationArgs(entry.summaryArgs),
    )
    : entry.summary || `${entry.kind} · ${entry.status}`;
  summary.textContent = activitySummaryWithoutDecorativeEmoji(localizedSummary);
  line.appendChild(summary);
  const chips = document.createElement('span');
  chips.className = 'activity-event__chips';
  const duration = activityDuration(entry.durationMs);
  if (duration) {
    const durationNode = document.createElement('span');
    durationNode.className = 'activity-event__duration';
    durationNode.textContent = duration;
    chips.appendChild(durationNode);
  }
  if (entry.count > 1) {
    const repeated = document.createElement('span');
    repeated.className = 'activity-event__repeated';
    repeated.textContent = `×${entry.count}`;
    repeated.title = textFor(
      ports, 'activity.timeline.repeated', 'Repeated {count} times',
      { count: entry.count },
    );
    chips.appendChild(repeated);
  }
  if (isCompaction && entry.archiveId) {
    const inspect = document.createElement('button');
    inspect.type = 'button';
    inspect.className = 'activity-event__compaction-open';
    inspect.dataset.conversationAction = 'open-compaction';
    inspect.dataset.operation = entry.archiveId;
    inspect.textContent = textFor(
      ports, 'compactCard.viewSnapshot', 'View pre-compaction snapshot',
    );
    chips.appendChild(inspect);
  }
  if (terminalError) {
    chips.appendChild(disclosureChevron(document, 'activity-event__chevron'));
  }
  if (chips.childNodes.length) line.appendChild(chips);
  body.appendChild(line);
  if (isCompaction) {
    const tokenFlow = buildCompactionTokenFlow(document, entry, ports);
    if (tokenFlow) body.appendChild(tokenFlow);
  }
  if (entry.detail && !terminalError) {
    const detail = document.createElement('div');
    detail.className = 'activity-event__detail';
    /* A nested reasonKey (route-switch cause) is itself an i18n key: resolve
     * it to a localized phrase first so the {reason} placeholder reads in the
     * UI language instead of leaking an English fallback or a raw enum. */
    const rawArgs = entry.detailArgs;
    const reasonKey = typeof rawArgs?.reasonKey === 'string' ? rawArgs.reasonKey : '';
    const resolvedArgs = rawArgs
      ? {
        ...rawArgs,
        ...(reasonKey
          ? {
            reason: textFor(
              ports, reasonKey,
              typeof rawArgs.reason === 'string' ? rawArgs.reason : reasonKey,
              activityInterpolationArgs(
                rawArgs.reasonArgs && typeof rawArgs.reasonArgs === 'object'
                  && !Array.isArray(rawArgs.reasonArgs)
                  ? rawArgs.reasonArgs as Readonly<Record<string, unknown>>
                  : undefined),
            ),
          }
          : {}),
      }
      : rawArgs;
    detail.textContent = entry.detailKey
      ? textFor(
        ports, entry.detailKey, entry.detail,
        activityInterpolationArgs(resolvedArgs),
      ) : entry.detail;
    body.appendChild(detail);
  }
  const summaryReasonKey = typeof entry.summaryArgs?.reasonKey === 'string'
    ? entry.summaryArgs.reasonKey : '';
  const reasonFact = isCompaction || entry.reasonCode === summaryReasonKey
    ? ''
    : entry.reasonCode?.startsWith('stream.retryReason.')
      ? textFor(ports, entry.reasonCode, entry.reasonCode)
      : entry.reasonCode;
  const triggerFact = isCompaction && entry.trigger
    ? entry.trigger.replaceAll('_', ' ') : '';
  const facts = [
    entry.model, entry.providerId, entry.keyHint, entry.statusCode
      ? `HTTP ${entry.statusCode}` : '', triggerFact, reasonFact,
  ].filter(Boolean);
  const appendFacts = (parent: HTMLElement): void => {
    if (!facts.length) return;
    const factsNode = document.createElement('div');
    factsNode.className = 'activity-event__facts';
    factsNode.textContent = facts.join(' · ');
    parent.appendChild(factsNode);
  };
  if (terminalError) {
    const summary = document.createElement('summary');
    summary.className = 'activity-event__terminal-summary';
    summary.append(marker, body);
    const content = document.createElement('div');
    content.className = 'activity-event__terminal-content';
    content.appendChild(
      buildFailureReceipt(document, terminalError, turn, ports));
    const error = document.createElement('pre');
    error.className = 'activity-event__terminal-envelope';
    try {
      error.textContent = JSON.stringify(terminalError, null, 2);
    } catch {
      error.textContent = terminalError.message;
    }
    content.appendChild(error);
    appendFacts(content);
    row.append(summary, content);
  } else {
    appendFacts(body);
    row.append(marker, body);
  }
  return row;
}

export function renderActivityEventBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'activity-event' }>,
  ports: ClassicConversationRendererPorts,
  turn?: ConversationTurnViewModel,
): void {
  const previousDetails = node.querySelector<HTMLDetailsElement>(
    ':scope > details.activity-event--terminal-error',
  );
  node.className = 'conversation-block conversation-block--activity-event';
  node.replaceChildren(
    buildActivityEventRow(
      node.ownerDocument,
      block.value,
      ports,
      block.terminalError,
      previousDetails?.open,
      turn,
    ),
  );
}
