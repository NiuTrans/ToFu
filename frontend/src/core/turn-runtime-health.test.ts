// @vitest-environment jsdom
/** Healthy frames refresh evidence, not receiptless catalog/Surface demand. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createConversationTurnRuntime, type RuntimeConversation } from './turn-runtime';
import { conversationConnectionHealth } from './connection-health';
import { createConversationSurfaceController } from '../conversation/application/conversation-surface-controller';
import type { ConversationSyncApi, ConversationSyncSnapshot } from '../api/conversation-sync.generated';

class Source extends EventTarget {
  readyState = 1;
  closed = false;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  close() { this.closed = true; this.readyState = 2; }
  emit(type: string, payload: object, cursor = '') {
    this.dispatchEvent(new MessageEvent(type, { lastEventId: cursor, data: JSON.stringify({
      contract: 'tofu.conversation-sync.event/v1', conversationId: 'health-test', type, ...payload,
    }) }));
  }
}

const runtimes: ReturnType<typeof createConversationTurnRuntime>[] = [];
const turn = {
  turnId: 'turn', presentationId: 'turn', conversationId: 'health-test', laneId: 'main',
  ordinal: 1, actor: 'assistant' as const, kind: 'text', runId: '', status: 'running' as const,
  projection: { content: 'initial' }, projectionRevision: 1, settlement: {}, createdAt: 1, updatedAt: 1,
};
function fixture(hasFinder = true) {
  const conversation: RuntimeConversation = { id: 'health-test', title: 'Synthetic conversation' };
  const snapshot: ConversationSyncSnapshot = {
    ok: true, contract: 'tofu.conversation-sync.snapshot/v1',
    scope: { kind: 'conversation', ownerId: 7, threadId: 'health-test' },
    conversationId: 'health-test', conversationRevision: 1, syncSeq: 1, cursor: 'cursor-1',
    serverBootId: 'boot', heartbeatIntervalMs: 15000, settings: {}, turns: [turn], attempts: [], queueItems: [],
  };
  const api = { snapshot: vi.fn(async () => snapshot) };
  const sources: Source[] = [];
  const onHealth = vi.fn(); const renderState = vi.fn(() => true);
  let domStale = false;
  const runtime = createConversationTurnRuntime({
    api: api as unknown as ConversationSyncApi,
    ...(hasFinder ? {findConversation: (id: string) => id === conversation.id ? conversation : null} : {}),
    isActive: () => true, isDomStale: () => domStale, onHealth, renderState,
    eventSourceFactory: () => { const source = new Source(); sources.push(source); return source as unknown as EventSource; },
  });
  runtimes.push(runtime);
  return { runtime, conversation, sources, onHealth, renderState, api,
    setDomStale: (value: boolean) => { domStale = value; } };
}

beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(100000); });
afterEach(() => {
  runtimes.splice(0).forEach(runtime => runtime.disposeConversation('health-test'));
  vi.useRealTimers(); vi.restoreAllMocks();
});

describe('stream health visual demand', () => {
  it('passes ownerless initial health through and seeds diagnostics before first bound paint', async () => {
    const { runtime, conversation, sources, onHealth, renderState } = fixture(false);
    runtime.ensureRuntimeStore(conversation.id);
    expect(onHealth).toHaveBeenCalledWith(conversation.id, expect.objectContaining({state: 'idle'}));
    await runtime.hydrateConversation(conversation);
    expect(conversation._conversationSyncHealth).toBeDefined();
    expect(renderState).toHaveBeenCalled();
    sources[0].onopen?.(); onHealth.mockClear();
    expect(conversation._conversationSyncHealth?.state).toBe('connecting');
    expect(conversation._conversationSyncHealth?.lastFrameAt).toBeUndefined();
    vi.setSystemTime(100100);
    sources[0].emit('sync.heartbeat', {cursor: 'cursor-1', serverBootId: 'boot', degraded: false});
    expect(conversation._conversationSyncHealth?.lastFrameAt).toBe(100100);
    expect(onHealth).toHaveBeenCalledTimes(1);
    expect(onHealth).toHaveBeenLastCalledWith(conversation.id, expect.objectContaining({state: 'live'}));
    onHealth.mockClear();
    vi.setSystemTime(100200);
    sources[0].emit('sync.heartbeat', {cursor: 'cursor-1', serverBootId: 'boot', degraded: false});
    expect(conversation._conversationSyncHealth?.lastFrameAt).toBe(100200);
    expect(onHealth).not.toHaveBeenCalled();
  });

  it('keeps unsent local drafts transport-free', async () => {
    const { runtime, conversation, api, sources } = fixture();
    conversation._localOnly = true;
    await runtime.wakeConversation(conversation);
    expect(api.snapshot).not.toHaveBeenCalled();
    expect(sources).toHaveLength(0);
    expect(conversation._localOnly).toBe(true);
  });

  it('shows distinct trouble reasons and clears them on healthy recovery', async () => {
    const { runtime, conversation, sources, onHealth } = fixture();
    await runtime.hydrateConversation(conversation);
    const source = sources[0]; source.onopen?.(); onHealth.mockClear();
    source.emit('sync.heartbeat', {cursor: 'cursor-1', serverBootId: 'boot', degraded: true});
    expect(onHealth).toHaveBeenLastCalledWith(conversation.id,
      expect.objectContaining({state: 'degraded', reason: 'storage-read-degraded'}));
    source.emit('sync.heartbeat', {cursor: 'cursor-1', serverBootId: 'boot', degraded: false, pushWithheld: true});
    expect(onHealth).toHaveBeenLastCalledWith(conversation.id,
      expect.objectContaining({state: 'degraded', reason: 'storage-write-wedged'}));
    source.emit('sync.heartbeat', {cursor: 'cursor-1', serverBootId: 'boot', degraded: false});
    expect(onHealth).toHaveBeenLastCalledWith(conversation.id, expect.objectContaining({state: 'live'}));
    expect(conversation._conversationSyncHealth?.reason).toBeUndefined();
  });
  it('applies twenty ordered changes without repeated healthy visual callbacks', async () => {
    const { runtime, conversation, sources, onHealth, renderState, api } = fixture();
    await runtime.hydrateConversation(conversation);
    const source = sources[0]; source.onopen?.();
    // Establish liveness with a real frame; opening alone is not delivery.
    source.emit('sync.heartbeat', {cursor: 'cursor-1', serverBootId: 'boot', degraded: false});
    onHealth.mockClear(); renderState.mockClear();
    for (let index = 1; index <= 20; index++) {
      vi.setSystemTime(100000 + index);
      source.emit('turn.upsert', {
        syncSeq: index + 1, occurredAt: index,
        payload: { conversationRevision: index + 1, turns: [{
          ...turn, projection: { content: `exact-${index}` }, projectionRevision: index + 1,
        }] },
      }, `cursor-${index + 1}`);
    }
    expect(onHealth).not.toHaveBeenCalled();
    expect(api.snapshot).toHaveBeenCalledTimes(1);
    const state = runtime.ensureRuntimeStore('health-test').getState();
    expect(state.turnsById.turn?.projection.content).toBe('exact-20');
    expect(state.turnsById.turn?.projectionRevision).toBe(21);
    expect(renderState).toHaveBeenCalledTimes(20);
    expect(conversation._conversationSyncHealth?.lastFrameAt).toBe(100020);
    expect(conversationConnectionHealth.get('health-test')?.lastFrameAt).toBe(100020);
  });

  it('ordered changes enqueue one Surface paint without health-driven render amplification', async () => {
    const container = document.createElement('main'); document.body.append(container);
    const frames: Array<() => void> = [];
    const controller = createConversationSurfaceController({
      isActive: () => true, getContainer: () => container,
      schedule: render => { frames.push(render); return () => {}; },
    });
    const renderState = vi.fn((conversation, state, context) => controller.render(conversation, state, context));
    const onHealth = vi.fn();
    const sources: Source[] = [];
    const conversation = { id: 'health-test' };
    const api = { snapshot: vi.fn(async () => ({
      ok: true, contract: 'tofu.conversation-sync.snapshot/v1',
      scope: { kind: 'conversation', ownerId: 7, threadId: 'health-test' },
      conversationId: 'health-test', conversationRevision: 1, syncSeq: 1, cursor: 'cursor-1',
      serverBootId: 'boot', heartbeatIntervalMs: 15000, settings: {}, turns: [turn], attempts: [], queueItems: [],
    })) };
    const runtime = createConversationTurnRuntime({
      api: api as unknown as ConversationSyncApi, isActive: () => true,
      isDomStale: () => !controller.ownsConversation(conversation.id), renderState, onHealth,
      eventSourceFactory: () => { const source = new Source(); sources.push(source); return source as unknown as EventSource; },
    });
    runtimes.push(runtime);
    const flush = () => frames.splice(0).forEach(render => render());
    try {
      await runtime.hydrateConversation(conversation); flush();
      sources[0].emit('sync.heartbeat', {cursor: 'cursor-1', serverBootId: 'boot', degraded: false});
      onHealth.mockClear(); renderState.mockClear();
      for (let index = 1; index <= 20; index++) {
        vi.setSystemTime(100000 + index);
        sources[0].emit('turn.upsert', { syncSeq: index + 1, occurredAt: index,
          payload: { conversationRevision: index + 1, turns: [{
            ...turn, status: index === 20 ? 'completed' : 'running',
            projection: { content: `exact-${index}` }, projectionRevision: index + 1,
          }] },
        }, `cursor-${index + 1}`);
      }
      expect(onHealth).not.toHaveBeenCalled();
      expect(renderState).toHaveBeenCalledTimes(20);
      expect(frames).toHaveLength(1);
      expect(api.snapshot).toHaveBeenCalledTimes(1);
      flush(); expect(container.textContent).toContain('exact-20');
      expect(controller.ownsConversation(conversation.id)).toBe(true);
    } finally { controller.dispose(); container.remove(); }
  });

  it('healthy frame deduplication cannot suppress an uncommitted Surface repair', async () => {
    const { runtime, conversation, sources, onHealth, setDomStale } = fixture();
    await runtime.hydrateConversation(conversation);
    const heartbeat = () => sources[0].emit('sync.heartbeat', {
      cursor: 'cursor-1', serverBootId: 'boot', degraded: false,
    });
    heartbeat(); onHealth.mockClear();
    heartbeat(); expect(onHealth).not.toHaveBeenCalled();
    setDomStale(true); heartbeat();
    expect(onHealth).toHaveBeenCalledTimes(1);
    expect(onHealth).toHaveBeenLastCalledWith(conversation.id, expect.objectContaining({state: 'live'}));
    setDomStale(false); onHealth.mockClear(); heartbeat();
    expect(onHealth).not.toHaveBeenCalled();
  });

  it('keeps trouble observations and recovery visible without an added timer', async () => {
    const { runtime, conversation, sources, onHealth } = fixture();
    await runtime.hydrateConversation(conversation);
    const source = sources[0]; source.onopen?.();
    onHealth.mockClear();
    source.onerror?.();
    expect(onHealth).toHaveBeenLastCalledWith('health-test', expect.objectContaining({ state: 'recovering' }));
    const frameAt = conversationConnectionHealth.get('health-test')?.lastFrameAt;
    vi.setSystemTime(101200); source.onerror?.();
    expect(onHealth).toHaveBeenCalledTimes(2);
    expect(conversation._conversationSyncHealth?.lastFrameAt).toBe(frameAt);
    source.emit('sync.heartbeat', { cursor: 'cursor-1', serverBootId: 'boot', degraded: false });
    expect(onHealth).toHaveBeenLastCalledWith('health-test', expect.objectContaining({ state: 'live' }));
    onHealth.mockClear();
    source.emit('sync.heartbeat', { cursor: 'cursor-1', serverBootId: 'boot', degraded: false });
    expect(onHealth).not.toHaveBeenCalled();
    runtime.disposeConversation(conversation.id);
    expect(onHealth).toHaveBeenLastCalledWith('health-test', expect.objectContaining({ state: 'closed' }));
    expect(source.closed).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('does not overwrite a newer reentrant health verdict with an outer live reading', async () => {
    const { runtime, conversation, sources, onHealth } = fixture();
    await runtime.hydrateConversation(conversation);
    sources[0].onopen?.();
    const unsubscribe = conversationConnectionHealth.subscribe('health-test', value => {
      if (value.state === 'live' && value.lastFrameAt === 100010) runtime.setTransportSuspended(true);
    });
    try {
      vi.setSystemTime(100010); onHealth.mockClear();
      sources[0].emit('sync.heartbeat', { cursor: 'cursor-1', serverBootId: 'boot', degraded: false });
      expect(conversationConnectionHealth.get('health-test')?.state).toBe('idle');
      expect(conversation._conversationSyncHealth?.state).toBe('idle');
      expect(onHealth).toHaveBeenLastCalledWith('health-test', expect.objectContaining({ state: 'idle' }));
    } finally { unsubscribe(); }
  });
});
