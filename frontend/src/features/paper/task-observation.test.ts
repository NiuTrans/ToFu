/** One media observation lifetime owns replay, reads, wakeups and teardown. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { featureRegistry } from '../../feature-registry';
import { createPaperTaskObserver, type PaperPushEvent } from './push-transport';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function fixture() {
  let taskId = 'task-a';
  let push!: (event: PaperPushEvent) => void;
  const state = { status: 'generating', lastEventAt: 1 };
  const unsubscribe = vi.fn();
  featureRegistry.pushSubscribe = vi.fn((_channel, _taskId, handler) => { push = handler; });
  featureRegistry.pushUnsubscribe = unsubscribe;
  const read = vi.fn(async (_taskId: string, _cursor: number): Promise<Response | null> =>
    Response.json({ ok: true, done: false, events: [], next_cursor: 0 }));
  const consume = vi.fn(() => true);
  const snapshot = vi.fn();
  const terminal = vi.fn();
  const lost = vi.fn();
  const render = vi.fn();
  const progress = vi.fn();
  const activity = vi.fn();
  const warn = vi.fn();
  const observer = createPaperTaskObserver({
    channel: 'paper', pollIntervalMs: 1200,
    state: () => state, taskId: () => taskId, read, consume, snapshot, terminal, lost,
    presentation: { render, progress, activity }, warn,
  });
  return { observer, state, read, consume, snapshot, terminal, lost, render,
    progress, activity, unsubscribe, warn, push: (event: PaperPushEvent) => push(event),
    setTask: (value: string) => { taskId = value; } };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.stubGlobal('window', globalThis);
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('Paper task observation', () => {
  it('deduplicates push and replay while retaining the producer cursor', async () => {
    const f = fixture();
    f.observer.schedule();
    f.push({ seq: 0, type: 'phase_started' });
    f.read.mockResolvedValueOnce(Response.json({ ok: true, done: false,
      events: [{ seq: 0, type: 'phase_started' }, { seq: 1, type: 'progress' }],
      cursor: { next: 2 } }));
    await f.observer.poll();
    await f.observer.poll();
    expect(f.consume).toHaveBeenCalledTimes(2);
    expect(f.read.mock.calls).toEqual([['task-a', 0], ['task-a', 2]]);
    f.observer.stop();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('rearms one timer and subscription without duplicating the activity ticker', async () => {
    const f = fixture();
    for (let i = 0; i < 20; i++) {
      f.observer.schedule();
      f.observer.startActivity();
    }
    expect(featureRegistry.pushSubscribe).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(2);
    await vi.advanceTimersByTimeAsync(1200);
    expect(f.read).toHaveBeenCalledTimes(1);
    expect(featureRegistry.pushSubscribe).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(2);
    f.observer.reset();
    expect(vi.getTimerCount()).toBe(0);
    f.observer.schedule();
    f.observer.startActivity();
    expect(featureRegistry.pushSubscribe).toHaveBeenCalledTimes(2);
    expect(vi.getTimerCount()).toBe(2);
    f.observer.stop();
    expect(f.unsubscribe).toHaveBeenCalledTimes(2);
  });

  it('keeps one in-flight read and wakes terminal push without a sleeping duplicate', async () => {
    const f = fixture();
    const pending = deferred<Response>();
    f.read.mockReturnValueOnce(pending.promise);
    f.observer.schedule();
    f.push({ seq: 1, type: 'done' });
    await f.observer.poll();
    expect(f.read).toHaveBeenCalledTimes(1);
    pending.resolve(Response.json({ ok: true, done: true, status: 'done' }));
    await vi.advanceTimersByTimeAsync(0);
    expect(f.terminal).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(5000);
    expect(f.read).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('keeps the activity clock ticking between silent reads and stops it on detach', async () => {
    const f = fixture();
    await f.observer.poll();
    const painted = f.activity.mock.calls.length;
    await vi.advanceTimersByTimeAsync(1000);
    expect(f.activity).toHaveBeenCalledTimes(painted + 1);
    expect(f.state.lastEventAt).toBe(1);
    await vi.advanceTimersByTimeAsync(300);
    expect(f.state.lastEventAt).toBe(1);
    f.observer.stop();
    const stopped = f.activity.mock.calls.length;
    await vi.advanceTimersByTimeAsync(5000);
    expect(f.activity).toHaveBeenCalledTimes(stopped);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('preserves five consecutive failed reads and resets that sequence after success', async () => {
    const f = fixture();
    f.read.mockResolvedValue(null);
    for (let i = 0; i < 4; i++) await f.observer.poll();
    expect(f.lost).not.toHaveBeenCalled();
    f.read.mockResolvedValueOnce(Response.json({ ok: true, done: false }));
    await f.observer.poll();
    for (let i = 0; i < 4; i++) await f.observer.poll();
    expect(f.lost).not.toHaveBeenCalled();
    await f.observer.poll();
    expect(f.lost).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each(['success', 'failure'] as const)(
    'fences a detached %s even when the same task is reattached before it settles', async (result) => {
      const f = fixture();
      const old = deferred<Response>();
      f.read.mockReturnValueOnce(old.promise);
      const previous = f.observer.poll();
      f.observer.reset();
      await f.observer.poll();
      if (result === 'success') old.resolve(Response.json({ ok: true, done: true }));
      else old.reject(new Error('old request failed'));
      await previous;
      expect(f.read).toHaveBeenCalledTimes(2);
      expect(f.snapshot).toHaveBeenCalledTimes(1);
      expect(f.terminal).not.toHaveBeenCalled();
      expect(f.lost).not.toHaveBeenCalled();
      f.observer.stop();
    },
  );

  it('rejects late task and push callbacks after replacement or close', async () => {
    const f = fixture();
    f.observer.schedule();
    const oldPush = f.push;
    f.setTask('task-b');
    oldPush({ seq: 9, type: 'phase_started' });
    expect(f.consume).not.toHaveBeenCalled();
    f.observer.reset();
    f.observer.schedule();
    f.observer.stop();
    f.push({ seq: 10, type: 'progress' });
    expect(f.consume).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });
});
