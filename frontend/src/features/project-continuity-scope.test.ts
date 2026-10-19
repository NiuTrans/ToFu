import { describe, expect, it, vi } from 'vitest';
import { assertContinuityScope, createContinuityReadFence, ownsContinuityScope, type ContinuityScope } from './project-continuity-scope';
const scope = (): ContinuityScope => ({ path: '/project/', conversationId: 'coord', valid: () => true,
  binding: { projectPath: '/project', conversationId: 'coord', revision: 7, availability: 'available',
    capabilities: { assign: false, sendMessage: false, steerSubagent: false } },
  readBinding: vi.fn(async () => scope().binding) });
describe('continuity scope without DOM', () => {
  it('accepts only the latest reply in the same live scope', () => {
    let valid = true; const fence = createContinuityReadFence(() => valid);
    const first = fence.begin(), second = fence.begin();
    expect(fence.accepts(first)).toBe(false); expect(fence.accepts(second)).toBe(true);
    valid = false; expect(fence.accepts(second)).toBe(false);
  });
  it('requires the actual coordinator, not an assign capability or title', async () => {
    const value = scope(); expect(ownsContinuityScope(value)).toBe(true);
    value.binding.conversationId = 'other'; expect(ownsContinuityScope(value)).toBe(false);
    await expect(assertContinuityScope(value)).rejects.toThrow('stale');
    expect(value.readBinding).not.toHaveBeenCalled();
  });
  it('fences a binding revision change or navigation during the read', async () => {
    const value = scope(); value.readBinding = vi.fn(async () => ({ ...value.binding, revision: 8 }));
    await expect(assertContinuityScope(value)).rejects.toThrow('stale');
    value.readBinding = vi.fn(async () => { value.valid = () => false; return value.binding; });
    await expect(assertContinuityScope(value)).rejects.toThrow('stale');
  });
});
