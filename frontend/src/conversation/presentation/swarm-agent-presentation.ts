/**
 * Swarm launch identity and bounded card evidence shared by push and reload.
 * Durable rosters scope session status rows; transient cards enrich, never hide,
 * model/clock/tool receipts. No transport, routing, timers or durable writes.
 */
type Row = Record<string, any>;
const row = (value: unknown): Row => value && typeof value === 'object'
  && !Array.isArray(value) ? value as Row : {};
const rows = (value: unknown): Row[] => Array.isArray(value)
  ? value.filter(item => item && typeof item === 'object') : [];
const terminal = (status: unknown): boolean => [
  'done', 'completed', 'failed', 'error', 'aborted', 'cancelled', 'canceled', 'interrupted',
].includes(String(status ?? ''));

const activePhases = new Set([
  'running', 'starting', 'thinking', 'tool_use', 'writing', 'searching',
  'coding', 'analyzing', 'retrying',
]);
const queuedPhases = new Set(['pending', 'queued', 'waiting']);

const admissionWait = (agent: Row): boolean => queuedPhases.has(String(agent.status || ''))
  && ['capacity', 'dependencies'].includes(agent.waitReason);

/** Phase-only start frames are worker evidence, never still-queued stubs. */
function agentStatus(agent: Row): string {
  const status = String(agent.status || '');
  if (terminal(status)) return status === 'completed' ? 'done' : status;
  if (status === 'stalled') return status;
  if (admissionWait(agent)) return 'pending';
  if (activePhases.has(status) || activePhases.has(String(agent.phase || ''))) return 'running';
  if (queuedPhases.has(status) || (!status && queuedPhases.has(agent.phase))) return 'pending';
  return status || 'unknown';
}

/** A non-admitted worker is not idle by choice; show the evidenced blocker. */
export function swarmAgentWaitLabel(agent: Row,
  tt: (key: string, fallback: string) => string): string {
  if (agentStatus(agent) !== 'pending') return '';
  if (agent.waitReason === 'dependencies') return tt('swarm.wait.dependencies', 'Waiting for prerequisites');
  if (agent.waitReason === 'capacity') return tt('swarm.wait.capacity', 'Waiting for capacity');
  return tt('swarm.wait.pending', 'Waiting to start');
}

/** Log identity is display-only; routing continues to use the canonical spec ID. */
export function swarmAgentLogId(agent: Row): string {
  const id = String(agent.id || agent.agentId || '');
  return id ? `agent-${agent.role || 'general'}-${id}` : '';
}

export function swarmLaunchReceipt(round: Row): Row {
  try {
    const payload = row(typeof round.toolContent === 'string'
      ? JSON.parse(round.toolContent) : round.toolContent);
    if (Array.isArray(payload.agents)) return payload;
    if (payload.contractVersion !== 'tofu.tool-result/v2'
        && !(payload.contractVersion === undefined && typeof payload.summary === 'string')) return {};
    return rows(payload.items).find(item => Array.isArray(item.agents)) ?? {};
  } catch { return {}; }
}

/** Launch errors are persisted tool evidence, not an empty running swarm.
 * Do not invent a roster from requested agents: launch may fail before admission.
 */
export function swarmLaunchFailure(round: Row): string {

  if (round.toolName !== 'spawn_agents' && !round._swarm) return '';
  if (swarmLaunchReceipt(round).agents?.length || row(round._swarmSnapshot).agents?.length) return '';
  const raw = typeof round.toolContent === 'string' ? round.toolContent.trim() : '';
  if (/^Swarm tool error:/i.test(raw)) return raw;
  try {
    const payload = row(typeof round.toolContent === 'string' ? JSON.parse(raw) : round.toolContent);
    if (payload.status === 'error' || payload.isError === true) {
      const detail = payload.summary || payload.error;
      if (typeof detail === 'string') return detail;
    }
  } catch { /* Plain error text remains the legacy authority. */ }
  if (round.status === 'error' || round.status === 'failed') return raw || String(round.error || '');
  const failure = rows(round.results).find(result => result.isError === true);
  return failure ? String(failure.error || failure.text || failure.snippet || '') : '';
}

/** Missing run evidence is unbound; an execution id is only a legacy lookup alias. */
export function swarmRoundExplicitRunId(round: Row): string {
  const receipt = swarmLaunchReceipt(round);
  return String(row(round._swarmSnapshot).swarm_id || receipt.swarm_run_id
    || receipt.swarm_id || round._swarmRunId || '');
}

