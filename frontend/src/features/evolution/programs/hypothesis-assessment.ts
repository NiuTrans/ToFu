/** Four independent reviewer judgments bound to the original hypothesis.
 * A matching digest identifies the reviewed claim; judgments never certify
 * consumer value from experiment completion. No transport, timer or cache.
 */
import type { ExplorationHypothesis } from '../../../api/evolution-programs';
import type { HypothesisAssessment } from '../../../api/evolution-runs';
const axes = ['problem', 'feasibility', 'novelty', 'net_value'] as const;
function node(tag: 'p' | 'li' | 'ul' | 'strong' | 'span', text = '') {
  const value = document.createElement(tag);value.textContent = text;return value;
}
export function mountHypothesisAssessment(host: HTMLElement, copy: (key: string) => string) {
  let generation = 0, disposed = false;
  host.dataset.hypothesisAssessment = 'true';
  const note = node('p', copy('assessment.note'));note.className = 'exp-muted';
  const status = node('p');status.className = 'exp-muted';status.setAttribute('role', 'status');
  const list = node('ul');list.className = 'exp-checks';
  const fields = axes.map(axis => {
    const row = node('li');row.dataset.assessmentAxis = axis;
    const outcome = node('span');outcome.className = 'exp-check-outcome';
    const evidence = node('p');row.append(node('strong', copy('assessment.' + axis)), outcome, evidence);list.append(row);
    return {outcome, evidence};
  });host.append(note, status, list);
  function clear(message: string) {
    status.textContent = message;
    fields.forEach(field => {field.outcome.textContent = copy('outcome.unknown');field.evidence.textContent = '';});
  }
  function reset() {generation++;clear(copy('assessment.unavailable'));}
  reset();
  return {
    reset,
    async update(hypothesis: ExplorationHypothesis, supplied?: HypothesisAssessment) {
      const current = ++generation;clear(copy('assessment.unavailable'));
      if (!supplied) return;
      const assessment = structuredClone(supplied);
      if (!/^[a-f0-9]{64}$/.test(assessment.hypothesis_sha256 || '') || axes.some(axis => {
        const value = assessment[axis];return !value || !['pass', 'fail', 'unknown'].includes(value.outcome)
          || typeof value.evidence_ref !== 'string' || !value.evidence_ref.trim() || Array.from(value.evidence_ref).length > 500;
      })) return;
      clear(copy('assessment.pending'));
      try {
        const ordered = Object.fromEntries(Object.keys(hypothesis).sort().map(key =>
          [key, hypothesis[key as keyof ExplorationHypothesis]]));
        const bytes = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify(ordered)));
        if (disposed || current !== generation) return;
        const digest = Array.from(new Uint8Array(bytes), byte => byte.toString(16).padStart(2, '0')).join('');
        if (digest !== assessment.hypothesis_sha256) {clear(copy('assessment.mismatch'));return;}
        status.textContent = '';
        axes.forEach((axis, index) => {
          fields[index]!.outcome.textContent = copy('outcome.' + assessment[axis].outcome);
          fields[index]!.evidence.textContent = assessment[axis].evidence_ref;
        });
      } catch {if (!disposed && current === generation) clear(copy('assessment.unavailable'));}
    },
    dispose() {disposed = true;generation++;host.replaceChildren();},
  };
}
