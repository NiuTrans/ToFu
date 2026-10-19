import { describe, expect, it, vi } from 'vitest';
import { createSelectionBranch } from './selection-branch';

describe('selected passage branch handoff', () => {
  it('enters only the acknowledged lane, after creation settles', async () => {
    let acknowledge!: (value: { lane: { laneId: string } }) => void;
    const open = vi.fn();
    const create = vi.fn(() => new Promise<{ lane: { laneId: string } }>(resolve => { acknowledge = resolve; }));
    const completion = createSelectionBranch({ create, isCurrent: () => true, open, error: vi.fn() });
    expect(open).not.toHaveBeenCalled();
    acknowledge({ lane: { laneId: 'server-lane' } });
    expect(await completion).toBe(true);
    expect(open).toHaveBeenCalledExactlyOnceWith('server-lane');
    expect(create).toHaveBeenCalledOnce();
  });
  it('does not change the composer after the user navigates away', async () => {
    const open = vi.fn(), error = vi.fn();
    expect(await createSelectionBranch({ create: async () => ({ lane: { laneId: 'old-chat' } }), isCurrent: () => false, open, error })).toBe(true);
    expect(open).not.toHaveBeenCalled();
    expect(error).not.toHaveBeenCalled();
  });
  it('keeps a rejected creation retryable and reports the real error', async () => {
    const failure = new Error('Revision conflict');
    const open = vi.fn(), error = vi.fn();
    expect(await createSelectionBranch({ create: async () => { throw failure; }, isCurrent: () => true, open, error })).toBe(false);
    expect(open).not.toHaveBeenCalled();
    expect(error).toHaveBeenCalledExactlyOnceWith(failure);
  });
  it('does not create a second lane after an acknowledged presentation failure', async () => {
    const failure = new Error('Surface unavailable'), error = vi.fn();
    expect(await createSelectionBranch({ create: async () => ({ lane: { laneId: 'created' } }), isCurrent: () => true, open: () => { throw failure; }, error })).toBe(true);
    expect(error).toHaveBeenCalledExactlyOnceWith(failure);
  });
});
