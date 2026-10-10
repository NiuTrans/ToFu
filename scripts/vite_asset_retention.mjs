#!/usr/bin/env node

/* Bounded retention planner for previously published Vite asset generations.
 *
 * A long-lived tab imports lazy feature chunks by content-hashed URL for the
 * document's whole lifetime, so pruning a generation the moment it leaves the
 * manifest surfaces later as the click-time feature-load failure. The build
 * therefore keeps every pinned (current and previous graph) asset plus a
 * bounded window of dropped generations, newest first, capped by age and by
 * bytes so the local static mirror budget still holds.
 */

export const VITE_ASSET_RETENTION_POLICY = Object.freeze({
  maxAgeMs: 7 * 24 * 60 * 60 * 1000,
  maxRetainedBytes: 48 * 1024 * 1024,
});

// The static mirror ships static/vite within a 64 MiB default budget and the
// current graph is ~8 MiB, so retained generations must stay well under it.
const MAX_AGE_LIMIT_MS = 30 * 24 * 60 * 60 * 1000;
const MAX_RETAINED_BYTES_LIMIT = 56 * 1024 * 1024;

function boundedInteger(raw, fallback, minimum, maximum) {
  const value = typeof raw === 'string' && raw.trim() !== ''
    ? Number.parseInt(raw, 10)
    : Number.NaN;
  if (!Number.isFinite(value)) return fallback;
  return Math.min(maximum, Math.max(minimum, value));
}

export function retentionPolicyFromEnvironment(env = process.env) {
  return Object.freeze({
    maxAgeMs: boundedInteger(
      env.TOFU_VITE_ASSET_RETENTION_MAX_AGE_MS,
      VITE_ASSET_RETENTION_POLICY.maxAgeMs,
      0,
      MAX_AGE_LIMIT_MS,
    ),
    maxRetainedBytes: boundedInteger(
      env.TOFU_VITE_ASSET_RETENTION_MAX_BYTES,
      VITE_ASSET_RETENTION_POLICY.maxRetainedBytes,
      0,
      MAX_RETAINED_BYTES_LIMIT,
    ),
  });
}

/* candidates: files outside the pinned set as { name, size, mtimeMs }.
 * Returns the deterministic list of names to delete: newest-first survivors
 * within the age window while cumulative bytes fit the budget; everything
 * else goes. mtimes in the future count as age zero. */
export function planRetentionDeletions(
  candidates,
  nowMs,
  policy = VITE_ASSET_RETENTION_POLICY,
) {
  const now = Number.isFinite(nowMs) ? nowMs : 0;
  const ordered = candidates.slice().sort((a, b) => (
    (b.mtimeMs - a.mtimeMs)
    || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0)
  ));
  const deletions = [];
  let retainedBytes = 0;
  for (const candidate of ordered) {
    const size = Number.isFinite(candidate.size) && candidate.size > 0
      ? candidate.size
      : 0;
    const mtimeMs = Number.isFinite(candidate.mtimeMs) ? candidate.mtimeMs : 0;
    const ageMs = Math.max(0, now - mtimeMs);
    if (ageMs <= policy.maxAgeMs
        && retainedBytes + size <= policy.maxRetainedBytes) {
      retainedBytes += size;
    } else {
      deletions.push(candidate.name);
    }
  }
  return deletions;
}
