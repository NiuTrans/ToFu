/**
 * Account-scoped Codex earned-reset notification owner.
 *
 * The backend is authoritative for availability.  This controller only accepts
 * a fresh `state=available` projection with a stable opaque notification key;
 * unknown, stale, malformed and unauthenticated states never become a prompt.
 * Seen keys are bounded in localStorage so reloads, tabs and account switches
 * do not turn one entitlement into repeated noise.
 */

export const CODEX_RESET_NOTICE_STORAGE_KEY = 'tofu_codex_reset_notice_v1';
export const CODEX_RESET_NOTICE_INTERVAL_MS = 30 * 60 * 1000;
export const CODEX_RESET_NOTICE_REFRESH_RETRY_MS = 2500;
export const CODEX_RESET_NOTICE_PUSH_FALLBACK_MS = 15 * 1000;
export const CODEX_RESET_NOTICE_MAX_REFRESH_RETRIES = 6;
export const CODEX_RESET_NOTICE_MAX_SEEN_KEYS = 16;
export const CODEX_RESET_NOTICE_PUSH_CHANNEL = 'oauth';
export const CODEX_RESET_NOTICE_PUSH_TASK_ID = 'codex-reset';
export const CODEX_RESET_NOTICE_PUSH_EVENT_TYPE = 'codex.reset_offer.updated';

interface ResetOfferRecord {
  state?: unknown;
  available_count?: unknown;
  notification_key?: unknown;
  captured_at?: unknown;
  stale?: unknown;
  refreshing?: unknown;
  retry_after_seconds?: unknown;
}

export interface CodexResetOffer {
  state: 'available' | 'none' | 'unknown';
  availableCount: number | null;
  notificationKey: string;
  capturedAt: number | null;
  stale: boolean;
  refreshing: boolean;
  retryAfterSeconds: number;
}

export interface SubscriptionResetNotice {
  title: string;
  detail: string;
  hint: string;
  onClick(): void;
}

export interface SubscriptionResetNoticeDependencies {
  readStatus(): Promise<unknown>;
  notify(notice: SubscriptionResetNotice): boolean | void;
  translate?(key: string, values?: Record<string, unknown>): string;
  openSettings?(): unknown;
  switchSettingsTab?(tabId: string): unknown;
  storage?: Pick<Storage, 'getItem' | 'setItem'>;
  now?(): number;
  setTimeout?(callback: () => void, delayMs: number): unknown;
  clearTimeout?(handle: unknown): void;
  setInterval?(callback: () => void, delayMs: number): unknown;
  clearInterval?(handle: unknown): void;
  isVisible?(): boolean;
  subscribeOfferUpdates?(
    listener: (frame: unknown) => void,
  ): (() => void) | null;
}

export interface SubscriptionResetNoticeController {
  readonly source: 'typed';
  start(): void;
  checkNow(): Promise<void>;
  checkIfDue(): Promise<void>;
  destroy(): void;
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function integer(value: unknown, minimum = 0): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= minimum
    ? value
    : null;
}

function notificationKey(value: unknown): string {
  const key = typeof value === 'string' ? value.trim() : '';
  return /^[0-9a-f]{24}$/.test(key) ? key : '';
}

export function normalizeCodexResetOffer(value: unknown): CodexResetOffer | null {
  const raw = record(value) as ResetOfferRecord | null;
  if (!raw) return null;
  const state = raw.state;
  if (state !== 'available' && state !== 'none' && state !== 'unknown') return null;
  const stale = raw.stale === true;
  const refreshing = raw.refreshing === true;
  const capturedAt = raw.captured_at === null
    ? null
    : integer(raw.captured_at, 1);
  const retryAfterSeconds = integer(raw.retry_after_seconds, 0) ?? 0;

  if (state === 'available') {
    const availableCount = integer(raw.available_count, 1);
    const key = notificationKey(raw.notification_key);
    if (availableCount === null || !key) return null;
    return {
      state, availableCount, notificationKey: key, capturedAt,
      stale, refreshing, retryAfterSeconds,
    };
  }
  if (state === 'none') {
    if (raw.available_count !== 0) return null;
    return {
      state, availableCount: 0, notificationKey: '', capturedAt,
      stale, refreshing, retryAfterSeconds,
    };
  }
  if (raw.available_count !== null && raw.available_count !== undefined) return null;
  return {
    state, availableCount: null, notificationKey: '', capturedAt,
    stale, refreshing, retryAfterSeconds,
  };
}

export function extractAuthenticatedCodexResetOffer(value: unknown): CodexResetOffer | null {
  const root = record(value);
  const codex = record(root?.codex);
  if (!codex || codex.authenticated !== true) return null;
  return normalizeCodexResetOffer(codex.reset_offer);
}

