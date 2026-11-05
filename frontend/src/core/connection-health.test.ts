import { afterEach, describe, expect, it, vi } from 'vitest';
import { ConnectionHealthStore } from './connection-health';
import type { ConnectionHealth } from '../api/conversation-sync.generated';

afterEach(() => vi.restoreAllMocks());
const health: ConnectionHealth = {
  state: 'degraded', transport: 'conversation-sse', observedAt: 1,
  generation: 1, retryCount: 1,
};

describe('connection health observer boundary', () => {
  it('refreshes raw evidence without repeating an unchanged aggregate', () => {
    const store = new ConnectionHealthStore();
    const aggregate = vi.fn(); const readings = vi.fn();
    store.subscribeAggregate(aggregate);
    store.subscribe('conv', readings);
    for (let index = 1; index <= 20; index++) {
      store.set('conv', { ...health, state: 'live', lastFrameAt: index, observedAt: index });
    }
    expect(readings).toHaveBeenCalledTimes(20);
    expect(store.get('conv')?.lastFrameAt).toBe(20);
    expect(aggregate).toHaveBeenCalledTimes(1);
    store.set('conv', { ...health });
    store.set('other', { ...health, state: 'offline' });
    store.set('conv', { ...health, observedAt: 99, retryCount: 9 });
    expect(store.aggregate().count).toBe(2);
    expect(aggregate).toHaveBeenCalledTimes(3);
    store.clear('other'); store.clear('conv'); store.clear('missing');
    expect(store.aggregate()).toMatchObject({ count: 0, degraded: false });
    expect(aggregate).toHaveBeenCalledTimes(5);
  });

  it('does not discard transport-only or observation-only evidence', () => {
    const store = new ConnectionHealthStore(); const seen = vi.fn();
    store.subscribe('conv', seen);
    store.set('conv', health);
    store.set('conv', { ...health, transport: 'task-sse' });
    expect(store.get('conv')?.transport).toBe('task-sse');
    store.set('conv', { ...health, transport: 'task-sse', observedAt: 2 });
    expect(store.get('conv')?.observedAt).toBe(2);
    expect(seen).toHaveBeenCalledTimes(3);
  });

  it('does not replay stale aggregate evidence after a nested count ABA', () => {
    const store = new ConnectionHealthStore();
    const readings: Array<{count: number; at: number}> = [];
    let now = 1;
    vi.spyOn(Date, 'now').mockImplementation(() => now);
    let reentered = false;
    store.subscribeAggregate(value => {
      if (value.count !== 1 || reentered) return;
      reentered = true;
      now = 3;
      store.clear('conv');
      now = 4;
      store.set('conv', {...health, observedAt: 2});
    });
    store.subscribeAggregate(value => readings.push(value));
    now = 2;
    store.set('conv', health);
    expect(readings.map(({count, at}) => ({count, at}))).toEqual([
      {count: 0, at: 1}, {count: 0, at: 3}, {count: 1, at: 4},
    ]);
    expect(store.aggregate().count).toBe(1);
    expect(store.get('conv')?.observedAt).toBe(2);
  });

  it('keeps aggregate listeners current when a callback clears health', () => {
    const store = new ConnectionHealthStore();
    const seen: number[] = [];
    store.subscribeAggregate(value => {
      if (value.count === 1) store.clear('conv');
    });
    store.subscribeAggregate(value => seen.push(value.count));
    store.set('conv', health);
    expect(seen).toEqual([0, 0]);
    expect(store.aggregate().count).toBe(0);
  });
  it('contains subscriber failures during registration, publication and cleanup', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const store = new ConnectionHealthStore();
    store.set('conv', health);
    const fail = vi.fn(() => { throw new Error('observer failed'); });
    const unsubscribe = store.subscribe('conv', fail);
    const unsubscribeAggregate = store.subscribeAggregate(fail);
    const seen = vi.fn(); const aggregate = vi.fn();
    store.subscribe('conv', seen);
    store.subscribeAggregate(aggregate);
    store.set('conv', { ...health, state: 'live' });
    expect(seen).toHaveBeenLastCalledWith(expect.objectContaining({ state: 'live' }));
    expect(aggregate).toHaveBeenLastCalledWith(expect.objectContaining({ degraded: false }));
    store.clear('conv');
    expect(store.get('conv')).toBeUndefined();
    expect(warn).toHaveBeenCalled();
    unsubscribe(); unsubscribeAggregate(); fail.mockClear();
    store.set('conv', health);
    expect(fail).not.toHaveBeenCalled();
  });

  it('gives every subscriber the same immutable health evidence', () => {
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const store = new ConnectionHealthStore();
    store.subscribe('conv', value => { value.state = 'live'; });
    const seen = vi.fn(); store.subscribe('conv', seen);
    store.set('conv', { ...health });
    expect(seen).toHaveBeenCalledWith(store.get('conv'));
    expect(store.get('conv')?.state).toBe('degraded');
    expect(store.aggregate().degraded).toBe(true);
  });
});
