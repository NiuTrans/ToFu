/**
 * Compaction receipt and token-flow DOM projection. Uses typed evidence; never
 * estimates or mutates compaction state.
 * Entry points: compactionGlyph, buildCompactionTokenFlow,
 * renderCompactionBlock.
 * Dependencies: conversation-view-model, ports, stable-dom, icons,
 * localized-copy.
 */
import type { ConversationBlockViewModel } from '../../presentation/conversation-view-model';
import type { ClassicConversationRendererPorts } from './ports';
import { preserveDetailsOpen } from './stable-dom';
import { lineSvgIcon } from './icons';
import { textFor } from './localized-copy';

interface CompactionAccounting {
  tokensBefore?: number;
  tokensAfter?: number;
  reductionPercent?: number;
  tokenCountKind?: string;
}

export function compactionGlyph(document: Document): SVGSVGElement {
  /* Two rails converge through a narrow waist: a context boundary, not an
   * error/status glyph. The same mark connects manual cards and Turn events. */
  return lineSvgIcon(document, [
    'M2.5 3.25h3L8 6.5l2.5-3.25h3',
    'M2.5 12.75h3L8 9.5l2.5 3.25h3',
  ]);
}

function finiteCompactionCount(value: number | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? Math.round(value) : null;
}

function formatCompactionCount(value: number, estimated: boolean): string {
  return `${estimated ? '≈' : ''}${value.toLocaleString()}`;
}

export function buildCompactionTokenFlow(
  document: Document,
  accounting: CompactionAccounting,
  ports: ClassicConversationRendererPorts,
): HTMLElement | null {
  const before = finiteCompactionCount(accounting.tokensBefore);
  if (before == null) return null;
  const after = finiteCompactionCount(accounting.tokensAfter);
  const estimated = accounting.tokenCountKind !== 'exact';
  const explicitReduction = finiteCompactionCount(accounting.reductionPercent);
  const reduction = explicitReduction == null
    ? (after == null || before === 0
      ? null : Math.max(0, Math.min(100, Math.round((1 - after / before) * 100))))
    : Math.min(100, explicitReduction);
  const flow = document.createElement('span');
  flow.className = 'compaction-token-flow';
  if (after == null) flow.classList.add('is-pending');
  flow.dataset.tokenCountKind = estimated ? 'estimated' : 'exact';
  flow.title = textFor(ports, 'activity.compaction.messageScope',
    'Whole message list at each compaction boundary, not summary-only. Excludes later runtime restoration and provider/tool-schema overhead.');

  const stage = (
    className: string,
    labelKey: string,
    labelFallback: string,
    value: number | null,
  ): HTMLElement => {
    const node = document.createElement('span');
    node.className = `compaction-token-stage ${className}`;
    if (value != null) node.dataset.tokenCount = String(value);
    const label = document.createElement('span');
    label.className = 'compaction-token-label';
    label.textContent = textFor(ports, labelKey, labelFallback);
    const count = document.createElement('span');
    count.className = 'compaction-token-value';
    count.textContent = value == null
      ? textFor(ports, 'activity.compaction.pending', 'Calculating')
      : formatCompactionCount(value, estimated);
    node.append(label, count);
    return node;
  };

  const arrow = document.createElement('span');
  arrow.className = 'compaction-token-arrow';
  arrow.setAttribute('aria-hidden', 'true');
  arrow.textContent = '→';
  const beforeStage = stage(
    'is-before', 'activity.compaction.before', 'Before', before,
  );
  const afterStage = stage(
    'is-after', 'activity.compaction.after', 'After', after,
  );
  flow.append(beforeStage, arrow, afterStage);
  if (reduction != null) {
    const saved = document.createElement('span');
    saved.className = 'compaction-token-saved';
    saved.textContent = textFor(
      ports, 'activity.compaction.saved', '{pct}% less', { pct: reduction },
    );
    flow.appendChild(saved);
  }
  const beforeLabel = textFor(ports, 'activity.compaction.before', 'Before');
  const afterLabel = textFor(ports, 'activity.compaction.after', 'After');
  flow.setAttribute('aria-label', [
    `${beforeLabel} ${formatCompactionCount(before, estimated)} tokens`,
    after == null
      ? `${afterLabel} ${textFor(ports, 'activity.compaction.pending', 'Calculating')}`
      : `${afterLabel} ${formatCompactionCount(after, estimated)} tokens`,
  ].join(', '));
  return flow;
}

export function renderCompactionBlock(
  node: HTMLElement,
  block: Extract<ConversationBlockViewModel, { kind: 'compaction' }>,
  ports: ClassicConversationRendererPorts,
): void {
  const value = block.value;
  const document = node.ownerDocument;
  node.className = 'conversation-block conversation-block--compaction';
  const details = document.createElement('details');
  details.className = 'compact-card';
  details.open = preserveDetailsOpen(node, false);
  const summary = document.createElement('summary');
  summary.className = 'compact-card-head';
  const icon = document.createElement('span');
  icon.className = 'compact-card-icon';
  icon.setAttribute('aria-hidden', 'true');
  icon.appendChild(compactionGlyph(document));
  const title = document.createElement('span');
  title.className = 'compact-card-title';
  title.textContent = textFor(ports, 'compactCard.title', 'Context compacted');
  summary.append(icon, title);
  const tokenFlow = buildCompactionTokenFlow(document, value, ports);
  if (tokenFlow) summary.appendChild(tokenFlow);
  const toggle = document.createElement('span');
  toggle.className = 'compact-card-toggle';
  toggle.textContent = textFor(ports, 'compactCard.expand', 'Expand summary');
  summary.appendChild(toggle);
  const body = document.createElement('div');
  body.className = 'compact-card-body';
  const markdown = document.createElement('div');
  markdown.className = 'md-content';
  markdown.innerHTML = ports.renderSafeMarkdownHtml(
    block.summaryMarkdown.replace(/^##[^\n]*\n+/, ''),
  );
  body.appendChild(markdown);
  if (value.archiveId) {
    const view = document.createElement('button');
    view.type = 'button';
    view.className = 'compact-card-view';
    view.dataset.conversationAction = 'open-compaction';
    view.dataset.operation = value.archiveId;
    view.textContent = textFor(
      ports, 'compactCard.viewSnapshot', 'View pre-compaction snapshot',
    );
    body.appendChild(view);
  }
  details.append(summary, body);
  node.replaceChildren(details);
}
