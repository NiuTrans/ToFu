import { describe, expect, it } from 'vitest';
import {
  CONVERSATION_HISTORY_BYTE_GROWTH,
  conversationLoadBudget,
  nextHistoryPageBytes,
} from './conversation-load-budget';

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

describe('chained history page budget', () => {
  const viewport = 1024 * 1024;
  const byteBound = (windowBytes: number) => ({ windowBytes, turns: 1, limit: 64, hasMore: true });

  it('doubles after a page cut short by bytes, up to the growth limit', () => {
    let budget = viewport;
    const budgets = [];
    for (let page = 0; page < 4; page += 1) {
      budget = nextHistoryPageBytes(viewport, byteBound(budget));
      budgets.push(budget);
    }
    expect(budgets).toEqual([2, 4, 4, 4].map(factor => factor * viewport));
    expect(CONVERSATION_HISTORY_BYTE_GROWTH * viewport).toBe(4 * viewport);
  });

  it('returns to the viewport budget when the Turn count or history bounds the page', () => {
    expect(nextHistoryPageBytes(viewport, { windowBytes: 4 * viewport, turns: 64, limit: 64, hasMore: true }))
      .toBe(viewport);
    expect(nextHistoryPageBytes(viewport, { windowBytes: 4 * viewport, turns: 1, limit: 64, hasMore: false }))
      .toBe(viewport);
  });

  it('never exceeds the 16 MiB contract ceiling', () => {
    const large = 8 * 1024 * 1024;
    expect(nextHistoryPageBytes(large, byteBound(large))).toBe(16 * 1024 * 1024);
    expect(nextHistoryPageBytes(16 * 1024 * 1024, byteBound(16 * 1024 * 1024))).toBe(16 * 1024 * 1024);
  });
});
