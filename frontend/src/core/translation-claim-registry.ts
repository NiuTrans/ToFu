/**
 * Responsibility: suppress duplicate per-Turn translation starts with a
 * page-lifetime, TTL-bound and capacity-bound claim registry.
 * Entry points: createTranslationClaimRegistry and TRANSLATION_CLAIMS.
 * Dependencies: clock and rejection reporting are injected.
 */

export const TRANSLATION_CLAIM_TTL_MS = 180_000;
export const MAX_TRANSLATION_CLAIMS = 256;

export interface TranslationClaimRejection {
  readonly reason: 'duplicate' | 'capacity';
  readonly key: string;
  readonly ageMs?: number;
}

export interface TranslationClaimRegistryOptions {
  readonly now?: () => number;
  readonly ttlMs?: number;
  readonly capacity?: number;
  readonly onRejected?: (rejection: TranslationClaimRejection) => void;
}

export interface TranslationClaimRegistry {
  claim(conversationId: unknown, turnId: unknown): boolean;
  release(conversationId: unknown, turnId: unknown): void;
  isClaimed(conversationId: unknown, turnId: unknown): boolean;
  activeCount(): number;
  clear(): void;
}

function positiveInteger(value: number | undefined, fallback: number): number {
  return Number.isFinite(value) && Number(value) >= 1
    ? Math.floor(Number(value)) : fallback;
}

function claimKey(conversationId: unknown, turnId: unknown): string {
  return conversationId && turnId
    ? `${String(conversationId)}::${String(turnId)}` : '';
}

function reportRejectedClaim(rejection: TranslationClaimRejection): void {
  if (rejection.reason === 'capacity') {
    console.warn(
      `[TranslateGuard] ${rejection.key} rejected: claim capacity reached`,
    );
    return;
  }
  console.debug(
    `[TranslateGuard] ${rejection.key} already claimed `
      + `${Math.round((rejection.ageMs ?? 0) / 1000)}s ago — standing down`,
  );
}

export function createTranslationClaimRegistry(
  options: TranslationClaimRegistryOptions = {},
): Readonly<TranslationClaimRegistry> {
  const now = options.now ?? Date.now;
  const ttlMs = positiveInteger(options.ttlMs, TRANSLATION_CLAIM_TTL_MS);
  const capacity = positiveInteger(options.capacity, MAX_TRANSLATION_CLAIMS);
  const onRejected = options.onRejected ?? reportRejectedClaim;
  const claimedAtByKey = new Map<string, number>();

  const pruneExpired = (currentTime: number): void => {
    for (const [key, claimedAt] of claimedAtByKey) {
      if (currentTime - claimedAt >= ttlMs) claimedAtByKey.delete(key);
    }
  };

  const claim = (conversationId: unknown, turnId: unknown): boolean => {
    const key = claimKey(conversationId, turnId);
    if (!key) return true;
    const currentTime = now();
    const priorClaimedAt = claimedAtByKey.get(key);
    if (priorClaimedAt !== undefined) {
      const ageMs = currentTime - priorClaimedAt;
      if (ageMs < ttlMs) {
        onRejected({ reason: 'duplicate', key, ageMs });
        return false;
      }
      claimedAtByKey.delete(key);
    }
    pruneExpired(currentTime);
    if (claimedAtByKey.size >= capacity) {
      onRejected({ reason: 'capacity', key });
      return false;
    }
    claimedAtByKey.set(key, currentTime);
    return true;
  };

  const release = (conversationId: unknown, turnId: unknown): void => {
    const key = claimKey(conversationId, turnId);
    if (key) claimedAtByKey.delete(key);
  };

  const isClaimed = (conversationId: unknown, turnId: unknown): boolean => {
    const key = claimKey(conversationId, turnId);
    if (!key) return false;
    const claimedAt = claimedAtByKey.get(key);
    if (claimedAt === undefined) return false;
    if (now() - claimedAt < ttlMs) return true;
    claimedAtByKey.delete(key);
    return false;
  };

  return Object.freeze({
    claim,
    release,
    isClaimed,
    activeCount(): number {
      pruneExpired(now());
      return claimedAtByKey.size;
    },
    clear(): void {
      claimedAtByKey.clear();
    },
  });
}

export const TRANSLATION_CLAIMS = createTranslationClaimRegistry();
