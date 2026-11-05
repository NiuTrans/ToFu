import { describe, expect, it, vi } from 'vitest';
import { COMPACTION_HISTORY_LIMITS, createCompactionHistoryState } from './compaction-history-state';

/** The fake endpoint answers with the archives that existed when it was called. */
function endpoint() {
  const archives: string[] = [];
  const gates: Array<() => void> = [];
  let calls = 0;
  let failures = 0;
  let gated = false;
  const onRefresh = vi.fn();
  const state = createCompactionHistoryState({
    list: async () => {
      calls += 1;
      const served = archives.map((id, index) => ({ id, createdAt: index + 1 }));
      if (gated) await new Promise<void>((release) => gates.push(release));
      if (failures > 0) {
        failures -= 1;
        throw new Error('synthetic list failure');
      }
      return { compactions: served };
    },
    onRefresh,
  });
  return {
    state,
    archives,
    onRefresh,
    calls: () => calls,
    failNext: () => { failures += 1; },
    hold: () => { gated = true; },
    release: () => { gated = false; gates.splice(0).forEach((release) => release()); },
  };
}

const settled = () => new Promise((resolve) => setTimeout(resolve, 0));

describe('compaction history count', () => {
  it('is unknown until a listing settles, then exact', async () => {
    const api = endpoint();
    expect(api.state.count('conv')).toBeNull();
    await api.state.refresh('conv');
    expect(api.state.count('conv')).toBe(0);
  });

  it('lists once on first observation and never again for unchanged evidence', async () => {
    const api = endpoint();
    api.state.observe('conv', []);
    api.state.observe('conv', []);
    await settled();
    expect(api.calls()).toBe(1);
    expect(api.state.count('conv')).toBe(0);
    for (let render = 0; render < 5; render += 1) api.state.observe('conv', []);
    await settled();
    expect(api.calls()).toBe(1);
    expect(api.onRefresh).toHaveBeenCalledTimes(1);
  });

  it('lists again inside the freshness window when a Turn references a new archive', async () => {
    const api = endpoint();
    await api.state.refresh('conv');
    api.state.observe('conv', []);
    await settled();
    expect(api.state.count('conv')).toBe(0);

    api.archives.push('archive-auto');
    api.state.observe('conv', ['archive-auto']);
    await settled();
    expect(api.calls()).toBe(2);
    expect(api.state.count('conv')).toBe(1);
    expect(api.state.get('conv').map((row) => row.archiveId)).toEqual(['archive-auto']);
    expect(api.onRefresh).toHaveBeenCalledTimes(2);

    api.state.observe('conv', ['archive-auto']);
    await settled();
    expect(api.calls()).toBe(2);
  });

  it('spends one listing on an archive the endpoint never lists', async () => {
    const api = endpoint();
    await api.state.refresh('conv');
    for (let render = 0; render < 5; render += 1) {
      api.state.observe('conv', ['summary-failed:task:3']);
      await settled();
    }
    expect(api.calls()).toBe(2);
    expect(api.state.count('conv')).toBe(0);
  });

  it('reconciles evidence that arrives while a listing is in flight', async () => {
    const api = endpoint();
    api.hold();
    const hydration = api.state.refresh('conv');
    await settled();
    api.archives.push('archive-late');
    api.state.observe('conv', ['archive-late']);
    expect(api.calls()).toBe(1);
    api.release();
    await hydration;
    await settled();
    expect(api.calls()).toBe(2);
    expect(api.state.count('conv')).toBe(1);
  });

  it('retries a failed listing only for new evidence', async () => {
    const api = endpoint();
    api.archives.push('archive-a');
    api.failNext();
    api.state.observe('conv', ['archive-a']);
    await settled();
    api.state.observe('conv', ['archive-a']);
    await settled();
    expect(api.calls()).toBe(1);
    expect(api.state.count('conv')).toBeNull();

    api.archives.push('archive-b');
    api.state.observe('conv', ['archive-a', 'archive-b']);
    await settled();
    expect(api.calls()).toBe(2);
    expect(api.state.count('conv')).toBe(2);
  });

  it('forgets evicted conversations completely, so they list again', async () => {
    const api = endpoint();
    api.state.observe('conv-0', []);
    await settled();
    for (let index = 1; index <= COMPACTION_HISTORY_LIMITS.conversations; index += 1) {
      api.state.observe(`conv-${index}`, []);
      await settled();
    }
    expect(api.state.count('conv-0')).toBeNull();
    const calls = api.calls();
    api.state.observe('conv-0', []);
    await settled();
    expect(api.calls()).toBe(calls + 1);
    expect(api.state.count('conv-0')).toBe(0);
  });
});
