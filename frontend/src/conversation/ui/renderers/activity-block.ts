/**
 * Activity-event DOM projection and chronology-local failure evidence. Labels,
 * glyphs and receipt markup have explicit dependencies.
 * Entry points: renderActivityEventBlock, renderActivityEventGroupBlock.
 * Dependencies: conversation-sync.generated, conversation-view-model, ports,
 * compaction-block, icons, localized-copy, failure-receipt, error-presentation.
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
import { preserveDetailsOpen } from './stable-dom';
import { buildFailureReceipt } from './failure-receipt';
import { createErrorEnvelopePresentation } from '../../../error-presentation';
import type { Translator } from '../../../i18n';

import { renderUsageRecoveryNotice } from './usage-recovery-notice';

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

function compactionTriggerLabel(
  trigger: string | undefined,
  ports: ClassicConversationRendererPorts,
): string {
  if (trigger === 'working_set') {
    return textFor(ports, 'activity.compaction.trigger.workingSet', 'Long context');
  }
  if (trigger === 'window') {
    return textFor(ports, 'activity.compaction.trigger.window', 'Context budget threshold reached');
  }
  if (trigger === 'dispatch_guard') {
    return textFor(ports, 'activity.compaction.trigger.dispatchGuard', 'Request too large before sending');
  }
  if (trigger === 'context_overflow') {
    return textFor(ports, 'activity.compaction.trigger.contextOverflow', 'Provider context overflow');
  }
  if (trigger === 'manual') {
    return textFor(ports, 'activity.compaction.trigger.manual', 'Manual compression');
  }
  return ''; // Unknown historical triggers remain inspectable, not guessed.
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
  previousCompactionOpen?: boolean,
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

  const diagnostics = isCompaction ? document.createElement('details') : null;
  if (diagnostics) {
    diagnostics.className = 'activity-event__compaction-details';
    diagnostics.open = previousCompactionOpen ?? false;
    const disclosure = document.createElement('summary');
    disclosure.textContent = textFor(ports, 'activity.compaction.details', 'Details');
    diagnostics.appendChild(disclosure);
  }
  const quietCompaction = isCompaction && entry.status === 'succeeded'
    && entry.severity === 'info';
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
      ports, 'activity.compaction.snapshot', 'View snapshot',
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
    (quietCompaction && diagnostics ? diagnostics : body).appendChild(detail);
  }
  const summaryReasonKey = typeof entry.summaryArgs?.reasonKey === 'string'
    ? entry.summaryArgs.reasonKey : '';
  const reasonFact = isCompaction || entry.reasonCode === summaryReasonKey
    ? ''
    : entry.reasonCode?.startsWith('stream.retryReason.')
      ? textFor(ports, entry.reasonCode, entry.reasonCode)
      : entry.reasonCode;
  const triggerFact = isCompaction ? compactionTriggerLabel(entry.trigger, ports) : '';
  const summaryModel = entry.summaryModel;
  const recordedModels = [...new Set((entry.summaryCalls ?? []).flatMap(call =>
    typeof call.model === 'string' && call.model ? [call.model] : []))];
  const summaryModels = summaryModel ? [summaryModel] : recordedModels;
  const summaryUnknown = !summaryModels.length || (!summaryModel
    && entry.summaryCalls?.some(call => typeof call.model !== 'string' || !call.model));
  const taskModel = entry.taskModel || entry.model; // Legacy model was the task.
  const modelFacts = isCompaction ? [
    ...summaryModels.map(model => textFor(ports, 'activity.compaction.summaryModel',
      'Summary model: {model}', { model })),
    summaryUnknown ? textFor(ports, 'activity.compaction.summaryModelUnknown', 'Summary model not recorded') : '',
    entry.summarySelectedModel && !summaryModels.includes(entry.summarySelectedModel)
      ? textFor(ports, 'activity.compaction.summarySelectedModel', 'Selected summary model: {model}', { model: entry.summarySelectedModel }) : '',
  ] : [entry.model, entry.providerId, entry.keyHint];
  const facts = [
    ...modelFacts, entry.statusCode ? `HTTP ${entry.statusCode}` : '', triggerFact, reasonFact,
  ].filter(Boolean);
  const appendFacts = (parent: HTMLElement): void => {
    if (!facts.length) return;
    const factsNode = document.createElement('div');
    factsNode.className = 'activity-event__facts';
    factsNode.textContent = facts.join(' · ');
    parent.appendChild(factsNode);
  };
  if (diagnostics) {
    const routeFacts = [
      taskModel ? textFor(ports, 'activity.compaction.taskModel', 'Main model: {model}', { model: taskModel }) : '',
      entry.summaryProviderId,
      entry.trigger,
    ].filter(Boolean);
    if (routeFacts.length) {
      const route = document.createElement('div');
      route.className = 'activity-event__facts';
      route.textContent = routeFacts.join(' · ');
      diagnostics.appendChild(route);
    }
  }
  if (terminalError) {
    const summary = document.createElement('summary');
    summary.className = 'activity-event__terminal-summary';
    summary.append(marker, body);
    const content = document.createElement('div');
    content.className = 'activity-event__terminal-content';
    content.appendChild(
      buildFailureReceipt(document, terminalError, turn, ports));
    const presentation = createErrorEnvelopePresentation({
      ownerDocument: document,
      translate: ((key: string, values?: Readonly<Record<string, unknown>>) =>
        textFor(ports, key, key, activityInterpolationArgs(values))) as Translator,
    });
    const message = document.createElement('div');
    // The common presentation owner escapes every envelope field.
    message.innerHTML = presentation.renderErrorEnvelope(terminalError);
    content.appendChild(message);
    const evidence = document.createElement('details');
    evidence.className = 'activity-event__error-diagnostics';
    const evidenceSummary = document.createElement('summary');
    evidenceSummary.textContent = textFor(ports, 'err.details', 'Technical details');
    evidence.appendChild(evidenceSummary);
    const error = document.createElement('pre');
    error.className = 'activity-event__terminal-envelope';
    try {
      error.textContent = JSON.stringify(terminalError, null, 2);
    } catch {
      error.textContent = terminalError.message;
    }
    evidence.appendChild(error);
    content.appendChild(evidence);
    appendFacts(content);

    if (diagnostics && diagnostics.childNodes.length > 1) content.appendChild(diagnostics);
    row.append(summary, content);
  } else {
    appendFacts(body);

    if (diagnostics && diagnostics.childNodes.length > 1) body.appendChild(diagnostics);
    row.append(marker, body);
  }
  return row;
}

/* One collapsed disclosure over a retry/failure storm. Members keep their
 * own rows and evidence inside; the summary states how many and against
 * which model(s), so the reader still sees what happened without a red wall.
 * Default closed — a settled turn should not spend vertical space on
 * transport noise; the reader's open state survives re-renders. */
