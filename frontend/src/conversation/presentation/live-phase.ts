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