export function swarmRoundRunId(round: Row): string {
  return swarmRoundExplicitRunId(round) || String(round.taskId || '');
}

/** A launch roster is stronger than session-wide or accidentally grafted rows. */
export function swarmRoundAgentIds(round: Row): string[] {
  const receipt = swarmLaunchReceipt(round);
  const snapshot = row(round._swarmSnapshot);
  const agents = Array.isArray(receipt.agents) ? rows(receipt.agents)
    : Array.isArray(snapshot.agents) ? rows(snapshot.agents) : rows(round._swarmAgents);
  return agents.map(agent => String(agent.id || agent.agentId || '')).filter(Boolean);
}

/** A transport subscription key is never producer scope. Older mirrors may
 * carry the producing task in taskId alongside canonical conversationId. */
export function swarmFrameExecutionTaskId(frame: Row): string {
  const conversationId = String(frame.conversationId || frame.convId || '');
  return String(frame.executionTaskId || (conversationId && frame.taskId
    && frame.taskId !== conversationId ? frame.taskId : '') || '');
}

/** Explicit execution/attempt evidence must agree; missing target scope is
 * not permission to borrow another launch's roster. Run identity is separate. */
export function swarmFrameMatchesExecution(frame: Row, round: Row): boolean {
  const taskId = swarmFrameExecutionTaskId(frame);
  return (!taskId || round.taskId === taskId)
    && (!frame.attemptId || round.attemptId === frame.attemptId);
}

export type SwarmLaunchSelection =
  | { kind: 'matched'; round: Row }
  | { kind: 'absent' | 'ambiguous' | 'ended' };

/** Prefer this wave's receipt/roster, then its current unbound tool row.
 * Ambiguous and ended owners are not absent: neither permits a provisional
 * panel. A distinct current launch is resolved before fencing ended siblings.
 */
export function swarmLaunchSelection(rounds: readonly Row[], frame: Row): SwarmLaunchSelection {
  const runId = String(frame.swarmId || frame.swarm_id || '');
  const ids = rows(frame.agents).map(agent => String(agent.id || agent.agentId || '')).filter(Boolean);
  const scoped = rounds.filter(round => round.toolName === 'spawn_agents'
    && swarmFrameMatchesExecution(frame, round) && !swarmLaunchFailure(round));
  const ended = (round: Row): boolean => Boolean(row(round._swarmSnapshot).settled || round._swarmEndTime);
  const candidates = scoped.filter(round => !ended(round));
  const matchesRun = (round: Row): boolean => !runId || swarmRoundRunId(round) === runId;
  const matching = candidates.filter(round => matchesRun(round)
    && ids.some(id => swarmRoundAgentIds(round).includes(id)));
  if (matching.length) return matching.length === 1
    ? { kind: 'matched', round: matching[0] } : { kind: 'ambiguous' };
  const unbound = candidates.filter(round => !round._swarmSnapshot && !round.toolContent
    && (!swarmRoundExplicitRunId(round) || matchesRun(round)));
  if (unbound.length) return unbound.length === 1
    ? { kind: 'matched', round: unbound[0] } : { kind: 'ambiguous' };
  if (scoped.some(round => ended(round) && matchesRun(round))) return { kind: 'ended' };
  return { kind: 'absent' };
}

/** Launch-tool completion is not child completion; UI-only flags may be absent. */
export function swarmRoundIsActive(round: Row, agents: Row[]): boolean {
  if (swarmLaunchFailure(round) || row(round._swarmSnapshot).settled || round._swarmEndTime) return false;
  return Boolean(round._swarmActive || round._asyncRunning || round.status === 'searching'
    || agents.some(agent => ['running', 'pending'].includes(agentStatus(agent))));
}

export function swarmRoundNeedsRecovery(round: Row): boolean {
  return Boolean((round._swarm || round.toolName === 'spawn_agents')
    && !swarmLaunchFailure(round) && !row(round._swarmSnapshot).settled && !round._swarmEndTime);
}

/** Tool completion within one round is progress too, so it resets poll backoff. */
export function swarmStatusFingerprint(agents: Row[]): string {
  return JSON.stringify(agents.map(agent => [
    agent.id || agent.agentId || '', agent.status || '', agent.phase || '',
    agent.waitReason || '', agent.startedAt ?? agent._startedAt ?? null,
    agent.round || agent.rounds || agent.roundsUsed || 0,
    rows(agent.toolCalls ?? agent._toolCalls).slice(-30).map(call => [call.callId, call.status, call.elapsed,
      String(call.preview || '').length, String(call.error || '').length,
      call.previewFullChars, call.errorFullChars, call.previewTruncated, call.errorTruncated]),
  ]));
}

