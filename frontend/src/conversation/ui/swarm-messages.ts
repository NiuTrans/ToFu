/** Run-level mailbox presentation only. No fetching, dispatch, or durable state.
 * mountSwarmMessages owns keyed DOM disclosures; receipt status comes from the
 * shared evidence presenter. The caller owns snapshot projection and teardown.
 */
import type { Translator } from '../../i18n';
import { createButton, createField } from '../../ui';
import { mergeMessageReceipt, messageReceiptStatus } from '../presentation/agent-message-receipts';

export interface SwarmMessageReceipt {
  swarmRunId: string;
  agentId: string;
  messageId: string;
  senderTaskId?: string;
  submittedAt?: number | string;
  state?: string;
  inputInjectedAt?: number | string;
  inputConfirmedAt?: number | string;
  content?: string;
  kind?: string;
}
export interface SwarmMessagesSnapshot {
  swarm_id: string;
  messageReceipts?: readonly SwarmMessageReceipt[];
  messageReceiptsAvailable?: boolean;
  messageParticipants?: readonly { id: string; taskId?: string; label: string }[];
}
export interface SwarmMessagesOptions {
  /** Exact-run participant identity; no global selector inside an agent card. */
  participantId?: string;
}
export interface SwarmMessagesHandle {
  element: HTMLElement;
  update(snapshot: SwarmMessagesSnapshot, translate?: Translator): void;
  dispose(): void;
}
type MessageRow = { el: HTMLLIElement; direction: HTMLElement; status: HTMLElement;
  time: HTMLElement; details: HTMLDetailsElement; preview: HTMLElement; collapse: HTMLElement; body: HTMLElement; evidence: HTMLElement };
