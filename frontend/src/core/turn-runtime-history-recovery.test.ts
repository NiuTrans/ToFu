// @vitest-environment jsdom
/** A replacing tail-window recovery keeps loaded history in the Store, then the
 * runtime re-reads that range through the continuous history frontier. */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createConversationTurnRuntime } from './turn-runtime';
import type {
  ConversationSyncApi, ConversationSyncSnapshot, ConversationTurnPage, TurnRecord,
} from '../api/conversation-sync.generated';

class Source extends EventTarget {
  readyState = 1;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  close() { this.readyState = 2; }
}

const turn = (turnId: string, ordinal: number, status: 'running' | 'completed' = 'completed') => ({
  turnId, presentationId: turnId, conversationId: 'conv', laneId: 'main', ordinal,
  actor: 'assistant', kind: 'reply', runId: '', status, currentAttemptId: null,
  projectionRevision: 1, createdAt: ordinal, updatedAt: ordinal, settlement: {},
  projection: { content: turnId },
}) as unknown as TurnRecord;

const tailSnapshot = (syncSeq: number, turns: TurnRecord[], totalTurns: number) => ({
  ok: true, contract: 'tofu.conversation-sync.snapshot/v1',
  scope: { kind: 'conversation', ownerId: 7, threadId: 'conv' }, conversationId: 'conv',
  conversationRevision: syncSeq, syncSeq, cursor: `cursor-${syncSeq}`, serverBootId: 'test',
  heartbeatIntervalMs: 15000, settings: {}, queueItems: [], attempts: [], turns,
  turnWindow: { laneId: 'main', nextBeforeOrdinal: turns[0]!.ordinal, hasMore: true, totalTurns },
}) as unknown as ConversationSyncSnapshot;

const olderPage = (syncSeq: number, beforeOrdinal: number, turns: TurnRecord[], totalTurns: number) => ({
  ok: true, contract: 'tofu.conversation-sync.turn-page/v1', conversationId: 'conv',
  conversationRevision: syncSeq, syncSeq, cursor: `cursor-${syncSeq}`, laneId: 'main',
  beforeOrdinal, nextBeforeOrdinal: turns[0]?.ordinal ?? null, hasMore: false, totalTurns,
  turns, attempts: [],
}) as ConversationTurnPage;

const first = turn('first', 0), deleted = turn('deleted', 1), third = turn('third', 2);
const input = turn('input', 3), answer = turn('answer', 4), live = turn('live', 5, 'running');
// A compaction removed `deleted` while the stream was down.
const rereadBelow = (syncSeq: number, beforeOrdinal: number) => olderPage(syncSeq, beforeOrdinal,
  [first, third, input, answer].filter(item => item.ordinal < beforeOrdinal), 5);

const cleanup: Array<() => void> = [];
afterEach(() => { cleanup.splice(0).forEach(dispose => dispose()); });

async function loadedConversation(
  recoveries: ConversationSyncSnapshot[],
  reread = (syncSeq: number, beforeOrdinal: number) =>
    Promise.resolve(rereadBelow(syncSeq, beforeOrdinal)),
) {
  const api = {
    snapshot: vi.fn().mockResolvedValueOnce(tailSnapshot(1, [input, answer, live], 6)),
    turnPage: vi.fn((_id: string, _lane: string, syncSeq: number, beforeOrdinal = 0) => (
      beforeOrdinal === 3
        ? Promise.resolve(olderPage(syncSeq, 3, [first, deleted, third], 6))
        : reread(syncSeq, beforeOrdinal))),
  };
  for (const snapshot of recoveries) api.snapshot.mockResolvedValueOnce(snapshot);
  const sources: Source[] = [];
  const runtime = createConversationTurnRuntime({
    api: api as unknown as ConversationSyncApi, isActive: () => true, renderState: () => true,
    eventSourceFactory: () => {
      const source = new Source();
      sources.push(source);
      return source as unknown as EventSource;
    },
  });
  cleanup.push(() => runtime.disposeConversation('conv'));
  const conversation = { id: 'conv' };
  const store = await runtime.hydrateConversation(conversation);
  await runtime.loadConversationTurnPage(conversation, 'main', 3, 64);
  const resetRequired = () => sources.at(-1)!.onmessage!(new MessageEvent('message', {
    data: JSON.stringify({ contract: 'tofu.conversation-sync.event/v1', conversationId: 'conv',
      type: 'sync.reset_required', cursor: 'cursor-0', reason: 'cursor_invalid' }),
  }));
  return { api, runtime, store, resetRequired };
}

// The running Turn outgrew the byte budget: recovery admits only it.
const liveOnly = (syncSeq: number) => tailSnapshot(syncSeq, [live], 5);

