/** Report completion metadata presentation, shared by article paint and Deepen.
 * Consumes recorded metadata only; report-events owns event state, report-runtime
 * owns replay and identity, and the existing currency owner formats CNY. */
import { featureRegistry } from '../../../feature-registry';
import { escapeHtml } from '../../../html-safety';
import { t, type I18nKey } from '../../../i18n';

/** Fields consumed by this view, not a separate producer/wire contract. */
export interface ReportFinishMetadata {
  model?: string;
  costCny?: number;
  costUsd?: number;
  totalCostCny?: number;
  totalCostUsd?: number;
  promptTokens?: number;
  completionTokens?: number;
  secondPasses?: Record<string, { costCny?: number } | undefined>;
}

export interface ReportMetadataPresentation {
  translate(key: I18nKey): string;
  formatCny?: (value: number) => string;
}

function presentation(): ReportMetadataPresentation {
  const format = featureRegistry.formatCny;
  return {
    translate: t,
    formatCny: typeof format === 'function'
      ? format as ReportMetadataPresentation['formatCny'] : undefined,
  };
}

export function reportCostBreakdown(
  meta: ReportFinishMetadata | null | undefined,
  ports: ReportMetadataPresentation = presentation(),
): string {
  if (!meta?.secondPasses) return '';
  const format = (value: unknown): string | null => {
    if (typeof value !== 'number' || value <= 0) return null;
    return ports.formatCny?.(value) ?? `¥${value.toFixed(4)}`;
  };
  const parts: string[] = [];
  const base = format(meta.costCny);
  if (base) parts.push(`${ports.translate('paper.xpCostBody')} ${base}`);
  const names: Record<string, I18nKey> = {
    insight: 'paper.xpPassInsight',
    termfill: 'paper.xpPassTermfill',
    checkpoints: 'paper.xpPassCheckpoints',
    deepen: 'paper.xpPassDeepen',
  };
  Object.entries(meta.secondPasses).forEach(([key, row]) => {
    const cost = format(row?.costCny);
    const label = names[key];
    if (cost) parts.push(`${label ? ports.translate(label) : key} ${cost}`);
  });
  return parts.join(' + ');
}

export function renderReportFinishTag(
  meta: ReportFinishMetadata | null | undefined,
  ports: ReportMetadataPresentation = presentation(),
): string {
  if (!meta?.model) return '';
  const translate = ports.translate;
  const parts = ['<span class="paper-finish-model" title="'
    + escapeHtml(translate('paper.finishModelTitle')) + '">'
    + '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a3 3 0 0 0-3 3v1a3 3 0 0 0-3 3 3 3 0 0 0 0 6 3 3 0 0 0 3 3v1a3 3 0 0 0 6 0v-1a3 3 0 0 0 3-3 3 3 0 0 0 0-6 3 3 0 0 0-3-3V5a3 3 0 0 0-3-3z"/></svg>'
    + escapeHtml(meta.model) + '</span>'];
  let cost = '';
  let costTitle = translate('paper.finishCostTitle');
  // Preserve the recorded-total policy; this view never re-prices usage.
  const hasPasses = meta.secondPasses && typeof meta.totalCostCny === 'number'
    && meta.totalCostCny > 0;
  const cny = hasPasses ? meta.totalCostCny : meta.costCny;
  const usd = hasPasses ? meta.totalCostUsd : meta.costUsd;
  if (typeof cny === 'number' && cny > 0) {
    cost = ports.formatCny?.(cny) ?? `¥${cny.toFixed(4)}`;
  } else if (typeof usd === 'number' && usd > 0) {
    cost = `$${usd.toFixed(4)}`;
  }
  if (hasPasses) costTitle = reportCostBreakdown(meta, ports) || costTitle;
  if (cost) parts.push('<span class="paper-finish-cost" title="'
    + escapeHtml(costTitle) + '">' + escapeHtml(cost) + '</span>');
  const inputTokens = meta.promptTokens || 0;
  const outputTokens = meta.completionTokens || 0;
  if (inputTokens || outputTokens) {
    const format = (value: number): string => {
      if (value >= 1000000) return `${(value / 1000000).toFixed(1)}M`;
      if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
      return String(value);
    };
    parts.push('<span class="paper-finish-tokens" title="'
      + escapeHtml(translate('paper.finishTokensTitle')) + '">'
      + format(inputTokens) + ' → ' + format(outputTokens) + ' tok</span>');
  }
  return '<div class="paper-report-finish-tag" role="contentinfo">'
    + '<span class="paper-finish-label">'
    + escapeHtml(translate('paper.finishGeneratedBy')) + '</span>'
    + parts.join('') + '</div>';
}

/** Reconcile only the article's completion tag; keep body and interaction nodes.
 * Exact DOM equality avoids replacing an unchanged tag on replay. */
export function syncReportFinishTag(
  container: Element,
  meta: ReportFinishMetadata | null | undefined,
  ports?: ReportMetadataPresentation,
): boolean {
  const article = container.matches('.paper-report-article')
    ? container : container.querySelector('.paper-report-article');
  if (!article) return false;
  const current = article.querySelector(':scope > .paper-report-finish-tag');
  const html = renderReportFinishTag(meta, ports);
  if (!html) {
    current?.remove();
    return Boolean(current);
  }
  const wrapper = article.ownerDocument.createElement('div');
  wrapper.innerHTML = html;
  const next = wrapper.firstElementChild!;
  if (current?.isEqualNode(next)) return false;
  if (current) current.replaceWith(next);
  else article.appendChild(next);
  return true;
}
