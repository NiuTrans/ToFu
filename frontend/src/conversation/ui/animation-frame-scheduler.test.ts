import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createAnimationFrameScheduler, type AnimationFrameHost } from './animation-frame-scheduler';

beforeEach(() => vi.useFakeTimers());
afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); });

function clock(throwOnCancel = false) {
  const frames: FrameRequestCallback[] = [];
  const cancelFrame = vi.fn();
  const clearTimer = vi.fn((handle: number) => {
    if (throwOnCancel) throw new Error('timer cancellation denied');
    clearTimeout(handle);
  });
  const host: AnimationFrameHost = {
    requestAnimationFrame: callback => { frames.push(callback); return frames.length; },
    cancelAnimationFrame: cancelFrame,
    setTimeout: (callback, delay) => setTimeout(callback, delay) as unknown as number,
    clearTimeout: clearTimer,
  };
  return { host, frames, cancelFrame, clearTimer, scheduler: createAnimationFrameScheduler(host) };
}

describe('bounded DOM render clock', () => {
  it('normal native frames cancel the deadline and commit once', () => {
    const f = clock(); const render = vi.fn(); f.scheduler.schedule(render);
    f.frames[0](Date.now()); vi.advanceTimersByTime(250);
    expect(render).toHaveBeenCalledTimes(1); expect(vi.getTimerCount()).toBe(0);
  });

  it('a withheld frame converges once at the bounded deadline', () => {
    const f = clock(); const render = vi.fn(); f.scheduler.schedule(render);
    vi.advanceTimersByTime(250); f.frames[0](Date.now());
    expect(render).toHaveBeenCalledTimes(1); expect(f.cancelFrame).toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('cancellation fences both callbacks and releases the timer', () => {
    const f = clock(); const render = vi.fn(); const cancel = f.scheduler.schedule(render);
    cancel(); cancel(); f.frames[0](Date.now()); vi.advanceTimersByTime(250);
    expect(render).not.toHaveBeenCalled(); expect(vi.getTimerCount()).toBe(0);
  });

  it('timer cancellation failure cannot skip the commit or duplicate it', () => {
    const f = clock(true); const render = vi.fn(); f.scheduler.schedule(render);
    expect(() => f.frames[0](Date.now())).not.toThrow();
    expect(render).toHaveBeenCalledTimes(1); vi.advanceTimersByTime(250);
    expect(render).toHaveBeenCalledTimes(1); expect(vi.getTimerCount()).toBe(0);
  });

  it('failed cancellation still fences a disposed render', () => {
    const f = clock(true); const render = vi.fn(); const cancel = f.scheduler.schedule(render);
    expect(cancel).not.toThrow(); f.frames[0](Date.now()); vi.advanceTimersByTime(250);
    expect(render).not.toHaveBeenCalled(); expect(vi.getTimerCount()).toBe(0);
  });

  it('supports a synchronous frame host without a trailing deadline', () => {
    const render = vi.fn(); const timer = vi.fn();
    const host: AnimationFrameHost = { requestAnimationFrame: callback => { callback(1); return 1; },
      cancelAnimationFrame: vi.fn(), setTimeout: timer, clearTimeout: vi.fn() };
    createAnimationFrameScheduler(host).schedule(render);
    expect(render).toHaveBeenCalledTimes(1); expect(timer).not.toHaveBeenCalled();
  });
});
