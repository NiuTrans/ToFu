/** Office gate-proposal presentation: pure HTML projections over the bounded
 * pending list. Semantics (enable journey, waiver, receipts) reuse the
 * evolution.page.* copy contract shared with the capabilities detail surface;
 * only office chrome adds evolution.studio.* keys. No DOM, no timers.
 *
 * Receipts live in studio state and are passed back in on every render: the
 * polling refresh re-projects the drawer every few seconds, so anything kept
 * only in the DOM (an inline regression receipt, a waiver stop, a decided
 * outcome) would be wiped mid-journey. Decided items that have left the
 * pending list keep a dismissible row until the owner acknowledges them.
 */
import type { EvolutionGatePending, EvolutionGateResult, EvolutionProposalItem } from '../../api/evolution-runs';
import { esc } from './components/ui';
import { t, type I18nKey, type I18nParamsFor } from '../../i18n';

type UnparameterizedEvolutionKey = {
  [K in Extract<I18nKey, `evolution.${string}`>]: I18nParamsFor<K> extends never ? K : never
}[Extract<I18nKey, `evolution.${string}`>];
const page = (key: string): string => t(`evolution.page.${key}` as UnparameterizedEvolutionKey);
const badge = (status: string): string => t(`evolution.badge.${status}` as UnparameterizedEvolutionKey);
type UnparameterizedStudioKey = {
  [K in Extract<I18nKey, `evolution.studio.${string}`>]: I18nParamsFor<K> extends never ? K : never
}[Extract<I18nKey, `evolution.studio.${string}`>];
const studio = (key: string): string => t(`evolution.studio.${key}` as UnparameterizedStudioKey);
const proposalEvidence = (n: number): string =>
  t('evolution.studio.proposalEvidence' as Extract<I18nKey, 'evolution.studio.proposalEvidence'>, {n});

export interface GateReceipt {result: EvolutionGateResult; title: string}
export type GateReceipts = ReadonlyMap<string, GateReceipt>;

function proposalRow(item: EvolutionProposalItem, receipts: GateReceipts): string {
  const receipt = receipts.get(item.mutation_id);
  const pending = Boolean(item.activation_intent);
  return `<article class="evo-proposal-row" data-kind="${esc(item.kind)}" data-key="${esc(item.mutation_id)}">` +
    `<div class="evo-proposal-main"><h3>${esc(item.title)}</h3>` +
    `<div class="evo-row-meta"><span class="evo-badge" data-status="${esc(item.status)}">${esc(pending?page('activationPending'):badge(item.status))}</span>` +
    ` <span class="evo-muted">${esc(item.kind)} · v${item.version}</span> · ` +
    `<span class="evo-muted">${esc(proposalEvidence(item.evidence_count))}</span></div></div>` +
    `<div class="evo-row-actions">` +
    `<button type="button" class="evo-button evo-primary" data-studio-action="proposal-enable" data-id="${esc(item.mutation_id)}">${esc(page(pending?'continueActivation':'verifyEnable'))}</button>` +
    (pending ? '' : `<button type="button" class="evo-button" data-studio-action="proposal-reject" data-id="${esc(item.mutation_id)}">${esc(page('reject'))}</button>`) +
    `<a class="evo-button" href="#/evolution/capabilities/${encodeURIComponent(item.mutation_id)}">${esc(page('viewEvidence'))}</a>` +
    `</div><div class="evo-gate-result" data-gate-result="${esc(item.mutation_id)}" role="status">${pending ? esc(page('activationHint')) : receipt ? gateResultHtml(item.mutation_id, receipt.result) : ''}</div></article>`;
}

function decidedRow(mutationId: string, receipt: GateReceipt): string {
  const status = receipt.result.final_status;
  const message = status === 'active' ? page('enabled')
    : status === 'rejected' ? badge('rejected')
    : status === 'regression_failed' ? page('regressionFailed')
    : page('unknown');
  return `<article class="evo-proposal-row evo-proposal-decided" data-key="decided-${esc(mutationId)}">` +
    `<div class="evo-proposal-main"><h3>${esc(receipt.title)}</h3>` +
    `<p class="evo-gate-outcome" data-status="${esc(status)}">${esc(message)}</p></div>` +
    `<div class="evo-row-actions"><button type="button" class="evo-button" data-studio-action="proposal-dismiss" data-id="${esc(mutationId)}">${esc(studio('dismiss'))}</button></div></article>`;
}

/** Drawer content for the office's pending gate proposals. */
export function proposalsPanelHtml(pending: EvolutionGatePending,
                                   receipts: GateReceipts = new Map()): string {
  const pendingIds = new Set(pending.items.map(item => item.mutation_id));
  const rows = pending.items.map(item => proposalRow(item, receipts)).join('');
  const decided = [...receipts.entries()]
    .filter(([id]) => !pendingIds.has(id))
    .map(([id, receipt]) => decidedRow(id, receipt)).join('');
  const decidedSection = decided
    ? `<h3 class="evo-proposals-decided-head">${esc(studio('decidedTitle'))}</h3>${decided}` : '';
  const degraded = pending.degraded ? `<p class="evo-muted">${esc(studio('floorDegraded'))}</p>` : '';
  const emptyHint = pending.items.length || decided
    ? '' : `<p class="evo-muted">${esc(studio('proposalsEmpty'))}</p>`;
  return `<section class="evo-proposals" aria-label="${esc(studio('proposalsTitle'))}">${degraded}${decidedSection}${rows}${emptyHint}</section>`;
}

/** Post-action receipt inside one proposal row; honest stops keep their
 * receipt and the unmeasurable path requires a second explicit waiver click. */
export function gateResultHtml(mutationId: string, result: EvolutionGateResult): string {
  const status = result.final_status;
  const message = status === 'active' ? page('enabled')
    : status === 'regression_failed' ? page('regressionFailed')
    : status === 'needs_waiver' ? page('waiverHint')
    : status === 'rejected' ? badge('rejected')
    : page('unknown');
  const receipt = result.receipt
    ? `<pre>${esc(JSON.stringify(result.receipt, null, 2))}</pre>` : '';
  const waiver = status === 'needs_waiver'
    ? `<button type="button" class="evo-button evo-primary" data-studio-action="proposal-waiver" data-id="${esc(mutationId)}">${esc(page('waiver'))}</button>` : '';
  return `<p class="evo-gate-outcome" data-status="${esc(status)}">${esc(message)}</p>${receipt}${waiver}`;
}
