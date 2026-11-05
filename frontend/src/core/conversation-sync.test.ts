// @vitest-environment jsdom
/** Recovery and disposal specifications for the single conversation sync owner. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ConversationSyncCoordinator, type ConversationSyncCoordinatorOptions } from './conversation-sync';
import { conversationConnectionHealth } from './connection-health';
import {
  CONVERSATION_SYNC_STREAM_POLICY,
  conversationSyncApi,
  type ConversationSyncApi, type ConversationSyncSnapshot,
} from '../api/conversation-sync.generated';
import type { RequestOptions } from '../api/transport';
import { conversationLoadBudget } from './conversation-load-budget';
import type { ConversationStreamRequest } from './conversation-event-multiplexer';

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
  constructor(readonly request: ConversationStreamRequest) { super(); }
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
    turnPage: vi.fn(), roundDirectory: vi.fn(),
  };
  const onSnapshot = vi.fn();
  const onProtocolError = vi.fn();
  const coordinator = new ConversationSyncCoordinator({
    conversationId: 'conv', api: api as unknown as ConversationSyncApi,
    onSnapshot, onProtocolError, onAttemptEvent: () => true, onTurnDelta: () => true,
    eventSourceFactory: request => {
      const source = new Source(request); sources.push(source);
      return source;
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
    expect(sources[1].request.after).toBe('cursor-9');
  });

  it('recovers a failed initial hydration without opening an uninitialized stream', async () => {
    const { coordinator, api, sources } = fixture();
    api.snapshot.mockRejectedValueOnce(new Error('offline'));
    await expect(coordinator.resume()).rejects.toThrow('offline');
    expect(sources).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(2000);
    expect(api.snapshot).toHaveBeenCalledTimes(2);
    expect(sources).toHaveLength(1);
    expect(sources[0].request.after).toBe('cursor-1');
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

  it('refuses history pages, but ranks wait for a pending replacement snapshot', async () => {
    const { coordinator, api } = fixture();
    await coordinator.hydrate(false);
    const gate = deferred<ConversationSyncSnapshot>();
    api.snapshot.mockImplementationOnce(() => gate.promise);
    const recovery = coordinator.recover();
    await expect(coordinator.loadTurnPage('main')).rejects.toThrow('authoritative snapshot');
    api.roundDirectory.mockImplementation(async (_id, laneId, syncSeq, offset) => ({
      ok: true, ownerId: 7, conversationId: 'conv', laneId, syncSeq, stale: false,
      totalRounds: 0, offset, entries: [] }));
    const ranks = coordinator.loadRoundDirectory('main');
    await vi.advanceTimersByTimeAsync(0);
    expect(api.turnPage).not.toHaveBeenCalled();
    expect(api.roundDirectory).not.toHaveBeenCalled();
    gate.resolve(snapshot(9)); await recovery;
    await expect(ranks).resolves.toMatchObject({ syncSeq: 9 });
    expect(api.roundDirectory.mock.calls[0].slice(0, 3)).toEqual(['conv', 'main', 9]);
  });

  it('reports a failed replacement to a waiting rank reader', async () => {
    const { coordinator, api } = fixture();
    await coordinator.hydrate(false);
    api.snapshot.mockRejectedValueOnce(new Error('offline'));
    const recovery = coordinator.recover().catch(() => undefined);
    await expect(coordinator.loadRoundDirectory('main')).rejects.toThrow('active snapshot');
    await recovery;
    expect(api.roundDirectory).not.toHaveBeenCalled();
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

/** A streamed attempt event: advances the replay head; related Turns, if any,
 * are Turn changes. */
const tick = (syncSeq: number, related: object[] = []) => ({ syncSeq, occurredAt: 1, payload: {
  event: { conversationId: 'conv', turnId: 'live', attemptId: 'attempt-live', seq: syncSeq,
    projectionRevision: syncSeq, type: 'projection_updated', payload: {
      projectionPatch: { version: 1, baseRevision: syncSeq - 1, targetRevision: syncSeq, operations: [] },
      ...(related.length ? { turns: related } : {}) } } } });

