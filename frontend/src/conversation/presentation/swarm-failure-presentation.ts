/** Transient failure notices and truthful outcome counts; no transport or durable state. */
import { normalizeSwarmAgent } from './swarm-agent-presentation';
type Row = Record<string, any>;
export type SwarmTranslate = (key: string, vars?: Record<string, string | number>) => string;
export function swarmOutcomeStatus(agent: Row): string {
  if (['aborted', 'cancelled', 'canceled'].includes(String(agent.status))) return 'cancelled';
  if (agent.status === 'interrupted') return 'interrupted';
  if (agent.status === 'failed' || agent.status === 'error' || agent.error) return 'failed';
  return normalizeSwarmAgent(agent).status;
}
export function swarmOutcomeCounts(agents: readonly Row[]) {
  const counts = { running: 0, pending: 0, succeeded: 0, failed: 0, cancelled: 0, interrupted: 0 };
  for (const agent of agents) {
    const status = swarmOutcomeStatus(agent);
    if (status === 'done') counts.succeeded++;
    else if (status in counts) counts[status as keyof typeof counts]++;
  }
  return counts;
}
export function swarmFailureReason(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value && typeof value === 'object') {
    const row = value as Row;
    return swarmFailureReason(row.detail || row.raw || row.message || row.error);
  }
  return '';
}
/** Route admission failure is local; never report it as provider outage. */
export function swarmAgentFailureCard(error: unknown, preview: unknown, modifiedFiles: number,
  t: SwarmTranslate): { title: string; detail: string; recovery: string } {
  const reason = swarmFailureReason(error) || swarmFailureReason(preview);
  const data = error && typeof error === 'object' && !Array.isArray(error) ? error as Row : {};
  const code = data.code || (data.reason && typeof data.reason === 'object' ? data.reason.code : '');
  const noSlot = code === 'route_unavailable'
    || (typeof code !== 'string' || !code) && /\bno slot available\b/i.test(reason);
  const localContextFailure = !code || code === 'internal'
    ? /^Automatic context compaction failed locally before provider dispatch\b/.test(reason)
    : code === 'context_compaction_failed';
  if (localContextFailure) return {
    title: t('swarm.failure.contextPreparation'),
    detail: reason,
    recovery: t(modifiedFiles > 0
      ? 'swarm.failure.contextPreparationWithEdits' : 'swarm.failure.contextPreparationRecovery'),
  };
  return {
    title: t(noSlot ? 'swarm.failure.noSlot' : 'swarm.failure.generic'),
    detail: reason || t('swarm.phase.failed'),
    recovery: t(noSlot
      ? (modifiedFiles > 0 ? 'swarm.failure.noSlotWithEdits' : 'swarm.failure.noSlotRecovery')
      : (modifiedFiles > 0 ? 'swarm.failure.caution' : 'swarm.failure.genericRecovery')),
  };
}

export interface SwarmFailureNotice { conversationId: string; title: string; detail: string; hint: string }
/** One controller lifetime, at most 256 run/agent keys. Snapshot rendering never alerts. */
export function createSwarmFailureNotices(t: SwarmTranslate, notify: (notice: SwarmFailureNotice) => void) {
  const seen = new Set<string>();
  return (conversationId: string, runId: string, frame: Row, agents: readonly Row[], previous?: Row) => {
    if (!frame.agentId || !['swarm_agent_complete', 'swarm_agent_error'].includes(frame.type)) return;
    const status = swarmOutcomeStatus({ ...frame, status: frame.status || (frame.type === 'swarm_agent_error' ? 'failed' : 'done') });
    if (status !== 'failed') return;
    const key = JSON.stringify([conversationId, runId, frame.agentId]);
    if (seen.has(key)) return;
    seen.add(key);
    if (seen.size > 256) seen.delete(seen.values().next().value!);
    if (previous && ['failed', 'done', 'cancelled'].includes(swarmOutcomeStatus(previous))) return;
    const counts = swarmOutcomeCounts(agents);
    const agent = agents.find(row => row.id === frame.agentId || row.agentId === frame.agentId);
    const edits = Number(frame.modifiedFiles ?? agent?.modifiedFiles ?? 0);
    const failure = swarmAgentFailureCard(frame.error || frame.content, frame.summary || agent?.preview,
      Number.isFinite(edits) ? edits : 0, t);
    notify({ conversationId, title: t('swarm.failure.title', { agent: String(frame.agentId).slice(0, 100) }),
      detail: `${failure.title}: ${failure.detail}`.slice(0, 500),
      hint: t('swarm.failure.remaining', { running: counts.running, waiting: counts.pending }) + ' ' + failure.recovery });
  };
}
