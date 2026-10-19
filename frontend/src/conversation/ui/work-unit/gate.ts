/** Owner-bound human review form. The same guidance identity keeps its draft, focus and submission receipt across polls. */
import { createField } from '../../../ui';
import { button, node, setText } from './dom';
import { terminal, type WorkAction, type WorkState, type WorkText, type WorkUnit } from './contracts';
export function createGateView(doc: Document, text: WorkText, act: (action: WorkAction) => void) {
  const root = node(doc, 'section', 'ptool-wu-gate'); root.hidden = true;
  const title = node(doc, 'h4', 'ptool-wu-gate-title');
  const deck = node(doc, 'p', 'ptool-wu-gate-deck');
  const pages = node(doc, 'ol', 'ptool-wu-gate-pages');
  const input = doc.createElement('textarea'); input.className = 'ui-input'; input.rows = 3;
  const field = createField({ control: input, label: text('workUnit.reviewFeedback', 'Changes to the plan (optional)') }, doc);
  const actions = node(doc, 'div', 'ptool-wu-actions');
  const message = node(doc, 'p', 'ptool-wu-action-message'); message.setAttribute('role', 'status');
  let guidanceId = '', fingerprint = '';
  const approve = button(doc, text('wu.gateApprove', 'Generate as planned'), () => {
    // Never discard a typed revision by silently choosing approval.
    // This exact protocol reply is recognized by the shared human-guidance
    // owner; translated button labels must not accidentally request a rewrite.
    const response = input.value.trim() || 'continue';
    act({ name: 'gate', guidanceId, response });
  }, true);
  const revise = button(doc, text('wu.gateSubmit', 'Submit changes'), () => {
    const response = input.value.trim();
    if (!response) { input.focus(); return; }
    act({ name: 'gate', guidanceId, response });
  });
  actions.append(approve, revise); root.append(title, deck, pages, field.el, actions, message);
  return { root, update(unit: WorkUnit, state: WorkState): void {
    const gate = !terminal(unit) ? unit.pendingGate : undefined;
    root.hidden = !gate?.guidanceId;
    if (!gate?.guidanceId) { guidanceId = ''; input.value = ''; return; }
    if (guidanceId !== gate.guidanceId) { guidanceId = gate.guidanceId; input.value = ''; fingerprint = ''; }
    root.dataset.wuGate = guidanceId;
    const label = unit.kind === 'motion-video'
      ? text('workUnit.reviewStoryboard', 'Review the storyboard') : text('wu.gateTitle', 'Review the outline');
    setText(title, label); setText(deck, gate.title || ''); deck.hidden = !gate.title;
    setText(field.el.querySelector('label')!, text('workUnit.reviewFeedback', 'Changes to the plan (optional)'));
    input.placeholder = text('workUnit.reviewPlaceholder', 'Tell Tofu what to change before production continues.');
    setText(approve, text('wu.gateApprove', 'Generate as planned')); setText(revise, text('wu.gateSubmit', 'Submit changes'));
    const next = JSON.stringify(gate.pages || []);
    if (next !== fingerprint) {
      fingerprint = next; pages.replaceChildren();
      for (const page of (Array.isArray(gate.pages) ? gate.pages : []).slice(0, 24)) {
        const row = node(doc, 'li', 'ptool-wu-gate-page');
        row.append(node(doc, 'span', 'ptool-wu-gate-page-n', String(page.page)), node(doc, 'span', '', page.key_message || ''));
        pages.append(row);
      }
    }
    const operation = state.operations.get('gate:' + guidanceId);
    const disabled = Boolean(operation?.busy || operation?.accepted);
    approve.disabled = disabled; revise.disabled = disabled; input.disabled = disabled;
    message.classList.toggle('ptool-wu-error', Boolean(operation?.error));
    setText(message, operation?.error || (operation?.accepted ? text('workUnit.reviewSubmitted', 'Submitted. Waiting for production to continue.') : ''));
  } };
}
