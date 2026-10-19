// @vitest-environment jsdom
import {describe, it, expect, vi} from 'vitest';
import {ConversationSyncCoordinator} from './conversation-sync';
import type {ConversationSyncApi, ConversationSyncSnapshot, ConversationRoundDirectory, ConversationTurnPage} from '../api/conversation-sync.generated';

function fixture() {
  let owner = 11; let head = 10;
  const api = {
    snapshot: vi.fn(async () => ({conversationId: 'a', conversationRevision: head,
      syncSeq: head, cursor: `head-${head}`, scope: {ownerId: owner, threadId: 'a'},
      turns: [], attempts: [], settings: {}} as unknown as ConversationSyncSnapshot)),
    roundDirectory: vi.fn(async (_id: string, laneId: string, syncSeq: number, offset: number) => ({
      ownerId: owner, conversationId: 'a', laneId, syncSeq, stale: false, offset,
      entries: [], totalRounds: 120,
    } as ConversationRoundDirectory)),
    turnPage: vi.fn(async () => ({conversationId: 'a', laneId: 'main', syncSeq: head,
      turns: [], attempts: [], beforeOrdinal: 31, nextBeforeOrdinal: 23,
      totalTurns: 500, hasMore: true} as unknown as ConversationTurnPage)),
  };
  const published = vi.fn();
  const coordinator = new ConversationSyncCoordinator({conversationId: 'a',
    api: api as unknown as ConversationSyncApi, onSnapshot: () => {},
    onAttemptEvent: () => true, onTurnDelta: () => true, onTurnPage: published});
  return {api, coordinator, published, advance: () => head++, switchOwner: () => owner++};
}

describe('head/owner scoped compact round directory', () => {
  it('requires a hydrated snapshot and fences every owner/lane/head response', async () => {
    const {api, coordinator} = fixture();
    await expect(coordinator.loadRoundDirectory('main')).rejects.toThrow('active snapshot');
    await coordinator.hydrate(false);
    expect((await coordinator.loadRoundDirectory('main', 119, 1)).totalRounds).toBe(120);
    expect(api.roundDirectory).toHaveBeenCalledWith('a', 'main', 10, 119, 1, undefined,
      expect.objectContaining({timeout: expect.any(Number)}));
    for (const mismatch of [{ownerId: 99}, {conversationId: 'b'}, {laneId: 'branch'},
      {syncSeq: 9}, {stale: true}, {offset: 3}]) {
      api.roundDirectory.mockImplementationOnce(async () => ({ownerId: 11, conversationId: 'a',
        laneId: 'main', syncSeq: 10, stale: false, offset: 0, entries: [], totalRounds: 120,
        ...mismatch}));
      await expect(coordinator.loadRoundDirectory('main')).rejects.toThrow('stale or mismatched');
    }
    coordinator.close();
  });
  it('rejects directory completion after head or owner changes, and after close', async () => {
    for (const kind of ['head', 'owner', 'close']) {
      const {api, coordinator, advance, switchOwner} = fixture(); await coordinator.hydrate(false);
      let resolve!: (page: ConversationRoundDirectory) => void;
      api.roundDirectory.mockImplementationOnce(() => new Promise(done => {resolve = done;}));
      const flight = coordinator.loadRoundDirectory('main');
      const rejected = expect(flight).rejects.toThrow('stale or mismatched');
      if (kind === 'close') coordinator.close();
      else { if (kind === 'head') advance(); else switchOwner(); await coordinator.hydrate(false); }
      resolve({ownerId: 11, conversationId: 'a', laneId: 'main', syncSeq: 10, stale: false,
        offset: 0, entries: [], totalRounds: 120});
      await rejected; coordinator.close();
    }
  });
  it('publishes detached pages through the same ingestion port without changing the head', async () => {
    const {coordinator, published} = fixture(); await coordinator.hydrate(false);
    const key = coordinator.roundDirectoryRevision;
    await coordinator.loadTurnPage('main', 31, 8, 'detached');
    expect(published).toHaveBeenCalledWith(expect.objectContaining({beforeOrdinal: 31}), 'detached');
    expect(coordinator.roundDirectoryRevision).toBe(key);
    let resolve!: (page: ConversationTurnPage) => void;
    const second = fixture(); await second.coordinator.hydrate(false);
    second.api.turnPage.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
    const flight = second.coordinator.loadTurnPage('main', 31, 8, 'detached');
    const rejected = expect(flight).rejects.toThrow('stale');
    // Even a reset retaining the same syncSeq/owner invalidates earlier pages.
    await second.coordinator.hydrate(false);
    resolve({conversationId: 'a', laneId: 'main', syncSeq: 10, turns: [], attempts: [],
      beforeOrdinal: 31, nextBeforeOrdinal: 23, totalTurns: 500, hasMore: true} as unknown as ConversationTurnPage);
    await rejected; expect(second.published).not.toHaveBeenCalled(); second.coordinator.close();
    coordinator.close();
  });
});