export function swarmAgentStart(value: unknown, now = Date.now()): number | undefined {
  const start = Number(value);
  return Number.isFinite(start) && start > 1e12 && start <= now + 300000
    ? Math.min(start, now) : undefined;
}

function evidenceMerge(base: Row, next: Row): Row {
  const merged = { ...base };
  for (const [key, value] of Object.entries(next)) {
    if (value !== '' && value != null && (!Array.isArray(value) || value.length)) {
      merged[key] = value;
    }
  }
  if (terminal(base.status) && base.status !== 'interrupted' && !terminal(next.status)) {
    merged.status = base.status;
    if (base.phase) merged.phase = base.phase;
    for (const key of ['preview', 'error', 'elapsed', 'tokens']) {
      if (base[key] != null && base[key] !== '') merged[key] = base[key];
    }
  }
  return merged;
}

export function normalizeSwarmAgent(agent: Row): Row {
  const normalized: Row = { ...agent, id: String(agent.id || agent.agentId || '') };
  normalized.status = agentStatus(agent);

  if (terminal(normalized.status) && (!normalized.phase
      || activePhases.has(normalized.phase) || queuedPhases.has(normalized.phase)
      || normalized.phase === 'unknown')) {
    normalized.phase = normalized.status === 'interrupted' ? 'interrupted'
      : ['failed', 'error'].includes(normalized.status) ? 'error'
      : ['aborted', 'cancelled', 'canceled'].includes(normalized.status) ? 'aborted' : 'done';
  }
  if (normalized.status === 'pending' && admissionWait(normalized)) {
    normalized.phase = 'pending';
    // Explicit nulls also clear mutable live-card adapters using Object.assign.
    normalized.startedAt = null;
    normalized._startedAt = null;
  }
  if (agent.status === 'retrying' && !agent.phase) normalized.phase = 'retrying';
  if (normalized.status === 'running'
      && (queuedPhases.has(normalized.phase) || normalized.phase === 'unknown')) normalized.phase = 'running';
  const start = swarmAgentStart(normalized.startedAt ?? normalized._startedAt);
  if (start) normalized._startedAt = start;
  else if (!admissionWait(normalized)) delete normalized._startedAt;
  if (Array.isArray(agent.toolCalls)) normalized._toolCalls = agent.toolCalls;
  if (typeof agent.toolCallsOmitted === 'number') normalized._toolCallsOmitted = agent.toolCallsOmitted;
  // Wire aliases are inputs, not a second copy of mutable presentation state.
  // Keeping them would overwrite a newer merged _toolCalls on the next read.
  delete normalized.toolCalls;
  delete normalized.toolCallsOmitted;
  return normalized;
}

function mergeTimeline(base: Row[], next: Row[], baseOmitted = 0, nextOmitted = 0,
  nextIsWindow = false): { calls: Row[]; omitted: number } {
  const calls = base.map(call => ({ ...call }));
  const baseIds = new Set(base.map(call => call.callId).filter(Boolean));
  // Without a shared occurrence, joining two windows would create a hidden
  // gap and make omitted + length cease to describe the newest position.
  // Keep the newer complete window; repeated/older windows (including legacy
  // id-less rows) cannot append stale calls or invent occurrence identities.
  if (nextIsWindow && base.length && next.length
      && !next.some(call => call.callId && baseIds.has(call.callId))) {
    if (nextOmitted + next.length <= baseOmitted + base.length) next = [];
    else {
      calls.length = 0;
      base = [];
      baseOmitted = 0;
    }
  }
  let beforeBase = 0, firstNext: Row | undefined;
  for (let n = 0; n < next.length; n += 1) {
    const call = next[n];
    const index = call.callId ? calls.findIndex(old => old.callId === call.callId) : -1;
    if (index >= 0) {
      calls[index] = evidenceMerge(calls[index], call);
      if (!firstNext) firstNext = calls[index];
    } else {
      // Insert an older prefix before its next shared occurrence, not after
      // the latest live call. Unanchored new delta events still append.
      const anchor = next.slice(n + 1).find(candidate => candidate.callId
        && calls.some(old => old.callId === candidate.callId));
      const at = anchor ? calls.findIndex(old => old.callId === anchor.callId) : calls.length;
      const copy = { ...call };
      calls.splice(at, 0, copy);
      if (at <= beforeBase && base.length) beforeBase += 1;
      if (!firstNext) firstNext = copy;
    }
  }
  const beforeNext = firstNext ? calls.indexOf(firstNext) : 0;
  const knownOmitted = Math.max(0, baseOmitted - beforeBase,
    next.length ? nextOmitted - beforeNext : 0);
  // Match the backend panel budget (JSON is conservatively ASCII-escaped).
  for (const call of calls) for (const key of ['preview', 'error']) {
    if (typeof call[key] !== 'string' || call[key].length <= 2000) continue;
    call[`${key}FullChars`] = Math.max(Number(call[`${key}FullChars`] || 0), call[key].length);
    call[`${key}Truncated`] = true;
    call[key] = call[key].slice(0, 2000);
  }
  let omitted = Math.max(0, calls.length - 30);
  if (omitted) calls.splice(0, omitted);
  const bytes = () => JSON.stringify(calls).replace(/[^\x00-\x7f]/g, '\\u0000').length;
  while (calls.length && bytes() > 32768) { calls.shift(); omitted += 1; }
  return { calls, omitted: knownOmitted + omitted };
}

