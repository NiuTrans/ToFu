// @vitest-environment jsdom
/** Byte budgets of chained older-history pages in the sync coordinator. */
import { afterEach, expect, it, vi } from 'vitest';
import { ConversationSyncCoordinator } from './conversation-sync';
import type {
  ConversationSyncApi, ConversationSyncSnapshot, ConversationTurnPage,
} from '../api/conversation-sync.generated';
import type { RequestOptions } from '../api/transport';

const VIEWPORT_BYTES = 1024 * 1024; // conversationLoadBudget at 960×800.

function snapshot(): ConversationSyncSnapshot {
  return { ok: true, contract: 'tofu.conversation-sync.snapshot/v1',
    scope: { kind: 'conversation', ownerId: 7, threadId: 'conv' }, conversationId: 'conv',
    conversationRevision: 1, syncSeq: 1, cursor: 'cursor-1', serverBootId: 'boot',
    heartbeatIntervalMs: 15000, settings: {}, turns: [], attempts: [], queueItems: [] };
}

/** A page holding `count` Turns just below `before`. */
function page(before: number, count: number): ConversationTurnPage {
  const turns = Array.from({ length: count }, (_, index) => ({
    turnId: `turn-${before - count + index}`, conversationId: 'conv', laneId: 'main',
    ordinal: before - count + index, projection: {},
  }));
  return { conversationId: 'conv', laneId: 'main', syncSeq: 1, turns, attempts: [],
    beforeOrdinal: before, nextBeforeOrdinal: before - count, totalTurns: 900,
    hasMore: before - count > 0 } as unknown as ConversationTurnPage;
}

const coordinators: ConversationSyncCoordinator[] = [];
afterEach(() => { coordinators.splice(0).forEach(coordinator => coordinator.close()); });

async function fixture(turnsPerPage: (limit: number) => number) {
  const turnPage = vi.fn(async (_id: string, _lane: string, _seq: number, before?: number,
    limit = 64, _options?: RequestOptions) => page(before ?? 900, turnsPerPage(limit)));
  const api = { snapshot: vi.fn(async () => snapshot()), turnPage,
    roundDirectory: vi.fn() };
  const coordinator = new ConversationSyncCoordinator({
    conversationId: 'conv', api: api as unknown as ConversationSyncApi,
    snapshotViewport: () => ({ width: 960, height: 800 }),
    onSnapshot: () => {}, onTurnPage: () => {}, onAttemptEvent: () => true, onTurnDelta: () => true,
    eventSourceFactory: () => { throw new Error('History reads open no stream'); },
  });
  coordinators.push(coordinator);
  await coordinator.hydrate(false);
  const budgets = () => turnPage.mock.calls.map(call => Number(call[5]?.query?.windowBytes));
  return { coordinator, budgets };
}

it('grows the byte budget of chained pages that bytes cut short, up to four viewport budgets', async () => {
  const { coordinator, budgets } = await fixture(() => 1);
  let before = 100;
  for (let read = 0; read < 4; read += 1) {
    const loaded = await coordinator.loadTurnPage('main', before, 64);
    before = loaded.nextBeforeOrdinal!;
  }
  expect(budgets()).toEqual([1, 2, 4, 4].map(factor => factor * VIEWPORT_BYTES));
  // A detached jump and a read that does not continue the chain start over.
  await coordinator.loadTurnPage('main', 40, 8, 'detached');
  await coordinator.loadTurnPage('main', 70, 64);
  expect(budgets().slice(4)).toEqual([VIEWPORT_BYTES, VIEWPORT_BYTES]);
});

it('keeps the viewport budget for pages bounded by their Turn count', async () => {
  const { coordinator, budgets } = await fixture(limit => limit);
  const first = await coordinator.loadTurnPage('main', 900, 64);
  await coordinator.loadTurnPage('main', first.nextBeforeOrdinal!, 64);
  expect(budgets()).toEqual([VIEWPORT_BYTES, VIEWPORT_BYTES]);
});
