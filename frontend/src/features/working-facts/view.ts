/** Bounded fact/consumer page projection. Decisions and request lifecycle stay in panel.ts. */
import { createButton, createField } from '../../ui';
import type { WorkingFactEntry, WorkingFactConsumer, WorkingFactOwnerDecision } from '../../api/working-facts.generated';
import type { Draft, Text } from './ports';

export function node(doc: Document, tag: string, value = '', className = '') {
  const el = doc.createElement(tag); el.textContent = value; el.className = className; return el;
}
export function status(t: Text, value: string) {
  const labels: Record<string, string> = {
    observed: 'Source observation', owner_asserted: 'Owner assessment', needs_review: 'Needs review',
    stale: 'Source changed', unavailable: 'Source unavailable', withdrawn: 'Withdrawn',
    current: 'Recorded basis unchanged', changed: 'Recorded basis changed', unconfirmed: 'Use not confirmed',
  };
  return t('workingFacts.status.' + value, labels[value] || value);
}
export function factRow(doc: Document, fact: WorkingFactEntry, draft: Draft, t: Text,
  submit: (action: WorkingFactOwnerDecision['action']) => void) {
  const el = doc.createElement('details'); el.dataset.workingFact = fact.id;
  const summary = node(doc, 'summary', status(t, fact.status) + ' · ' + fact.text);
  el.append(summary);
  // Materialize only expanded rows; the current page owns at most twenty rows.
  el.addEventListener('toggle', () => {
    if (!el.open || el.childElementCount > 1) return;
    const body = node(doc, 'div', '', 'ui-stack');
    body.append(node(doc, 'p', fact.text), node(doc, 'p', `${fact.id} · v${fact.revision}`));
    for (const [index, source] of fact.sources.entries()) {
      const label = (source.kind === 'criterion' && source.targetKind === 'turn'
        ? t('workingFacts.turn', 'Turn') + ' · ' : '') + t('workingFacts.source.' + source.kind, source.kind);
      if (fact.sourceStates[index]) body.append(node(doc, 'p', status(t, fact.sourceStates[index])));
      body.append(node(doc, 'p', `${label}: ${source.kind === 'file' ? source.path : source.kind === 'tool_result' ? source.toolName + ' / ' + source.toolCallId : source.id}${source.kind === 'criterion' ? ' / ' + source.criterionId : ''} · ${source.version}`));
      if (source.kind === 'tool_result') {
        body.append(node(doc, 'p', t('workingFacts.reason.historical_tool_observation', 'Historical tool observation; current external state is not verified.')));
        const observed = node(doc, 'time', new Date(source.observedAt).toLocaleString(doc.documentElement.lang || undefined));
        observed.setAttribute('datetime', new Date(source.observedAt).toISOString());
        const timing = node(doc, 'p', t('workingFacts.observedAt', 'Observed at') + ': ');
        timing.append(observed);
        body.append(timing, node(doc, 'p', t('workingFacts.sourceCoverage', 'Original source coverage') + ': '
          + t('workingFacts.coverage.' + source.sourceCoverage, source.sourceCoverage)));
      }
      if (source.kind !== 'fact') body.append(node(doc, 'blockquote', source.quote));
    }
    if (fact.note) body.append(node(doc, 'p', t('workingFacts.lastNote', 'Latest owner note') + ': ' + fact.note));
    const edit = createField({ label: t('workingFacts.correction', 'Corrected text'), value: draft.text }, doc);
    const note = createField({ label: t('workingFacts.note', 'Reason or review note'), value: draft.note, required: true }, doc);
    for (const [field, key] of [[edit, 'text'], [note, 'note']] as const) {
      const input = field.control as HTMLInputElement; input.maxLength = 2000;
      input.addEventListener('input', () => { draft[key] = input.value; draft.edited = true; });
    }
    const actions = node(doc, 'div', '', 'ui-row ui-row--wrap');
    for (const [action, fallback] of [['correct', 'Save correction'], ['confirm', 'Confirm my assessment'], ['retract', 'Withdraw fact']] as const) {
      actions.append(createButton({ label: t('workingFacts.' + action, fallback), size: 'sm', variant: 'secondary', wrap: true,
        onClick: () => submit(action) }, doc));
    }
    body.append(edit.el, note.el, actions); el.append(body);
  });
  return el;
}

export function consumerRow(doc: Document, row: WorkingFactConsumer, t: Text) {
  const el = doc.createElement('details'); el.dataset.factConsumer = row.taskId;
  const assessment = row.requiresRevalidation ? t('workingFacts.revalidate', 'Needs revalidation')
    : row.consumptionUncertain ? t('workingFacts.uncertain', 'Use not fully confirmed') : t('workingFacts.unchanged', 'Recorded basis unchanged');
  const uncertain = row.requiresRevalidation && row.consumptionUncertain ? ' · ' + t('workingFacts.uncertain', 'Use not fully confirmed') : '';
  el.append(node(doc, 'summary', assessment + uncertain + ' · ' + row.taskId));
  if (row.turnId) el.append(node(doc, 'p', t('workingFacts.turn', 'Turn') + ': ' + row.turnId));
  if (row.attemptId) el.append(node(doc, 'p', t('workingFacts.attempt', 'Attempt') + ': ' + row.attemptId));
  el.addEventListener('toggle', () => {
    if (!el.open || el.querySelector('[data-dependencies]')) return;
    const body = node(doc, 'div', '', 'ui-stack'); body.dataset.dependencies = '';
    for (const dependency of row.dependencies) {
      const range = (first: number | null, last: number | null) => first === null ? '—' : first === last ? `v${first}` : `v${first} … v${last}`;
      const item = node(doc, 'div', '', 'ui-stack ui-stack--tight');
      item.append(node(doc, 'p', dependency.id + ' · ' + status(t, dependency.status)),
        node(doc, 'p', t('workingFacts.prepared', 'Prepared versions') + ': ' + range(dependency.preparedFirstRevision, dependency.preparedLastRevision)),
        node(doc, 'p', t('workingFacts.confirmed', 'Confirmed versions') + ': ' + range(dependency.confirmedFirstRevision, dependency.confirmedLastRevision)),
        node(doc, 'p', t('workingFacts.current', 'Current version') + ': ' + range(dependency.currentRevision, dependency.currentRevision)));
      for (const reason of dependency.reasons) item.append(node(doc, 'p', t('workingFacts.reason.' + reason, reason)));
      body.append(item);
    }
    el.append(body);
  });
  return el;
}