export function mergeSwarmAgent(base: Row, next: Row): Row {
  const left = normalizeSwarmAgent(base), right = normalizeSwarmAgent(next);
  const merged = evidenceMerge(left, right);

  if (merged.status === 'interrupted') {
    merged._startedAt = 0;
    delete merged.startedAt;
  }
  // A launch placeholder has no authority to undo physical worker entry.
  if (left.status === 'running' && ['pending', 'unknown'].includes(right.status)
      && !admissionWait(next)) {
    merged.status = left.status;
    merged.phase = left.phase || 'running';
  }
  if (right.status === 'unknown' && !next.status && !next.phase) merged.status = left.status;
  // Status responses carry only a short objective; do not replace the full
  // launch/snapshot instruction with that lossy label.
  if (typeof left.objective === 'string' && typeof right.objective === 'string'
      && left.objective.startsWith(right.objective)) merged.objective = left.objective;
  // An older snapshot cannot restore the previous attempt's clock after a
  // retry has entered. Both wire and live clocks carry epoch milliseconds.
  const starts = [left._startedAt, right._startedAt].filter((value): value is number =>
    typeof value === 'number' && Number.isFinite(value));
  merged._startedAt = starts.length ? Math.max(...starts) : undefined;
  if (merged._startedAt) merged.startedAt = merged._startedAt;
  const timeline = mergeTimeline(rows(left._toolCalls), rows(right._toolCalls),
    Number(left._toolCallsOmitted || 0), Number(right._toolCallsOmitted || 0),
    Array.isArray(next.toolCalls) || typeof next.toolCallsOmitted === 'number'
      || typeof next._toolCallsOmitted === 'number');
  merged._toolCalls = timeline.calls;
  merged._toolCallsOmitted = timeline.omitted;
  merged.tools = [...new Set([...rows(left._toolCalls).map(call => call.toolName),
    ...(Array.isArray(left.tools) ? left.tools : []), ...(Array.isArray(right.tools) ? right.tools : []),
    ...timeline.calls.map(call => call.toolName)].filter(Boolean))];
  return normalizeSwarmAgent(merged);
}

export function mergeSwarmAgentCards(round: Row, recovered: Row[]): Row[] {
  if (row(round._swarmSnapshot).settled) return recovered;
  const live = rows(round._swarmAgents);
  const allowed = new Set(swarmRoundAgentIds(round));
  const byId = new Map(recovered.map(agent => [agent.id, normalizeSwarmAgent(agent)]));
  for (const agent of live) {
    if (allowed.size && !allowed.has(agent.id)) continue;
    byId.set(agent.id, mergeSwarmAgent(byId.get(agent.id) ?? {}, agent));
  }
  return [...byId.values()].filter(agent => !allowed.size || allowed.has(agent.id));
}

/** Status is session-scoped: only this launch's roster may enrich this card. */
export function swarmStatusAgents(round: Row, agents: Row[]): Row[] {
  const authoritative = Array.isArray(swarmLaunchReceipt(round).agents)
    || Array.isArray(row(round._swarmSnapshot).agents);
  if (!authoritative) return agents; // legacy/provisional roster has no launch receipt yet
  const ids = new Set(swarmRoundAgentIds(round));
  return agents.filter(agent => ids.has(String(agent.id || agent.agentId || '')));
}
