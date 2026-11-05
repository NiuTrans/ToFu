// @vitest-environment jsdom
import { expect, it, vi } from 'vitest';
import { createContextMeter, type ContextMeterSnapshot } from './context-meter';

const { open, close } = vi.hoisted(() => ({ open: vi.fn(), close: vi.fn() }));
vi.mock('../working-facts/dialog', () => ({ openWorkingFacts: open }));
const settle = () => new Promise(resolve => setTimeout(resolve, 0));

it('loads facts only on demand and retires the mounted dialog on navigation and destruction', async () => {
  open.mockReturnValue(close);
  const host = document.createElement('div'); document.body.append(host);
  const snapshot: ContextMeterSnapshot = { used: 0, limit: null, known: false, exact: false, pctRaw: 0,
    pct: null, zone: 'healthy', compactions: 0, convId: 'first', busy: false, hasHistory: false };
  const workingFacts = { load: vi.fn(), save: vi.fn(), consumers: vi.fn() };
  const meter = createContextMeter({ host, read: () => ({ ...snapshot }), translate: (_, fallback) => fallback,
    onCompact: vi.fn(), onHistory: vi.fn(), workingFacts });
  try {
    expect(open).not.toHaveBeenCalled(); expect(workingFacts.load).not.toHaveBeenCalled();
    meter.el.click(); document.querySelector<HTMLButtonElement>('[data-open-working-facts]')!.click();
    snapshot.convId = 'second'; meter.update({ ...snapshot }); await settle(); await settle();
    expect(open).not.toHaveBeenCalled();
    meter.el.click(); document.querySelector<HTMLButtonElement>('[data-open-working-facts]')!.click();
    await settle(); await settle();
    expect(open).toHaveBeenCalledWith('second', workingFacts, expect.any(Function));
    snapshot.convId = 'third'; meter.update({ ...snapshot });
    expect(close).toHaveBeenCalledTimes(1);
    meter.el.click(); document.querySelector<HTMLButtonElement>('[data-open-working-facts]')!.click();
    await settle(); meter.destroy(); expect(close).toHaveBeenCalledTimes(2);
  } finally { meter.destroy(); host.remove(); }
});

it('shows a scheduled working-view compaction and offers to cancel it instead of another one', () => {
  const host = document.createElement('div'); document.body.append(host);
  const snapshot: ContextMeterSnapshot = { used: 0, limit: null, known: false, exact: false, pctRaw: 0,
    pct: null, zone: 'healthy', compactions: 0, convId: 'router', busy: true, hasHistory: false,
    compactionScheduled: true, compactionAppliesAt: 'next_model_call' };
  const onCompact = vi.fn(), onCancelScheduled = vi.fn();
  const meter = createContextMeter({ host, read: () => ({ ...snapshot }), translate: (_, fallback) => fallback,
    onCompact, onCancelScheduled, onHistory: vi.fn() });
  try {
    meter.el.click();
    const action = document.querySelector<HTMLButtonElement>('.ctx-pop-compact')!;
    const status = document.querySelector<HTMLElement>('.ctx-pop-status')!;
    // The pending request survives the run that will apply it.
    expect(status.hidden).toBe(false);
    expect(status.textContent).toBe('Compaction scheduled for the next model call');
    expect(action.textContent).toBe('Cancel scheduled compaction');
    expect(action.disabled).toBe(false);
    expect(meter.el.dataset.compactionScheduled).toBe('1');
    expect(meter.el.getAttribute('aria-label')).toContain('Compaction scheduled for the next model call');
    action.click();
    expect(onCancelScheduled).toHaveBeenCalledWith('router'); expect(onCompact).not.toHaveBeenCalled();
    snapshot.compactionCancelPending = true; meter.update({ ...snapshot });
    expect(action.textContent).toBe('Cancelling…'); expect(action.disabled).toBe(true);
    // A busy conversation without a pending request schedules a new one.
    snapshot.compactionScheduled = false; snapshot.compactionCancelPending = false; meter.update({ ...snapshot });
    expect(status.hidden).toBe(true);
    expect(action.textContent).toBe('Compact at the next model call');
    expect(action.disabled).toBe(false);
    expect(meter.el.dataset.compactionScheduled).toBeUndefined();
    action.click(); expect(onCompact).toHaveBeenCalledWith('router');
    snapshot.busy = false; meter.update({ ...snapshot });
    expect(action.textContent).toBe('Compact context now');
    snapshot.compactionScheduled = true; snapshot.compactionAppliesAt = null; meter.update({ ...snapshot });
    expect(status.textContent).toBe('Compaction scheduled for the next reply');
  } finally { meter.destroy(); host.remove(); }
});

it('cannot offer cancellation without a cancel port', () => {
  const host = document.createElement('div'); document.body.append(host);
  const snapshot: ContextMeterSnapshot = { used: 0, limit: null, known: false, exact: false, pctRaw: 0,
    pct: null, zone: 'healthy', compactions: 0, convId: 'router', busy: false, hasHistory: false,
    compactionScheduled: true };
  const onCompact = vi.fn();
  const meter = createContextMeter({ host, read: () => ({ ...snapshot }), translate: (_, fallback) => fallback,
    onCompact, onHistory: vi.fn() });
  try {
    meter.el.click();
    const action = document.querySelector<HTMLButtonElement>('.ctx-pop-compact')!;
    expect(action.disabled).toBe(true);
    action.click(); expect(onCompact).not.toHaveBeenCalled();
  } finally { meter.destroy(); host.remove(); }
});

it('reports an unknown compaction count until the history listing settles', () => {
  const host = document.createElement('div'); document.body.append(host);
  const snapshot: ContextMeterSnapshot = { used: 0, limit: null, known: false, exact: false, pctRaw: 0,
    pct: null, zone: 'healthy', compactions: null, convId: 'conv', busy: false, hasHistory: false };
  const meter = createContextMeter({ host, read: () => ({ ...snapshot }), translate: (_, fallback) => fallback,
    onCompact: vi.fn(), onHistory: vi.fn() });
  const compactions = () => document.querySelectorAll('#ctxBarPopover dd')[2]?.textContent;
  try {
    meter.el.click();
    expect(compactions()).toBe('Unknown');
    meter.update({ ...snapshot, compactions: 1 });
    expect(compactions()).toBe('1');
  } finally { meter.destroy(); host.remove(); }
});
