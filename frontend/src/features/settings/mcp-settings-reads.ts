/** MCP read and observer ownership for one Settings opening.
 * Entry: captureMcpSettingsReads / notifyMcpSettingsVisibility. The retained
 * presenter supplies projections; API, installs and command settlement retain
 * their existing authorities. Repeated reads share work, and retirement aborts
 * only read resources. Breaker policy remains the existing 3–15 second refresh
 * and 1 second visible countdown, with no background observer while hidden.
 */
import {createLifecycleScope, type LifecycleScope} from '../../lifecycle';
import {currentSettingsReadSession, type SettingsReadSession} from './panel-read-session';

type Body = Record<string, unknown>;
interface ReadResponse {ok: boolean; status?: number; json(): Promise<Body>}
interface ReadOptions {signal?: AbortSignal}
interface BreakerEntry {installed?: boolean; connected?: boolean; breaker?: {retry_in?: number}}
export interface McpSettingsReadPorts {
  catalog(options: ReadOptions): Promise<ReadResponse | null>;
  updates(options: ReadOptions): Promise<ReadResponse | null>;
  tools(server: string, options: ReadOptions): Promise<ReadResponse | null>;
  loading(): void;
  catalogLoaded(data: Body): void;
  catalogSummary(data: Body): void;
  catalogFailed(error: unknown, display?: boolean): void;
  updatesLoaded(data: Body): void;
  updatesFailed(error: unknown): void;
  toolsLoaded(server: string, data: Body): void;
  toolsFailed(server: string, error: unknown): void;
  invalidateTools(): void;
  entries(): readonly BreakerEntry[];
  tickBreakers(): void;
}
interface ReadFlight {scope: LifecycleScope; promise: Promise<void>}
let catalogReadOrder = 0;
let publishedCatalogOrder = 0;

function publishSummary(ports: McpSettingsReadPorts, data: Body, order: number): void {
  // Compare successful publications: a later aborted view read must not
  // suppress an accepted command's still-valid application summary.
  if (order < publishedCatalogOrder) return;
  publishedCatalogOrder = order;
  ports.catalogSummary(data);
}

async function readBody(request: Promise<ReadResponse | null>, current = () => true): Promise<Body | null> {
  const response = await request;
  if (!current()) return null;
  if (!response?.ok) throw new Error('HTTP ' + (response?.status || 'no response'));
  const data = await response.json();
  return current() ? data : null;
}

class McpSettingsReads {
  private readonly scope = createLifecycleScope(window);
  private readonly grid = document.getElementById('mcpCatalogGrid');
  private readonly reads = new Map<string, ReadFlight>();
  private refreshTimer = 0;
  private tickTimer = 0;
  private catalogFailed = false;
  private catalogOrder = 0;
  private committedRefresh: Promise<void> | null = null;

  constructor(readonly session: SettingsReadSession, private ports: McpSettingsReadPorts) {
    this.scope.listen(session.scope.signal, 'abort', () => this.scope.destroy(), {once: true});
    this.scope.add(() => this.reads.clear());
  }
  isCurrent(): boolean {
    return this.session.isCurrent() && !this.scope.signal.aborted
      && document.getElementById('mcpCatalogGrid') === this.grid;
  }
  private visible(): boolean { return this.isCurrent() && !!this.grid?.isConnected && this.grid.offsetParent !== null; }

  private read(
    key: string, load: (options: ReadOptions) => Promise<ReadResponse | null>,
    publish: (data: Body) => void, failed: (error: unknown) => void,
  ): Promise<void> {
    if (!this.isCurrent()) return Promise.resolve();
    const pending = this.reads.get(key);
    if (pending) return pending.promise;
    const scope = createLifecycleScope(window);
    scope.listen(this.scope.signal, 'abort', () => scope.destroy(), {once: true});
    const current = () => this.isCurrent() && this.reads.get(key) === flight && !scope.signal.aborted;
    const flight: ReadFlight = {scope, promise: Promise.resolve().then(async () => {
      try {
        const data = await readBody(load({signal: scope.signal}), current);
        if (data) publish(data);
      } catch (error) { if (current()) failed(error); }
      finally {
        if (this.reads.get(key) === flight) this.reads.delete(key);
        scope.destroy();
      }
    })};
    this.reads.set(key, flight);
    return flight.promise;
  }

