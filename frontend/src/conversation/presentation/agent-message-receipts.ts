/** Presentation of authoritative mailbox facts; no dispatch, timers or inferred acceptance. */
import type { Translator } from '../../i18n';
type Row = Record<string, any>;
const object = (v: any): Row => v && typeof v === 'object' && !Array.isArray(v) ? v : {};
const parse = (v: any): Row => { try { return object(typeof v === 'string' ? JSON.parse(v) : v); } catch { return {}; } };
export function messageReceiptIdentity(round: Row): Row {
  const receipt = parse(round.toolContent);
  const assignment = object(receipt.assignment);
  return { swarmRunId: assignment.swarm_run_id || receipt.swarm_run_id,
    agentId: assignment.agent_id || receipt.agent_id,
    messageId: assignment.message_id || receipt.message_id };
}
export function sameMessageReceipt(a: Row, b: Row): boolean {
  return ['swarmRunId', 'agentId', 'messageId'].every(k => typeof a[k] === 'string' && a[k] && a[k] === b[k]);
}
export function mergeMessageReceipt(old: Row, next: Row): Row {
  const merged = { ...old, ...Object.fromEntries(Object.entries(next).filter(([, value]) => value !== undefined)) };
  if (old.state === 'input_confirmed' && next.state !== 'input_confirmed'
      || old.state === 'unconfirmed' && next.state === 'submitted') merged.state = old.state;
  for (const key of ['inputInjectedAt', 'inputConfirmedAt']) {
    if (old[key]) merged[key] = old[key];
  }
  return merged;
}
const receiptKey = (receipt: Row): string => JSON.stringify([
  receipt.swarmRunId, receipt.agentId, receipt.messageId,
]);
/** Mirrored run/agent/sender evidence is one receipt, in first-seen order. */
export function coalesceMessageReceipts(receipts: readonly Row[]): Row[] {
  const accepted = new Map<string, Row>();
  for (const receipt of receipts) {
    if (!sameMessageReceipt(receipt, receipt)
        || !['submitted', 'input_confirmed', 'unconfirmed'].includes(receipt.state)) continue;
    const key = receiptKey(receipt);
    accepted.set(key, mergeMessageReceipt(accepted.get(key) || {}, receipt));
  }
  return [...accepted.values()];
}
/** Index only for this projection pass. No transcript cache survives the call. */
export function applyMessageReceipts(projection: Row, receipts: readonly Row[]): boolean {
  const accepted = coalesceMessageReceipts(receipts);
  if (!accepted.length) return false;
  const rounds: Row[] = projection.toolRounds || [];
  const senders = new Map<string, Row[]>(), runs = new Map<string, Row[]>();
  const add = (index: Map<string, Row[]>, key: string, round: Row) => {
    const rows = index.get(key);
    if (rows) rows.push(round); else index.set(key, [round]);
  };
  for (const round of rounds) {
    // Tool output can be megabytes. Parse it at most once per round in a pass,
    // never once per receipt (including copies of receipts on agent rows).
    let output: Row | undefined;
    const readOutput = () => output ??= parse(round.toolContent);
    if (round.toolName === 'send_agent_message') {
      const value = readOutput(), assignment = object(value.assignment);
      const receipt = { swarmRunId: assignment.swarm_run_id || value.swarm_run_id,
        agentId: assignment.agent_id || value.agent_id,
        messageId: assignment.message_id || value.message_id };
      if (sameMessageReceipt(receipt, receipt)) add(senders, receiptKey(receipt), round);
    }
    const run = object(round._swarmSnapshot).swarm_id || round._swarmRunId || readOutput().swarm_run_id;
    if (typeof run === 'string' && run) add(runs, run, round);
  }
  let changed = false;
  for (const receipt of accepted) {
    let content: string | undefined = typeof receipt.content === 'string' ? receipt.content : undefined;
    for (const round of senders.get(receiptKey(receipt)) || []) {
      round._agentMessageReceipt = mergeMessageReceipt(object(round._agentMessageReceipt), receipt);
      if (typeof content !== 'string') content = parse(round.toolArgs).content;
      if (typeof content === 'string') round._agentMessageReceipt.content = content;
      changed = true;
    }
    for (const round of runs.get(receipt.swarmRunId) || []) {
      const snapshot = object(round._swarmSnapshot);
      const runMessages: Row[] = round._swarmMessageReceipts || [];
      const messageIndex = runMessages.findIndex(old => sameMessageReceipt(old, receipt));
      const message = mergeMessageReceipt(messageIndex < 0 ? {} : runMessages[messageIndex],
        { ...receipt, ...(typeof content === 'string' ? { content } : {}) });
      round._swarmMessageReceipts = messageIndex < 0 ? [...runMessages, message]
        : runMessages.map((old, index) => index === messageIndex ? message : old);
      changed = true;
      for (const agent of [...(round._swarmAgents || []), ...(snapshot.agents || [])]) {
        if ((agent.id || agent.agentId) !== receipt.agentId) continue;
        const messages: Row[] = agent.messageReceipts || [];
        const index = messages.findIndex(old => sameMessageReceipt(old, receipt));
        const value = mergeMessageReceipt(index < 0 ? {} : messages[index], { ...receipt, ...(typeof content === 'string' ? { content } : {}) });
        agent.messageReceipts = index < 0 ? [...messages, value] : messages.map((old, i) => i === index ? value : old);
        changed = true;
      }
    }
  }
  return changed;
}
export function applyMessageReceipt(projection: Row, receipt: Row): boolean {
  return applyMessageReceipts(projection, [receipt]);
}
export function projectMessageReceipts(projection: Row): void {
  const receipts: Row[] = [];
  // Collect before applying: mirrored receipts must not become new work in
  // the same traversal as their source is copied to other rounds/agents.
  for (const round of projection.toolRounds || []) {
    receipts.push(...(round._swarmMessageReceipts || []),
      ...(object(round._swarmSnapshot).messageReceipts || []));
    for (const agent of [...(round._swarmAgents || []), ...(object(round._swarmSnapshot).agents || [])]) {
      for (const receipt of agent.messageReceipts || []) receipts.push({ ...receipt,
        runEnded: ['done', 'completed', 'failed', 'error', 'aborted', 'cancelled', 'interrupted'].includes(agent.status) });
    }
  }
  applyMessageReceipts(projection, receipts);
}
export function messageReceiptStatus(receipt: Row, translate: Translator, terminal = false): string {
  return translate(receipt.state === 'input_confirmed' ? 'tool.agentMessageInputConfirmed'
    : receipt.state === 'unconfirmed' || terminal || receipt.runEnded ? 'tool.agentMessageUnconfirmed'
    : receipt.inputInjectedAt ? 'tool.agentMessageInputInserted' : 'tool.agentMessageSubmitted');
}
/** Ephemeral display entries only: never add receipt rows to model tool calls.
 * Keep tool order; place messages before the first later evidenced tool start.
 * Missing timestamps stay at the tail, in durable sequence/identity order.
 */