export function renderActivityEventGroupBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'activity-event-group' }>,
  ports: ClassicConversationRendererPorts,
  turn?: ConversationTurnViewModel,
): void {
  const document = node.ownerDocument;
  const previousSummary = node.querySelector('summary');
  const summaryHadFocus = previousSummary === document.activeElement;
  const defaultOpen = preserveDetailsOpen(node, false);
  node.className = 'conversation-block conversation-block--activity-event '
    + 'conversation-block--activity-event-group';

  const details = document.createElement('details');
  details.className = 'activity-event-group';
  details.open = defaultOpen;

  const summary = document.createElement('summary');
  summary.className = 'activity-event-group__summary';
  const marker = document.createElement('span');
  marker.className = 'activity-event-group__marker';
  marker.setAttribute('aria-hidden', 'true');
  marker.appendChild(lineSvgIcon(document, [
    'M7.1 2.8 2.2 11.7a1 1 0 0 0 .88 1.5h9.84a1 1 0 0 0 .88-1.5L8.9 2.8a1.03 1.03 0 0 0-1.8 0Z',
    'M8 6v3.1M8 11.35h.01',
  ]));
  const label = document.createElement('span');
  label.className = 'activity-event-group__label';
  label.textContent = textFor(
    ports, 'activity.retryGroup.summary',
    '{count} failed or retried requests', { count: block.items.length },
  );
  const chips = document.createElement('span');
  chips.className = 'activity-event__chips';
  const models = [...new Set(block.items.flatMap(
    item => (item.value.model ? [item.value.model] : []),
  ))];
  if (models.length) {
    const modelNode = document.createElement('span');
    modelNode.className = 'activity-event-group__models';
    modelNode.textContent = models.join(' · ');
    chips.appendChild(modelNode);
  }
  const totalDuration = block.items.reduce(
    (total, item) => total + (item.value.durationMs ?? 0), 0,
  );
  if (totalDuration > 0) {
    const durationNode = document.createElement('span');
    durationNode.className = 'activity-event__duration';
    durationNode.textContent = activityDuration(totalDuration);
    chips.appendChild(durationNode);
  }
  chips.appendChild(disclosureChevron(document, 'activity-event-group__chevron'));
  summary.append(marker, label, chips);

  const body = document.createElement('div');
  body.className = 'activity-event-group__body';
  for (const item of block.items) {
    body.appendChild(buildActivityEventRow(
      document, item.value, ports, undefined, undefined, turn,
    ));
  }
  details.append(summary, body);
  node.replaceChildren(details);
  if (summaryHadFocus) summary.focus();
}

