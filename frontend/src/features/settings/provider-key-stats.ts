/** Provider credential statistics read lifetime and ordered runtime overrides.
 * Dispatch owns daily health and override policy. Rows stay opaque here; this
 * owner projects its namespace evidence, coalesces reads, and serializes only
 * writes to the same server namespace/credential. Closing drops view state,
 * not accepted writes; no DOM or durable credential state is retained.
 */
type Row = Record<string, unknown>;
interface Snapshot extends Row {
  providers: Record<string, Record<string, Row>>;
  key_namespace?: string;
}
interface OverrideBody {provider_id: string; key_name: string; enabled: boolean | null}
interface StatsPorts {
  document(): object | null;
  read(): Promise<unknown>;
  write(body: OverrideBody): Promise<unknown>;
  refresh(): void;
  failed(): void;
}
interface AcknowledgedRow {providerId: string; credentialId: string; namespace: string; row: Row}
interface Period {
  document: object | null;
  snapshot: Snapshot | null;
  pending: Promise<Snapshot | null> | null;
  status: 'idle' | 'loading' | 'ready' | 'failed';
  acknowledgedDuringRead: Map<string, AcknowledgedRow>;
}

function record(value: unknown): value is Row {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function snapshot(value: unknown): Snapshot | null {
  if (!record(value) || !record(value.providers)) return null;
  // Namespace projection needs maps; leave each health row to its dispatch owner.
  if (Object.values(value.providers).some(bucket => !record(bucket))) return null;
  return value as Snapshot;
}

function bucket(value: Snapshot | null, providerId: string, credentialId: string) {
  const providers = value?.providers || {};
  if (typeof value?.key_namespace === 'string') {
    const id = value.key_namespace + providerId;
    return Object.hasOwn(providers, id) ? {id, keys: providers[id]} : null;
  }
  const candidates = Object.keys(providers).filter(id => id !== providerId && id.endsWith(':' + providerId));
  if (Object.hasOwn(providers, providerId)) candidates.unshift(providerId);
  const matches = candidates.filter(id => Object.hasOwn(providers[id], credentialId) && providers[id][credentialId]);
  const id = matches.length === 1 ? matches[0] : undefined;
  return id ? {id, keys: providers[id]} : null;
}

function namespace(value: Snapshot | null, providerId: string, credentialId: string): string | null {
  // An explicit empty prefix is valid. An absent prefix is not authority to
  // invent a bare-provider override that the owner-scoped dispatcher won't read.
  if (typeof value?.key_namespace === 'string') return value.key_namespace + providerId;
  return bucket(value, providerId, credentialId)?.id || null;
}

function withRow(value: Snapshot, id: string, credentialId: string, row: Row): Snapshot {
  return {...value, providers: {...value.providers,
    [id]: {...value.providers[id], [credentialId]: row}}};
}

function enqueue(queue: Map<string, Promise<void>>, key: string, run: () => Promise<void>): Promise<void> {
  const pending = (queue.get(key) || Promise.resolve()).then(run, run);
  queue.set(key, pending);
  const release = () => { if (queue.get(key) === pending) queue.delete(key); };
  void pending.then(release, release);
  return pending;
}

class ProviderKeyStats {
  private current: Period | null = null;
  private readonly queues = new WeakMap<object, Map<string, Promise<void>>>();
  private readonly targets = new Map<string, Promise<void>>();
  private readonly intents = new WeakMap<object, Map<string, {enabled: boolean | null}>>();

  constructor(private readonly ports: StatsPorts) {}

  get snapshot(): Snapshot | null {
    if (this.current && this.current.document !== this.ports.document()) this.dispose();
    return this.current?.snapshot || null;
  }

  get status(): Period['status'] {
    void this.snapshot;
    return this.current?.status || 'idle';
  }

  row(providerId: string, credentialId: string): Row | null {
    const keys = bucket(this.snapshot, providerId, credentialId)?.keys;
    return keys && Object.hasOwn(keys, credentialId) ? keys[credentialId] || null : null;
  }

  pending(providerId: string, credentialId: string): boolean | null | undefined {
    const document = this.ports.document();
    return document ? this.intents.get(document)?.get(JSON.stringify([providerId, credentialId]))?.enabled : undefined;
  }

  load(): Promise<Snapshot | null> {
    const period = this.period();
    return period ? this.loadPeriod(period) : Promise.resolve(null);
  }

  override(providerId: string, credentialId: string, enabled: boolean | null): Promise<void> {
    const period = this.period();
    const document = period?.document;
    if (!period || !document) { this.ports.failed(); return Promise.resolve(); }
    const key = JSON.stringify([providerId, credentialId]);
    const intent = {enabled};
    let intents = this.intents.get(document);
    if (!intents) { intents = new Map(); this.intents.set(document, intents); }
    intents.set(key, intent);
    const read = this.loadPeriod(period);
    let queue = this.queues.get(document);
    if (!queue) { queue = new Map(); this.queues.set(document, queue); }
    // Reserve click order before namespace reads finish. Once resolved, the
    // canonical target queue also orders posts across a refreshed draft.
    const pending = enqueue(queue, key, () => this.prepareWrite(document, providerId, credentialId, enabled, read));
    const settled = () => {
      if (intents.get(key) !== intent) return;
      intents.delete(key);
      if (this.current?.document === document && this.active(this.current)) this.ports.refresh();
    };
    void pending.then(settled, settled);
    return pending;
  }

  dispose(): void {
    const period = this.current;
    this.current = null;
    if (!period) return;
    period.document = null;
    period.snapshot = null;
    period.pending = null;
    period.acknowledgedDuringRead.clear();
  }

  private period(): Period | null {
    void this.snapshot;
    const document = this.ports.document();
    if (!document) return null;
    if (!this.current) this.current = {document, snapshot: null, pending: null,
      status: 'idle', acknowledgedDuringRead: new Map()};
    return this.current;
  }

  private active(period: Period): boolean {
    if (this.current !== period) return false;
    if (period.document !== this.ports.document()) { this.dispose(); return false; }
    return true;
  }

  private loadPeriod(period: Period): Promise<Snapshot | null> {
    if (period.pending) return period.pending;
    if (period.status === 'ready') return Promise.resolve(period.snapshot);
    period.status = 'loading';
    period.pending = this.read(period);
    this.ports.refresh();
    return period.pending;
  }

  private async read(period: Period): Promise<Snapshot | null> {
    let value: Snapshot | null = null;
    try { value = snapshot(await Promise.resolve().then(() => this.ports.read())); }
    catch { /* Null/error reads remain retryable, never a cached empty success. */ }
    if (this.active(period)) {
      if (value) {
        for (const update of period.acknowledgedDuringRead.values()) {
          if (namespace(value, update.providerId, update.credentialId) === update.namespace) {
            value = withRow(value, update.namespace, update.credentialId, update.row);
          }
        }
        period.snapshot = value;
      }
      period.status = value ? 'ready' : 'failed';
      period.pending = null;
      period.acknowledgedDuringRead.clear();
      this.ports.refresh();
    }
    // A retired view's read may still supply namespace evidence to a write
    // already requested there. The write separately validates draft identity.
    return value;
  }

  private async prepareWrite(document: object, providerId: string, credentialId: string,
    enabled: boolean | null, read: Promise<Snapshot | null>): Promise<void> {
    const value = await read;
    const target = namespace(value, providerId, credentialId);
    if (!value || target === null || this.ports.document() !== document) { this.ports.failed(); return; }
    return enqueue(this.targets, JSON.stringify([target, credentialId]),
      () => this.write(document, providerId, credentialId, target, enabled, value));
  }

  private async write(document: object, providerId: string, credentialId: string,
    target: string, enabled: boolean | null, value: Snapshot): Promise<void> {
    if (this.ports.document() !== document) { this.ports.failed(); return; }
    let response: unknown;
    try { response = await this.ports.write({provider_id: target, key_name: credentialId, enabled}); }
    catch { response = null; }
    if (!record(response) || !record(response.row)) { this.ports.failed(); return; }
    this.publish(document, {providerId, credentialId, namespace: target, row: response.row}, value);
  }

  private publish(document: object, update: AcknowledgedRow, evidence: Snapshot): void {
    const period = this.current;
    if (!period || !this.active(period)) return;
    const currentNamespace = namespace(period.snapshot, update.providerId, update.credentialId);
    if (currentNamespace !== null && currentNamespace !== update.namespace) return;
    if (period.pending) period.acknowledgedDuringRead.set(JSON.stringify([update.namespace, update.credentialId]), update);
    // A refreshed draft can represent the same backend target. Until its read
    // confirms the namespace, retain the receipt without publishing its row.
    if (period.document !== document && currentNamespace === null) return;
    const base = period.snapshot || {providers: {}, key_namespace: evidence.key_namespace};
    period.snapshot = withRow(base, update.namespace, update.credentialId, update.row);
    this.ports.refresh();
  }
}

export function createProviderKeyStats(ports: StatsPorts): ProviderKeyStats {
  return new ProviderKeyStats(ports);
}