export function extractCodexResetOfferPush(value: unknown): CodexResetOffer | null {
  const frame = record(value);
  if (frame?.type !== CODEX_RESET_NOTICE_PUSH_EVENT_TYPE
      || frame.provider !== 'codex') return null;
  const offer = normalizeCodexResetOffer(frame.reset_offer);
  // This event is a completion receipt. A busy projection is either from an
  // incompatible producer or malformed input and must not extend polling.
  return offer?.refreshing ? null : offer;
}

function interpolate(template: string, values?: Record<string, unknown>): string {
  if (!values) return template;
  return template.replace(/\{([A-Za-z0-9_]+)\}/g, (token, name: string) => (
    Object.prototype.hasOwnProperty.call(values, name)
      ? String(values[name] ?? '')
      : token
  ));
}

function translate(
  dependencies: SubscriptionResetNoticeDependencies,
  key: string,
  fallback: string,
  values?: Record<string, unknown>,
): string {
  const translated = dependencies.translate?.(key, values);
  return translated && translated !== key
    ? translated
    : interpolate(fallback, values);
}

function readSeenKeys(
  storage: SubscriptionResetNoticeDependencies['storage'],
): string[] {
  if (!storage) return [];
  try {
    const parsed: unknown = JSON.parse(
      storage.getItem(CODEX_RESET_NOTICE_STORAGE_KEY) || '[]',
    );
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((value): value is string => notificationKey(value) !== '')
      .slice(-CODEX_RESET_NOTICE_MAX_SEEN_KEYS);
  } catch {
    return [];
  }
}

function writeSeenKeys(
  storage: SubscriptionResetNoticeDependencies['storage'],
  keys: readonly string[],
): void {
  if (!storage) return;
  try {
    storage.setItem(
      CODEX_RESET_NOTICE_STORAGE_KEY,
      JSON.stringify(keys.slice(-CODEX_RESET_NOTICE_MAX_SEEN_KEYS)),
    );
  } catch {
    // Sandboxed/private WebViews may reject storage; in-memory dedupe remains.
  }
}

function openResetSettings(dependencies: SubscriptionResetNoticeDependencies): void {
  try {
    const opened = dependencies.openSettings?.();
    if (opened && typeof (opened as PromiseLike<unknown>).then === 'function') {
      void Promise.resolve(opened).then(() => {
        dependencies.switchSettingsTab?.('oauth');
      });
    } else {
      dependencies.switchSettingsTab?.('oauth');
    }
  } catch {
    // The persistent Settings callout remains discoverable if navigation fails.
  }
}

function noticeFor(
  offer: CodexResetOffer,
  dependencies: SubscriptionResetNoticeDependencies,
): SubscriptionResetNotice {
  const count = offer.availableCount ?? 0;
  return {
    title: translate(
      dependencies,
      'settings.oauthResetNoticeTitle',
      'Codex usage reset available',
    ),
    detail: count === 1
      ? translate(
        dependencies,
        'settings.oauthResetNoticeDetailOne',
        'OpenAI reports one earned usage-limit reset for this account.',
      )
      : translate(
        dependencies,
        'settings.oauthResetNoticeDetailMany',
        'OpenAI reports {count} earned usage-limit resets for this account.',
        { count },
      ),
    hint: translate(
      dependencies,
      'settings.oauthResetNoticeHint',
      'Review it in Subscription settings. Tofu never redeems it automatically.',
    ),
    onClick: () => openResetSettings(dependencies),
  };
}