  populate(): Promise<void> {
    if (!this.isCurrent()) return Promise.resolve();
    if (this.committedRefresh) return this.committedRefresh;
    if (!this.reads.has('catalog')) this.ports.loading();
    let order = 0;
    return this.read('catalog', options => {
      order = ++catalogReadOrder;
      this.catalogOrder = order;
      return this.ports.catalog(options);
    }, data => {
      publishSummary(this.ports, data, order);
      this.publishCatalog(data, order);
    }, error => { this.catalogFailed = true; this.ports.catalogFailed(error); });
  }
  prepareCommittedRefresh(order: number): void {
    this.catalogOrder = order;
    this.committedRefresh = null;
    for (const read of this.reads.values()) read.scope.destroy();
    this.reads.clear();
    this.ports.invalidateTools();
  }
  retainCommittedRefresh(refresh: Promise<void>): void {
    this.committedRefresh = refresh;
    const release = () => { if (this.committedRefresh === refresh) this.committedRefresh = null; };
    void refresh.then(release, release);
  }
  acceptsCatalog(order: number): boolean { return this.isCurrent() && this.catalogOrder === order; }
  publishCatalog(data: Body, order: number): void {
    if (!this.acceptsCatalog(order)) return;
    this.catalogFailed = false;
    this.ports.catalogLoaded(data);
    if (this.isCurrent()) void this.checkUpdates();
  }
  checkUpdates(): Promise<void> {
    return this.read('updates', options => this.ports.updates(options),
      data => this.ports.updatesLoaded(data), error => this.ports.updatesFailed(error));
  }
  loadTools(server: string): Promise<void> {
    return this.read('tools:' + server, options => this.ports.tools(server, options),
      data => this.ports.toolsLoaded(server, data), error => this.ports.toolsFailed(server, error));
  }

  scheduleBreakerRefresh(): void {
    this.scope.cancelTimeout(this.refreshTimer);
    this.refreshTimer = 0;
    const active = this.ports.entries().filter(entry => entry.installed && !entry.connected && entry.breaker);
    if (!this.visible() || !active.length) { this.stopTick(); return; }
    const soonest = Math.min(...active.map(entry => Math.max(0, entry.breaker?.retry_in || 0)));
    const delay = Math.min(15000, Math.max(3000, (soonest + 1) * 1000));
    this.refreshTimer = this.scope.timeout(() => {
      this.refreshTimer = 0;
      if (this.visible()) void this.populate();
    }, delay);
    this.startTick();
  }
  startTick(): void {
    if (this.tickTimer || !this.visible()) return;
    this.tickTimer = this.scope.interval(() => {
      if (this.visible()) this.ports.tickBreakers();
      else this.visibilityChanged();
    }, 1000);
  }
  stopTick(): void { this.scope.cancelInterval(this.tickTimer); this.tickTimer = 0; }
  visibilityChanged(): void {
    if (this.visible()) {
      if (this.catalogFailed) void this.populate();
      else this.scheduleBreakerRefresh();
    } else {
      this.scope.cancelTimeout(this.refreshTimer);
      this.refreshTimer = 0;
      this.stopTick();
    }
  }
}

let current: McpSettingsReads | null = null;
export function captureMcpSettingsReads(ports: McpSettingsReadPorts): McpSettingsReads | null {
  const session = currentSettingsReadSession();
  if (!session?.isCurrent()) return null;
  if (!current?.isCurrent() || current.session !== session) current = new McpSettingsReads(session, ports);
  return current;
}
export function notifyMcpSettingsVisibility(): void { current?.visibilityChanged(); }

/** Accepted commands keep their acknowledgement path after panel retirement.
 * Read after the write (never reuse a pre-write flight), publish the common tool
 * summary, and paint only the opening captured when this refresh began.
 */
export function refreshMcpSettingsAfterWrite(ports: McpSettingsReadPorts): Promise<void> {
  const view = captureMcpSettingsReads(ports), order = ++catalogReadOrder;
  view?.prepareCommittedRefresh(order);
  const refresh = Promise.resolve().then(async () => {
    try {
      const data = await readBody(ports.catalog({}));
      if (!data) return;
      publishSummary(ports, data, order);
      view?.publishCatalog(data, order);
    } catch (error) { ports.catalogFailed(error, !!view?.acceptsCatalog(order)); }
  });
  view?.retainCommittedRefresh(refresh);
  return refresh;
}
