/**
 * Authoritative conversation catalog load orchestration.
 *
 * Responsibility: own conditional requests, retry/single-flight state,
 * applied-snapshot validation, and a bounded best-effort metadata-cache write.
 * Entry point: `createConversationCatalogLoader`. Dependencies: injected
 * transport, projection, cache, clock, and diagnostic ports; no DOM, browser
 * globals, transcript state, or owner identity.
 */

type UnknownRecord = Record<string, unknown>;

export interface ConversationCatalogResponse {
  readonly status: number;
  readonly ok: boolean;
  readonly headers?: {
    get(name: string): string | null;
  };
  json(): Promise<unknown>;
}

export interface ConversationCatalogRequest {
  readonly headers: Readonly<Record<string, string>>;
  readonly timeoutMs: number;
  readonly signal: AbortSignal;
}

export interface ConversationCatalogLoaderPorts {
  requestCatalog(
    request: ConversationCatalogRequest,
  ): Promise<ConversationCatalogResponse | null | undefined>;
  applyAuthoritativeRows(
    rows: readonly unknown[],
    totalCount: number | null,
  ): readonly string[];
  hasEveryAppliedRow(conversationIds: ReadonlySet<string>): boolean;
  writeCache(rows: readonly unknown[]): Promise<unknown>;
  wait?(milliseconds: number, signal: AbortSignal): Promise<void>;
  setTimeout?(callback: () => void, milliseconds: number): number;
  clearTimeout?(timer: number): void;
  warn(message: string): void;
}

export interface ConversationCatalogLoader {
  load(): Promise<void>;
  serverLoadOk(): boolean;
  serverTotalCount(): number | null;
  destroy(): void;
}

export const CONVERSATION_CATALOG_CACHE_WRITE_BUDGET = Object.freeze({
  maximumInFlight: 1,
  maximumPending: 1,
});

interface FetchedConversationCatalog {
  readonly rows: readonly unknown[];
  readonly etag: string | null;
  readonly totalCount: number | null;
}

const REQUEST_TIMEOUT_MS = 12_000;
const MAXIMUM_ATTEMPTS = 3;
const MAXIMUM_RETRY_DELAY_MS = 5_000;

function record(value: unknown): UnknownRecord | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as UnknownRecord
    : null;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function responseHeader(
  response: ConversationCatalogResponse,
  name: string,
): string | null {
  try {
    return response.headers?.get(name) ?? null;
  } catch {
    return null;
  }
}

function awaitWithAbort<T>(pending: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const onAbort = (): void => {
      signal.removeEventListener('abort', onAbort);
      reject(signal.reason ?? new Error('Conversation catalog request aborted.'));
    };
    if (signal.aborted) {
      // Observe a noncooperative port's later rejection as well.
      void pending.catch(() => {});
      onAbort();
      return;
    }
    signal.addEventListener('abort', onAbort, { once: true });
    void pending.then((value) => {
      signal.removeEventListener('abort', onAbort);
      resolve(value);
    }, (error: unknown) => {
      signal.removeEventListener('abort', onAbort);
      reject(error);
    });
  });
}

