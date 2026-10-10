/** Compare at most two owner-confirmed hypothesis snapshots across loaded pages.
 * Presentation only: program/receipt records own truth; review never infers value.
 * No transport, timers, model calls or persistent cache. Page owns source reads.
 */
import type { ExplorationProgram, ExplorationReceipt, ExplorationStep } from '../../../api/evolution-programs';
import { mountHypothesisAssessment } from './hypothesis-assessment';
import { createField } from '../../../ui';

type Candidate = {key: string; step: ExplorationStep; phase: 'queued' | 'current' | 'archived';
  runId?: string; sequence?: number; receipt?: ExplorationReceipt};
const fields = ['problem', 'audience', 'baseline', 'mechanism', 'scenario', 'expected_value',
  'assumptions', 'falsifier', 'evidence_refs'] as const;
function node<K extends keyof HTMLElementTagNameMap>(tag: K, text = '', className = '') {
  const value = document.createElement(tag); value.textContent = text; value.className = className; return value;
}
function text(element: HTMLElement, value: string) {if (element.textContent !== value) element.textContent = value;}

export function mountHypothesisComparison(host: HTMLElement, ports: {
  copy: (key: string) => string;
  sources: (receipt: ExplorationReceipt) => HTMLElement | undefined;
}) {
  const copy = ports.copy;
  let owner = '', disposed = false;
  let available = new Map<string, Candidate>();
  const selected: Array<Candidate | undefined> = [undefined, undefined];
  host.className = 'exp-comparison'; host.dataset.hypothesisComparison = 'true';
  const disclosure = node('details');
  disclosure.append(node('summary', copy('compare.title')), node('p', copy('compare.note'), 'exp-muted'));
  const scope = node('p', copy('compare.scope'), 'exp-muted');
  const warning = node('p', '', 'exp-notice'); warning.setAttribute('role', 'status');
  const selectors = node('div', '', 'exp-comparison-selectors');
  const panels = node('div', '', 'exp-comparison-panels');
  disclosure.append(scope, warning, selectors, panels); host.append(disclosure);
  const slots = [0, 1].map(index => {
    const select = node('select'); select.className = 'ui-input'; select.dataset.compareSlot = String(index);
    selectors.append(createField({label: copy(index ? 'compare.second' : 'compare.first'),
      control: select, controlId: `exp-compare-${index}`}).el);
    const article = node('article'); article.dataset.comparisonSlot = String(index); article.hidden = true;
    const heading = node('h3');
    const status = node('p', '', 'exp-phase');
    const retained = node('p', copy('compare.retained'), 'exp-muted'); retained.hidden = true;
    const dl = node('dl');
    const values = fields.map(key => {
      const dd = node('dd'); const value = node('p'); dd.dataset.hypothesisField = key; dd.append(value);
      dl.append(node('dt', copy('hypothesis.' + key)), dd); return value;
    });
    const review = node('details', '', 'exp-comparison-review');
    review.append(node('summary', copy('compare.review')));
    const assessmentHost = node('div');
    const assessment = mountHypothesisAssessment(assessmentHost, copy);
    const evidence = node('div'); review.append(assessmentHost, evidence);
    article.append(heading, status, retained, dl, review); panels.append(article);
    select.addEventListener('change', () => {
      if (disposed) return;
      const candidate = available.get(select.value);
      selected[index] = candidate ? structuredClone(candidate) : undefined;
      render();
    });
    return {select, article, heading, status, retained, values, review, evidence, assessment, signature: ''};
  });
  function render() {
    const duplicate = !!selected[0] && selected[0]?.key === selected[1]?.key;
    warning.hidden = !duplicate; text(warning, duplicate ? copy('compare.duplicate') : '');
    slots.forEach((slot, index) => {
      const candidate = selected[index];
      const options = [...available.values()];
      const signature = JSON.stringify(options.map(item => [item.key, item.step.title, item.phase, item.sequence]));
      if (slot.select.dataset.options !== signature) {
        slot.select.replaceChildren(new Option(copy('compare.choose'), ''));
        options.forEach(item => slot.select.append(new Option(
          `${copy('compare.' + item.phase)}${item.sequence ? ' ' + item.sequence : ''} · ${item.step.title}`, item.key)));
        slot.select.dataset.options = signature;
      }
      slot.select.value = candidate?.key || '';
      slot.article.hidden = !candidate;
      if (!candidate) {
        slot.assessment.reset();slot.signature = '';slot.evidence.replaceChildren();text(slot.heading, '');text(slot.status, '');
        slot.values.forEach(value => text(value, ''));return;
      }
      text(slot.heading, candidate.step.title);
      text(slot.status, `${copy('compare.' + candidate.phase)}${candidate.sequence ? ' ' + candidate.sequence : ''}`
        + (candidate.receipt ? ' · ' + copy('compare.outcome.' + candidate.receipt.outcome) : ''));
      // Loaded candidates are refreshed below. A pinned snapshot absent from the
      // new view is retained explicitly, without asserting it is still queued.
      slot.retained.hidden = loadedKeys.has(candidate.key);
      const hypothesis = candidate.step.hypothesis!;
      fields.forEach((key, fieldIndex) => {
        const value = hypothesis[key]; text(slot.values[fieldIndex]!, Array.isArray(value) ? value.join('\n') : value);
      });
      const evidenceSignature = JSON.stringify([candidate.receipt, candidate.runId, candidate.step.checks]);
      if (evidenceSignature === slot.signature) return;
      slot.signature = evidenceSignature;
      void slot.assessment.update(hypothesis, candidate.receipt?.review?.hypothesis_assessment);
      const body: HTMLElement[] = [];
      if (!candidate.receipt) body.push(node('p', copy('compare.noReview'), 'exp-muted'));
      else {
        const receipt = candidate.receipt;
        body.push(node('p', copy('compare.reviewScope'), 'exp-muted'));
        const results = receipt.review?.requirement_results || [];
        if (!results.length) body.push(node('p', copy('reviewEmpty'), 'exp-muted'));
        const list = node('ul', '', 'exp-checks');
        results.forEach(result => {
          const match = /^requirement-([1-9]\d*)$/.exec(result.requirement_id);
          const requirements = candidate.step.kind === 'final' ? [...originalChecks, ...candidate.step.checks] : candidate.step.checks;
          const label = match ? requirements[Number(match[1]) - 1] || result.requirement_id : result.requirement_id;
          const item = node('li'); item.append(node('strong', label),
            node('span', copy('outcome.' + result.outcome), 'exp-check-outcome'), node('p', result.evidence_ref)); list.append(item);
        });
        body.push(list);
        if (receipt.review?.criterion_results?.length) {
          const rubric = node('ul', '', 'exp-checks');
          receipt.review.criterion_results.forEach(result => {
            const item = node('li');item.append(node('strong', result.criterion_id),
              node('span', copy('outcome.' + result.outcome), 'exp-check-outcome'), node('p', result.evidence_ref));rubric.append(item);
          });
          body.push(rubric);
        }
        body.push(node('p', copy('result')), node('pre', receipt.result || copy('resultEmpty'), 'exp-result'));
        if (receipt.actuals_complete === false) body.push(node('p', copy('usageIncomplete'), 'exp-muted'));
        const controls = ports.sources(receipt);if (controls) body.push(controls);
      }
      if (candidate.runId) {
        const link = node('a', copy('openRoom'), 'exp-link');link.href = `#/evolution/runs/${encodeURIComponent(candidate.runId)}`;body.push(link);
      }
      slot.evidence.replaceChildren(...body);
    });
  }
  let loadedKeys = new Set<string>();
  let originalChecks: string[] = [];
  return {
    owner(id: string) {
      if (id === owner) return;
      owner = id;selected.fill(undefined);available.clear();loadedKeys.clear();originalChecks = [];
      disclosure.open = false;slots.forEach(slot => {slot.review.open = false;});render();
    },
    update(program: ExplorationProgram | null, receipts: ExplorationReceipt[]) {
      if (disposed || !program || program.program_id !== owner) return;
      const candidates: Candidate[] = [];
      if (program.current?.step.hypothesis) candidates.push({key: 'run:' + program.current.run_id,
        step: program.current.step, phase: 'current', runId: program.current.run_id, sequence: program.current.sequence});
      (program.current ? program.queue.slice(1) : program.queue).forEach(step => {
        if (step.hypothesis) candidates.push({key: 'queue:' + JSON.stringify(step), step, phase: 'queued'});
      });
      receipts.forEach(receipt => {
        if (receipt.program_id === owner && receipt.step.hypothesis) candidates.push({key: 'run:' + receipt.run_id,
          step: receipt.step, phase: 'archived', runId: receipt.run_id, sequence: receipt.sequence, receipt});
      });
      available = new Map(candidates.map(candidate => [candidate.key, candidate]));
      loadedKeys = new Set(available.keys());originalChecks = program.acceptance;
      selected.forEach((snapshot, index) => {
        if (!snapshot) return;
        const current = available.get(snapshot.key);
        if (current) selected[index] = structuredClone(current);
        else available.set(snapshot.key, snapshot);
      });
      text(scope, candidates.length ? copy('compare.scope') : copy('compare.empty'));
      render();
    },
    dispose() {disposed = true;slots.forEach(slot => slot.assessment.dispose());selected.fill(undefined);available.clear();loadedKeys.clear();host.replaceChildren();},
  };
}
