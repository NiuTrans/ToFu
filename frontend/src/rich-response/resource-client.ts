/** Request-owned resource reads shared by identical visible surfaces.
 * No model call, durable cache or business state. Last release aborts the read,
 * clears the refresh timer and drops data. Hidden documents perform no refresh.
 */
import { request } from '../api/transport';
import type { resourceSnapshot } from './contract.generated';
import { validateRichResponse } from './validation';

export type SurfaceContext = { conversationId: string; turnId: string } | { runId: string };
export interface ResourceState { phase: 'idle' | 'loading' | 'ready' | 'error'; snapshot?: resourceSnapshot }
export type ResourceReader = (scope: SurfaceContext, provider: string, signal: AbortSignal, connection?: string, queue?: string) => Promise<resourceSnapshot>;
export interface ResourceSubscription {
  refresh(): void; active(value: boolean): void; live(value: boolean): void; dispose(): void;
}
interface Observer { active: boolean; live: boolean; notify(state: ResourceState): void }
interface Entry {
  scope: SurfaceContext; provider: string; connection?: string; queue?:string; state: ResourceState; observers: Set<Observer>;
  flight?: AbortController; timer?: ReturnType<typeof setTimeout>;
}

export const readResources: ResourceReader = async (scope, provider, signal, connection, queue) => {
  const query = new URLSearchParams({ ...scope, provider, ...(connection?{connection}:{}), ...(queue?{queue}:{}) });
  const result = await request<{version: number; provider: string; snapshot: resourceSnapshot}>(
    '/api/response-surfaces/resources?' + query, {signal});
  if (result.version !== 1 || result.provider !== provider || !validateRichResponse({version:1, title:'Resources',
    blocks:[{type:'resource_panel', id:'resources', title:'Resources', provider, snapshot:result.snapshot}]})) {
    throw new Error('Invalid resource projection');
  }
  return result.snapshot;
};

export class ResourceHub {
  private entries = new Map<string, Entry>();
  constructor(private doc: Document, private read: ResourceReader = readResources) {}
  private visible = (entry: Entry) => this.doc.visibilityState !== 'hidden' && [...entry.observers].some(item => item.active);
  private clearTimer(entry: Entry) { if (entry.timer !== undefined) clearTimeout(entry.timer); entry.timer = undefined; }
  private emit(entry: Entry) {
    for (const item of [...entry.observers]) {
      // A disposed observer or a failed optional presenter cannot poison peers.
      if (entry.observers.has(item)) { try { item.notify(entry.state); } catch { /* isolated presenter */ } }
    }
  }
  private schedule(entry: Entry) {
    this.clearTimer(entry);
    if (!entry.flight && this.visible(entry) && [...entry.observers].some(item => item.active && item.live)) {
      // Data freshness cadence, not a concurrency or capacity limit.
      entry.timer = setTimeout(() => { entry.timer = undefined; this.refresh(entry); }, 30_000);
    }
  }
  private refresh(entry: Entry) {
    if (!this.visible(entry) || entry.flight) return;
    this.clearTimer(entry);
    const flight = new AbortController(); entry.flight = flight;
    entry.state = {...entry.state, phase:'loading'}; this.emit(entry);
    if (entry.flight !== flight || flight.signal.aborted) return;
    void Promise.resolve().then(() => {
      if (entry.flight !== flight || flight.signal.aborted) throw new Error('Disposed resource read');
      return this.read(entry.scope, entry.provider, flight.signal, entry.connection, entry.queue);
    }).then(snapshot => {
      if (entry.flight !== flight || flight.signal.aborted) return;
      entry.state = {phase:'ready', snapshot};
    }, () => {
      if (entry.flight === flight && !flight.signal.aborted) entry.state = {...entry.state, phase:'error'};
    }).finally(() => {
      if (entry.flight !== flight) return;
      entry.flight = undefined; this.emit(entry); this.schedule(entry);
    });
  }
  private reconcileEntry(entry: Entry) {
      if (!this.visible(entry)) {
        this.clearTimer(entry); entry.flight?.abort(); entry.flight = undefined;
        if (entry.state.phase === 'loading') entry.state = {...entry.state, phase:entry.state.snapshot ? 'ready' : 'idle'};
      } else if (!entry.state.snapshot || [...entry.observers].some(item => item.active && item.live)) this.refresh(entry);
      else this.schedule(entry);
  }
  private reconcile = () => { for (const entry of this.entries.values()) this.reconcileEntry(entry); };
  subscribe(scope: SurfaceContext, provider: string, notify: Observer['notify'], connection?:string, queue?:string): ResourceSubscription {
    const owned = 'runId' in scope ? {runId:scope.runId} : {conversationId:scope.conversationId, turnId:scope.turnId};
    const key = JSON.stringify([owned, provider, connection, queue]);
    let entry = this.entries.get(key);
    if (!entry) {
      entry = {scope:owned, provider, connection, queue, state:{phase:'idle'}, observers:new Set()};
      if (!this.entries.size) this.doc.addEventListener('visibilitychange', this.reconcile);
      this.entries.set(key, entry);
    }
    const current = entry, item: Observer = {active:false, live:false, notify};
    current.observers.add(item); try { notify(current.state); } catch { /* isolated presenter */ }
    let disposed = false;
    return {
      refresh: () => {if (!disposed) this.refresh(current);},
      active: value => {if (!disposed && item.active !== value) {
        const wasVisible=this.visible(current);item.active=value;
        if(wasVisible&&this.visible(current))this.schedule(current);else this.reconcileEntry(current);
      }},
      live: value => {if (!disposed) {item.live=value; this.schedule(current);}},
      dispose: () => {
        if (disposed) return; disposed = true; current.observers.delete(item);
        if (!current.observers.size) {
          this.clearTimer(current); current.flight?.abort(); current.flight=undefined; this.entries.delete(key);
          if (!this.entries.size) this.doc.removeEventListener('visibilitychange', this.reconcile);
        } else if (this.visible(current)) this.schedule(current); else this.reconcileEntry(current);
      },
    };
  }
}
const hubs = new WeakMap<Document, ResourceHub>();
export function resourceHub(doc: Document): ResourceHub {
  let hub = hubs.get(doc); if (!hub) {hub = new ResourceHub(doc); hubs.set(doc,hub);} return hub;
}