export function createConversationCatalogLoader(
  ports: ConversationCatalogLoaderPorts,
): ConversationCatalogLoader {
  let disposed = false;
  const lifetime = new AbortController();
  const scheduleTimeout = ports.setTimeout ?? globalThis.setTimeout.bind(globalThis);
  const cancelTimeout = ports.clearTimeout ?? globalThis.clearTimeout.bind(globalThis);
  let lastServerLoadOk = false;
  let serverTotalCount: number | null = null;
  let catalogEtag: string | null = null;
  let appliedSnapshotIds: ReadonlySet<string> | null = null;
  let loadFlight: Promise<void> | null = null;
  let cacheWriteFlight: Promise<void> | null = null;
  let pendingCacheRows: readonly unknown[] | null = null;

  const warn = (message: string): void => {
    try {
      ports.warn(message);
    } catch {
      // Diagnostics are never allowed to become a second load failure.
    }
  };

  const hasCompleteAppliedSnapshot = (): boolean => (
    appliedSnapshotIds !== null
      && ports.hasEveryAppliedRow(appliedSnapshotIds)
  );

  const readResponse = async (headers: Record<string, string>): Promise<{
    response: ConversationCatalogResponse | null | undefined;
    payload: unknown;
  }> => {
    const controller = new AbortController();
    const onDispose = (): void => controller.abort(lifetime.signal.reason);
    lifetime.signal.addEventListener('abort', onDispose, { once: true });
    const timer = scheduleTimeout(() => controller.abort(
      new Error('Conversation catalog response timed out.'),
    ), REQUEST_TIMEOUT_MS);
    try {
      if (lifetime.signal.aborted) onDispose();
      if (controller.signal.aborted) throw controller.signal.reason;
      const response = await awaitWithAbort(ports.requestCatalog({
        headers, timeoutMs: REQUEST_TIMEOUT_MS, signal: controller.signal,
      }), controller.signal);
      const payload = response?.ok && response.status !== 304
        ? await awaitWithAbort(response.json(), controller.signal) : null;
      return { response, payload };
    } finally {
      cancelTimeout(timer);
      lifetime.signal.removeEventListener('abort', onDispose);
      // Release an unread failure body too; there is no downstream body owner.
      controller.abort();
    }
  };

  const waitForRetry = async (milliseconds: number): Promise<void> => {
    if (ports.wait) {
      await awaitWithAbort(ports.wait(milliseconds, lifetime.signal), lifetime.signal);
      return;
    }
    let timer = 0;
    try {
      await awaitWithAbort(new Promise<void>((resolve) => {
        timer = scheduleTimeout(resolve, milliseconds);
      }), lifetime.signal);
    } finally {
      cancelTimeout(timer);
    }
  };

  const fetchRows = async (
    forceUnconditional = false,
    requestBudget = { remaining: MAXIMUM_ATTEMPTS },
  ): Promise<FetchedConversationCatalog | null> => {
    lastServerLoadOk = false;
    const headers: Record<string, string> = {};
    if (catalogEtag && !forceUnconditional) {
      headers['If-None-Match'] = catalogEtag;
    }

    let response: ConversationCatalogResponse | null | undefined;
    let wirePayload: unknown = null;
    for (let attempt = 0; requestBudget.remaining > 0; attempt += 1) {
      if (disposed) return null;
      requestBudget.remaining -= 1;
      ({ response, payload: wirePayload } = await readResponse(headers));
      if (disposed) return null;
      if (response?.status !== 503 || requestBudget.remaining === 0) break;
      const hint = responseHeader(response, 'Retry-After');
      const seconds = hint !== null && /^[0-9]{1,16}$/.test(hint)
        ? Number(hint) : attempt + 1;
      await waitForRetry(Math.min(MAXIMUM_RETRY_DELAY_MS, seconds * 1_000));
    }

    if (!response) throw new Error('Conversation catalog is unreachable.');
    if (response.status === 304) {
      const snapshotIsComplete = hasCompleteAppliedSnapshot();
      if (!snapshotIsComplete && !forceUnconditional) {
        return fetchRows(true, requestBudget);
      }
      if (!snapshotIsComplete) {
        throw new Error(
          'Conversation catalog returned 304 without an applied server snapshot.',
        );
      }
      lastServerLoadOk = true;
      return null;
    }
    if (!response.ok) {
      throw new Error(
        `Conversation catalog failed with HTTP ${response.status}.`,
      );
    }

    const payload = record(wirePayload);
    if (!payload || !Array.isArray(payload.items)) {
      throw new Error('Conversation catalog returned an invalid response.');
    }
    // Completeness belongs to this response; an older total cannot prove
    // that missing rows in a later page were deleted.
    let fetchedTotalCount: number | null = null;
    const totalHeader = responseHeader(response, 'X-Total-Count');
    if (totalHeader !== null && /^[0-9]{1,16}$/.test(totalHeader)) {
      const parsed = Number(totalHeader);
      if (Number.isSafeInteger(parsed) && parsed >= 0) fetchedTotalCount = parsed;
    }
    return {
      rows: payload.items,
      etag: responseHeader(response, 'ETag'),
      totalCount: fetchedTotalCount,
    };
  };

  const scheduleCacheWrite = (rows: readonly unknown[]): void => {
    if (disposed) return;
    pendingCacheRows = rows;
    if (cacheWriteFlight) return;

    const drainLatest = async (): Promise<void> => {
      while (!disposed && pendingCacheRows) {
        const rowsToWrite = pendingCacheRows;
        pendingCacheRows = null;
        try {
          await ports.writeCache(rowsToWrite);
        } catch (error) {
          warn(`cache write failed: ${errorMessage(error)}`);
        }
      }
      if (disposed) pendingCacheRows = null;
    };

    /* Defer all cache work until after applyAuthoritativeRows has returned,
     * which includes the retained adapter's user-visible render. */
    cacheWriteFlight = Promise.resolve()
      .then(drainLatest)
      .then(() => {
        cacheWriteFlight = null;
        if (pendingCacheRows) scheduleCacheWrite(pendingCacheRows);
      });
  };

  const runLoad = async (): Promise<void> => {
    try {
      const fetched = await fetchRows();
      if (disposed || fetched === null) return;
      const appliedIds = ports.applyAuthoritativeRows(
        fetched.rows, fetched.totalCount,
      );
      appliedSnapshotIds = new Set(
        appliedIds.filter((id) => typeof id === 'string' && id.length > 0),
      );
      /* The validator describes the applied in-memory snapshot, not merely a
       * response we managed to decode. Commit it with the snapshot IDs only
       * after merge/render succeeds. Otherwise the next wake would send the
       * unearned ETag, receive 304, then need a second unconditional full-page
       * request to recover the snapshot that never landed. */
      catalogEtag = fetched.etag;
      serverTotalCount = fetched.totalCount;
      lastServerLoadOk = true;
      scheduleCacheWrite(fetched.rows);
    } catch (error) {
      lastServerLoadOk = false;
      if (!disposed) warn(errorMessage(error));
    }
  };

  const load = (): Promise<void> => {
    if (disposed) return Promise.resolve();
    if (loadFlight) return loadFlight;
    const currentFlight = runLoad().finally(() => {
      if (loadFlight === currentFlight) loadFlight = null;
    });
    loadFlight = currentFlight;
    return currentFlight;
  };

  const destroy = (): void => {
    if (disposed) return;
    disposed = true;
    lifetime.abort(new Error('Conversation catalog loader disposed.'));
    pendingCacheRows = null;
    appliedSnapshotIds = null;
  };

  return Object.freeze({
    load,
    serverLoadOk: () => lastServerLoadOk,
    serverTotalCount: () => serverTotalCount,
    destroy,
  });
}
