/** Pure presentation over the backend-authored live phase in TurnState. */

type UnknownRecord = Record<string, unknown>;

export interface ConversationRateLimitPresentation {
  model: string;
  attempt: number;
}

/** Return sidebar-visible rate-limit detail, or null for every other wait. */
export function presentConversationRateLimit(
  phase: unknown,
): ConversationRateLimitPresentation | null {
  if (!phase || typeof phase !== 'object' || Array.isArray(phase)) return null;
  const value = phase as UnknownRecord;
  if (value.phase !== 'retrying') return null;
  const detailArgs = value.detailArgs && typeof value.detailArgs === 'object'
      && !Array.isArray(value.detailArgs)
    ? value.detailArgs as UnknownRecord : {};
  // Subscription/billing quota exhaustion is NOT a transient rate limit: it
  // won't recover in seconds, so it must not light the "限流中/rate-limited"
  // chip. It renders through its own detailKey/reasonKey copy instead.
  const quotaExhausted = value.detailKey === 'stream.phase.subscriptionQuotaReached'
    || value.detailKey === 'stream.phase.waitingQuotaReset'
    || detailArgs.reasonKey === 'stream.retryReason.subscriptionQuotaReached';
  if (quotaExhausted) return null;
  const rateLimited = value.detailKey === 'stream.phase.retryRateLimited'
    || detailArgs.reasonKey === 'stream.retryReason.waitingForModel'
    || detailArgs.reasonKey === 'stream.retryReason.rateLimited';
  if (!rateLimited) return null;
  const attempt = Number(value.attempt || 0);
  return {
    model: typeof detailArgs.model === 'string' ? detailArgs.model : '',
    attempt: Number.isFinite(attempt) ? attempt : 0,
  };
}

import type { TransientTurnPresentation } from '../domain/transient-turn';

export type LivePhaseText = (
  key: string, fallback: string, args?: Readonly<Record<string, string | number>>,
) => string;

/** One label policy for normal attempts, virtual users and Goal root summaries.
 * Caller owns DOM and Goal role context; storage warnings always stay local. */
export function presentLivePhaseLabel(value: TransientTurnPresentation, text: LivePhaseText): string {
  const phase = value.phase || 'working';
  const phaseFallback = phase === 'executor_preparing'
    ? "Task accepted; binding it to this server's task scheduler…"
    : phase === 'executor_queued'
      ? "Waiting in this server's AI task queue; syncing queue position and available slots (not model/API quota)…"
      : phase === 'worker_starting' ? 'Server execution slot acquired; starting the task…' : '';
  const localizedDetail = value.detailKey
    ? text(value.detailKey, value.detail || value.label || phaseFallback, value.detailArgs) : '';
  let label = localizedDetail || value.detail || value.label;
  if (phase === 'warming') label = text('autopilot.warming', 'Autopilot is starting…');
  else if (phase === 'thinking_active') label = text('stream.thinking.active', 'Thinking…');
  else if (phase === 'llm_thinking' && !label) label = text('stream.phase.generatingResponse', 'Generating response…');
  else if (phase === 'tool_exec' && value.tools?.length) label = value.detail || value.tools.join(', ');
  else if (phase === 'retrying' && !localizedDetail && !value.detail) label = text('stream.phase.retrying', 'Retrying…');
  else if (phase === 'storage_wedged') label = text('stream.phase.storageWedged', 'Storage write stalled — retrying…');
  else if (phase === 'waiting' && !localizedDetail && !value.detail) label = text('stream.phase.waitingWorkerStatus', 'Waiting for an execution status update…');
  else if (phase === 'responding' && !value.detail) label = text('autopilot.warming', 'Autopilot is responding…');
  if (!label && phase === 'waiting_model') label = text('stream.phase.waitingModel', 'Sent to the model, waiting for it to start replying…');
  const route = value.modelRoute;
  if (route?.selectedModel && route.resolvedModel && route.selectedModel !== route.resolvedModel
      && value.detailKey !== 'stream.phase.modelRouted') {
    const routeLabel = text('stream.phase.modelRouted', 'Model routing: {from} → {to} ({role}, {tier})', {
      from: route.selectedModel, to: route.resolvedModel, role: route.role, tier: route.tier,
    });
    label = label ? `${routeLabel} · ${label}` : routeLabel;
  }
  return label;
}

/** Choose a joined Goal worker phase only for a waiting root, not warnings. */
export function liveStatusPhase(value: TransientTurnPresentation): TransientTurnPresentation {
  return value.goalProgress?.rootSummary && value.phase === 'waiting'
    ? value.goalProgress.currentPhase ?? value : value;
}
