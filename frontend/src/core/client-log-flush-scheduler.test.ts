import { expect, it } from 'vitest';
import { createClientLogFlushScheduler } from './client-log-flush-scheduler';

it('coalesces urgent requests during a flight and flushes fresh evidence on settlement', async () => {
  const timers = new Map<number, () => void>();
  let next = 0, pending = true, flushes = 0;
  let finish!: () => void;
  const scheduler = createClientLogFlushScheduler({
    baseDelayMs: 60000, random: () => 0.5,
    visibility: { hidden: false, addEventListener() {}, removeEventListener() {} },
    schedule: { setTimeout: callback => { timers.set(++next, callback); return next; },
      clearTimeout: id => { timers.delete(id as number); } },
    hasPending: () => pending,
    flush: () => { flushes++; pending = false; return new Promise<void>(resolve => { finish = resolve; }); },
  });
  scheduler.flushNow(); pending = true;
  for (let i = 0; i < 100; i++) scheduler.flushNow();
  expect(flushes).toBe(1); expect(timers.size).toBe(0);
  finish();
  for (let i = 0; i < 5; i++) await Promise.resolve();
  expect(flushes).toBe(2); expect(timers.size).toBe(0);
  pending = true; scheduler.flushNow(); scheduler.destroy(); finish();
  for (let i = 0; i < 5; i++) await Promise.resolve();
  expect(flushes).toBe(2); expect(timers.size).toBe(0);
  expect(scheduler.snapshot().destroyed).toBe(true);
});
