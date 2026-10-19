/** One dialog mount owns facts, unsaved drafts and a single consumer page.
 * All writes bind the displayed aggregate revision. Unknown write outcomes require
 * an explicit reload; disposal aborts and fences replies. No polling or task writes.
 */
import { createAlert, createButton, createTabs } from '../../ui';
import type { WorkingFactsView, WorkingFactConsumersView, WorkingFactOwnerDecision } from '../../api/working-facts.generated';
import type { WorkingFactsPorts, Draft, Text } from './ports';
import { node, factRow, consumerRow } from './view';

export function mountWorkingFacts(host: HTMLElement, conversationId: string, ports: WorkingFactsPorts, translate: Text) {
  const doc = host.ownerDocument, abort = new AbortController();
  const t = translate;
  let facts: WorkingFactsView | null = null, consumers: WorkingFactConsumersView | null = null;
  let busy = false, needsReload = true, offset = 0;
  const drafts = new Map<string, Draft>(), expanded = new Set<string>();
  const feedback = node(doc, 'div'), factsFeedback = node(doc, 'div'), impactFeedback = node(doc, 'div');
  const fields = doc.createElement('fieldset');
  const factList = node(doc, 'div', '', 'ui-stack'), impactList = node(doc, 'div', '', 'ui-stack');
  const factsPanel = node(doc, 'div', '', 'ui-stack'), impactPanel = node(doc, 'div', '', 'ui-stack');
  const revision = node(doc, 'p');
  const reload = createButton({ label: t('workingFacts.reload', 'Reload latest facts and impact'), variant: 'secondary', size: 'sm', wrap: true,
    onClick: () => void load() }, doc);
  fields.append(factList); fields.setAttribute('aria-label', t('workingFacts.facts', 'Current facts'));
  factsPanel.append(node(doc, 'p', t('workingFacts.help', 'Inspect the sources behind the agent’s working facts. Your corrections guide future context; an owner assessment does not independently verify a result.')), factsFeedback, revision, fields);
  impactPanel.append(node(doc, 'p', t('workingFacts.coverage', 'Only recorded working-fact context since tracking was enabled is covered. Missing tasks may still be affected.')),
    node(doc, 'p', t('workingFacts.impactHelp', 'This reports changes to task inputs, not task acceptance. Existing outputs are kept; no task is rerun automatically.')),
    impactFeedback, impactList);
  const tabs = createTabs({ tabs: [
    { id: 'facts', label: t('workingFacts.facts', 'Current facts'), panel: factsPanel },
    { id: 'impact', label: t('workingFacts.impact', 'Task impact'), panel: impactPanel },
  ] }, doc);
  host.className = 'working-facts ui-stack'; host.dataset.workingFacts = conversationId;
  host.append(reload, feedback, tabs.el);
  function alert(target: HTMLElement, title: string, body: string) {
    target.replaceChildren(createAlert({ variant: 'error', title, body }).el);
  }
  function lock(value: boolean) {
    busy = value; reload.disabled = value; fields.disabled = value || needsReload;
    host.setAttribute('aria-busy', String(value));
    impactList.querySelectorAll('button').forEach(button => { button.disabled = value; });
  }
  function validateScope(value: { conversationId: string }) {
    if (value.conversationId !== conversationId) throw new Error(t('workingFacts.scopeError', 'The response belongs to another conversation.'));
  }
  function renderFacts() {
    if (!facts) return;
    revision.textContent = t('workingFacts.revision', 'Facts revision') + ': ' + facts.revision;
    factList.replaceChildren();
    if (!facts.facts.length) factList.append(node(doc, 'p', t('workingFacts.empty', 'No source-bound working facts have been recorded yet.')));
    offset = Math.min(offset, Math.max(0, Math.ceil(facts.facts.length / 20) - 1) * 20);
    for (const fact of facts.facts.slice(offset, offset + 20)) {
      const draft = drafts.get(fact.id) || { text: fact.text, note: '' }; drafts.set(fact.id, draft);
      if (!draft.edited) draft.text = fact.text;
      const row = factRow(doc, fact, draft, t, action => void save(fact.id, action));
      row.open = expanded.has(fact.id);
      row.addEventListener('toggle', () => { if (row.open) expanded.add(fact.id); else expanded.delete(fact.id); });
      factList.append(row);
    }
    const paging = node(doc, 'div', '', 'ui-row ui-row--wrap');
    if (offset) paging.append(createButton({ label: t('workingFacts.previousFacts', 'Previous facts'), variant: 'secondary', size: 'sm',
      onClick: () => { offset -= 20; renderFacts(); } }, doc));
    if (offset + 20 < facts.facts.length) paging.append(createButton({ label: t('workingFacts.nextFacts', 'Next facts'), variant: 'secondary', size: 'sm',
      onClick: () => { offset += 20; renderFacts(); } }, doc));
    factList.append(paging);
  }
  function renderImpact() {
    impactList.replaceChildren();
    if (!consumers) return;
    impactList.append(node(doc, 'p', t('workingFacts.revision', 'Facts revision') + ': ' + consumers.factsRevision));
    if (facts && consumers.factsRevision !== facts.revision) impactList.append(createAlert({ variant: 'warning',
      title: t('workingFacts.changedDuringRead', 'Facts changed during this read'),
      body: t('workingFacts.reloadHelp', 'Your draft is preserved. Reload and review the latest facts before submitting again.') }).el);
    if (!consumers.consumers.length) impactList.append(node(doc, 'p', t('workingFacts.noConsumers', 'No recorded tasks on this page. This does not prove that previous tasks were unaffected.')));
    for (const consumer of consumers.consumers) impactList.append(consumerRow(doc, consumer, t));
    const paging = node(doc, 'div', '', 'ui-row ui-row--wrap');
    paging.append(createButton({ label: t('workingFacts.firstTasks', 'Refresh from first task page'), variant: 'secondary', size: 'sm', wrap: true,
      onClick: () => void page('') }, doc));
    if (consumers.nextCursor) paging.append(createButton({ label: t('workingFacts.nextTasks', 'Next task page'), variant: 'secondary', size: 'sm',
      onClick: () => void page(consumers!.nextCursor) }, doc));
    impactList.append(paging, node(doc, 'p', t('workingFacts.pageHelp', 'Each page is read live. Version ranges show the earliest and latest recorded versions, not every intermediate version.')));
  }
  async function readConsumers(cursor: string) {
    // Retire the previous result before any write refresh or page fetch; an error
    // must never leave stale impact looking like the result of the new decision.
    consumers = null; impactList.replaceChildren(); impactFeedback.replaceChildren();
    try {
      const result = await ports.consumers(conversationId, cursor, abort.signal);
      if (abort.signal.aborted) return;
      validateScope(result); consumers = result; renderImpact();
    } catch (cause) {
      if (!abort.signal.aborted) alert(impactFeedback, t('workingFacts.impactError', 'Task impact could not be loaded'), String(cause));
    }
  }
  async function readFacts(minimumRevision = facts?.revision || 0) {
    factsFeedback.replaceChildren();
    try {
      const result = await ports.load(conversationId, abort.signal);
      if (abort.signal.aborted) return;
      validateScope(result);
      if (result.revision < minimumRevision) throw new Error(t('workingFacts.changedDuringRead', 'Facts changed during this read'));
      facts = result; needsReload = false; renderFacts(); renderImpact();
    } catch (cause) {
      needsReload = true;
      if (!abort.signal.aborted) alert(factsFeedback, t('workingFacts.loadError', 'Facts could not be loaded'), String(cause));
    }
  }
  async function load() {
    if (busy || abort.signal.aborted) return;
    lock(true); feedback.replaceChildren();
    await Promise.all([readFacts(), readConsumers('')]);
    if (!abort.signal.aborted) lock(false);
  }
  async function page(cursor: string) {
    if (busy || abort.signal.aborted) return;
    lock(true); await readConsumers(cursor); if (!abort.signal.aborted) lock(false);
  }
  async function save(id: string, action: WorkingFactOwnerDecision['action']) {
    if (busy || needsReload || !facts || abort.signal.aborted) return;
    const draft = drafts.get(id)!;
    if (!draft.note.trim() || (action === 'correct' && !draft.text.trim())) {
      alert(feedback, t('workingFacts.required', 'A reason is required'), t('workingFacts.requiredHelp', 'Add a note explaining your decision and nonempty text for a correction.')); return;
    }
    lock(true); feedback.replaceChildren();
    // A failed write may have committed: require an owner-initiated read, never replay.
    consumers = null; impactList.replaceChildren();
    try {
      const result = await ports.save(conversationId, { action, id, revision: facts.revision, note: draft.note,
        ...(action === 'correct' ? { text: draft.text } : {}) }, abort.signal);
      if (abort.signal.aborted) return;
      // Mutation responses contain only the selected fact. Refresh the full
      // graph so independent rows remain visible and dependent statuses update.
      validateScope(result); drafts.delete(id);
      feedback.replaceChildren(createAlert({ variant: 'success', title: t('workingFacts.saved', 'Owner decision saved') }).el);
      await Promise.all([readFacts(result.revision), readConsumers('')]);
    } catch (cause) {
      needsReload = true;
      if (!abort.signal.aborted) {
        alert(feedback, t('workingFacts.saveError', 'Decision was not confirmed'), t('workingFacts.reloadHelp', 'Your draft is preserved. Reload and review the latest facts before submitting again.') + ' ' + String(cause));
        alert(impactFeedback, t('workingFacts.impactUnknown', 'Impact needs a fresh read'), t('workingFacts.reloadHelp', 'Your draft is preserved. Reload and review the latest facts before submitting again.'));
      }
    } finally {
      if (!abort.signal.aborted) { lock(false); reload.focus({ preventScroll: true }); }
    }
  }
  void load();
  return () => { abort.abort(); drafts.clear(); expanded.clear(); host.replaceChildren(); };
}