const identity = (r: SwarmMessageReceipt): string => JSON.stringify([r.swarmRunId, r.agentId, r.messageId]);
const compare = (a: string, b: string): number => a < b ? -1 : a > b ? 1 : 0;
function submittedTime(value: SwarmMessageReceipt['submittedAt']): number {
  if (typeof value === 'number' || (typeof value === 'string' && /^\d+(\.\d+)?$/.test(value))) {
    // Mailbox timestamps are epoch milliseconds, unlike swarm lifecycle seconds.
    const milliseconds = Number(value);
    return milliseconds > 0 && Number.isFinite(new Date(milliseconds).getTime()) ? milliseconds : Infinity;
  }
  if (typeof value === 'string' && /^\d{4}-\d\d-\d\dT/.test(value)) {
    const parsed = Date.parse(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return Infinity;
}
function text(node: HTMLElement, value: string): void {
  if (node.textContent !== value) node.textContent = value;
}

export function mountSwarmMessages(container: HTMLElement, initial: SwarmMessagesSnapshot, initialTranslate: Translator, config: SwarmMessagesOptions = {}): SwarmMessagesHandle {
  const document = container.ownerDocument;
  const element = document.createElement('section'); element.className = 'sw-messages';
  const heading = document.createElement('h4');
  const field = createField({ label: '', choices: [] }, document);
  const filter = field.control as HTMLSelectElement;

  field.el.hidden = !!config.participantId;
  const notice = document.createElement('p'); notice.className = 'sw-messages-notice';
  const list = document.createElement('ol'); list.className = 'sw-messages-list';
  let visibleLimit = 3;
  let snapshot = initial;
  let translate = initialTranslate;
  let runId = initial.swarm_id;
  let disposed = false;
  let receipts = new Map<string, SwarmMessageReceipt>();
  const rows = new Map<string, MessageRow>();
  const earlier = createButton({ label: '', variant: 'ghost', size: 'sm', wrap: true,
    onClick: () => { visibleLimit = earlier.dataset.hasEarlier === 'true' ? visibleLimit + 20 : 3; render(); } }, document);
  element.append(heading, field.el, notice, earlier, list);
  container.append(element);

  function participant(id: string, sender: boolean): { id: string; label: string } {

    // Exact-run root routing does not establish a project Coordinator binding.
    if (id === snapshot.swarm_id) return { id, label: translate('swarm.messagesParent') };
    const participants = snapshot.messageParticipants || [];
    const taskMatches = sender ? participants.filter(p => p.taskId === id) : [];
    const matches = taskMatches.length ? taskMatches : participants.filter(p => p.id === id);
    const identities = new Set(matches.map(p => JSON.stringify([p.id, p.label])));
    if (identities.size === 1) return { id: matches[0].id,
      label: matches[0].id === snapshot.swarm_id ? translate('swarm.messagesParent') : `${matches[0].label} (${matches[0].id})` };
    return { id, label: sender ? `${translate('swarm.messagesUnknownSender')}${id ? ` (${id})` : ''}` : id };
  }
  function makeRow(): MessageRow {
    const el = document.createElement('li'); el.className = 'sw-messages-row';
    const direction = document.createElement('div'); direction.className = 'sw-messages-direction';
    const status = document.createElement('span'); status.className = 'sw-messages-status';
    const time = document.createElement('span'); time.className = 'sw-messages-time';
    const details = document.createElement('details'); details.className = 'sw-messages-content';
    const summary = document.createElement('summary');
    // Native [open] styles switch the accessible summary without a delayed toggle handler.
    const preview = document.createElement('span'); preview.className = 'sw-messages-preview';
    const collapse = document.createElement('span'); collapse.className = 'sw-messages-collapse';
    summary.append(preview, collapse);
    const body = document.createElement('div'); body.className = 'sw-messages-body';
    const evidence = document.createElement('dl'); evidence.className = 'sw-messages-evidence';
    const diagnostics = document.createElement('details'); diagnostics.className = 'sw-messages-diagnostics';
    const diagnosticsSummary = document.createElement('summary');
    diagnostics.append(diagnosticsSummary, evidence);
    details.append(summary, body, diagnostics); el.append(direction, status, time, details);
    return { el, direction, status, time, details, preview, collapse, body, evidence };
  }
  function render(): void {
    text(heading, translate('swarm.messagesTitle'));
    element.setAttribute('aria-label', translate('swarm.messagesTitle'));
    text(field.el.querySelector('label')!, translate('swarm.messagesParticipant'));
    const options = new Map<string, string>([['', translate('swarm.messagesAll')]]);
    for (const p of snapshot.messageParticipants || []) options.set(p.id, participant(p.id, false).label);
    for (const r of receipts.values()) {
      const sender = participant(r.senderTaskId || '', true);
      if (sender.id) options.set(sender.id, sender.label);
      options.set(r.agentId, participant(r.agentId, false).label);
    }
    // Keep the select itself mounted and preserve a reader-selected filter.
    if (filter.value && !options.has(filter.value)) options.set(filter.value, filter.selectedOptions[0]?.textContent || filter.value);
    const selected = config.participantId || filter.value;
    const wanted = [...options];
    if (JSON.stringify([...filter.options].map(o => [o.value, o.textContent])) !== JSON.stringify(wanted)) {
      filter.replaceChildren(...wanted.map(([value, label]) => {
        const option = document.createElement('option'); option.value = value; option.textContent = label; return option;
      }));
      filter.value = selected;
    }
    const ordered = [...receipts].sort(([aId, a], [bId, b]) =>
      (submittedTime(a.submittedAt) - submittedTime(b.submittedAt)) || compare(aId, bId));
    const matching = ordered.filter(([, r]) => !selected || r.agentId === selected || participant(r.senderTaskId || '', true).id === selected);
    const visible = new Set(matching.slice(-visibleLimit).map(([id]) => id));

    const matchingIds = new Set(matching.map(([id]) => id));
    const hasEarlier = matching.length > visibleLimit;
    earlier.hidden = matching.length <= 3;
    earlier.dataset.hasEarlier = String(hasEarlier);
    text(earlier, translate(hasEarlier ? 'swarm.messagesEarlier' : 'swarm.messagesRecent'));
    earlier.setAttribute('aria-expanded', String(visibleLimit > 3));
    notice.hidden = matching.length > 0 && snapshot.messageReceiptsAvailable === true;
    text(notice, translate(snapshot.messageReceiptsAvailable !== true || snapshot.messageReceipts === undefined
      ? 'swarm.messagesUnavailable' : 'swarm.messagesEmpty'));
    const focused = document.activeElement as HTMLElement | null;
    let cursor = list.firstElementChild;
    for (const [id, receipt] of ordered) {
      let row = rows.get(id);
      const matchesFilter = matchingIds.has(id);
      const retained = row && (row.details.open || (focused && row.el.contains(focused)));
      if (!visible.has(id) && !retained) {
        if (row) { if (cursor === row.el) cursor = row.el.nextElementSibling; row.el.remove(); rows.delete(id); }
        continue;
      }
      if (!row) { row = makeRow(); rows.set(id, row); }
      const sender = participant(receipt.senderTaskId || '', true);
      text(row.direction, `${sender.label} → ${participant(receipt.agentId, false).label}`);
      text(row.status, ['submitted', 'input_confirmed', 'unconfirmed'].includes(receipt.state || '')
        ? messageReceiptStatus(receipt, translate) : translate('tool.agentMessageUnconfirmed'));

      row.status.dataset.state = receipt.state || 'unknown';
      const time = submittedTime(receipt.submittedAt);
      text(row.time, Number.isFinite(time) ? new Date(time).toISOString() : translate('swarm.messagesTimeUnavailable'));
      const body = typeof receipt.content === 'string' ? receipt.content : translate('tool.agentMessageUnavailable');
      text(row.preview, body.length > 160 ? `${body.slice(0, 160)}…` : body);
      text(row.collapse, translate('toolCmd.collapse'));
      text(row.body, body);

      text(row.details.querySelector('.sw-messages-diagnostics > summary') as HTMLElement, translate('swarm.messagesReceiptDetails'));
      const evidence: [string, string][] = [
        [translate('swarm.messagesRunId'), receipt.swarmRunId],
        [translate('swarm.messagesMessageId'), receipt.messageId],
      ];
      for (const [key, label] of [
        ['submittedAt', 'swarm.messagesSubmittedAt'], ['inputInjectedAt', 'swarm.messagesInjectedAt'],
        ['inputConfirmedAt', 'swarm.messagesConfirmedAt'],
      ] as const) {
        if (receipt[key] !== undefined) {
          const milliseconds = submittedTime(receipt[key]);
          evidence.push([translate(label), Number.isFinite(milliseconds) ? new Date(milliseconds).toISOString() : String(receipt[key])]);
        }
      }
      const evidenceKey = JSON.stringify(evidence);
      if (row.evidence.dataset.evidence !== evidenceKey) {
        row.evidence.dataset.evidence = evidenceKey;
        row.evidence.replaceChildren(...evidence.flatMap(([label, value]) => {
          const term = document.createElement('dt'); term.textContent = label;
          const description = document.createElement('dd'); description.textContent = value;
          return [term, description];
        }));
      }
      row.el.dataset.messageId = receipt.messageId;
      row.el.hidden = !matchesFilter;
      if (row.el !== cursor) list.insertBefore(row.el, cursor);
      cursor = row.el.nextElementSibling;
    }
    // Moving a keyed row can blur its native summary in browsers without moveBefore.
    if (focused && element.contains(focused) && document.activeElement !== focused) focused.focus({ preventScroll: true });
  }
  function update(next: SwarmMessagesSnapshot, nextTranslate = translate): void {
    if (disposed) return;
    if (runId !== next.swarm_id) {
      runId = next.swarm_id; visibleLimit = 3; filter.value = ''; receipts.clear();
      rows.clear(); list.replaceChildren();
    }
    snapshot = next; translate = nextTranslate;
    const merged = new Map<string, SwarmMessageReceipt>();
    for (const receipt of next.messageReceipts || []) {
      if (receipt.swarmRunId !== runId || !receipt.agentId || !receipt.messageId) continue;
      const id = identity(receipt);
      merged.set(id, mergeMessageReceipt(merged.get(id) || receipts.get(id) || {}, receipt) as SwarmMessageReceipt);
    }
    receipts = merged;
    for (const [id, row] of rows) if (!receipts.has(id)) { row.el.remove(); rows.delete(id); }
    render();
  }
  filter.addEventListener('change', render);
  update(initial);
  return { element, update, dispose() {
    disposed = true; filter.removeEventListener('change', render); rows.clear(); receipts.clear(); element.remove();
  } };
}
