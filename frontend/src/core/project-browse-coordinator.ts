/**
 * Responsibility: bound, cache, cancel, and validate project-folder reads.
 * Entry point: createProjectBrowseCoordinator.
 * Dependencies: a caller-supplied session-storage resolver and browse request.
 */

export interface ProjectBrowseDirectory {
  path: string;
  name: string;
  hasCode: boolean;
  hidden: boolean;
  itemCount: number;
  detailsDeferred: boolean;
}

export interface ProjectBrowseData {
  path: string;
  dirs: ProjectBrowseDirectory[];
  parent: string | null;
  filesCount: number;
  truncated: boolean;
}

export type ProjectBrowseOutcome =
  | { kind: 'success'; data: ProjectBrowseData }
  | { kind: 'failed'; message: string }
  | { kind: 'cancelled' };

export interface ProjectBrowseLoad {
  cached: ProjectBrowseData | null;
  completion: Promise<ProjectBrowseOutcome>;
}

export interface ProjectBrowseCoordinator {
  load(
    path: string,
    showHidden: boolean,
    request: (signal: AbortSignal | undefined) => Promise<unknown>,
  ): ProjectBrowseLoad;
  cancel(): void;
  invalidate(path: string): void;
}

type StoragePort = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;
type CacheEntry = { savedAt: number; data: ProjectBrowseData; bytes: number };

const STORAGE_KEY = 'tofu_project_browse_cache_v1';
const CACHE_TTL_MS = 5 * 60 * 1000;
const CACHE_MAX_ENTRIES = 16;
const CACHE_MAX_BYTES = 256 * 1024;
const CACHE_MAX_ENTRY_BYTES = 128 * 1024;

const isRecord = (value: unknown): value is Record<string, unknown> => (
  value !== null && typeof value === 'object' && !Array.isArray(value)
);

const storageBytes = (value: string): number => value.length * 3;
const cacheKey = (path: string, showHidden: boolean): string => (
  `${showHidden ? 'hidden' : 'visible'}:${path || '~'}`
);

const normalizeBrowseData = (value: unknown): ProjectBrowseData | null => {
  if (!isRecord(value) || typeof value.path !== 'string'
      || value.path.length > 4096 || !Array.isArray(value.dirs)) {
    return null;
  }
  const dirs: ProjectBrowseDirectory[] = [];
  for (const candidate of value.dirs) {
    if (!isRecord(candidate)) continue;
    const path = typeof candidate.path === 'string' ? candidate.path : '';
    const name = typeof candidate.name === 'string' ? candidate.name : '';
    if (!path || !name || path.length > 4096 || name.length > 1024) continue;
    dirs.push({
      path,
      name,
      hasCode: Boolean(candidate.hasCode),
      hidden: Boolean(candidate.hidden),
      itemCount: Math.max(0, Number(candidate.itemCount) || 0),
      detailsDeferred: Boolean(candidate.detailsDeferred),
    });
  }
  const parent = value.parent == null ? null : String(value.parent);
  if (parent !== null && parent.length > 4096) return null;
  return {
    path: value.path,
    dirs,
    parent,
    filesCount: Math.max(0, Number(value.filesCount) || 0),
    truncated: Boolean(value.truncated),
  };
};

const failureMessage = (value: unknown): string => {
  if (isRecord(value) && value.error != null) return String(value.error);
  if (value instanceof Error && value.message) return value.message;
  return value == null ? 'Browse failed' : String(value);
};

const isAbortFailure = (value: unknown): boolean => (
  isRecord(value) && (value.name === 'AbortError' || value.code === 'aborted')
);

