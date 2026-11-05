// @vitest-environment jsdom
/** Conversation composition preserves replay authority and owns shell lifetimes. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createConversationTurnRuntime, type RuntimeConversation, type TurnRuntimeOptions } from './turn-runtime';
import type { ConversationSyncApi, ConversationSyncSnapshot, TurnRecord } from '../api/conversation-sync.generated';
import type { ConversationStreamRequest } from './conversation-event-multiplexer';
import { createConversationSurfaceController } from '../conversation/application/conversation-surface-controller';

class Source extends EventTarget {
  readyState = 0;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  constructor(readonly request: ConversationStreamRequest) { super(); }
  close() { this.readyState = 2; }
  emit(type: string, fields: object, cursor: string) {
    this.dispatchEvent(new MessageEvent(type, { lastEventId: cursor, data: JSON.stringify({
      contract: 'tofu.conversation-sync.event/v1', conversationId: 'conv', type, ...fields,
    }) }));
  }
}

function turn(revision: number, status: 'running' | 'completed', id = 'answer'): TurnRecord {
  return { turnId: id, presentationId: id, conversationId: 'conv', actor: 'assistant',
    laneId: 'main', ordinal: id === 'answer' ? 1 : 2, kind: 'reply', runId: '',
    status, currentAttemptId: id === 'answer' ? 'attempt' : null,
    projectionRevision: revision, createdAt: 1, updatedAt: revision, settlement: {},
    projection: { content: status === 'completed' ? 'The complete committed answer' : '' } };
}

function snapshot(head: number): ConversationSyncSnapshot {
  return { ok: true, contract: 'tofu.conversation-sync.snapshot/v1',
    scope: { kind: 'conversation', ownerId: 7, threadId: 'conv' }, conversationId: 'conv',
    conversationRevision: head, syncSeq: head, cursor: `cursor-${head}`,
    serverBootId: 'boot', heartbeatIntervalMs: 15000, settings: {}, queueItems: [],
    turns: [turn(head === 1 ? 1 : 2, head === 1 ? 'running' : 'completed')],
    attempts: [{ attemptId: 'attempt', turnId: 'answer', conversationId: 'conv',
      commandId: 'command', taskId: 'task', operation: 'generate',
      baseProjectionRevision: 1, resumeAnchor: {}, createdAt: 1,
      status: head === 1 ? 'running' : 'completed' }] };
}

const cleanups: Array<() => void> = [];
beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { cleanups.splice(0).forEach(dispose => dispose()); vi.useRealTimers(); });

function fixture(overrides: Partial<TurnRuntimeOptions> = {}) {
  const sources: Source[] = [];
  const settings = vi.fn();
  const metadata = vi.fn();
  const api = { snapshot: vi.fn(async () => snapshot(1)) };
  const runtime = createConversationTurnRuntime({ api: api as unknown as ConversationSyncApi,
    isActive: () => true, renderState: () => true, applySettings: settings,
    applySnapshotMetadata: metadata, onProtocolError: vi.fn(), eventSourceFactory: request => {
      const source = new Source(request); sources.push(source); return source;
    }, ...overrides });
  const conversation = { id: 'conv' };
  cleanups.push(() => runtime.disposeConversation('conv'));
  return { runtime, conversation, api, sources, settings, metadata };
}

describe('conversation binding ownership and teardown', () => {
  it('ingests every ordered change while painting is withheld or fails, then commits the latest graph', async () => {
    const container = document.createElement('main'); document.body.append(container);
    const frames: Array<() => void> = [];
    const commits = vi.fn();
    const protocolError = vi.fn();
    const applied: number[] = [];
    let fail = true;
    const warning = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const controller = createConversationSurfaceController({
      isActive: () => true, getContainer: () => container,
      schedule: callback => { frames.push(callback); return () => {}; },
      nativeRenderers: { renderBlock(node, block) {
        if (fail) throw new Error('Injected appearance failure');
        if (block.kind === 'text') node.textContent = block.markdown;
      } },
      afterConversationCommit: commits,
    });
    cleanups.push(() => { controller.dispose(); container.remove(); warning.mockRestore(); });
    const f = fixture({ renderState: (conversation, state, context) => controller.render(conversation, state, context),
      onProtocolError: protocolError,
      onStateApplied: (_conversation, state) => { applied.push(state.conversationRevision); } });
    const store = await f.runtime.hydrateConversation(f.conversation);
    const connection = f.runtime.connect(store, 'attempt');
    for (let revision = 2; revision <= 65; revision++) {
      f.sources[0].emit('turn.upsert', { syncSeq: revision, occurredAt: revision,
        payload: { conversationRevision: revision, turns: [turn(revision, 'completed')] } }, `cursor-${revision}`);
    }
    expect(connection.cursor).toBe('cursor-65');
    expect(store.getState().turnsById.answer?.projectionRevision).toBe(65);
    expect(applied.filter(revision => revision >= 2)).toEqual(Array.from({ length: 64 }, (_, i) => i + 2));
    expect(frames).toHaveLength(1);
    expect(commits).not.toHaveBeenCalled();
    expect(container.childElementCount).toBe(0);
    frames.shift()!(); // A failed design commit does not request a new snapshot.
    expect(controller.ownsConversation('conv')).toBe(false);
    expect(connection.cursor).toBe('cursor-65');
    f.sources[0].emit('turn.upsert', { syncSeq: 66, occurredAt: 66,
      payload: { conversationRevision: 66, turns: [turn(66, 'completed')] } }, 'cursor-66');
    expect(connection.cursor).toBe('cursor-66');
    fail = false;
    frames.shift()!();
    expect(commits).toHaveBeenCalledTimes(1);
    expect(commits.mock.calls[0][1].conversationRevision).toBe(66);
    expect(container.textContent).toContain('The complete committed answer');
    expect(controller.ownsConversation('conv')).toBe(true);
    expect(f.api.snapshot).toHaveBeenCalledTimes(1);
    expect(protocolError).not.toHaveBeenCalled();
  });

  it.each(['queued', 'reading'] as const)(
    'coalesced hydration hands the shared snapshot to the latest shell while %s', async phase => {
      let deliver!: (value: ConversationSyncSnapshot) => void;
      const response = new Promise<ConversationSyncSnapshot>(resolve => { deliver = resolve; });
      const applied = vi.fn();
      const f = fixture({ onStateApplied: applied });
      f.api.snapshot.mockImplementationOnce(() => response);
      const first = f.runtime.hydrateConversation(f.conversation);
      if (phase === 'reading') await Promise.resolve();
      const replacement = { id: 'conv', title: 'latest catalog shell' };
      const second = f.runtime.hydrateConversation(replacement);
      expect(second).toBe(first);
      deliver(snapshot(1));
      const store = await second;

      expect(f.api.snapshot).toHaveBeenCalledTimes(1);
      expect(f.settings.mock.calls.map(([conversation]) => conversation)).toEqual([replacement]);
      expect(f.metadata.mock.calls.map(([conversation]) => conversation)).toEqual([replacement]);
      applied.mockClear();
      store.dispatch({ type: 'command_response', response: {
        conversationRevision: 2, turn: turn(2, 'completed'),
      } });
      expect(applied.mock.calls.map(([conversation]) => conversation)).toEqual([replacement]);
    },
  );

  it.each(['applySettings', 'applySnapshotMetadata', 'onStateApplied'] as const)(
    'hydration transfers to a replacement requested inside %s without another read', async callback => {
      const replacement = { id: 'conv', title: 'replacement from host callback' };
      let replacementLoad: Promise<unknown> | undefined;
      const applied = vi.fn();
      const renderState = vi.fn<NonNullable<TurnRuntimeOptions['renderState']>>(() => true);
      const transfer = vi.fn((conversation: RuntimeConversation) => {
        if (conversation !== replacement) replacementLoad = f.runtime.hydrateConversation(replacement);
      });
      const f = fixture({ onStateApplied: applied, renderState, [callback]: transfer });
      const loading = f.runtime.hydrateConversation(f.conversation);
      const store = await loading;
      expect(replacementLoad).toBe(loading);
      expect(f.api.snapshot).toHaveBeenCalledTimes(1);
      expect(renderState).toHaveBeenCalled();
      expect(renderState.mock.calls.every(([conversation]) => conversation === replacement)).toBe(true);
      applied.mockClear(); transfer.mockClear();
      store.dispatch({ type: 'command_response', response: {
        conversationRevision: 2, turn: turn(2, 'completed'),
      } });
      const observed = callback === 'onStateApplied' ? transfer : applied;
      expect(observed.mock.calls.map(([conversation]) => conversation)).toEqual([replacement]);
    },
  );

  it('adopts a draft shell whose first Turn authority already committed', async () => {
    // A first send whose ACK was lost (Stop while it started, a transport
    // timeout) still committed its Turn. Left local-only, the shell paused its
    // stream after the next change and never received the terminal settlement.
    const f = fixture();
    const draft: RuntimeConversation = { id: 'conv', _localOnly: true };
    const store = await f.runtime.hydrateConversation(draft);

    expect(draft._localOnly).toBe(false);
    f.sources[0].emit('turn.upsert', { syncSeq: 2, occurredAt: 2,
      payload: { conversationRevision: 2, turns: [turn(2, 'running')] } }, 'cursor-2');
    expect(f.runtime.hasLiveConnection('conv')).toBe(true);
    f.sources[0].emit('turn.upsert', { syncSeq: 3, occurredAt: 3,
      payload: { conversationRevision: 3, turns: [turn(3, 'completed')] } }, 'cursor-3');
    expect(store.getState().turnsById.answer?.status).toBe('completed');
  });

  it('keeps a draft shell local-only when authority has no conversation', async () => {
    const f = fixture();
    f.api.snapshot.mockRejectedValueOnce(Object.assign(new Error('Not found'), { status: 404 }));
    const draft: RuntimeConversation = { id: 'conv', _localOnly: true };

    await expect(f.runtime.hydrateConversation(draft)).rejects.toThrow('Not found');

    expect(draft._localOnly).toBe(true);
    expect(f.sources).toHaveLength(0);
  });

  it('a new shell claimed by wake while hydration is pending remains the binding', async () => {
    const applied = vi.fn();
    const f = fixture({ onStateApplied: applied });
    const store = await f.runtime.hydrateConversation(f.conversation);
    let deliver!: (value: ConversationSyncSnapshot) => void;
    f.api.snapshot.mockImplementationOnce(() => new Promise(resolve => { deliver = resolve; }));
    const loading = f.runtime.hydrateConversation(f.conversation);
    await Promise.resolve();
    const replacement = { id: 'conv', title: 'new active shell' };
    const waking = f.runtime.wakeConversation(replacement);
    deliver(snapshot(2));
    await Promise.all([loading, waking]);
    applied.mockClear();

    store.dispatch({ type: 'command_response', response: {
      conversationRevision: 3, turn: turn(3, 'completed'),
    } });

    expect(applied.mock.calls.map(([conversation]) => conversation)).toEqual([replacement]);
    expect(f.api.snapshot).toHaveBeenCalledTimes(2);
  });

  it('a reentrant newer settled graph cannot be reconnected by the outer running graph', async () => {
    const renderState = vi.fn<NonNullable<TurnRuntimeOptions['renderState']>>(() => true);
    let active = true;
    const f = fixture({ renderState, isActive: () => active });
    const store = await f.runtime.hydrateConversation(f.conversation);
    // The outer projection captured active demand. Its render callback settles
    // the Turn after navigation, so only the inner graph can decide connection demand.
    renderState.mockImplementationOnce((_conversation, state) => {
      active = false;
      store.dispatch({ type: 'command_response', response: {
        conversationRevision: state.conversationRevision + 1, turn: turn(3, 'completed'),
      } });
      return true;
    });
    store.dispatch({ type: 'command_response', response: {
      conversationRevision: 2, turn: turn(2, 'running'),
    } });

    expect(store.getState().turnsById.answer?.status).toBe('completed');
    expect(f.runtime.hasLiveConnection('conv')).toBe(false);
    expect(f.sources.every(source => source.readyState === 2)).toBe(true);
  });

  it('activity reconciliation cannot pause the shell claimed by its render callback', async () => {
    let active: RuntimeConversation;
    const renderState = vi.fn<NonNullable<TurnRuntimeOptions['renderState']>>(() => true);
    const f = fixture({ renderState, isActive: conversation => conversation === active });
    active = f.conversation;
    f.api.snapshot.mockResolvedValueOnce(snapshot(2));
    await f.runtime.hydrateConversation(f.conversation);
    const replacement = { id: 'conv', title: 'new active shell' };
    let waking: Promise<unknown> | undefined;
    renderState.mockImplementationOnce(() => {
      active = replacement;
      waking = f.runtime.wakeConversation(replacement);
      return true;
    });

    f.runtime.reconcileConversationActivity('conv');
    await waking;

    expect(renderState.mock.calls.at(-1)?.[0]).toBe(replacement);
    expect(f.runtime.hasLiveConnection('conv')).toBe(true);
    expect(f.api.snapshot).toHaveBeenCalledTimes(1);
  });

  it.each(['onStateApplied', 'persist', 'isDomStale'] as const)(
    'a newer graph committed inside %s remains the visible projection', async callback => {
      let reenter = () => {};
      const renderState = vi.fn<NonNullable<TurnRuntimeOptions['renderState']>>(() => true);
      const f = fixture({ renderState, [callback]: () => { reenter(); return false; } });
      const store = await f.runtime.hydrateConversation(f.conversation);
      reenter = () => {
        reenter = () => {};
        store.dispatch({ type: 'command_response', response: {
          conversationRevision: 3, turn: turn(3, 'completed'),
        } });
      };

      store.dispatch({ type: 'command_response', response: {
        conversationRevision: 2, turn: turn(2, 'running'),
      } });

      expect(store.getState().conversationRevision).toBe(3);
      expect((f.conversation as RuntimeConversation)._serverRev).toBe(3);
      expect(renderState.mock.calls.at(-1)?.[1].conversationRevision).toBe(3);
    },
  );

  it('detaches the owned store subscription without an optional catalog lookup', async () => {
    const applied = vi.fn();
    const f = fixture({ onStateApplied: applied });
    const store = await f.runtime.hydrateConversation(f.conversation);
    f.runtime.disposeConversation('conv');
    const before = applied.mock.calls.length;

    store.dispatch({ type: 'command_response', response: {
      conversationRevision: 2, turn: turn(2, 'completed'),
    } });

    expect(applied).toHaveBeenCalledTimes(before);
    expect(f.runtime.readRuntimeState('conv')).toBeNull();
    expect(f.sources.every(source => source.readyState === 2)).toBe(true);
  });

  it('transfers one subscription when the catalog replaces a conversation shell', async () => {
    const applied = vi.fn();
    const f = fixture({ onStateApplied: applied });
    const store = await f.runtime.hydrateConversation(f.conversation);
    const replacement = { id: 'conv', title: 'replacement shell' };
    await f.runtime.hydrateConversation(replacement);
    applied.mockClear();

    store.dispatch({ type: 'command_response', response: {
      conversationRevision: 2, turn: turn(2, 'completed'),
    } });

    expect(applied.mock.calls.map(([conversation]) => conversation)).toEqual([replacement]);
  });

  it('releases state and subscriptions even when DOM disposal throws', async () => {
    const failure = new Error('injected DOM disposal failure');
    const disposeRenderedState = vi.fn().mockImplementationOnce(() => { throw failure; });
    const applied = vi.fn();
    const f = fixture({ disposeRenderedState, onStateApplied: applied });
    const store = await f.runtime.hydrateConversation(f.conversation);

    expect(() => f.runtime.disposeConversation('conv')).toThrow(failure);
    expect(f.runtime.readRuntimeState('conv')).toBeNull();
    expect(f.sources.every(source => source.readyState === 2)).toBe(true);
    const before = applied.mock.calls.length;
    store.dispatch({ type: 'command_response', response: {
      conversationRevision: 2, turn: turn(2, 'completed'),
    } });
    expect(applied).toHaveBeenCalledTimes(before);
  });

  it('retains cleanup reads while ignoring recursive disposal', async () => {
    const reads: unknown[] = [];
    const disposeRenderedState = vi.fn(() => {
      reads.push(f.runtime.readRuntimeState('conv'));
      if (disposeRenderedState.mock.calls.length === 1) f.runtime.disposeConversation('conv');
    });
    const f = fixture({ disposeRenderedState });
    const store = await f.runtime.hydrateConversation(f.conversation);

    f.runtime.disposeConversation('conv');

    expect(disposeRenderedState).toHaveBeenCalledTimes(1);
    expect(reads).toEqual([store.getState()]);
    expect(f.runtime.readRuntimeState('conv')).toBeNull();
    expect(f.sources.every(source => source.readyState === 2)).toBe(true);
  });

  it.each(['replacement', 'disposal'] as const)(
    'withdraws deferred terminal UI work after shell %s', async action => {
      const releases: Array<() => void> = [];
      const renderState = vi.fn(() => true);
      const updateSendButton = vi.fn();
      const f = fixture({ renderState, updateSendButton,
        deferTerminalRelease: release => releases.push(release) });
      const conversation: RuntimeConversation = f.conversation;
      const store = await f.runtime.hydrateConversation(conversation);
      conversation._finishingStream = true;
      conversation._finishingAttemptId = 'attempt';
      store.dispatch({ type: 'command_response', response: {
        conversationRevision: 2, turn: turn(2, 'completed'),
      } });
      expect(releases).toHaveLength(1);
      if (action === 'replacement') await f.runtime.wakeConversation({ id: 'conv' });
      else f.runtime.disposeConversation('conv');
      renderState.mockClear(); updateSendButton.mockClear();

      releases.forEach(release => release());

      expect(renderState).not.toHaveBeenCalled();
      expect(updateSendButton).not.toHaveBeenCalled();
    },
  );

  it('does not report an old live attempt as newly settled after reopening', async () => {
    const onTurnSettled = vi.fn();
    const f = fixture({ onTurnSettled });
    await f.runtime.hydrateConversation(f.conversation);
    f.runtime.disposeConversation('conv');
    f.api.snapshot.mockResolvedValue(snapshot(2));

    await f.runtime.hydrateConversation({ id: 'conv' });

    expect(onTurnSettled).not.toHaveBeenCalled();
  });

  it('cancels hydration queued before the first runtime allocation', async () => {
    const f = fixture();
    const hydration = f.runtime.hydrateConversation(f.conversation);
    const rejected = expect(hydration).rejects.toThrow(/disposed/);
    f.runtime.disposeConversation('conv');
    await rejected;

    expect(f.api.snapshot).not.toHaveBeenCalled();
    expect(f.sources).toHaveLength(0);
    expect(f.runtime.readRuntimeState('conv')).toBeNull();
  });

  it.each(['applySettings', 'applySnapshotMetadata', 'onStateApplied'] as const)(
    'cannot rebind a hydration disposed by %s', async callback => {
      const renderState = vi.fn(() => true);
      const f = fixture({ renderState, [callback]: () => f.runtime.disposeConversation('conv') });

      await expect(f.runtime.hydrateConversation(f.conversation)).rejects.toThrow(/disposed/);

      expect(f.runtime.readRuntimeState('conv')).toBeNull();
      expect(renderState).not.toHaveBeenCalled();
      expect(f.sources).toHaveLength(0);
    },
  );

  it('publishes final health before the host withdraws its rendering demand', async () => {
    const order: string[] = [];
    const f = fixture({
      onHealth: (_id, health) => { if (health.state === 'closed') order.push('closed'); },
      disposeRenderedState: () => order.push('dispose'),
    });
    await f.runtime.hydrateConversation(f.conversation);

    f.runtime.disposeConversation('conv');

    expect(order).toEqual(['closed', 'dispose']);
    expect(f.sources.every(source => source.readyState === 2)).toBe(true);
    expect(f.runtime.readRuntimeState('conv')).toBeNull();
  });

  it('releases subscriptions and timers across repeated replacement and disposal', async () => {
    const applied = vi.fn();
    const f = fixture({ onStateApplied: applied });
    for (let cycle = 0; cycle < 100; cycle++) {
      const store = await f.runtime.hydrateConversation({ id: 'conv' });
      const replacement = { id: 'conv' };
      await f.runtime.wakeConversation(replacement);
      applied.mockClear();
      store.dispatch({ type: 'command_response', response: {
        conversationRevision: 2, turn: turn(2, 'completed'),
      } });
      expect(applied.mock.calls.map(([conversation]) => conversation)).toEqual([replacement]);
      f.runtime.disposeConversation('conv');
      applied.mockClear();
      store.dispatch({ type: 'command_response', response: {
        conversationRevision: 3, turn: turn(3, 'completed'),
      } });
      expect(applied).not.toHaveBeenCalled();
      expect(f.sources.every(source => source.readyState === 2)).toBe(true);
      expect(vi.getTimerCount()).toBe(0);
    }
  });
});

describe('required conversation projection ingestion', () => {
  it('does not acknowledge a stale full snapshot after a newer partial command acknowledgement', async () => {
    const f = fixture();
    const store = await f.runtime.hydrateConversation(f.conversation);
    let resolve!: (value: ConversationSyncSnapshot) => void;
    f.api.snapshot.mockImplementationOnce(() => new Promise(yes => { resolve = yes; }))
      .mockResolvedValue(snapshot(3));
    const hydration = f.runtime.hydrateConversation(f.conversation);
    const rejected = expect(hydration).rejects.toThrow(/snapshot.*(stale|applied)/i);
    await vi.advanceTimersByTimeAsync(0);
    store.dispatch({ type: 'command_response', response: {
      conversationRevision: 3, turn: turn(3, 'completed', 'unrelated'),
    } });
    const settingsBefore = f.settings.mock.calls.length;
    const metadataBefore = f.metadata.mock.calls.length;
    resolve(snapshot(2));
    await rejected;
    expect(store.getState().turnsById.answer?.status).toBe('running');
    expect(f.sources).toHaveLength(1);
    expect(f.settings).toHaveBeenCalledTimes(settingsBefore);
    expect(f.metadata).toHaveBeenCalledTimes(metadataBefore);
    await vi.advanceTimersByTimeAsync(2000);
    expect(f.api.snapshot).toHaveBeenCalledTimes(3);
    expect(store.getState().turnsById.answer?.status).toBe('completed');
    expect(store.getState().turnsById.answer?.projection.content).toBe('The complete committed answer');
    expect(f.sources).toHaveLength(2);
    expect(f.sources[1].request.after).toBe('cursor-3');
  });

  it.each(['turn.upsert', 'turn.deleted'] as const)('repairs a silently rejected %s instead of consuming its replay cursor', async type => {
    const f = fixture();
    const store = await f.runtime.hydrateConversation(f.conversation);
    store.dispatch({ type: 'command_response', response: {
      conversationRevision: 3, turn: turn(3, 'completed', 'unrelated'),
    } });
    f.api.snapshot.mockResolvedValue(snapshot(3));
    f.sources[0].emit(type, { syncSeq: 2, occurredAt: 1,
      payload: { conversationRevision: 2,
        ...(type === 'turn.upsert' ? { turns: [turn(2, 'completed')] }
          : { deletedTurnIds: ['answer'] }),
      } }, 'cursor-2');
    await vi.advanceTimersByTimeAsync(0);
    expect(f.api.snapshot).toHaveBeenCalledTimes(2);
    expect(store.getState().turnsById.answer?.status).toBe('completed');
    expect(f.sources[1].request.after).toBe('cursor-3');
  });
});
