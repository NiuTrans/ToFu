/** Declared output editing and exact-version selection for the shared review presenter.
 * Owns only mount-local drafts; the server owns membership, versions and acceptance.
 */
import { createButton, createChoice, createField } from '../ui';
import type { TurnReviewView, TurnOutputSlot, TurnOutputEvidence, TurnCriterionView } from '../api/artifact-review.generated';

export interface TurnOutputPorts {
  load(signal: AbortSignal): Promise<Array<{ id: string; title: string }>>;
  open(artifactId: string): void;
}
type Text = (key: string, fallback: string) => string;
type Transact = (action: () => Promise<void>) => Promise<void>;

export function createTurnReviewOutputs(doc: Document, ports: TurnOutputPorts, text: Text) {
  let draft: TurnOutputSlot[] = [];
  let choices: Array<{ id: string; title: string }> | null = null;
  const selected = new Map<string, TurnOutputEvidence[]>();
  const paragraph = (value: string) => { const node = doc.createElement('p'); node.textContent = value; return node; };
  const button = (label: string, onClick: () => void) => createButton({ label, onClick, variant: 'secondary', size: 'sm', wrap: true }, doc);
  const label = (output: { kind: string; label: string }) => output.kind === 'answer' ? text('answer', 'Answer') : output.label;
  function preview(host: HTMLElement, output: { artifactId: string | null }) {
    if (output.artifactId) host.append(button(text('openOutput', 'Open output in a new tab'), () => ports.open(output.artifactId!)));
  }
  function capture(view: TurnReviewView): TurnOutputEvidence[] {
    return view.outputs.filter(output => output.available && output.version).map(output => ({
      id: output.id, version: output.version!, label: output.label, kind: output.kind, artifactId: output.artifactId,
    }));
  }
  return {
    hasSelectionDraft() { return selected.size > 0; },
    begin(view: TurnReviewView) {
      draft = view.outputs.filter(output => output.kind === 'artifact').map(output => ({
        id: output.id, label: output.label, artifactId: output.artifactId!,
      }));
    },
    configuration() { return draft.map(output => ({ ...output })); },
    bindings(criterion: TurnCriterionView) {
      return (selected.get(criterion.id) ?? criterion.evidence?.outputBindings ?? []).map(({ id, version }) => ({ id, version }));
    },
    settled(criterionId: string) { selected.delete(criterionId); },
    retain(ids: Set<string>) { for (const id of selected.keys()) if (!ids.has(id)) selected.delete(id); },
    dispose() { draft = []; choices = null; selected.clear(); },
    summary(host: HTMLElement, view: TurnReviewView) {
      const section = doc.createElement('section'); section.className = 'ui-stack'; section.dataset.reviewOutputs = '';
      section.append(paragraph(text('scopeHelp', 'Acceptance covers the answer and the outputs you explicitly include. Check that the declared delivery is complete.')));
      for (const output of view.outputs) {
        const row = doc.createElement('div'); row.className = 'ui-stack'; row.dataset.reviewOutput = output.id;
        row.append(paragraph(label(output) + ' · ' + (!output.available ? text('outputUnavailable', 'Unavailable')
          : output.covered ? text('outputCovered', 'Covered by current passing evidence') : text('outputUncovered', 'Needs current passing evidence'))));
        preview(row, output); section.append(row);
      }
      if (view.executionStatus !== 'completed') section.append(paragraph(text('executionPending', 'Task execution has not completed. Individual judgments can be recorded; final acceptance remains pending.')));
      host.append(section);
    },
    editor(host: HTMLElement, rerender: () => void, transact: Transact, signal: AbortSignal) {
      const section = doc.createElement('section'); section.className = 'ui-stack'; section.dataset.reviewOutputEditor = '';
      section.append(paragraph(text('editScope', 'Declared delivery outputs')),
        paragraph(text('answerIncluded', 'The task answer is always included. Add the exact file versions to review.')));
      for (const output of draft) {
        const row = doc.createElement('div'); row.className = 'ui-stack'; row.dataset.reviewOutputDraft = output.id;
        const field = createField({ label: text('outputLabel', 'Output label'), value: output.label }, doc);
        field.control.addEventListener('input', () => { output.label = (field.control as HTMLInputElement).value; });
        row.append(field.el); preview(row, output);
        row.append(button(text('removeOutput', 'Remove output from delivery'), () => { draft = draft.filter(value => value.id !== output.id); rerender(); }));
        section.append(row);
      }
      section.append(button(text('loadOutputs', 'Choose conversation outputs'), () => {
        void transact(async () => { const value = await ports.load(signal); if (!signal.aborted) choices = value; });
      }));
      const available = choices?.filter(choice => !draft.some(output => output.artifactId === choice.id));
      if (available?.length) {
        const chooser = createField({ label: text('outputVersion', 'Output version'), choices: available.map(choice => ({
          value: choice.id, label: (choice.title || text('untitledOutput', 'Untitled output')) + ' · ' + choice.id.slice(0, 8),
        })) }, doc);
        section.append(chooser.el, button(text('includeOutput', 'Include this output version'), () => {
          const choice = available.find(value => value.id === (chooser.control as HTMLSelectElement).value);
          if (!choice) return;
          draft.push({ id: 'output_' + crypto.randomUUID().replace(/-/g, ''), label: choice.title || text('untitledOutput', 'Untitled output'), artifactId: choice.id });
          rerender();
        }));
        if (draft.length) {
          const target = createField({ label: text('replaceOutput', 'Replace an included output'),
            choices: draft.map(output => ({ value: output.id, label: output.label })) }, doc);
          section.append(target.el, button(text('replaceVersion', 'Use selected version for this output'), () => {
            const output = draft.find(value => value.id === (target.control as HTMLSelectElement).value);
            const choice = available.find(value => value.id === (chooser.control as HTMLSelectElement).value);
            if (output && choice) { output.artifactId = choice.id; rerender(); }
          }));
        }
      } else if (available) section.append(paragraph(text('noOutputs', 'No additional conversation outputs are available.')));
      host.append(section);
    },
    evidence(host: HTMLElement, criterion: TurnCriterionView) {
      for (const binding of criterion.evidence?.outputBindings ?? []) {
        const row = doc.createElement('div'); row.className = 'ui-stack'; row.dataset.reviewOutputEvidence = binding.id;
        row.append(paragraph(text('reviewedOutput', 'Reviewed output') + ': ' + label(binding) + ' · ' + binding.version.slice(0, 12)));
        preview(row, binding); host.append(row);
      }
    },
    selection(host: HTMLElement, view: TurnReviewView, criterion: TurnCriterionView, rerender: () => void) {
      const values = selected.get(criterion.id) ?? criterion.evidence?.outputBindings ?? [];
      const current = capture(view);
      const section = doc.createElement('fieldset'); section.className = 'ui-stack'; section.dataset.reviewOutputSelection = criterion.id;
      const legend = doc.createElement('legend'); legend.textContent = text('checkedOutputs', 'Output versions checked for this decision'); section.append(legend);
      if (criterion.kind === 'constraint') section.append(paragraph(text('constraintScope', 'A global constraint must be checked against every declared output.')));
      for (const output of current) {
        section.append(createChoice({ label: label(output), checked: values.some(value => value.id === output.id && value.version === output.version),
          onChange: checked => {
            const next = values.filter(value => value.id !== output.id);
            if (checked) next.push(output); selected.set(criterion.id, next); rerender();
          } }, doc).el);
      }
      for (const old of values.filter(value => !current.some(output => output.id === value.id && output.version === value.version))) {
        const row = doc.createElement('div'); row.className = 'ui-row';
        row.append(paragraph(label(old) + ' · ' + text('selectedOldVersion', 'Earlier version remains selected')),
          button(text('removeBinding', 'Remove this selected version'), () => {
            selected.set(criterion.id, values.filter(value => value !== old)); rerender();
          }));
        section.append(row);
      }
      section.append(button(text('selectCurrentOutputs', 'Select all current output versions'), () => {
        selected.set(criterion.id, current); rerender();
      }));
      host.append(section);
    },
  };
}