export const createProjectBrowseCoordinator = (
  resolveStorage: () => StoragePort | null,
  now: () => number = Date.now,
): ProjectBrowseCoordinator => {
  const cache = new Map<string, CacheEntry>();
  let cacheLoaded = false;
  let cacheBytes = 0;
  let requestSequence = 0;
  let activeController: AbortController | null = null;

  const storage = (): StoragePort | null => {
    try {
      return resolveStorage();
    } catch {
      return null;
    }
  };

  const persist = (): void => {
    try {
      const target = storage();
      if (!target) return;
      const entries = Array.from(cache, ([key, entry]) => ({
        key,
        savedAt: entry.savedAt,
        data: entry.data,
      }));
      const payload = JSON.stringify({ version: 1, entries });
      if (storageBytes(payload) <= CACHE_MAX_BYTES) {
        target.setItem(STORAGE_KEY, payload);
      } else {
        target.removeItem(STORAGE_KEY);
      }
    } catch {
      // Cache persistence cannot affect authoritative folder browsing.
    }
  };

  const loadCache = (): void => {
    if (cacheLoaded) return;
    cacheLoaded = true;
    let document: unknown = null;
    try {
      const raw = storage()?.getItem(STORAGE_KEY);
      document = raw && storageBytes(raw) <= CACHE_MAX_BYTES
        ? JSON.parse(raw) : null;
    } catch {
      document = null;
    }
    const rows = isRecord(document) && document.version === 1
      && Array.isArray(document.entries) ? document.entries : [];
    const timestamp = now();
    for (const row of rows.slice(-CACHE_MAX_ENTRIES)) {
      if (!isRecord(row) || typeof row.key !== 'string') continue;
      const data = normalizeBrowseData(row.data);
      const savedAt = Number(row.savedAt) || 0;
      if (!data || savedAt > timestamp + 60_000
          || timestamp - savedAt > CACHE_TTL_MS) continue;
      const bytes = storageBytes(JSON.stringify(data));
      if (bytes > CACHE_MAX_ENTRY_BYTES
          || cacheBytes + bytes > CACHE_MAX_BYTES) continue;
      cache.set(row.key, { savedAt, data, bytes });
      cacheBytes += bytes;
    }
  };

  const getCached = (key: string): ProjectBrowseData | null => {
    loadCache();
    const entry = cache.get(key);
    if (!entry) return null;
    if (now() - entry.savedAt > CACHE_TTL_MS) {
      cache.delete(key);
      cacheBytes = Math.max(0, cacheBytes - entry.bytes);
      persist();
      return null;
    }
    cache.delete(key);
    cache.set(key, entry);
    return normalizeBrowseData(entry.data);
  };

  const setCached = (key: string, value: ProjectBrowseData): void => {
    loadCache();
    const data = normalizeBrowseData(value);
    if (!data) return;
    const bytes = storageBytes(JSON.stringify(data));
    if (bytes > CACHE_MAX_ENTRY_BYTES) return;
    const previous = cache.get(key);
    if (previous) {
      cache.delete(key);
      cacheBytes = Math.max(0, cacheBytes - previous.bytes);
    }
    while (cache.size >= CACHE_MAX_ENTRIES
        || cacheBytes + bytes > CACHE_MAX_BYTES) {
      const oldestKey = cache.keys().next().value as string | undefined;
      if (oldestKey === undefined) break;
      const oldest = cache.get(oldestKey);
      cache.delete(oldestKey);
      cacheBytes = Math.max(0, cacheBytes - (oldest?.bytes ?? 0));
    }
    if (cacheBytes + bytes > CACHE_MAX_BYTES) return;
    cache.set(key, { savedAt: now(), data, bytes });
    cacheBytes += bytes;
    persist();
  };

  const cancel = (): void => {
    requestSequence += 1;
    activeController?.abort();
    activeController = null;
  };

  const load = (
    path: string,
    showHidden: boolean,
    request: (signal: AbortSignal | undefined) => Promise<unknown>,
  ): ProjectBrowseLoad => {
    const requestedPath = String(path || '~');
    const key = cacheKey(requestedPath, showHidden);
    const cached = getCached(key);
    cancel();
    const sequence = requestSequence;
    const controller = typeof AbortController === 'undefined'
      ? null : new AbortController();
    activeController = controller;
    const completion = (async (): Promise<ProjectBrowseOutcome> => {
      try {
        const raw = await request(controller?.signal);
        if (sequence !== requestSequence || controller?.signal.aborted) {
          return { kind: 'cancelled' };
        }
        if (isRecord(raw) && raw.error != null) {
          return { kind: 'failed', message: failureMessage(raw) };
        }
        const data = normalizeBrowseData(raw);
        if (!data) return { kind: 'failed', message: 'Browse failed' };
        setCached(key, data);
        return { kind: 'success', data };
      } catch (error) {
        if (sequence !== requestSequence || controller?.signal.aborted
            || isAbortFailure(error)) return { kind: 'cancelled' };
        return { kind: 'failed', message: failureMessage(error) };
      } finally {
        if (sequence === requestSequence) activeController = null;
      }
    })();
    return { cached, completion };
  };

  const invalidate = (path: string): void => {
    loadCache();
    const normalizedPath = String(path || '');
    const exactKeys = new Set([
      cacheKey(normalizedPath, false), cacheKey(normalizedPath, true),
    ]);
    for (const [key, entry] of Array.from(cache.entries())) {
      if (!exactKeys.has(key) && entry.data.path !== normalizedPath) continue;
      cache.delete(key);
      cacheBytes = Math.max(0, cacheBytes - entry.bytes);
    }
    persist();
  };

  return Object.freeze({ load, cancel, invalidate });
};
