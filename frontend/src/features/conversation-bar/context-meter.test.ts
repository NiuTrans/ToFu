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
