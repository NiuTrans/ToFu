/** Send-message display only; never dispatches, retries, or creates receipts.
 * Entry point: renderSendAgentMessageHtml. The existing tool-card port owns
 * lifecycle/disclosure. Dependencies: generated surface contract, shared result
 * source selection, escaping and locale authority. Request args own the body;
 * only exact known receipts/outcome texts establish the quiet submission state.
 */
import { messageReceiptIdentity, sameMessageReceipt, messageReceiptStatus } from './agent-message-receipts';
import type { Translator } from '../../i18n';
import { escapeHtmlText } from '../../html-safety';
import { TOOL_RESULT_SURFACES } from './tool-result-surfaces.generated';
import { resolveToolResultText, toolResultSurfacePolicy } from './tool-result-surfaces';
import type { ToolResultHeaderHtml } from './tool-result-presentation';

type RecordValue = Readonly<Record<string, unknown>>;
type ReceiptState = keyof typeof TOOL_RESULT_SURFACES.sendMessageReceipt.outcomes;
const MAX_ARGUMENT_JSON_CHARS = 65_536;
const MAX_RESULT_CHARS = 120_000;
// Backend messageChars counts code points: a valid 4096-emoji message uses
// 8192 UTF-16 units. This bound must not silently clip a valid authored body.
const MAX_MESSAGE_CODE_UNITS = 8192;
function record(value: unknown): RecordValue {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as RecordValue : {};
}
function text(value: unknown): string { return typeof value === 'string' ? value : ''; }
function parsedRecord(value: unknown): RecordValue {
  if (typeof value !== 'string') return record(value);
  if (value.length > MAX_ARGUMENT_JSON_CHARS) return {};
  try { return record(JSON.parse(value)); } catch { return {}; }
}
function exactFields(value: RecordValue, fields: readonly string[]): boolean {
  return Object.keys(value).length === fields.length
    && fields.every(key => Object.prototype.hasOwnProperty.call(value, key));
}
function receiptRecordState(receipt: RecordValue, args: RecordValue): ReceiptState | null {
  const policy = TOOL_RESULT_SURFACES.sendMessageReceipt;
  if (!text(receipt.message_id) || !['reference', 'steer'].includes(text(receipt.kind))
      || !Number.isInteger(receipt.contentChars) || Number(receipt.contentChars) < 1
      || !Object.prototype.hasOwnProperty.call(policy.outcomes, text(receipt.state))) return null;
  const state = receipt.state as ReceiptState;
  if (exactFields(receipt, policy.stableFields)) {
    if (!text(receipt.retained_agent_id)
        || !(policy.stableScopes as readonly string[]).includes(text(receipt.scope))
        || receipt.execution_started !== (state === 'turn_started')
        || text(args.agent_id) || text(args.swarm_run_id)) return null;
    for (const key of ['retained_agent_id', 'scope']) {
      if (typeof args[key] === 'string' && receipt[key] !== args[key]) return null;
    }
    const assignment = record(receipt.assignment);
    const allowedFields: readonly string[] = [...policy.assignmentFields, ...policy.assignmentOptionalFields];
    if (!policy.assignmentFields.every(key => text(assignment[key]))
        || Object.keys(assignment).some(key => !allowedFields.includes(key))) return null;
    if (Object.prototype.hasOwnProperty.call(assignment, 'launch')
        && record(assignment.launch).status !== 'async_launched') return null;

    if (Object.prototype.hasOwnProperty.call(assignment, 'message_id')
        && !text(assignment.message_id)) return null;
    return state;
  }
  if (!exactFields(receipt, policy.fields) || text(args.retained_agent_id)
      || !['agent_id', 'swarm_run_id'].every(key => text(receipt[key]))
      || receipt.execution_started !== false
      || !(policy.legacyStates as readonly string[]).includes(state)) return null;
  // Historical exact receipt identities cannot certify a different request.
  for (const key of ['agent_id', 'swarm_run_id']) {
    if (typeof args[key] === 'string' && receipt[key] !== args[key]) return null;
  }
  return state;
}
function receiptState(source: string, args: RecordValue): ReceiptState | null {
  const policy = TOOL_RESULT_SURFACES.sendMessageReceipt;
  // Only legacy receipts are compacted to text. A stable outcome needs its
  // pinned assignment; a bare launch-accepted sentence cannot certify a turn.
  if (!text(args.retained_agent_id)) {
    for (const state of policy.legacyStates) {
      if (source === policy.outcomes[state]) return state;
    }
  }
  return receiptRecordState(parsedRecord(source), args);
}
function submissionRejected(payload: RecordValue): boolean {
  // The code/reason is diagnostic, not submission authority. Conflicting or
  // unknown top-level evidence must not be upgraded by a nested error field.
  const state = Object.prototype.hasOwnProperty.call(payload, 'submission_state')
    ? payload.submission_state : record(payload.error).submission_state;
  return state === 'rejected';
}
function failureText(source: string): string {
  const payload = parsedRecord(source);
  if (payload.status !== 'error' && !payload.error) return source;
  const error = record(payload.error);
  const code = text(payload.code) || text(error.code);
  const action = text(payload.next_action) || text(payload.nextAction)
    || text(error.next_action) || text(error.nextAction);
  // A generic summary must not hide the raw producer diagnostic.
  const lines = [code, text(payload.summary), text(payload.message), text(error.message),
    text(payload.error), action].filter(Boolean);
  // Unknown error shapes must remain inspectable rather than become success.
  return lines.length ? [...new Set(lines)].join('\n') : source;
}

