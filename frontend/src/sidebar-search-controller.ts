/** Sidebar search projection owner. Ports supply transport and presentation;
 * no catalog writes, browser globals, transcript reads, or background polling.
 * Retained adapter can be deleted when the sidebar becomes a typed surface. */
export interface SearchConversation {
  id: string;
  title?: string;
  updatedAt?: number;
  createdAt?: number;
  rev?: number;
  msgCount?: number;
  settings?: Record<string, unknown>;
  [key: string]: unknown;
}
export interface SidebarSearchHit {
  id: string;
  matchField: string;
  matchSnippet?: string | null;
  matchRole?: string | null;
  conversation: SearchConversation;
}
export interface SidebarSearchPage {
  ok: boolean;
  items: SidebarSearchHit[];
  nextCursor: string | null;
  coverage: 'ready' | 'indexing' | 'partial';
  coverageReasons: string[];
}
export interface SidebarSearchState {
  query: string;
  items: SidebarSearchHit[];
  loading: boolean;
  error: boolean;
  authoritative: boolean;

  truncated: boolean;
  nextCursor: string | null;
  coverage: SidebarSearchPage['coverage'];
  coverageReasons: string[];
}
interface Ports {
  fetchPage(query: string, cursor: string | null, signal: AbortSignal): Promise<SidebarSearchPage>;
  render(state: SidebarSearchState): void;
  now?: () => number;
  debounceMs?: number;
}
const LIMIT = 500;
const REFRESH_MS = 30_000;
export function createSidebarSearchController(ports: Ports) {
  const now = ports.now || Date.now;
  let state: SidebarSearchState = empty();
  let epoch = 0;
  let destroyed = false;
  let flight: AbortController | null = null;
  let pending: (() => void) | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let lastRefresh = -Infinity;

  let lastRetry = -Infinity;
  let fingerprint = '';
  let failedCursor: string | null = null;
  function empty(): SidebarSearchState {
    return { query: '', items: [], loading: false, error: false,
      authoritative: false, truncated: false, nextCursor: null, coverage: 'ready', coverageReasons: [] };
  }
  function emit() { if (!destroyed && state.query) ports.render({ ...state, items: [...state.items] }); }
  function cancel() {
    epoch++;
    if (timer !== null) clearTimeout(timer);
    timer = null;
    pending = null;
    flight?.abort();
  }
  function schedule(cursor: string | null, delay = 0) {
    const token = epoch;
    const query = state.query;
    state.loading = true;
    state.error = false;
    emit();
    const run = () => {
      if (destroyed || token !== epoch) return;
      if (flight) { pending = run; return; }
      void request(query, cursor, token);
    };
    if (delay) timer = setTimeout(() => { timer = null; run(); }, delay);
    else run();
  }
  async function request(query: string, cursor: string | null, token: number) {
    const ac = new AbortController();
    flight = ac;
    let pageCursor = cursor;
    let replace = cursor === null;
    let automatic = 0;
    const current = () => !destroyed && token === epoch && !ac.signal.aborted;
    try {
      for (;;) {
        failedCursor = pageCursor;
        const page = await ports.fetchPage(query, pageCursor, ac.signal);
        if (!current()) return;
        if (!page || page.ok !== true || !Array.isArray(page.items)
            || !['ready', 'indexing', 'partial'].includes(page.coverage)) {
          throw new Error('Invalid search page');
        }
        const rows = new Map((replace ? [] : state.items).map(hit => [hit.id, hit]));
        let dropped = false;
        for (const hit of page.items) {
          if (!hit?.id || hit.conversation?.id !== hit.id) continue;
          if (rows.has(hit.id) || rows.size < LIMIT) rows.set(hit.id, hit);

          else dropped = true;
        }
        state.items = [...rows.values()];
        state.authoritative = true;
        state.coverage = page.coverage;
        state.coverageReasons = (page.coverageReasons || []).slice(0, 32);

        state.truncated = (replace ? false : state.truncated) || dropped
          || (rows.size >= LIMIT && !!page.nextCursor);
        state.nextCursor = rows.size < LIMIT && typeof page.nextCursor === 'string'
          ? page.nextCursor : null;
        replace = false;
        if (page.items.length || !state.nextCursor || automatic >= 3) break;
        // Empty partial pages can advance through bounded candidate scans.
        automatic++;
        pageCursor = state.nextCursor;
      }
    } catch (_) {
      if (current()) state.error = true; // Never pretend cached titles are complete history.
    } finally {
      if (flight === ac) flight = null;
      if (current()) { state.loading = false; emit(); }
      const next = pending;
      pending = null;
      next?.();
    }
  }
  return {
    setQuery(query: string, local: SidebarSearchHit[] = [], metadataFingerprint = '') {
      if (destroyed) return;
      query = query.trim().toLowerCase();
      if (query === state.query) {
        if (!query) return;
        if (!state.authoritative) state.items = local.slice(0, LIMIT);
        const changed = fingerprint !== metadataFingerprint;
        fingerprint = metadataFingerprint;
        emit(); // Local arrivals reconcile presentation without resetting server membership.
        if (changed && !state.loading && !state.error && now() - lastRefresh >= REFRESH_MS) {
          lastRefresh = now();
          schedule(null);
        }
        return;
      }
      cancel();
      state = { ...empty(), query, items: local.slice(0, LIMIT) };
      fingerprint = metadataFingerprint;
      lastRefresh = now();
      if (query) schedule(null, ports.debounceMs ?? 180);
    },
    retry() {
      if (destroyed || !state.query || state.loading) return;
      // Explicit recovery is immediate once, then at most once per second.
      // Metadata refresh throttling must not turn a user's retry into a 30s wait.
      const delay = Math.max(0, 1_000 - (now() - lastRetry));
      lastRetry = now() + delay;
      lastRefresh = now();
      schedule(state.error ? failedCursor : null, delay);
    },
    loadMore() {
      if (!destroyed && state.query && !state.loading && state.nextCursor && state.items.length < LIMIT) {
        schedule(state.nextCursor);
      }
    },
    clear() { cancel(); state = empty(); fingerprint = ''; },
    destroy() { destroyed = true; cancel(); state = empty(); },
    snapshot() { return { ...state, items: [...state.items] }; },
  };
}
