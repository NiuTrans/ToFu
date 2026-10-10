/** Browser storage capability boundary for typed feature owners. */

export interface BrowserStorageHost {
  readonly localStorage?: Storage;
  readonly sessionStorage?: Storage;
  readonly indexedDB?: IDBFactory;
}

export type BrowserStorageKind = 'local' | 'session';

/**
 * Resolve the browser's local storage without making feature owners probe a
 * global directly. Access can throw in sandboxed/private browser contexts, so
 * an unavailable capability is represented explicitly and callers fail soft.
 */
export function resolveBrowserLocalStorage(
  host: BrowserStorageHost = globalThis as unknown as BrowserStorageHost,
): Storage | undefined {
  try {
    return host.localStorage;
  } catch {
    return undefined;
  }
}

/** Resolve page-scoped storage without assuming that the host exposes it. */
export function resolveBrowserSessionStorage(
  host: BrowserStorageHost = globalThis as unknown as BrowserStorageHost,
): Storage | undefined {
  try {
    return host.sessionStorage;
  } catch {
    return undefined;
  }
}

function resolveStorage(
  kind: BrowserStorageKind,
  host?: BrowserStorageHost,
): Storage | undefined {
  return kind === 'local'
    ? resolveBrowserLocalStorage(host)
    : resolveBrowserSessionStorage(host);
}

/**
 * Read a browser-owned value. Browser policy, quota implementations, and test
 * hosts may throw either while resolving Storage or while invoking a method.
 */
export function readBrowserStorage(
  key: string,
  kind: BrowserStorageKind = 'local',
  host?: BrowserStorageHost,
): string | null {
  try {
    return resolveStorage(kind, host)?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

/** Persist a value when the requested storage capability is usable. */
export function writeBrowserStorage(
  key: string,
  value: string,
  kind: BrowserStorageKind = 'local',
  host?: BrowserStorageHost,
): boolean {
  try {
    const storage = resolveStorage(kind, host);
    if (!storage) return false;
    storage.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

/** Remove a value when the requested storage capability is usable. */
export function removeBrowserStorage(
  key: string,
  kind: BrowserStorageKind = 'local',
  host?: BrowserStorageHost,
): boolean {
  try {
    const storage = resolveStorage(kind, host);
    if (!storage) return false;
    storage.removeItem(key);
    return true;
  } catch {
    return false;
  }
}

/** Resolve IndexedDB through the same explicit, fail-soft capability seam. */
export function resolveBrowserIndexedDb(
  host: BrowserStorageHost = globalThis as unknown as BrowserStorageHost,
): IDBFactory | undefined {
  try {
    return host.indexedDB;
  } catch {
    return undefined;
  }
}
