// @vitest-environment jsdom
/** Recovery and disposal specifications for the single conversation sync owner. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ConversationSyncCoordinator, type ConversationSyncCoordinatorOptions } from './conversation-sync';
import { conversationConnectionHealth } from './connection-health';
import {
  CONVERSATION_SYNC_STREAM_POLICY,
  type ConversationSyncApi, type ConversationSyncSnapshot,
} from '../api/conversation-sync.generated';
import type { RequestOptions } from '../api/transport';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

class Source extends EventTarget {
  closed = false;
  readyState = 0;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  constructor(readonly url: string) { super(); }
  close() { this.closed = true; this.readyState = 2; }
  emit(type: string, fields: object, cursor = '') {
    this.dispatchEvent(new MessageEvent(type, { lastEventId: cursor, data: JSON.stringify({
      contract: 'tofu.conversation-sync.event/v1', conversationId: 'conv', type, ...fields,
    }) }));
  }
}

function snapshot(head = 1): ConversationSyncSnapshot {
  return { ok: true, contract: 'tofu.conversation-sync.snapshot/v1',
    scope: { kind: 'conversation', ownerId: 7, threadId: 'conv' }, conversationId: 'conv',
    conversationRevision: head, syncSeq: head, cursor: `cursor-${head}`,
    serverBootId: 'boot', heartbeatIntervalMs: 15000,
    settings: {}, turns: [], attempts: [], queueItems: [] };
}

const coordinators: ConversationSyncCoordinator[] = [];
function fixture(overrides: Partial<ConversationSyncCoordinatorOptions> = {}) {
  const sources: Source[] = [];
  const api = {
    snapshot: vi.fn(async (_id: string, _options?: RequestOptions) => snapshot()),
    eventsUrl: (_id: string, cursor: string) => `/events?after=${cursor}`,
    turnPage: vi.fn(), roundDirectory: vi.fn(),
  };
  const onSnapshot = vi.fn();
  const onProtocolError = vi.fn();
  const coordinator = new ConversationSyncCoordinator({
    conversationId: 'conv', api: api as unknown as ConversationSyncApi,
    onSnapshot, onProtocolError, onAttemptEvent: () => true, onTurnDelta: () => true,
    eventSourceFactory: url => {
      const source = new Source(url); sources.push(source);
      return source as unknown as EventSource;
    }, ...overrides,
  });
  coordinators.push(coordinator);
  return { coordinator, api, sources, onSnapshot, onProtocolError };
}

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => {
  coordinators.splice(0).forEach(coordinator => coordinator.close());
  vi.useRealTimers(); vi.restoreAllMocks();
});

describe('snapshot recovery barrier', () => {
  it('retries a failed reset snapshot before opening any replay stream', async () => {
    const { coordinator, api, sources, onSnapshot } = fixture();
    await coordinator.resume();
    const recovered = deferred<ConversationSyncSnapshot>();
    api.snapshot.mockRejectedValueOnce(new Error('storage unavailable'))
      .mockImplementationOnce(() => recovered.promise);
    sources[0].emit('sync.reset_required', { cursor: 'reset-head-9', reason: 'server_restarted' });
    await vi.advanceTimersByTimeAsync(2000);
    expect(coordinator.cursor).toBe('cursor-1');
    expect(api.snapshot).toHaveBeenCalledTimes(3);
    expect(sources).toHaveLength(1);
    const wake = coordinator.wake();
    coordinator.invalidate();
    const resume = coordinator.resume();
    expect(api.snapshot).toHaveBeenCalledTimes(3);
    recovered.resolve(snapshot(9));
    await Promise.all([wake, resume]);
    expect(onSnapshot).toHaveBeenLastCalledWith(expect.objectContaining({ syncSeq: 9 }));
    expect(sources).toHaveLength(2);
    expect(sources[1].url).toContain('cursor-9');
  });

  it('recovers a failed initial hydration without opening an uninitialized stream', async () => {
    const { coordinator, api, sources } = fixture();
    api.snapshot.mockRejectedValueOnce(new Error('offline'));
    await expect(coordinator.resume()).rejects.toThrow('offline');
    expect(sources).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(2000);
    expect(api.snapshot).toHaveBeenCalledTimes(2);
    expect(sources).toHaveLength(1);
    expect(sources[0].url).toContain('cursor-1');
  });

  it('keeps cursor publication behind successful projection ingestion', async () => {
    const onSnapshot = vi.fn().mockImplementationOnce(() => {})
      .mockImplementationOnce(() => { throw new Error('projection failed'); });
    const { coordinator, api, sources } = fixture({ onSnapshot });
    await coordinator.resume();
    api.snapshot.mockResolvedValue(snapshot(9));
    await expect(coordinator.recover()).rejects.toThrow('projection failed');
    expect(coordinator.cursor).toBe('cursor-1');
    expect(sources).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(2000);
    expect(coordinator.cursor).toBe('cursor-9');
    expect(sources).toHaveLength(2);
  });

  it('refuses history reads while a required replacement snapshot is pending', async () => {
    const { coordinator, api } = fixture();
    await coordinator.hydrate(false);
    const gate = deferred<ConversationSyncSnapshot>();
    api.snapshot.mockImplementationOnce(() => gate.promise);
    const recovery = coordinator.recover();
    await expect(coordinator.loadTurnPage('main')).rejects.toThrow('authoritative snapshot');
    await expect(coordinator.loadRoundDirectory('main')).rejects.toThrow('active snapshot');
    expect(api.turnPage).not.toHaveBeenCalled();
    expect(api.roundDirectory).not.toHaveBeenCalled();
    gate.resolve(snapshot(9)); await recovery;
  });

  it.each(['pause', 'suspend'] as const)('retains recovery across %s without background retries', async kind => {
    const { coordinator, api, sources } = fixture();
    await coordinator.resume();
    api.snapshot.mockRejectedValueOnce(new Error('offline')).mockResolvedValue(snapshot(9));
    await expect(coordinator.recover()).rejects.toThrow('offline');
    if (kind === 'pause') coordinator.pause(); else coordinator.setSuspended(true);
    await vi.advanceTimersByTimeAsync(60000);
    expect(api.snapshot).toHaveBeenCalledTimes(2);
    expect(sources).toHaveLength(1);
    if (kind === 'pause') await coordinator.resume(); else {
      coordinator.setSuspended(false); await vi.advanceTimersByTimeAsync(0);
    }
    expect(api.snapshot).toHaveBeenCalledTimes(3);
    expect(coordinator.cursor).toBe('cursor-9');
  });
});

describe('history page head repair', () => {
  const page = (syncSeq: number, beforeOrdinal: number) => ({
    conversationId: 'conv', laneId: 'main', syncSeq, turns: [], attempts: [],
    beforeOrdinal, nextBeforeOrdinal: 1, totalTurns: 900, hasMore: true,
  } as unknown as import('../api/conversation-sync.generated').ConversationTurnPage);

  it('repairs one stale head from an authoritative snapshot and replays the cursor', async () => {
    const onTurnPage = vi.fn();
    const { coordinator, api, onSnapshot } = fixture({ onTurnPage });
    await coordinator.hydrate(false);
    api.snapshot.mockResolvedValue(snapshot(9));
    api.turnPage
      .mockRejectedValueOnce(Object.assign(new Error('HTTP 409'), { status: 409 }))
      .mockImplementationOnce(async (_id, lane, syncSeq, before) => page(syncSeq, before));

    const loaded = await coordinator.loadTurnPage('main', 7, 64);

    expect(api.snapshot).toHaveBeenCalledTimes(2);
    expect(onSnapshot).toHaveBeenLastCalledWith(expect.objectContaining({ syncSeq: 9 }));
    expect(api.turnPage).toHaveBeenCalledTimes(2);
    expect(api.turnPage.mock.calls[1].slice(0, 4)).toEqual(['conv', 'main', 9, 7]);
    expect(loaded.syncSeq).toBe(9);
    expect(onTurnPage).toHaveBeenCalledTimes(1);
  });

  it('surfaces a non-stale history failure without a snapshot read', async () => {
    const { coordinator, api, onProtocolError } = fixture();
    await coordinator.hydrate(false);
    api.turnPage.mockRejectedValueOnce(Object.assign(new Error('unavailable'), { status: 503 }));
    await expect(coordinator.loadTurnPage('main', 7, 64)).rejects.toThrow('unavailable');
    expect(api.snapshot).toHaveBeenCalledTimes(1);
    expect(onProtocolError).toHaveBeenCalledTimes(1);
  });

  it('stops after the bounded number of repair attempts', async () => {
    const { coordinator, api } = fixture();
    await coordinator.hydrate(false);
    api.turnPage.mockRejectedValue(Object.assign(new Error('HTTP 409'), { status: 409 }));
    await expect(coordinator.loadTurnPage('main', 7, 64)).rejects.toThrow('HTTP 409');
    expect(api.turnPage).toHaveBeenCalledTimes(3);
    expect(api.snapshot).toHaveBeenCalledTimes(3);
  });
});
describe('transport liveness and observer isolation', () => {
  it('recovers a permanently closed source through a fresh snapshot with backoff', async () => {
    const { coordinator, api, sources, onSnapshot } = fixture();
    await coordinator.resume();
    api.snapshot.mockResolvedValue(snapshot(9));
    sources[0].readyState = 2; // Native EventSource after HTTP 204.
    sources[0].onerror?.();
    expect(coordinator.connected).toBe(false);
    expect(sources[0].closed).toBe(true);
    await coordinator.wake(); await coordinator.resume(); coordinator.invalidate();
    expect(api.snapshot).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1000);
    expect(onSnapshot).toHaveBeenLastCalledWith(expect.objectContaining({syncSeq: 9}));
    expect(sources).toHaveLength(2);
    expect(sources[1].url).toContain('cursor-9');

    sources[1].readyState = 2; sources[1].onerror?.();
    await vi.advanceTimersByTimeAsync(1000);
    expect(sources).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(1000);
    expect(sources).toHaveLength(3);
    sources[2].emit('sync.heartbeat', {cursor:'cursor-9', serverBootId:'boot', degraded:false});
    sources[2].readyState = 2; sources[2].onerror?.();
    await vi.advanceTimersByTimeAsync(1000);
    expect(sources).toHaveLength(4);
  });

  it.each(['pause', 'suspend', 'close'] as const)(
    'retires closed-source retries on %s and ignores late callbacks', async action => {
      const {coordinator, api, sources} = fixture();
      await coordinator.resume();
      const lateError = sources[0].onerror;
      sources[0].readyState = 2; lateError?.();
      if (action === 'suspend') coordinator.setSuspended(true);
      else coordinator[action]();
      lateError?.();
      await vi.advanceTimersByTimeAsync(60000);
      expect(api.snapshot).toHaveBeenCalledTimes(1);
      expect(sources.filter(source => !source.closed)).toHaveLength(0);
      expect(vi.getTimerCount()).toBe(0);
    },
  );

  it('bounds connecting time even when EventSource never opens or errors', async () => {
    const { coordinator, sources } = fixture();
    await coordinator.resume();
    await vi.advanceTimersByTimeAsync(CONVERSATION_SYNC_STREAM_POLICY.reconnectGraceMs + 2000);
    expect(sources[0].closed).toBe(true);
    expect(sources).toHaveLength(2);
    expect(sources.filter(source => !source.closed)).toHaveLength(1);
  });

  it('does not let repeated transport errors extend the last-frame deadline', async () => {
    const { coordinator, sources } = fixture();
    await coordinator.resume();
    sources[0].onopen?.();
    for (let index = 0; index < 4; index++) {
      await vi.advanceTimersByTimeAsync(10000);
      sources[0].onerror?.();
    }
    expect(sources[0].closed).toBe(true);
  });

  it('contains projection failure and recovers instead of losing the event cursor', async () => {
    const { coordinator, api, sources, onProtocolError } = fixture({
      onTurnDelta: () => { throw new Error('projection failed'); },
    });
    await coordinator.resume();
    api.snapshot.mockResolvedValue(snapshot(2));
    sources[0].emit('turn.deleted', { syncSeq: 2, occurredAt: 1, payload: { deletedTurnIds: ['old'] } }, 'cursor-2');
    await vi.advanceTimersByTimeAsync(0);
    expect(onProtocolError).toHaveBeenCalledWith(expect.objectContaining({ message: 'projection failed' }));
    expect(api.snapshot).toHaveBeenCalledTimes(2);
    expect(coordinator.cursor).toBe('cursor-2');
  });

  it('isolates health observers from projection and disposal', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const { coordinator, onSnapshot } = fixture({ onHealth: () => { throw new Error('observer failed'); } });
    await coordinator.resume();
    expect(onSnapshot).toHaveBeenCalledTimes(1);
    expect(() => coordinator.close()).not.toThrow();
    expect(conversationConnectionHealth.get('conv')).toBeUndefined();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('claims its reconnect timer before an observer pauses or invalidates', async () => {
    let coordinator!: ConversationSyncCoordinator;
    let failed = false;
    const f = fixture({ onProtocolError: () => { failed = true; }, onHealth: (_id, health) => {
      if (failed && health.state === 'recovering') { coordinator.invalidate(); coordinator.pause(); }
    } });
    coordinator = f.coordinator;
    await coordinator.resume();
    f.api.snapshot.mockRejectedValueOnce(new Error('offline'));
    await expect(coordinator.recover()).rejects.toThrow('offline');
    expect(vi.getTimerCount()).toBe(0);
    expect(f.sources.filter(source => !source.closed)).toHaveLength(0);
  });

  it('does not acknowledge a change after a projection callback starts recovery', async () => {
    let coordinator!: ConversationSyncCoordinator;
    let recovery!: Promise<ConversationSyncSnapshot>;
    const f = fixture({ onTurnDelta: () => { recovery = coordinator.recover(); return true; } });
    coordinator = f.coordinator;
    await coordinator.resume();
    const gate = deferred<ConversationSyncSnapshot>();
    f.api.snapshot.mockImplementationOnce(() => gate.promise);
    f.sources[0].emit('turn.deleted', { syncSeq: 2, occurredAt: 1, payload: {} }, 'cursor-2');
    expect(coordinator.cursor).toBe('cursor-1');
    gate.resolve(snapshot(3)); await recovery;
    expect(coordinator.cursor).toBe('cursor-3');
  });

  it('retries stream construction failures without refetching an accepted snapshot', async () => {
    const source = new Source('/events');
    const factory = vi.fn().mockImplementationOnce(() => { throw new Error('no transport'); })
      .mockReturnValue(source);
    const { coordinator, api } = fixture({ eventSourceFactory: factory });
    await expect(coordinator.resume()).rejects.toThrow('no transport');
    await vi.advanceTimersByTimeAsync(2000);
    expect(coordinator.connected).toBe(true);
    expect(factory).toHaveBeenCalledTimes(2);
    expect(api.snapshot).toHaveBeenCalledTimes(1);
  });
});

describe('owned read teardown', () => {
  it('fences a late snapshot even if a custom read port ignores cancellation', async () => {
    const { coordinator, api, onSnapshot, onProtocolError } = fixture();
    const gate = deferred<ConversationSyncSnapshot>();
    api.snapshot.mockImplementationOnce(() => gate.promise);
    const hydration = coordinator.hydrate();
    const rejected = expect(hydration).rejects.toThrow('closed');
    coordinator.close(); gate.resolve(snapshot(9)); await rejected;
    expect(onSnapshot).not.toHaveBeenCalled();
    expect(onProtocolError).not.toHaveBeenCalled();
    expect(coordinator.cursor).toBe('');
    expect(conversationConnectionHealth.get('conv')).toBeUndefined();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('leaves no live timers or streams after repeated recovery/disposal cycles', async () => {
    for (let index = 0; index < 100; index++) {
      const { coordinator, sources, api } = fixture();
      await coordinator.resume();
      await coordinator.recover();
      coordinator.close(); coordinator.close();
      expect(sources.every(source => source.closed)).toBe(true);
      expect(api.snapshot.mock.calls.every(([, options]) => options?.signal?.aborted)).toBe(true);
      expect(vi.getTimerCount()).toBe(0);
    }
  });

  it('aborts snapshot, history and directory reads and silences their late failures', async () => {
    const { coordinator, api, onProtocolError, sources } = fixture();
    await coordinator.hydrate(false);
    const reads: AbortSignal[] = [];
    const pending = (options: RequestOptions) => new Promise<never>((_resolve, reject) => {
      if (options.signal) {
        reads.push(options.signal);
        options.signal.addEventListener('abort', () => reject(new Error('disposed')), { once: true });
      }
    });
    api.turnPage.mockImplementation((...args: unknown[]) => pending(args[5] as RequestOptions));
    api.roundDirectory.mockImplementation((...args: unknown[]) => pending(args[6] as RequestOptions));
    api.snapshot.mockImplementation((_id, options) => pending(options!));
    const history = coordinator.loadTurnPage('main');
    const directory = coordinator.loadRoundDirectory('main');
    const hydration = coordinator.hydrate();
    const settled = Promise.allSettled([history, directory, hydration]);
    coordinator.close();
    expect(reads).toHaveLength(3);
    expect(reads.every(signal => signal.aborted)).toBe(true);
    expect((await settled).every(result => result.status === 'rejected')).toBe(true);
    expect(onProtocolError).not.toHaveBeenCalled();
    expect(sources).toHaveLength(0);
    expect(vi.getTimerCount()).toBe(0);
  });
});
