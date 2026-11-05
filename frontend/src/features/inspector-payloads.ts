/** Inspector payload evidence: live snapshots, existing FIFO cache and ordered read settlement.
 * Entry: createInspectorPayloads. API/source addressing arrives through ports; state/request
 * preference remains with inspector-evidence. Independent live reads stay fresh; only the
 * newest read (or later live snapshot) may replace cached evidence for the same address.
 */
import {resolveInspectorView} from './inspector-evidence';

type Message = Record<string, any>;
export interface InspectorPayload {messages: Message[]; [key: string]: any}
interface Snapshots {states?: InspectorPayload[]; rounds?: Record<string, InspectorPayload>}
interface Ports {
  snapshots(task: string): Snapshots | undefined;
  live(task: string): boolean;
  read(task: string, round: number, turn: string | undefined, kind: string | undefined, fold: boolean | undefined): Promise<InspectorPayload | null>;
  log(key: string, message: string, level?: string): void;
}
const CACHE_ENTRIES = 40;

class InspectorPayloads {
  private readonly cache = new Map<string, InspectorPayload>();
  private readonly pending = new Map<string, object>();
  constructor(private readonly ports: Ports) {}
  private remember(key: string, payload: InspectorPayload): InspectorPayload {
    if (!this.cache.has(key) && this.cache.size >= CACHE_ENTRIES) this.cache.delete(this.cache.keys().next().value!);
    this.cache.set(key, payload);
    return payload;
  }
  async read(task: string, round: number, turn = '', kind = 'request', options?: {fold?: boolean}): Promise<InspectorPayload | null> {
    const key = JSON.stringify([task, kind, turn, String(round)]), acc = this.ports.snapshots(task);
    const source = kind === 'state'
      ? acc?.states?.filter(row => row?.messages && String(row.roundNum) === String(round) && (!turn || row.turn === turn)).pop()
      : acc?.rounds?.[turn ? turn + '|' + round : String(round)];
    if (source?.messages) {
      const payload: InspectorPayload = {messages: source.messages, tools: source.tools,
        label: source.label, model: source.model, params: source.params,
        ...(kind === 'state' ? {kind: 'state'} : {turn: source.turn || turn})};
      // A later live observation invalidates the cache rights of an older server read.
      this.pending.delete(key);
      this.ports.log(key, 'live ' + kind + ' snapshot, msgs=' + payload.messages.length);
      return this.remember(key, payload);
    }
    const live = this.ports.live(task), cached = this.cache.get(key);
    if (!live && cached) {
      this.ports.log(key, 'cache serve, msgs=' + cached.messages.length, cached.messages.length ? 'info' : 'warn');
      return cached;
    }
    this.ports.log(key, 'server fetch (live=' + live + ', localAcc=' + !!acc + ', cached=' + !!cached + ')');
    const request = {};
    this.pending.set(key, request);
    try {
      let data: InspectorPayload | null;
      try { data = await this.ports.read(task, round, turn || undefined, kind === 'state' ? 'state' : undefined, options?.fold || undefined); }
      catch (error) { if ((error as {status?: number})?.status === 404) return null; throw error; }
      if (data?.messages) {
        this.ports.log(key, 'server ok, msgs=' + data.messages.length, data.messages.length ? 'info' : 'warn');
        if (this.pending.get(key) === request && !this.ports.live(task)) this.remember(key, data);
        return data;
      }
      this.ports.log(key, data ? 'payload without messages' : 'null (404 or error)', 'warn');
      return null;
    } finally { if (this.pending.get(key) === request) this.pending.delete(key); }
  }
  round(task: string, round: number, turn = ''): ReturnType<typeof resolveInspectorView> {
    return resolveInspectorView((t, r, tu, kind) => this.read(t, r, tu, kind, {fold: true}), task, round, turn);
  }
  async increment(task: string, round: number, kind: string, messages: Message[], turn = ''): Promise<Message[] | null> {
    const number = parseInt(String(round), 10);
    if (!Number.isFinite(number) || number <= 1 || !Array.isArray(messages)) return null;
    let previous: InspectorPayload | null;
    try { previous = await this.read(task, number - 1, turn, kind === 'state' ? 'state' : 'request'); }
    catch { this.ports.log(task + ':' + round, 'diff base unavailable', 'warn'); return null; }
    if (!previous || !Array.isArray(previous.messages) || !previous.messages.length) return null;
    const count = Math.min(previous.messages.length, messages.length);
    let prefix = 0;
    while (prefix < count && JSON.stringify(previous.messages[prefix]) === JSON.stringify(messages[prefix])) prefix++;
    return prefix > 0 ? messages.slice(prefix) : null;
  }
}

export function inspectorTailMessages(messages: Message[]): Message[] {
  if (!Array.isArray(messages)) return [];
  for (let i = messages.length - 1; i >= 0; i--) {
    const message = messages[i];
    if (message?.role === 'assistant' && Array.isArray(message.tool_calls) && message.tool_calls.length) return messages.slice(i);
  }
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i]?.role === 'user') {
      const tail = messages.slice(i + 1);
      if (tail.length) return tail;
      break;
    }
  }
  return messages.filter(message => message && message.role !== 'system');
}

export function createInspectorPayloads(ports: Ports): InspectorPayloads { return new InspectorPayloads(ports); }