export function createSubscriptionResetNoticeController(
  dependencies: SubscriptionResetNoticeDependencies,
): SubscriptionResetNoticeController {
  const now = dependencies.now ?? Date.now;
  const scheduleTimeout = dependencies.setTimeout
    ?? ((callback, delayMs) => globalThis.setTimeout(callback, delayMs));
  const cancelTimeout = dependencies.clearTimeout
    ?? ((handle) => globalThis.clearTimeout(handle as number));
  const scheduleInterval = dependencies.setInterval
    ?? ((callback, delayMs) => globalThis.setInterval(callback, delayMs));
  const cancelInterval = dependencies.clearInterval
    ?? ((handle) => globalThis.clearInterval(handle as number));

  let destroyed = false;
  let started = false;
  let checking = false;
  let lastCheckedAt = 0;
  let refreshRetryCount = 0;
  let failureRetryUsed = false;
  let retryTimer: unknown = null;
  let intervalTimer: unknown = null;
  let releaseOfferUpdates: (() => void) | null = null;
  let offerUpdatesSubscribed = false;
  let pushedOfferVersion = 0;
  let pendingVisibleOffer: CodexResetOffer | null = null;
  const inMemorySeen = new Set(readSeenKeys(dependencies.storage));

  function rememberSeen(key: string): void {
    for (const saved of readSeenKeys(dependencies.storage)) inMemorySeen.add(saved);
    inMemorySeen.add(key);
    const bounded = [...inMemorySeen].slice(-CODEX_RESET_NOTICE_MAX_SEEN_KEYS);
    inMemorySeen.clear();
    for (const saved of bounded) inMemorySeen.add(saved);
    writeSeenKeys(dependencies.storage, bounded);
  }

  function alreadySeen(key: string): boolean {
    for (const saved of readSeenKeys(dependencies.storage)) inMemorySeen.add(saved);
    return inMemorySeen.has(key);
  }

  function scheduleRetry(delayMs: number): void {
    if (destroyed || retryTimer !== null) return;
    retryTimer = scheduleTimeout(() => {
      retryTimer = null;
      void checkNow();
    }, Math.max(250, delayMs));
  }

  function cancelRetry(): void {
    if (retryTimer !== null) cancelTimeout(retryTimer);
    retryTimer = null;
  }

  function processOffer(offer: CodexResetOffer, refreshDelayMs: number): void {
    if (offer.refreshing) {
      if (refreshRetryCount < CODEX_RESET_NOTICE_MAX_REFRESH_RETRIES) {
        refreshRetryCount += 1;
        scheduleRetry(refreshDelayMs);
      }
      return;
    }
    refreshRetryCount = 0;
    if (offer.stale || offer.state === 'unknown') {
      if (!failureRetryUsed && offer.retryAfterSeconds > 0) {
        failureRetryUsed = true;
        scheduleRetry(Math.min(offer.retryAfterSeconds * 1000, 5 * 60 * 1000));
      }
      return;
    }
    failureRetryUsed = false;
    if (offer.state !== 'available' || !offer.notificationKey) return;
    if (alreadySeen(offer.notificationKey)) return;
    const notice = noticeFor(offer, dependencies);
    if (dependencies.notify(notice) === false) return;
    rememberSeen(offer.notificationKey);
  }

  function acceptOfferUpdate(frame: unknown): void {
    if (destroyed) return;
    const offer = extractCodexResetOfferPush(frame);
    if (!offer) return;
    pushedOfferVersion += 1;
    lastCheckedAt = now();
    cancelRetry();
    if (dependencies.isVisible?.() === false) {
      // Preserve the old visible-page notification contract. Keep only the
      // latest bounded projection and consume it on visibility reconciliation
      // without issuing another status request.
      pendingVisibleOffer = offer;
      return;
    }
    processOffer(offer, CODEX_RESET_NOTICE_PUSH_FALLBACK_MS);
  }

  async function checkNow(): Promise<void> {
    if (destroyed || checking) return;
    if (dependencies.isVisible?.() === false) return;
    checking = true;
    lastCheckedAt = now();
    const pushVersionAtRequestStart = pushedOfferVersion;
    try {
      const status = await dependencies.readStatus();
      // A completion frame published while this request was in flight is the
      // newer observation. Never let an earlier HTTP projection re-arm a poll.
      if (pushedOfferVersion !== pushVersionAtRequestStart) return;
      const offer = extractAuthenticatedCodexResetOffer(status);
      if (!offer) {
        refreshRetryCount = 0;
        failureRetryUsed = false;
        return;
      }
      processOffer(
        offer,
        offerUpdatesSubscribed
          ? CODEX_RESET_NOTICE_PUSH_FALLBACK_MS
          : CODEX_RESET_NOTICE_REFRESH_RETRY_MS,
      );
    } catch (error: unknown) {
      console.warn('[CodexResetNotice] status check failed', error);
    } finally {
      checking = false;
    }
  }

  async function checkIfDue(): Promise<void> {
    if (pendingVisibleOffer && dependencies.isVisible?.() !== false) {
      const offer = pendingVisibleOffer;
      pendingVisibleOffer = null;
      lastCheckedAt = now();
      processOffer(offer, CODEX_RESET_NOTICE_PUSH_FALLBACK_MS);
      return;
    }
    if (now() - lastCheckedAt < CODEX_RESET_NOTICE_INTERVAL_MS) return;
    await checkNow();
  }

  function start(): void {
    if (started || destroyed) return;
    started = true;
    try {
      const release = dependencies.subscribeOfferUpdates?.(acceptOfferUpdate);
      if (typeof release === 'function') {
        releaseOfferUpdates = release;
        offerUpdatesSubscribed = true;
      }
    } catch (error: unknown) {
      console.warn('[CodexResetNotice] push subscription failed', error);
    }
    intervalTimer = scheduleInterval(() => {
      void checkIfDue();
    }, CODEX_RESET_NOTICE_INTERVAL_MS);
    void checkNow();
  }

  function destroy(): void {
    if (destroyed) return;
    destroyed = true;
    cancelRetry();
    if (intervalTimer !== null) cancelInterval(intervalTimer);
    if (releaseOfferUpdates) {
      try { releaseOfferUpdates(); }
      catch (error: unknown) {
        console.warn('[CodexResetNotice] push unsubscribe failed', error);
      }
    }
    releaseOfferUpdates = null;
    offerUpdatesSubscribed = false;
    pendingVisibleOffer = null;
    intervalTimer = null;
  }

  return Object.freeze({
    source: 'typed' as const,
    start,
    checkNow,
    checkIfDue,
    destroy,
  });
}
