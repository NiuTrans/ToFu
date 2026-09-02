/** Browser storage capability boundary for typed feature owners. */

export interface BrowserStorageHost {
  readonly localStorage?: Storage;
}

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
