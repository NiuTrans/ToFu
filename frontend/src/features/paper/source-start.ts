/**
 * Shared client contract for content-addressed Paper task starts.
 * Owns canonical hash validation and the one deterministic source-miss retry
 * classifier used by Report and Q&A; task lifecycle and source ceilings stay
 * with their feature owners.
 */

type LooseObject = Record<string, unknown>;

export function canonicalPaperHash(value: unknown): string {
  const candidate = typeof value === 'string' ? value.trim() : '';
  return /^[a-f0-9]{8,64}$/.test(candidate) ? candidate : '';
}

export function paperSourceRetryRequired(error: unknown): boolean {
  const offered = error && typeof error === 'object'
    ? error as LooseObject
    : null;
  if (Number(offered?.status || 0) !== 400) return false;
  if (offered?.code === 'paper_source_required') return true;
  const body = offered?.body && typeof offered.body === 'object'
    ? offered.body as LooseObject
    : null;
  const message = String(
    offered?.message || body?.error || body?.message || '',
  );
  return message.includes('No paper_text provided');
}
