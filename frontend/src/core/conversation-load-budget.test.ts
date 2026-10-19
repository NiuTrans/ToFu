import { describe, expect, it } from 'vitest';
import { conversationLoadBudget } from './conversation-load-budget';

describe('conversation transport working set', () => {
  it('requests a viewport-sized tail instead of all 96 turns', () => {
    expect(conversationLoadBudget({ width: 960, height: 800 })).toEqual({
      windowLimit: 14, windowBytes: 1024 * 1024,
    });
  });
  it('scales up with larger reading panes and down with phones', () => {
    const phone = conversationLoadBudget({ width: 360, height: 640 });
    const desktop = conversationLoadBudget({ width: 1280, height: 960 });
    const large = conversationLoadBudget({ width: 3840, height: 2160 });
    expect(phone.windowLimit).toBeLessThan(desktop.windowLimit);
    expect(phone.windowBytes).toBeLessThan(desktop.windowBytes);
    expect(large.windowLimit).toBeGreaterThan(desktop.windowLimit);
    expect(large.windowBytes).toBeGreaterThan(desktop.windowBytes);
  });
  it('bounds transport growth and treats missing/invalid probes conservatively', () => {
    expect(conversationLoadBudget({ width: Infinity, height: NaN })).toEqual(conversationLoadBudget());
    expect(conversationLoadBudget({ width: -1, height: 0 })).toEqual(conversationLoadBudget());
    expect(conversationLoadBudget({ width: 1e9, height: 1e9 })).toEqual({
      windowLimit: 96, windowBytes: 16 * 1024 * 1024,
    });
    expect(conversationLoadBudget({ width: 1, height: 1 })).toEqual({
      windowLimit: 8, windowBytes: 256 * 1024,
    });
  });
});