describe('round directory under a streaming head', () => {
  const ranks = (syncSeq: number, extra: object = {}) => ({ ok: true, ownerId: 7,
    conversationId: 'conv', laneId: 'main', syncSeq, stale: false, totalRounds: 3, offset: 0,
    entries: [], ...extra });

  it('keeps ranks the server answered at a newer head while events advance this replica', async () => {
    const { coordinator, api, sources } = fixture();
    await coordinator.resume();
    const read = deferred<unknown>();
    api.roundDirectory.mockImplementationOnce(() => read.promise);
    const flight = coordinator.loadRoundDirectory('main', 0, 50);
    await vi.advanceTimersByTimeAsync(0);
    expect(api.roundDirectory.mock.calls[0].slice(0, 5)).toEqual(['conv', 'main', 1, 0, 50]);
    sources[0].emit('attempt.event', tick(2), 'cursor-2');
    // The server observed a head this replica has not replayed yet.
    read.resolve(ranks(3));
    await expect(flight).resolves.toMatchObject({ syncSeq: 3, totalRounds: 3 });
  });

  it('treats Turns an attempt announces as a rank change', async () => {
    const { coordinator, api, sources } = fixture();
    await coordinator.resume();
    const read = deferred<unknown>();
    api.roundDirectory.mockImplementationOnce(() => read.promise);
    const before = coordinator.roundDirectoryRevision;
    const flight = expect(coordinator.loadRoundDirectory('main')).rejects.toThrow('stale or mismatched');
    await vi.advanceTimersByTimeAsync(0);
    sources[0].emit('attempt.event', tick(2, [{ turnId: 'critic-1', presentationId: 'critic-1',
      conversationId: 'conv', laneId: 'main', ordinal: 9, actor: 'critic', kind: 'input', runId: '',
      status: 'completed', projectionRevision: 1, currentAttemptId: null, createdAt: 1, updatedAt: 1,
      settlement: { outcome: 'completed' }, projection: { content: 'review' } }]), 'cursor-2');
    expect(coordinator.roundDirectoryRevision).not.toBe(before);
    read.resolve(ranks(2));
    await flight;
  });

  it('rejects ranks that a rank change overtook, or that predate or refuse the head', async () => {
    const { coordinator, api, sources } = fixture();
    await coordinator.resume();
    const read = deferred<unknown>();
    api.roundDirectory.mockImplementationOnce(() => read.promise);
    const overtaken = expect(coordinator.loadRoundDirectory('main')).rejects.toThrow('stale or mismatched');
    await vi.advanceTimersByTimeAsync(0);
    sources[0].emit('turn.deleted', { syncSeq: 2, occurredAt: 1,
      payload: { conversationRevision: 2, deletedTurnIds: ['old'] } }, 'cursor-2');
    read.resolve(ranks(2));
    await overtaken;
    api.roundDirectory.mockResolvedValueOnce(ranks(1));
    await expect(coordinator.loadRoundDirectory('main')).rejects.toThrow('stale or mismatched');
    api.roundDirectory.mockResolvedValueOnce(ranks(9, { stale: true }));
    await expect(coordinator.loadRoundDirectory('main')).rejects.toThrow('stale or mismatched');
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

  const stale = (head: number) => Object.assign(new Error('HTTP 409'),
    { status: 409, body: { ok: false, currentSyncSeq: head } });

  it('waits for the live stream to deliver a reported head instead of replacing the projection', async () => {
    const onTurnPage = vi.fn();
    const { coordinator, api, sources } = fixture({ onTurnPage });
    await coordinator.resume();
    api.turnPage.mockRejectedValueOnce(stale(3))
      .mockImplementation(async (_id, _lane, syncSeq, before) => page(syncSeq, before));
    const loaded = coordinator.loadTurnPage('main', 40, 8, 'detached');
    await vi.advanceTimersByTimeAsync(0);
    expect(api.turnPage).toHaveBeenCalledTimes(1);
    sources[0].emit('attempt.event', tick(2), 'cursor-2');
    await vi.advanceTimersByTimeAsync(0);
    expect(api.turnPage).toHaveBeenCalledTimes(1);
    sources[0].emit('attempt.event', tick(3), 'cursor-3');
    await expect(loaded).resolves.toMatchObject({ syncSeq: 3 });
    expect(api.turnPage.mock.calls[1].slice(2, 4)).toEqual([3, 40]);
    expect(api.snapshot).toHaveBeenCalledTimes(1);
    expect(onTurnPage).toHaveBeenCalledTimes(1);
  });

  it('repairs from a snapshot when the live stream never delivers the head', async () => {
    const { coordinator, api } = fixture({ onTurnPage: vi.fn() });
    await coordinator.resume();
    api.snapshot.mockResolvedValue(snapshot(5));
    api.turnPage.mockRejectedValueOnce(stale(5))
      .mockImplementation(async (_id, _lane, syncSeq, before) => page(syncSeq, before));
    const loaded = coordinator.loadTurnPage('main', 40, 8, 'detached');
    await vi.advanceTimersByTimeAsync(1999);
    expect(api.snapshot).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(1);
    await expect(loaded).resolves.toMatchObject({ syncSeq: 5 });
    expect(api.snapshot).toHaveBeenCalledTimes(2);
  });

  it('publishes a continuous page across streamed attempt events, re-reading only after a Turn change', async () => {
    const onTurnPage = vi.fn();
    const { coordinator, api, sources } = fixture({ onTurnPage });
    await coordinator.resume();
    const first = deferred<ReturnType<typeof page>>();
    api.turnPage.mockImplementationOnce(() => first.promise)
      .mockImplementation(async (_id, _lane, syncSeq, before) => page(syncSeq, before));
    const streamed = coordinator.loadTurnPage('main', 7, 64);
    await vi.advanceTimersByTimeAsync(0);
    sources[0].emit('attempt.event', tick(2), 'cursor-2');
    first.resolve(page(1, 7));
    await expect(streamed).resolves.toMatchObject({ syncSeq: 1 });
    expect(api.turnPage).toHaveBeenCalledTimes(1);

    const second = deferred<ReturnType<typeof page>>();
    api.turnPage.mockImplementationOnce(() => second.promise);
    const changed = coordinator.loadTurnPage('main', 7, 64);
    await vi.advanceTimersByTimeAsync(0);
    sources[0].emit('turn.deleted', { syncSeq: 3, occurredAt: 1,
      payload: { conversationRevision: 3, deletedTurnIds: ['old'] } }, 'cursor-3');
    second.resolve(page(2, 7));
    await expect(changed).resolves.toMatchObject({ syncSeq: 3 });
    expect(onTurnPage.mock.calls.map(([published]) => published.syncSeq)).toEqual([1, 3]);
    expect(api.snapshot).toHaveBeenCalledTimes(1);
  });

  it.each(['continuous', 'detached'] as const)(
    'publishes a %s page read ahead of this replica when the stream applies exactly that head', async mode => {
      const cursors: string[] = [];
      let coordinator!: ConversationSyncCoordinator;
      const f = fixture({ onTurnPage: () => { cursors.push(coordinator.cursor); } });
      coordinator = f.coordinator;
      await coordinator.resume();
      f.api.turnPage.mockImplementation(async (_id, _lane, _syncSeq, before) => page(3, before));
      const loaded = coordinator.loadTurnPage('main', 7, 64, mode);
      await vi.advanceTimersByTimeAsync(0);
      // Each EventSource frame is its own task; flush between them.
      f.sources[0].emit('attempt.event', tick(2), 'cursor-2');
      await vi.advanceTimersByTimeAsync(0);
      expect(cursors).toEqual([]);
      // A Turn change the page already includes is not an overtake.
      f.sources[0].emit('turn.deleted', { syncSeq: 3, occurredAt: 1,
        payload: { conversationRevision: 3, deletedTurnIds: ['old'] } }, 'cursor-3');
      await vi.advanceTimersByTimeAsync(0);
      await expect(loaded).resolves.toMatchObject({ syncSeq: 3 });
      expect(cursors).toEqual(['cursor-3']);
      expect(f.api.turnPage).toHaveBeenCalledTimes(1);
      expect(f.api.snapshot).toHaveBeenCalledTimes(1);
    });

  it('re-reads a page read ahead of this replica when a Turn change follows its head', async () => {
    const onTurnPage = vi.fn();
    const { coordinator, api, sources } = fixture({ onTurnPage });
    await coordinator.resume();
    api.turnPage.mockImplementationOnce(async (_id, _lane, _syncSeq, before) => page(3, before))
      .mockImplementation(async (_id, _lane, syncSeq, before) => page(syncSeq, before));
    const loaded = coordinator.loadTurnPage('main', 7, 64);
    await vi.advanceTimersByTimeAsync(0);
    // Frames already queued apply before the page's head check runs.
    sources[0].emit('attempt.event', tick(2), 'cursor-2');
    sources[0].emit('attempt.event', tick(3), 'cursor-3');
    sources[0].emit('turn.deleted', { syncSeq: 4, occurredAt: 1,
      payload: { conversationRevision: 4, deletedTurnIds: ['old'] } }, 'cursor-4');
    await expect(loaded).resolves.toMatchObject({ syncSeq: 4 });
    expect(api.turnPage.mock.calls.map(call => call[2])).toEqual([1, 4]);
    expect(onTurnPage.mock.calls.map(([published]) => published.syncSeq)).toEqual([4]);
    expect(api.snapshot).toHaveBeenCalledTimes(1);
  });

  it('loads older history while a streaming reply commits past every read', async () => {
    // The server answers at its current head, which a live reply keeps two
    // commits ahead of what this replica has replayed when the read lands.
    const onTurnPage = vi.fn();
    const { coordinator, api, sources } = fixture({ onTurnPage });
    await coordinator.resume();
    let committed = 1;
    const stream = setInterval(() => {
      committed += 1;
      const head = committed;
      setTimeout(() => sources[0].emit('attempt.event', tick(head), `cursor-${head}`), 250);
    }, 100);
    api.turnPage.mockImplementation((_id, _lane, syncSeq, before) => new Promise(resolve => {
      setTimeout(() => resolve(page(Math.max(syncSeq, committed), before)), 150);
    }));
    await vi.advanceTimersByTimeAsync(1000);
    const loaded = coordinator.loadTurnPage('main', 7, 64);
    await vi.advanceTimersByTimeAsync(1000);
    clearInterval(stream);
    const published = await loaded;
    expect(api.turnPage).toHaveBeenCalledTimes(1);
    expect(published.syncSeq).toBeGreaterThan(api.turnPage.mock.calls[0][2] as number);
    expect(onTurnPage).toHaveBeenCalledTimes(1);
    expect(api.snapshot).toHaveBeenCalledTimes(1);
  });

  it('repairs from a snapshot when no live stream can deliver the head a page was read at', async () => {
    const onTurnPage = vi.fn();
    const { coordinator, api } = fixture({ onTurnPage });
    await coordinator.hydrate(false);
    api.snapshot.mockResolvedValue(snapshot(4));
    api.turnPage.mockImplementation(async (_id, _lane, _syncSeq, before) => page(4, before));
    await expect(coordinator.loadTurnPage('main', 7, 64)).resolves.toMatchObject({ syncSeq: 4 });
    expect(api.snapshot).toHaveBeenCalledTimes(2);
    expect(api.turnPage.mock.calls.map(call => call[2])).toEqual([1, 4]);
    expect(onTurnPage).toHaveBeenCalledTimes(1);
  });

  it('never publishes a page answered at a head older than its request', async () => {
    const { coordinator, api, sources } = fixture({ onTurnPage: vi.fn() });
    await coordinator.resume();
    sources[0].emit('attempt.event', tick(2), 'cursor-2');
    api.turnPage.mockImplementation(async (_id, _lane, _syncSeq, before) => page(1, before));
    await expect(coordinator.loadTurnPage('main', 7, 64)).rejects.toThrow('stale');
  });

  it('publishes a detached page across streamed attempt events, not across a Turn change', async () => {
    const onTurnPage = vi.fn();
    const { coordinator, api, sources } = fixture({ onTurnPage });
    await coordinator.resume();
    const first = deferred<ReturnType<typeof page>>();
    api.turnPage.mockImplementationOnce(() => first.promise)
      .mockImplementation(async (_id, _lane, syncSeq, before) => page(syncSeq, before));
    const streamed = coordinator.loadTurnPage('main', 40, 8, 'detached');
    await vi.advanceTimersByTimeAsync(0);
    sources[0].emit('attempt.event', tick(2), 'cursor-2');
    first.resolve(page(1, 40));
    await expect(streamed).resolves.toMatchObject({ syncSeq: 1 });
    expect(api.turnPage).toHaveBeenCalledTimes(1);

    const second = deferred<ReturnType<typeof page>>();
    api.turnPage.mockImplementationOnce(() => second.promise);
    const changed = coordinator.loadTurnPage('main', 20, 8, 'detached');
    await vi.advanceTimersByTimeAsync(0);
    sources[0].emit('turn.deleted', { syncSeq: 3, occurredAt: 1,
      payload: { conversationRevision: 3, deletedTurnIds: ['old'] } }, 'cursor-3');
    second.resolve(page(2, 20));
    await expect(changed).resolves.toMatchObject({ syncSeq: 3 });
    expect(onTurnPage.mock.calls.map(([published]) => published.syncSeq)).toEqual([1, 3]);
  });

  it('holds one continuous and one detached flight, so a jump never collides with the window', async () => {
    const { coordinator, api } = fixture({ onTurnPage: vi.fn() });
    await coordinator.hydrate(false);
    const window = deferred<ReturnType<typeof page>>();
    const jump = deferred<ReturnType<typeof page>>();
    api.turnPage.mockImplementationOnce(() => window.promise).mockImplementationOnce(() => jump.promise);
    const continuous = coordinator.loadTurnPage('main', 7, 64);
    const detached = coordinator.loadTurnPage('main', 40, 8, 'detached');
    await expect(coordinator.loadTurnPage('main', 3, 64)).rejects.toThrow('already loading');
    const sameJump = coordinator.loadTurnPage('main', 40, 8, 'detached');
    await vi.advanceTimersByTimeAsync(0);
    expect(api.turnPage).toHaveBeenCalledTimes(2);
    jump.resolve(page(1, 40)); window.resolve(page(1, 7));
    const [, jumped, shared] = await Promise.all([continuous, detached, sameJump]);
    expect(shared).toBe(jumped);
  });
});
describe('catch-up budget', () => {
  function budgetFixture() {
    const viewport = { width: 1920, height: 1200 };
    const built = fixture({ snapshotViewport: () => viewport });
    return { ...built, windowBytes: conversationLoadBudget(viewport).windowBytes };
  }

  it('declares the snapshot window on every stream and marks only snapshot cursors', async () => {
    const { coordinator, sources, windowBytes } = budgetFixture();
    await coordinator.hydrate();
    expect(sources[0].request).toEqual({
      conversationId: 'conv', after: 'cursor-1', windowBytes, cursorSource: 'snapshot' });
    sources[0].emit('turn.deleted', {
      syncSeq: 2, occurredAt: 1, payload: { deletedTurnIds: ['old'] } }, 'cursor-2');
    coordinator.pause();
    await coordinator.wake();
    expect(sources[1].request).toEqual({ conversationId: 'conv', after: 'cursor-2', windowBytes });
  });

  it('re-anchors an over-budget catch-up with one snapshot instead of replaying it', async () => {
    const { coordinator, api, sources, onProtocolError } = budgetFixture();
    await coordinator.hydrate();
    coordinator.pause();
    await coordinator.wake();
    api.snapshot.mockResolvedValueOnce(snapshot(40));
    sources[1].emit('sync.reset_required', {
      cursor: 'cursor-40', reason: 'replay_exceeds_window' }, 'cursor-40');
    await vi.advanceTimersByTimeAsync(0);
    expect(onProtocolError).not.toHaveBeenCalled();
    expect(api.snapshot).toHaveBeenCalledTimes(2);
    expect(sources[1].closed).toBe(true);
    expect(sources).toHaveLength(3);
    expect(coordinator.cursor).toBe('cursor-40');
    expect(sources[2].request).toMatchObject({ after: 'cursor-40', cursorSource: 'snapshot' });
  });
});
describe('transport liveness and observer isolation', () => {
  it('requests state replay without inline Inspector bodies and preserves cursor identity', () => {
    const url = new URL(conversationSyncApi.eventsUrl('conv', 'cursor +/1', 'page', 7), 'http://localhost');
    expect(url.searchParams.get('diagnosticPayload')).toBe('none');
    expect(url.searchParams.get('after')).toBe('cursor +/1');
    expect(url.searchParams.get('streamClientId')).toBe('page');
    expect(url.searchParams.get('streamGeneration')).toBe('7');
    expect(url.searchParams.has('windowBytes')).toBe(false);
    expect(url.searchParams.has('cursorSource')).toBe(false);
    const budgeted = new URL(conversationSyncApi.eventsUrl(
      'conv', 'c', 'page', 7, { windowBytes: 65536, cursorSource: 'snapshot' }), 'http://localhost');
    expect(budgeted.searchParams.get('windowBytes')).toBe('65536');
    expect(budgeted.searchParams.get('cursorSource')).toBe('snapshot');
  });
  it.each(['wake', 'invalidate'] as const)(
    '%s keeps an in-flight stream until the contract silence deadline', async action => {
      const { coordinator, sources, api } = fixture();
      await coordinator.resume();
      sources[0].emit('sync.heartbeat', { cursor: 'cursor-1', serverBootId: 'boot', degraded: false });
      await vi.advanceTimersByTimeAsync(CONVERSATION_SYNC_STREAM_POLICY.reconnectGraceMs / 2 + 1);
      await coordinator[action]();
      await vi.advanceTimersByTimeAsync(0);
      expect(sources[0].closed).toBe(false);
      expect(sources).toHaveLength(1);
      sources[0].emit('turn.deleted', { syncSeq: 2, occurredAt: 1, payload: { deletedTurnIds: ['old'] } }, 'cursor-2');
      expect(coordinator.cursor).toBe('cursor-2');
      expect(api.snapshot).toHaveBeenCalledTimes(1);
    },
  );

  it.each(['wake', 'invalidate'] as const)(
    '%s does not restart a replacement on the previous generation frame clock', async action => {
      const { coordinator, sources } = fixture();
      await coordinator.resume();
      sources[0].emit('sync.heartbeat', { cursor: 'cursor-1', serverBootId: 'boot', degraded: false });
      await vi.advanceTimersByTimeAsync(CONVERSATION_SYNC_STREAM_POLICY.reconnectGraceMs + 1000);
      expect(sources).toHaveLength(2);
      sources[1].onerror?.();
      await coordinator[action]();
      await vi.advanceTimersByTimeAsync(0);
      expect(sources[1].closed).toBe(false);
      expect(sources).toHaveLength(2);
    },
  );

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
    expect(sources[1].request.after).toBe('cursor-9');

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
    const source = new Source({ conversationId: 'conv', after: 'cursor-1' });
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

describe('network badge aggregate', () => {
  const aggregateCount = () => conversationConnectionHealth.aggregate().count;

  it('holds a stalled stream degraded through its reconnect until a valid frame arrives', async () => {
    const { coordinator, sources } = fixture();
    await coordinator.resume();
    sources[0].emit('sync.heartbeat', { cursor: 'cursor-1', serverBootId: 'boot', degraded: false });
    expect(aggregateCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(CONVERSATION_SYNC_STREAM_POLICY.reconnectGraceMs);
    expect(sources[0].closed).toBe(true);
    expect(conversationConnectionHealth.get('conv'))
      .toMatchObject({ state: 'degraded', reason: 'heartbeat-timeout' });
    expect(aggregateCount()).toBe(1);
    await vi.advanceTimersByTimeAsync(1000);
    expect(sources).toHaveLength(2);
    sources[1].onopen?.();
    sources[1].onerror?.();
    expect(conversationConnectionHealth.get('conv')?.state).toBe('degraded');
    expect(aggregateCount()).toBe(1);
    sources[1].emit('sync.heartbeat', { cursor: 'cursor-1', serverBootId: 'boot', degraded: false });
    expect(conversationConnectionHealth.get('conv')?.state).toBe('live');
    expect(aggregateCount()).toBe(0);
  });

  it('never counts degradation a delivered heartbeat reports as a network fault', async () => {
    const { coordinator, sources } = fixture();
    await coordinator.resume();
    sources[0].emit('sync.heartbeat', { cursor: 'cursor-1', serverBootId: 'boot', degraded: true });
    expect(conversationConnectionHealth.get('conv'))
      .toMatchObject({ state: 'degraded', reason: 'storage-read-degraded' });
    expect(aggregateCount()).toBe(0);
    sources[0].emit('sync.heartbeat', {
      cursor: 'cursor-1', serverBootId: 'boot', degraded: true, pushWithheld: true,
    });
    expect(conversationConnectionHealth.get('conv'))
      .toMatchObject({ state: 'degraded', reason: 'storage-write-wedged' });
    expect(aggregateCount()).toBe(0);
  });

  it.each(['pause', 'suspend'] as const)('releases a stall verdict on %s', async action => {
    const { coordinator } = fixture();
    await coordinator.resume();
    await vi.advanceTimersByTimeAsync(CONVERSATION_SYNC_STREAM_POLICY.reconnectGraceMs);
    expect(aggregateCount()).toBe(1);
    if (action === 'suspend') coordinator.setSuspended(true);
    else coordinator.pause();
    expect(conversationConnectionHealth.get('conv')?.state).toBe('idle');
    expect(aggregateCount()).toBe(0);
    if (action === 'suspend') coordinator.setSuspended(false);
    else await coordinator.resume();
    expect(conversationConnectionHealth.get('conv')?.state).toBe('connecting');
    expect(aggregateCount()).toBe(0);
  });
});

describe('heartbeat acknowledgement integrity', () => {
  it.each(['pause', 'close', 'suspend'] as const)('withdraws remaining heartbeat publication after reentrant %s', async action => {
    const onHealth = vi.fn();
    const onPushWithheld = vi.fn();
    const { coordinator, sources } = fixture({ onHealth, onPushWithheld });
    await coordinator.resume();
    onPushWithheld.mockClear();
    onHealth.mockImplementation((_id, _health, demand) => {
      if (demand !== 'heartbeat') return;
      if (action === 'suspend') coordinator.setSuspended(true);
      else coordinator[action]();
    });
    sources[0].emit('sync.heartbeat', {
      cursor: 'cursor-1', serverBootId: 'boot', degraded: false, pushWithheld: true,
    });
    expect(onPushWithheld).not.toHaveBeenCalled();
    expect(sources[0].closed).toBe(true);
    expect(coordinator.connected).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each([false, true])('publishes only the final heartbeat verdict (degraded=%s)', async degraded => {
    const onHealth = vi.fn();
    const { coordinator, sources } = fixture({ onHealth });
    await coordinator.resume();
    onHealth.mockClear();
    sources[0].emit('sync.heartbeat', { cursor: 'cursor-1', serverBootId: 'boot', degraded });
    expect(onHealth).toHaveBeenCalledTimes(1);
    expect(onHealth).toHaveBeenLastCalledWith('conv', expect.objectContaining({
      state: degraded ? 'degraded' : 'live',
      reason: degraded ? 'storage-read-degraded' : undefined,
    }), 'heartbeat');
  });

  it('recovers a heartbeat cursor that is ahead of the locally applied projection', async () => {
    const { coordinator, api, sources } = fixture();
    await coordinator.resume();
    api.snapshot.mockResolvedValue(snapshot(2));
    sources[0].emit('sync.heartbeat', { cursor: 'cursor-2', serverBootId: 'boot', degraded: false });
    expect(coordinator.cursor).toBe('cursor-1');
    await vi.advanceTimersByTimeAsync(0);
    expect(api.snapshot).toHaveBeenCalledTimes(2);
    expect(coordinator.cursor).toBe('cursor-2');
    expect(sources[1].request.after).toBe('cursor-2');
  });

  it('a matching heartbeat does not read or advance projection authority', async () => {
    const { coordinator, api, sources } = fixture();
    await coordinator.resume();
    sources[0].emit('sync.heartbeat', { cursor: 'cursor-1', serverBootId: 'boot', degraded: false });
    await vi.advanceTimersByTimeAsync(0);
    expect(api.snapshot).toHaveBeenCalledTimes(1);
    expect(coordinator.cursor).toBe('cursor-1');
    expect(conversationConnectionHealth.get('conv')?.state).toBe('live');
  });

  it('opening a socket does not invent receipt of a valid frame', async () => {
    const { coordinator, sources } = fixture();
    await coordinator.resume();
    sources[0].onopen?.();
    expect(conversationConnectionHealth.get('conv')?.lastFrameAt).toBeUndefined();
    expect(conversationConnectionHealth.get('conv')?.state).toBe('connecting');
    sources[0].emit('sync.heartbeat', { cursor: 'cursor-1', serverBootId: 'boot', degraded: false });
    expect(conversationConnectionHealth.get('conv')?.lastFrameAt).toBe(Date.now());
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
