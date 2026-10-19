/** Shared owner review of artifact versions and ordinary Turn deliveries.
 * Entry: mountArtifactReview / mountTurnReview; dependencies: generated DTOs and shared UI kit.
 * Requests and drafts belong to this mount; disposal aborts/fences late work.
 * No task status mutation, automatic acceptance, timers, or artifact HTML.
 */
import { createAlert, createButton, createField } from '../ui';
import { ARTIFACT_REVIEW_MAX_FACT_BINDINGS, type ArtifactCriterion, type ArtifactReviewCommand,
  type ArtifactReviewView, type ArtifactFactEvidence, type TurnReviewView, type TurnReviewCommand } from '../api/artifact-review.generated';
import { createTurnReviewOutputs, type TurnOutputPorts } from './turn-review-outputs';
import type { WorkingFactsView } from '../api/working-facts.generated';

interface ArtifactReviewPorts {
  load(signal: AbortSignal): Promise<ArtifactReviewView>;
  save(command: ArtifactReviewCommand, signal: AbortSignal): Promise<ArtifactReviewView>;
  loadFacts?(signal: AbortSignal): Promise<WorkingFactsView>;
  text(key: string, fallback: string): string;
}

type ReviewView = ArtifactReviewView | TurnReviewView;
type ReviewCommand = ArtifactReviewCommand | TurnReviewCommand;
interface ReviewPorts {
  load(signal: AbortSignal): Promise<ReviewView>;
  save(command: ReviewCommand, signal: AbortSignal): Promise<ReviewView>;
  loadFacts?(signal: AbortSignal): Promise<WorkingFactsView>;
  text(key: string, fallback: string): string;
  outputs?: TurnOutputPorts;
  subscribeInvalidation?(notify: () => void): () => void;
}
export interface TurnReviewPorts extends Omit<ReviewPorts, 'load' | 'save' | 'outputs'> {
  load(signal: AbortSignal): Promise<TurnReviewView>;
  save(command: TurnReviewCommand, signal: AbortSignal): Promise<TurnReviewView>;
  outputs: TurnOutputPorts;
}
export function mountArtifactReview(host: HTMLElement, ports: ArtifactReviewPorts): () => void {
  return mountReview(host, { ...ports, save: (command, signal) => {
    if ('outputBindings' in command || 'outputs' in command) throw new Error('Turn review requires a Turn target');
    return ports.save(command, signal);
  } });
}
export function mountTurnReview(host: HTMLElement, ports: TurnReviewPorts): () => void {
  return mountReview(host, { ...ports, save: (command, signal) => {
    if ('outputBindings' in command || 'outputs' in command) return ports.save(command, signal);
    throw new Error('Artifact review requires an artifact target');
  } });
}
function mountReview(host: HTMLElement, ports: ReviewPorts): () => void {
  const doc = host.ownerDocument;
  const abort = new AbortController();
  const text = (key: string, fallback: string) => ports.text('artifactReview.' + key, fallback);
  const outputs = ports.outputs ? createTurnReviewOutputs(doc, ports.outputs,
    (key, fallback) => ports.text('turnReview.' + key, fallback)) : null;
  let view: ReviewView | null = null;
  let open = Boolean(outputs);
  let busy = false;
  let refreshing = false;
  let refreshPending = false;
  let invalidations = 0;
  let operation = 0;
  let automaticError = false;
  let editing = false;
  let draft: ArtifactCriterion[] = [];
  let objective = '';
  const notes = new Map<string, string>();
  const factDrafts = new Map<string, ArtifactFactEvidence[]>();
  let facts: WorkingFactsView | null = null;
  let picking = '';
  const toggle = createButton({ label: text('title', 'Acceptance review'), variant: 'ghost', size: 'sm',
    onClick: () => { open = !open; syncOpen(); if (open && !view && !busy) void load(); } }, doc);
  toggle.dataset.reviewToggle = '';
  const content = doc.createElement('div'); content.className = 'ui-stack ui-stack--bounded';
  const feedback = doc.createElement('div');
  const updates = doc.createElement('div'); updates.dataset.reviewUpdates = ''; updates.setAttribute('role', 'status'); updates.hidden = true;
  const body = doc.createElement('div'); body.className = 'ui-stack ui-stack--bounded';
  content.append(updates, feedback, body);
  host.replaceChildren(toggle, content);
  host.className = 'artifact-review ui-stack ui-stack--bounded';
  host.dataset.artReview = '';
  if (outputs) { host.dataset.turnReview = ''; toggle.hidden = true; }
  function syncOpen() {
    toggle.setAttribute('aria-expanded', String(open));
    content.hidden = !open; host.dataset.open = String(open);
  }
  function button(label: string, onClick: () => void) {
    return createButton({ label, variant: 'secondary', size: 'sm', wrap: true, onClick }, doc);
  }
  function row() { const el = doc.createElement('div'); el.className = 'ui-row'; return el; }
  function paragraph(value: string) { const el = doc.createElement('p'); el.textContent = value; return el; }
  function field(label: string, value: string, change: (value: string) => void) {
    const result = createField({ label, value }, doc);
    result.control.addEventListener('input', () => change((result.control as HTMLInputElement).value));
    return result.el;
  }
  function error(message: string, automatic = false) {
    automaticError = automatic;
    feedback.replaceChildren(createAlert({ variant: 'error', title: text('saveError', 'Review was not confirmed'), body: message }).el);
    feedback.append(button(text('reload', 'Reload latest review'), () => void load()));
  }
  function applyView(result: ReviewView) {
    view = result;
    const ids = new Set(result.criteria.map(item => item.id));
    for (const id of notes.keys()) if (!ids.has(id)) notes.delete(id);
    for (const id of factDrafts.keys()) if (!ids.has(id)) factDrafts.delete(id);
    if (!ids.has(picking)) picking = '';
    outputs?.retain(ids);
  }
  function clearUpdates() { updates.replaceChildren(); updates.hidden = true; }
  function preserveInteraction() {
    const focused = doc.activeElement;
    return editing || Boolean(picking) || factDrafts.size > 0 || outputs?.hasSelectionDraft()
      || [...notes].some(([id, note]) => note !== (view?.criteria.find(item => item.id === id)?.evidence?.note ?? ''))
      || Boolean(focused && host.contains(focused) && focused.matches('input,textarea,select,[contenteditable]'));
  }
  async function refresh() {
    if (!refreshPending || refreshing || busy || !open || abort.signal.aborted) return;
    if (preserveInteraction()) {
      if (!updates.childElementCount) updates.append(button(text('reload', 'Reload latest review'), () => void load()));
      updates.hidden = false;
      return;
    }
    refreshing = true; refreshPending = false;
    const current = operation;
    try {
      const result = await ports.load(abort.signal);
      if (abort.signal.aborted || current !== operation) return;
      // Input may begin while the read is in flight. Keep that exact DOM and
      // version basis until the owner explicitly reloads or finishes editing.
      if (busy || preserveInteraction()) { refreshPending = true; return; }
      applyView(result); render();
      if (automaticError) { feedback.replaceChildren(); automaticError = false; }
      if (!refreshPending) clearUpdates();
    } catch (cause) {
      if (!abort.signal.aborted && current === operation) error(text('reloadHelp', 'Your draft is still here. Reload the latest review before retrying after a conflict.') + ' ' + String(cause), true);
    } finally {
      refreshing = false;
      if (refreshPending && !abort.signal.aborted) queueMicrotask(() => void refresh());
    }
  }
  async function transact(action: () => Promise<ReviewView | void>, closeEditor = false) {
    if (busy || abort.signal.aborted) return;
    operation++;
    const observed = invalidations;
    busy = true;
    automaticError = false;
    feedback.replaceChildren();
    body.setAttribute('aria-busy', 'true');
    body.querySelectorAll<HTMLButtonElement | HTMLInputElement>('button,input,select').forEach(el => { el.disabled = true; });
    try {
      const result = await action();
      if (abort.signal.aborted) return;
      if (result) {
        applyView(result); if (closeEditor) editing = false;
        if (observed === invalidations) { refreshPending = false; clearUpdates(); }
      }
      render();
    } catch (cause) {
      if (!abort.signal.aborted) error(text('reloadHelp', 'Your draft is still here. Reload the latest review before retrying after a conflict.') + ' ' + String(cause));
    } finally {
      busy = false;
      if (!abort.signal.aborted) {
        body.removeAttribute('aria-busy');
        body.querySelectorAll<HTMLButtonElement | HTMLInputElement>('button,input,select').forEach(el => { el.disabled = false; });
        if (refreshPending) queueMicrotask(() => void refresh());
      }
    }
  }
  async function load() {
    await transact(() => ports.load(abort.signal));
    if (!view && !abort.signal.aborted) {
      body.replaceChildren(button(text('reload', 'Reload latest review'), () => void load()));
    }
  }
  function save(command: ReviewCommand) {
    void transact(async () => {
      const result = await ports.save(command, abort.signal);
      if (!abort.signal.aborted && command.action === 'decision') { factDrafts.delete(command.criterionId); outputs?.settled(command.criterionId); }
      return result;
    }, command.action === 'requirements');
  }
  function chooseFacts(id: string) {
    if (!ports.loadFacts) return;
    void transact(async () => {
      const result = await ports.loadFacts!(abort.signal);
      if (!abort.signal.aborted) { facts = result; picking = id; }
    });
  }
  function renderFactPicker(item: HTMLElement, criterion: ReviewView['criteria'][number]) {
    if (!ports.loadFacts) return;
    item.append(button(text('chooseFacts', 'Choose supporting facts'), () => chooseFacts(criterion.id)));
    if (picking !== criterion.id || !facts) return;
    const selected = factDrafts.get(criterion.id) ?? criterion.evidence?.factBindings ?? [];
    const editor = doc.createElement('div'); editor.className = 'ui-stack'; editor.dataset.reviewFactPicker = '';
    editor.append(paragraph(text('nextEvidence', 'Supporting facts for the next decision')),
      paragraph(text('factsHelp', 'Choose the exact facts you checked. A current source binding does not verify correctness. Changes to selected sources make the review stale.')));
    for (const binding of selected) {
      const line = row();
      line.append(paragraph(binding.text + ' · ' + text('boundRevision', 'Bound revision') + ' ' + binding.revision),
        button(text('removeFact', 'Remove from next decision'), () => {
          factDrafts.set(criterion.id, selected.filter(value => value.id !== binding.id)); render();
        }));
      editor.append(line);
    }
    const available = facts.facts.filter(fact => fact.status === 'observed' || fact.status === 'owner_asserted');
    if (available.length) {
      const chooser = createField({ label: text('supportingFact', 'Supporting fact'),
        choices: available.map(fact => ({ value: fact.id, label: fact.text.slice(0, 120) + ' · v' + fact.revision })) }, doc);
      editor.append(chooser.el, button(text('addFact', 'Use this fact version'), () => {
        const fact = available.find(value => value.id === (chooser.control as HTMLSelectElement).value);
        if (!fact) return;
        const next = selected.filter(value => value.id !== fact.id);
        if (next.length >= ARTIFACT_REVIEW_MAX_FACT_BINDINGS) {
          error(text('factLimit', 'Remove a supporting fact before adding another.')); return;
        }
        factDrafts.set(criterion.id, [...next, { id: fact.id, revision: fact.revision, text: fact.text,
          statusAtReview: fact.status as 'observed' | 'owner_asserted' }]); render();
      }));
    } else editor.append(paragraph(text('noCurrentFacts', 'No current facts are available. Refresh or review the working facts first.')));
    if (!selected.length) editor.append(paragraph(text('manualEvidence', 'No working facts selected. This decision will rely on your review note.')));
    editor.append(button(text('refreshFacts', 'Refresh available facts'), () => chooseFacts(criterion.id)));
    item.append(editor);
  }
  function startEdit() {
    if (!view) return;
    editing = true; objective = view.objective;
    if ('outputs' in view) outputs?.begin(view);
    draft = view.criteria.map(({ id, text, kind }) => ({ id, text, kind })); render();
  }
  function renderEditor() {
    if (!view) return;
    body.append(field(text('objective', 'Objective'), objective, value => { objective = value; }));
    outputs?.editor(body, render, transact, abort.signal);
    draft.forEach((criterion, index) => {
      const item = doc.createElement('div'); item.className = 'ui-stack'; item.dataset.reviewDraft = criterion.id;
      item.append(field(text(criterion.kind, criterion.kind === 'constraint' ? 'Constraint' : 'Requirement') + ` ${index + 1}`,
        criterion.text, value => { criterion.text = value; }));
      item.append(button(text('remove', 'Remove criterion'), () => { draft = draft.filter(value => value !== criterion); render(); }));
      body.append(item);
    });
    const actions = row();
    for (const kind of ['requirement', 'constraint'] as const) {
      actions.append(button(text(kind === 'constraint' ? 'addConstraint' : 'addRequirement',
        kind === 'constraint' ? 'Add constraint' : 'Add requirement'), () => {
        draft.push({ id: crypto.randomUUID(), text: '', kind }); render();
      }));
    }
    actions.append(button(text('saveRequirements', 'Save requirements'), () => {
      if (!view) return;
      const command = { action: 'requirements' as const, requirementsRevision: view.requirementsRevision, objective, criteria: draft };
      save('outputs' in view && outputs ? { ...command, outputs: outputs.configuration() } : command);
    }));
    actions.append(button(text('cancel', 'Cancel editing'), () => { editing = false; render(); void refresh(); }));
    body.append(actions);
  }
  function render() {
    if (!view) return;
    body.replaceChildren();
    const labels = {
      unconfigured: text('unconfigured', 'No acceptance criteria yet'), pending: text('pending', 'Awaiting review'),
      changes_requested: text('changesRequested', 'Changes requested'), accepted: text('accepted', 'Accepted by owner'),
    };
    body.append(createAlert({ variant: view.status === 'accepted' ? 'success' : 'info', title: labels[view.status],
      body: outputs ? ports.text('turnReview.basis', 'Owner assessment of the declared task outputs. Execution completion does not imply acceptance.')
        : text('basis', 'Owner assessment of this artifact version. Execution completion does not imply acceptance.') }).el);
    if (editing) { renderEditor(); return; }
    if (view.objective) body.append(paragraph(view.objective));
    if ('outputs' in view) outputs?.summary(body, view);
    const states = { unreviewed: text('unreviewed', 'Not reviewed'), stale: text('stale', 'Previous review is stale'),
      pass: text('pass', 'Passed'), fail: text('fail', 'Needs changes') };
    for (const criterion of view.criteria) {
      const item = doc.createElement('section'); item.className = 'ui-stack'; item.dataset.reviewCriterion = criterion.id;
      const heading = doc.createElement('h3'); heading.textContent = criterion.text;
      item.append(heading, paragraph(text(criterion.kind, criterion.kind === 'constraint' ? 'Constraint' : 'Requirement') + ' · ' + states[criterion.state]));
      if (criterion.evidence) {
        item.append(paragraph(criterion.evidence.note), paragraph(criterion.evidence.reviewer + ' · ' +
          new Date(criterion.evidence.reviewedAt).toLocaleString(doc.documentElement.lang || undefined)));
        for (const binding of criterion.evidence.factBindings ?? []) {
          const dependency = criterion.factDependencies?.find(value => value.id === binding.id);
          const labels = { current: text('sourceCurrent', 'Source binding unchanged'),
            changed: text('sourceChanged', 'Supporting fact or source changed'),
            unavailable: text('sourceUnavailable', 'Supporting source unavailable'),
            needs_review: text('sourceNeedsReview', 'Supporting fact needs review') };
          const evidence = doc.createElement('div'); evidence.className = 'ui-stack'; evidence.dataset.reviewFactEvidence = binding.id;
          evidence.append(paragraph(binding.text), paragraph(text('boundRevision', 'Bound revision') + ': ' + binding.revision +
            ' · ' + text('currentRevision', 'Current revision') + ': ' + (dependency?.currentRevision ?? '—')),
            paragraph(dependency ? labels[dependency.status] : labels.unavailable));
          item.append(evidence);
        }
      }
      if ('outputs' in view && outputs) {
        const taskCriterion = view.criteria.find(value => value.id === criterion.id)!;
        outputs.evidence(item, taskCriterion); outputs.selection(item, view, taskCriterion, render);
      }
      renderFactPicker(item, criterion);
      item.append(field(text('evidence', 'Evidence and review notes'), notes.get(criterion.id) ?? '', value => notes.set(criterion.id, value)));
      const actions = row();
      for (const verdict of ['pass', 'fail'] as const) {
        actions.append(button(text(verdict === 'pass' ? 'confirmPass' : 'requestChanges', verdict === 'pass' ? 'I verified this' : 'Request changes'), () => {
          if (!view) return;
          const note = notes.get(criterion.id)?.trim() ?? '';
          if (!note) { error(text('noteRequired', 'Describe what you checked before recording a decision.')); return; }
          const command = { action: 'decision' as const, requirementsRevision: view.requirementsRevision,
            decisionsRevision: view.decisionsRevision,
            criterionId: criterion.id, criterionDigest: criterion.digest, verdict, note,
            factBindings: (factDrafts.get(criterion.id) ?? criterion.evidence?.factBindings ?? [])
              .map(({ id, revision }) => ({ id, revision })) };
          if ('outputs' in view && outputs) {
            const bindings = outputs.bindings(view.criteria.find(value => value.id === criterion.id)!);
            if (!bindings.length) { error(ports.text('turnReview.selectOutputsRequired', 'Select the output versions you checked before deciding.')); return; }
            save({ ...command, outputBindings: bindings });
          } else if ('artifactDigest' in view) save({ ...command, artifactDigest: view.artifactDigest });
        }));
      }
      item.append(actions); body.append(item);
    }
    const actions = row();
    actions.append(button(text('edit', 'Edit objective and criteria'), startEdit),
      button(text('reload', 'Reload latest review'), () => void load()));
    body.append(actions);
  }
  const onFocusOut = () => queueMicrotask(() => void refresh());
  host.addEventListener('focusout', onFocusOut);
  const stopObserving = ports.subscribeInvalidation?.(() => {
    if (abort.signal.aborted) return;
    invalidations++; refreshPending = true; void refresh();
  });
  syncOpen();
  if (open) void load();
  return () => {
    if (abort.signal.aborted) return;
    abort.abort(); host.removeEventListener('focusout', onFocusOut);
    try { stopObserving?.(); }
    finally { outputs?.dispose(); notes.clear(); factDrafts.clear(); facts = null; host.replaceChildren(); }
  };
}
