// @vitest-environment jsdom
/** Attempt callbacks follow their owned store, independent of catalog retention. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createConversationTurnRuntime, type TurnRuntimeOptions } from './turn-runtime';
import { assertConversationSyncSchema, type ConversationSyncApi, type ConversationSyncSnapshot, type TurnRecord } from '../api/conversation-sync.generated';

class Source extends EventTarget {
  readyState = 1;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  close() { this.readyState = 2; }
}

function turn(status: 'running' | 'completed', revision = 1): TurnRecord {
  return assertConversationSyncSchema<TurnRecord>('TurnRecord', {
    turnId: 'turn', presentationId: 'turn', conversationId: 'conv', laneId: 'main',
    ordinal: 1, actor: 'assistant', kind: 'reply', runId: '', status,
    currentAttemptId: 'attempt', projectionRevision: revision,
    createdAt: 1, updatedAt: revision, settlement: {}, projection: { content: `revision ${revision}` },
  });
}

function snapshot(status: 'running' | 'completed' = 'running', revision = 1): ConversationSyncSnapshot {
  return { ok: true, contract: 'tofu.conversation-sync.snapshot/v1',
    scope: { kind: 'conversation', ownerId: 7, threadId: 'conv' }, conversationId: 'conv',
    conversationRevision: revision, syncSeq: revision, cursor: `cursor-${revision}`,
    serverBootId: 'test', heartbeatIntervalMs: 15000, settings: {}, queueItems: [],
    turns: [turn(status, revision)], attempts: [{ attemptId: 'attempt', turnId: 'turn',
      conversationId: 'conv', commandId: 'command', taskId: 'task', operation: 'generate',
      status, baseProjectionRevision: 1, resumeAnchor: {}, createdAt: 1 }] };
}

const cleanup: Array<() => void> = [];
beforeEach(() => vi.useFakeTimers());
afterEach(() => { cleanup.splice(0).forEach(dispose => dispose()); vi.useRealTimers(); });

function fixture(overrides: Partial<TurnRuntimeOptions> = {}) {
  const api = { snapshot: vi.fn(async () => snapshot()) };
  const sources: Source[] = [];
  const onTurnSettled = vi.fn();
  const onProtocolError = vi.fn();
  const runtime = createConversationTurnRuntime({ api: api as unknown as ConversationSyncApi,
    isActive: () => true, renderState: () => true, onTurnSettled, onProtocolError,
    eventSourceFactory: () => { const s = new Source(); sources.push(s); return s as unknown as EventSource; },
    ...overrides });
  const conversation = { id: 'conv' };
  cleanup.push(() => runtime.disposeConversation('conv'));
  let syncSeq = 1;
  const terminal = (revision = 2) => sources.at(-1)?.onmessage?.(new MessageEvent('message', {
    lastEventId: `cursor-${++syncSeq}`, data: JSON.stringify({
      contract: 'tofu.conversation-sync.event/v1', conversationId: 'conv',
      type: 'attempt.event', syncSeq, occurredAt: revision,
      payload: { event: { conversationId: 'conv', turnId: 'turn', attemptId: 'attempt',
        seq: revision, projectionRevision: revision, type: 'terminal_settlement',
        payload: { status: 'completed', settlement: {}, projection: { content: `revision ${revision}` } } } },
    }),
  }));
  return { runtime, api, sources, conversation, onTurnSettled, onProtocolError, terminal };
}

describe.each(['tombstone', 'snapshot replacement'] as const)('%s attempt retirement', removal => {
  it('forgets live observations even when the removed attempt is absent at disposal', async () => {
    const f = fixture();
    const store = await f.runtime.hydrateConversation(f.conversation);
    store.dispatch({ type: 'snapshot', snapshot: {
      conversationRevision: 2,
      ...(removal === 'tombstone' ? { deletedTurnIds: ['turn'] }
        : { authoritativeFull: true, turns: [], attempts: [] }),
    } });
    expect(Object.keys(store.getState().attemptsById)).toHaveLength(0);
    f.runtime.disposeConversation('conv');
    f.api.snapshot.mockResolvedValue(snapshot('completed', 3));

    await f.runtime.hydrateConversation({ id: 'conv' });

    expect(f.onTurnSettled).not.toHaveBeenCalled();
  });

  it('withdraws removed callbacks before a late terminal frame arrives', async () => {
    const f = fixture();
    const store = await f.runtime.hydrateConversation(f.conversation);
    const hook = vi.fn();
    f.runtime.connect(store, 'attempt', { onTerminal: hook });
    store.dispatch({ type: 'snapshot', snapshot: {
      conversationRevision: 2,
      ...(removal === 'tombstone' ? { deletedTurnIds: ['turn'] }
        : { authoritativeFull: true, turns: [], attempts: [] }),
    } });
    f.api.snapshot.mockResolvedValue({ ...snapshot('completed', 3), turns: [], attempts: [] });

    f.terminal(3);

    expect(hook).not.toHaveBeenCalled();
  });
});

it('an older lease cannot withdraw the current callback for the same attempt', async () => {
  const f = fixture();
  const store = await f.runtime.hydrateConversation(f.conversation);
  const oldHook = vi.fn();
  const currentHook = vi.fn();
  const oldLease = f.runtime.connect(store, 'attempt', { onTerminal: oldHook });
  f.runtime.connect(store, 'attempt', { onTerminal: currentHook });

  oldLease.close(); oldLease.close();
  f.terminal();

  expect(store.getState().turnsById.turn?.status).toBe('completed');
  expect(oldHook).not.toHaveBeenCalled();
  expect(currentHook).toHaveBeenCalledTimes(1);
});

it('an old-view lease cannot withdraw a reopened view callback', async () => {
  const f = fixture();
  const oldStore = await f.runtime.hydrateConversation(f.conversation);
  const oldHook = vi.fn();
  const oldLease = f.runtime.connect(oldStore, 'attempt', { onTerminal: oldHook });
  f.runtime.disposeConversation('conv');
  const currentStore = await f.runtime.hydrateConversation({ id: 'conv' });
  const currentHook = vi.fn();
  f.runtime.connect(currentStore, 'attempt', { onTerminal: currentHook });

  oldLease.close();
  f.terminal();

  expect(currentStore.getState().turnsById.turn?.status).toBe('completed');
  expect(oldHook).not.toHaveBeenCalled();
  expect(currentHook).toHaveBeenCalledTimes(1);
});

it('rejects a connection through a retired store after its conversation reopens', async () => {
  const f = fixture();
  const oldStore = await f.runtime.hydrateConversation(f.conversation);
  f.runtime.disposeConversation('conv');
  await f.runtime.hydrateConversation({ id: 'conv' });
  expect(() => f.runtime.connect(oldStore, 'attempt')).toThrow(/disposed|retired/);
});

it('preserves a replacement registered during a terminal callback', async () => {
  const f = fixture();
  const store = await f.runtime.hydrateConversation(f.conversation);
  const nextHook = vi.fn();
  const firstHook = vi.fn(() => f.runtime.connect(store, 'attempt', { onTerminal: nextHook }));
  f.runtime.connect(store, 'attempt', { onTerminal: firstHook });

  f.terminal(2);
  f.terminal(3);

  expect(firstHook).toHaveBeenCalledTimes(1);
  expect(store.getState().turnsById.turn?.projectionRevision).toBe(3);
  expect(nextHook).toHaveBeenCalledTimes(1);
});

it('withdraws a throwing terminal callback while preserving the protocol error', async () => {
  const f = fixture();
  const store = await f.runtime.hydrateConversation(f.conversation);
  const failure = new Error('injected terminal hook failure');
  const hook = vi.fn(() => { throw failure; });
  f.runtime.connect(store, 'attempt', { onTerminal: hook });
  f.api.snapshot.mockResolvedValue(snapshot('completed', 2));

  f.terminal(2);
  await vi.waitFor(() => expect(f.sources).toHaveLength(2));
  f.terminal(3);

  expect(store.getState().turnsById.turn?.projectionRevision).toBe(3);
  expect(hook).toHaveBeenCalledTimes(1);
  expect(f.onProtocolError).toHaveBeenCalledExactlyOnceWith(failure);
});

it('closing a callback lease leaves the shared conversation transport running', async () => {
  const f = fixture();
  const store = await f.runtime.hydrateConversation(f.conversation);
  const hook = vi.fn();
  const connection = f.runtime.connect(store, 'attempt', { onTerminal: hook });

  connection.close(); connection.close();
  f.terminal(2);

  expect(hook).not.toHaveBeenCalled();
  expect(store.getState().turnsById.turn?.status).toBe('completed');
  expect(f.sources).toHaveLength(1);
  expect(f.sources[0]?.readyState).toBe(1);
});

it('keeps one settlement observation across a shell replacement', async () => {
  const f = fixture();
  await f.runtime.hydrateConversation(f.conversation);
  const replacement = { id: 'conv', title: 'new shell' };
  await f.runtime.wakeConversation(replacement);

  f.terminal();

  expect(f.onTurnSettled).toHaveBeenCalledTimes(1);
  expect(f.onTurnSettled.mock.calls[0]?.[0]).toBe(replacement);
});

it('retains a caller subscription until its future attempt is materialized', async () => {
  const f = fixture();
  f.api.snapshot.mockResolvedValue({ ...snapshot(), turns: [], attempts: [] });
  const store = await f.runtime.hydrateConversation(f.conversation);
  const hook = vi.fn();
  f.runtime.connect(store, 'attempt', { onTerminal: hook });
  store.dispatch({ type: 'command_response', response: { conversationRevision: 2 } });
  store.dispatch({ type: 'snapshot', snapshot: snapshot('running', 2) });

  f.terminal(3);

  expect(store.getState().turnsById.turn?.status).toBe('completed');
  expect(hook).toHaveBeenCalledTimes(1);
});