export function renderActivityEventBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'activity-event' }>,
  ports: ClassicConversationRendererPorts,
  turn?: ConversationTurnViewModel,
): void {
  const recoveryFocus = node.querySelector('[data-usage-recovery]')?.contains(node.ownerDocument.activeElement)
    ? node.ownerDocument.activeElement as HTMLElement : null;
  const previousDetails = node.querySelector<HTMLDetailsElement>(
    ':scope > details.activity-event--terminal-error',
  );
  const previousCompactionDetails = node.querySelector<HTMLDetailsElement>(
    '.activity-event__compaction-details',
  );
  const previousEvidence = node.querySelector<HTMLDetailsElement>(
    '.activity-event__error-diagnostics',
  );
  const evidenceHadFocus = previousEvidence?.querySelector('summary')
    === node.ownerDocument.activeElement;
  const detailsHadFocus = previousCompactionDetails?.querySelector('summary')
    === node.ownerDocument.activeElement;
  node.className = 'conversation-block conversation-block--activity-event';
  node.replaceChildren(
    buildActivityEventRow(
      node.ownerDocument,
      block.value,
      ports,
      block.terminalError,
      previousDetails?.open,
      turn,
      previousCompactionDetails?.open,
    ),
  );

  const canCheckUsage = block.terminalError?.kind === 'execution_token_usage_unknown'
    && turn && ['completed', 'failed', 'interrupted', 'truncated', 'cancelled', 'superseded'].includes(turn.status)
    && turn.source.conversationId && turn.turnId
    && Number.isSafeInteger(turn.projectionRevision) && turn.projectionRevision >= 0;
  renderUsageRecoveryNotice(node,
    node.querySelector<HTMLElement>('.activity-event__terminal-content'),
    canCheckUsage ? { conversationId: turn.source.conversationId, turnId: turn.turnId,
      projectionRevision: turn.projectionRevision } : undefined, ports);
  if (recoveryFocus?.isConnected) recoveryFocus.focus();

  const evidence = node.querySelector<HTMLDetailsElement>('.activity-event__error-diagnostics');
  if (evidence) {
    evidence.open = previousEvidence?.open ?? false;
    if (evidenceHadFocus) evidence.querySelector('summary')?.focus();
  }
  if (detailsHadFocus) {
    node.querySelector<HTMLElement>('.activity-event__compaction-details > summary')?.focus();
  }
}