export function renderSendAgentMessageHtml(
  roundValue: unknown,
  header: ToolResultHeaderHtml,
  translate: Translator,
  compactionLabelHtml: string,
): string {
  const round = record(roundValue);
  if (toolResultSurfacePolicy(round.toolName).frontendFormat !== 'sent_message'
      || !['done', 'completed', 'error', 'failed'].includes(text(round.status))) return '';
  // Leave historical deferred evidence to the existing owner-scoped lazy port.
  if (record(round._snapshotToolResult).preview !== undefined) return '';
  const args = parsedRecord(round.toolArgs);
  const authoritative = resolveToolResultText(round);
  const source = authoritative?.text || '';
  const payload = parsedRecord(source);
  const failed = ['error', 'failed'].includes(text(round.status))
    || payload.status === 'error' || Boolean(payload.error);
  const rejected = failed && submissionRejected(payload);
  const failedReceiptState = failed && !rejected
    ? receiptRecordState(record(payload.receipt), args) : null;
  const state = failed ? (failedReceiptState === 'unconfirmed' ? 'unconfirmed' : null)
    : receiptState(source, args);
  if (!state && !failed) return ''; // Preserve unknown producer semantics.
  const target = (text(args.retained_agent_id) || text(args.agent_id)).slice(0, 512);
  const title = target ? translate('tool.agentMessageTarget', { agent: target })
    : translate('tool.agentMessage');
  const liveReceipt = record(round._agentMessageReceipt);
  const liveMatches = !failed && sameMessageReceipt(messageReceiptIdentity(round), liveReceipt)
    && ['submitted', 'input_confirmed', 'unconfirmed'].includes(text(liveReceipt.state));
  const status = liveMatches ? messageReceiptStatus(liveReceipt, translate)
    : rejected ? translate('tool.agentMessageRejected')
    : failed && state === 'unconfirmed' ? translate('tool.agentMessageSavedUnconfirmed')
    : failed ? translate('tool.agentMessageFailed')
    : state === 'turn_started' ? translate('tool.agentMessageTurnStarted')
    : state === 'input_confirmed' ? translate('tool.agentMessageInputConfirmed')
    : state === 'unconfirmed' ? translate('tool.agentMessageUnconfirmed')
    : translate('tool.agentMessageSubmitted');
  const content = typeof args.content === 'string'
    ? args.content.slice(0, MAX_MESSAGE_CODE_UNITS) : '';
  const body = content ? `<pre class="ptool-result-pre"><code>${escapeHtmlText(content)}</code></pre>`
    : `<p class="ptool-result-trunc">${escapeHtmlText(translate('tool.agentMessageUnavailable'))}</p>`;
  const clipped = typeof args.content === 'string' && args.content.length > MAX_MESSAGE_CODE_UNITS;
  const errorText = failed ? failureText(source).slice(0, MAX_RESULT_CHARS) : '';
  const errorHtml = failed ? `<pre class="ptool-result-pre ptool-agent-message-error"><code>${escapeHtmlText(errorText
    || translate('tool.agentMessageErrorUnavailable'))}</code></pre>` : '';
  const reconciliationHtml = state === 'unconfirmed'
    ? `<p class="ptool-result-trunc">${escapeHtmlText(translate('tool.agentMessageUnconfirmedAction'))}</p>` : '';
  return `<details class="ptool-result-block ptool-agent-message${failed ? ' ptool-error' : ''}" data-tool-result-authority="${authoritative?.source || 'request'}" data-rn="${escapeHtmlText(round.roundNum)}" open>
    <summary class="ptool-line ptool-result-header">
      <span class="ptool-icon">${header.iconHtml}</span>${compactionLabelHtml}${header.rootPillHtml}
      <span class="ptool-text">${escapeHtmlText(title)}</span>${header.repairedBadgeHtml || ''}
      <span class="ptool-line-status${failed ? ' ptool-line-status--err' : ''}">${escapeHtmlText(status)}</span>
      ${header.rightControlsHtml || ''}
    </summary>
    <div class="ptool-result-body">${body}${clipped ? `<p class="ptool-result-trunc">${escapeHtmlText(translate('tool.resultTruncated', { n: MAX_MESSAGE_CODE_UNITS.toLocaleString() }))}</p>` : ''}${reconciliationHtml}${errorHtml}</div>
  </details>`;
}