describe('tail-window recovery', () => {
  it('never drops loaded Turns and removes only what the re-read page omits', async () => {
    const { api, store, resetRequired } = await loadedConversation([liveOnly(2)]);
    expect(store.getState().laneOrder.main)
      .toEqual(['first', 'deleted', 'third', 'input', 'answer', 'live']);
    const vanished: string[][] = [];
    store.subscribe((state) => {
      const missing = ['first', 'third', 'input', 'answer'].filter(id => !state.turnsById[id]);
      if (missing.length) vanished.push(missing);
    });

    resetRequired();

    await vi.waitFor(() => expect(store.getState().laneOrder.main)
      .toEqual(['first', 'third', 'input', 'answer', 'live']));
    expect(vanished).toEqual([]);
    expect(api.snapshot).toHaveBeenCalledTimes(2);
    expect(api.turnPage).toHaveBeenCalledTimes(2);
    expect(api.turnPage.mock.calls[1]?.slice(1, 5)).toEqual(['main', 2, 5, 64]);
    expect(store.getState().historyByLane.main)
      .toEqual({ nextBeforeOrdinal: 0, hasMore: false, totalTurns: 5 });
  });

  it('re-reads again when a later recovery raises the frontier mid-read', async () => {
    let release: (() => void) | null = null;
    const { api, store, resetRequired } = await loadedConversation(
      [tailSnapshot(2, [answer, live], 5), liveOnly(3)],
      (syncSeq, beforeOrdinal) => {
        const page = rereadBelow(syncSeq, beforeOrdinal);
        if (beforeOrdinal !== 4 || release) return Promise.resolve(page);
        return new Promise((resolve) => { release = () => resolve(page); });
      },
    );

    resetRequired();
    await vi.waitFor(() => expect(release).not.toBeNull());
    resetRequired();
    await vi.waitFor(() => expect(store.getState().historyByLane.main?.nextBeforeOrdinal).toBe(5));
    release!();

    await vi.waitFor(() => expect(store.getState().laneOrder.main)
      .toEqual(['first', 'third', 'input', 'answer', 'live']));
    expect(store.getState().historyByLane.main)
      .toEqual({ nextBeforeOrdinal: 0, hasMore: false, totalTurns: 5 });
    expect(api.turnPage.mock.calls.at(-1)?.[3]).toBe(5);
  });

  it('cancels a scheduled re-read when the conversation is disposed', async () => {
    const { api, runtime, store, resetRequired } = await loadedConversation([liveOnly(2)]);
    let applied = false;
    store.subscribe((state) => {
      if (applied || state.historyByLane.main?.retainedFromOrdinal === undefined) return;
      applied = true;
      // After the snapshot callback has scheduled its re-read, before it runs.
      queueMicrotask(() => runtime.disposeConversation('conv'));
    });

    resetRequired();

    await vi.waitFor(() => expect(applied).toBe(true));
    await new Promise(resolve => setTimeout(resolve, 20));
    expect(api.turnPage).toHaveBeenCalledTimes(1);
  });
});

/* A settled Turn below a later tail window whose post-settlement enrichment
 * (its translation) committed while the reader was elsewhere: the inactive,
 * settled conversation had paused its stream, so only a re-read can show it. */
describe('retained Turns of a conversation the reader left', () => {
  const enriched = {
    ...turn('settled', 0), projectionRevision: 2,
    projection: { content: 'settled', translatedContent: '已翻译' },
  } as unknown as TurnRecord;
  const reply = turn('reply', 1);

  async function leftConversation(
    reread: (syncSeq: number, beforeOrdinal: number) => Promise<ConversationTurnPage>,
  ) {
    let active = true;
    const api = {
      snapshot: vi.fn()
        .mockResolvedValueOnce({
          ...tailSnapshot(1, [turn('settled', 0), reply], 2),
          turnWindow: { laneId: 'main', nextBeforeOrdinal: null, hasMore: false, totalTurns: 2 },
        })
        .mockResolvedValueOnce(tailSnapshot(2, [reply], 2)),
      turnPage: vi.fn((_id: string, _lane: string, syncSeq: number, beforeOrdinal = 0) =>
        reread(syncSeq, beforeOrdinal)),
    };
    const runtime = createConversationTurnRuntime({
      api: api as unknown as ConversationSyncApi, isActive: () => active, renderState: () => true,
      eventSourceFactory: () => new Source() as unknown as EventSource,
    });
    cleanup.push(() => runtime.disposeConversation('conv'));
    const conversation = { id: 'conv' };
    const store = await runtime.hydrateConversation(conversation);
    const select = (selected: boolean) => {
      active = selected;
      runtime.reconcileConversationActivity('conv');
    };
    select(false);
    // A hydrate without a stream (e.g. a translation-done refresh) applies a
    // tail window that retains `settled` at its pre-enrichment revision.
    await runtime.hydrateConversation(conversation);
    await new Promise(resolve => setTimeout(resolve, 0));
    return { api, store, select };
  }

  it('re-reads them once the paused conversation opens its stream again', async () => {
    const { api, store, select } = await leftConversation((syncSeq, beforeOrdinal) =>
      Promise.resolve(olderPage(syncSeq, beforeOrdinal, [enriched], 2)));
    expect(store.getState().turnsById.settled?.projectionRevision).toBe(1);
    expect(store.getState().historyByLane.main?.retainedFromOrdinal).toBe(0);
    expect(api.turnPage).not.toHaveBeenCalled();

    select(true);

    await vi.waitFor(() => expect(store.getState().turnsById.settled?.projectionRevision).toBe(2));
    expect(store.getState().turnsById.settled?.projection?.translatedContent).toBe('已翻译');
    expect(store.getState().historyByLane.main?.retainedFromOrdinal).toBeUndefined();
    expect(api.turnPage.mock.calls[0]?.slice(1, 5)).toEqual(['main', 2, 1, 64]);
  });

  it('retries a failed re-read on the next stream it opens', async () => {
    let failures = 1;
    const { api, store, select } = await leftConversation((syncSeq, beforeOrdinal) => (
      failures-- > 0 ? Promise.reject(new Error('timeout'))
        : Promise.resolve(olderPage(syncSeq, beforeOrdinal, [enriched], 2))));
    select(true);
    await vi.waitFor(() => expect(api.turnPage).toHaveBeenCalledTimes(1));
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(store.getState().turnsById.settled?.projectionRevision).toBe(1);

    select(false);
    select(true);

    await vi.waitFor(() => expect(store.getState().turnsById.settled?.projectionRevision).toBe(2));
    expect(api.turnPage).toHaveBeenCalledTimes(2);
    expect(store.getState().historyByLane.main?.retainedFromOrdinal).toBeUndefined();
  });
});