export function projectAgentTimeline(agent: Row, translate: Translator): Row[] {
  const calls: Row[] = Array.isArray(agent._toolCalls) ? agent._toolCalls : [];
  const terminal = ['done', 'completed', 'failed', 'error', 'aborted', 'cancelled', 'interrupted'].includes(agent.status);
  const receipts = new Map<string, Row>();
  for (const receipt of agent.messageReceipts || []) {
    if (!sameMessageReceipt(receipt, receipt)) continue;
    const id = JSON.stringify([receipt.swarmRunId, receipt.agentId, receipt.messageId]);
    receipts.set(id, mergeMessageReceipt(receipts.get(id) || {}, receipt));
  }
  const time = (r: Row): number => Number(r.submittedAt || r.inputInjectedAt) > 0
    ? Number(r.submittedAt || r.inputInjectedAt) : Infinity;
  const messages = [...receipts.entries()].sort(([aid, a], [bid, b]) =>
    (time(a) - time(b)) || (Number(a.messageSequence || Infinity) - Number(b.messageSequence || Infinity)) || aid.localeCompare(bid));
  const buckets: Row[][] = Array.from({ length: calls.length + 1 }, () => []);
  for (const [id, receipt] of messages) {
    const index = calls.findIndex(call => Number(call.startedAt) > time(receipt));
    buckets[index < 0 ? calls.length : index].push({
      callId: `message:${id}`, entryKind: 'message_receipt', messageId: receipt.messageId,
      toolName: translate('tool.agentMessageReceived'),
      status: receipt.state === 'input_confirmed' ? 'done' : 'unknown',
      argsBrief: messageReceiptStatus(receipt, translate, terminal),
      preview: typeof receipt.content === 'string' ? receipt.content : translate('tool.agentMessageUnavailable'),
    });
  }
  return calls.flatMap((call, index) => [...buckets[index], call]).concat(buckets[calls.length]);
}
