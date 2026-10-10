/** Material review objections are server observations, never inferred value. */
import type { EvolutionRun } from '../../../api/evolution-runs';
import { esc } from '../components/ui';
import type { StudioCopy } from '../goal-workbench';

export function reviewEvidenceHtml(review: EvolutionRun['review'], copy: StudioCopy, learning?: EvolutionRun['learning']): string {
  const deferred = learning?.steps?.refetch?.result?.deferred ? `<p>${esc(copy('sourceRetrievalDeferred'))}</p>` : '';
  const audit = review?.evidence_audit;
  if (!audit) return deferred;
  const warning = audit.complete ? '' : `<p class="evo-mission-diagnosis">${esc(copy('evidenceAuditIncomplete'))}</p>`;
  const rows = audit.objections.slice(0, 8).map(objection => `<li><strong>${esc(copy(objection.classification === 'contradicted' ? 'evidenceContradicted' : 'evidenceUnverified'))}</strong><p>${esc(objection.claim)}</p><p>${esc(objection.evidence_ref)}</p></li>`).join('');
  return deferred + warning + (rows ? `<h4>${esc(copy('evidenceObjections'))}</h4><ul class="evo-mission-checks">${rows}</ul>` : '');
}
