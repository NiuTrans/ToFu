import { afterEach, describe, expect, it, vi } from 'vitest';
import { ConnectionHealthStore } from './connection-health';
import type { ConnectionHealth } from '../api/conversation-sync.generated';

afterEach(() => vi.restoreAllMocks());
const health: ConnectionHealth = {
  state: 'degraded', transport: 'conversation-sse', observedAt: 1,
  generation: 1, retryCount: 1,
};

describe('connection health observer boundary', () => {
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
