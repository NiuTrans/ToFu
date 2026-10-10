/** Present the workspace API's complete submission decision. No scientific gate
 * is recalculated here; missing/stale projections remain explicitly unchecked.
 */
import { escapeHtml } from '../../../../html-safety';

export interface SubmissionReadiness { ready: boolean; gates: Array<{ id: string; ok: boolean }> }
type Translate = (key: string, fallback: string) => string;
const labels: Record<string, [string, string]> = {
  protocol: ['gateProtocol', 'Falsification protocol is frozen'],
  run_evidence: ['gateRun', 'Current experiments have durable evidence'],
  claims: ['gateClaims', 'Every claim points to evidence'],
  manuscript: ['gateManuscript', 'Manuscript is complete and current'],
  compile: ['gateCompile', 'Current source compiles'],
  baseline_fairness: ['gateBaseline', 'Baselines are comparable'],
  uncertainty: ['gateUncertainty', 'Uncertainty is reported'],
  seeds: ['gateSeeds', 'Random seeds are recorded'],
  validation_split: ['gateValidation', 'Validation split is verified'],
  claim_traceability: ['gateTraceability', 'Claims trace to current evidence'],
};

export function submissionReadiness(value: unknown): SubmissionReadiness | null {
  if (!value || typeof value !== 'object') return null;
  const row = value as SubmissionReadiness;
  if (typeof row.ready !== 'boolean' || !Array.isArray(row.gates) || !row.gates.length
    || !row.gates.every(gate => gate && typeof gate.id === 'string' && typeof gate.ok === 'boolean')) return null;
  return { ready: row.ready, gates: row.gates.map(({ id, ok }) => ({ id, ok })) };
}

export function submissionGatesHtml(readiness: SubmissionReadiness | null, tr: Translate): string {
  if (!readiness) return `<strong>${escapeHtml(tr('evolution.research.gatesUnchecked', 'Save changes to check submission readiness'))}</strong>`;
  const status = readiness.ready ? tr('evolution.research.submissionReady', 'Submission gate passed')
    : tr('evolution.research.submissionBlocked', 'Submission gate blocked');
  const gates = readiness.gates.map(gate => {
    const label = labels[gate.id];
    const name = label ? tr('evolution.research.' + label[0], label[1]) : gate.id.replaceAll('_', ' ');
    return `<span data-gate-id="${escapeHtml(gate.id)}" class="${gate.ok ? 'is-ok' : ''}">${gate.ok ? '✓' : '○'} ${escapeHtml(name)}</span>`;
  }).join('');
  return `<strong>${escapeHtml(status)}</strong><details><summary>${escapeHtml(tr('evolution.research.gateDetails', 'View checks'))}</summary><div class="rsw-gate-list">${gates}</div></details>`;
}
