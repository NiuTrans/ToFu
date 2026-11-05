// @vitest-environment jsdom
/** Send startup repeats transient failures itself; only the user or a definitive rejection ends a send. */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiTransport } from '../api/transport';
import { createConversationsEndpoints } from '../api/endpoint-registry/conversations';
import { createEndpointPorts } from '../api/endpoint-registry/ports';
import { retryTransientSendStartup } from './send-startup';

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

const failure = (fields: Record<string, unknown>) => Object.assign(new Error('failure'), fields);
const stalledUntilAbort = (signal: AbortSignal) => new Promise<never>((_resolve, reject) => {
  signal.addEventListener('abort', () => reject(signal.reason), { once: true });
});

describe('retryTransientSendStartup', () => {
  it('repeats a transient failure on a fresh attempt signal until it succeeds', async () => {
    const signals: AbortSignal[] = [];
    const retries: number[] = [];
    const run = vi.fn(async (signal: AbortSignal) => {
      signals.push(signal);
      if (signals.length < 3) throw failure({ status: 503 });
      return 'accepted';
    });
    await expect(retryTransientSendStartup({
      signal: new AbortController().signal,
      run,
      retryDelayMs: () => 0,
      onRetry: ({ retry }) => retries.push(retry),
    })).resolves.toBe('accepted');
    expect(run).toHaveBeenCalledTimes(3);
    expect(new Set(signals).size).toBe(3);
    expect(retries).toEqual([1, 2]);
  });

  it('surfaces a definitive failure without repeating it', async () => {
    const definitive = failure({ status: 400 });
    const run = vi.fn(async () => { throw definitive; });
    const onRetry = vi.fn();
    await expect(retryTransientSendStartup({
      signal: new AbortController().signal, run, onRetry, retryDelayMs: () => 0,
    })).rejects.toBe(definitive);
    expect(run).toHaveBeenCalledTimes(1);
    expect(onRetry).not.toHaveBeenCalled();
  });

  it('aborts an attempt at its deadline and repeats it', async () => {
    vi.useFakeTimers();
    const signals: AbortSignal[] = [];
    const run = vi.fn((signal: AbortSignal) => {
      signals.push(signal);
      return signals.length === 1 ? stalledUntilAbort(signal) : Promise.resolve('accepted');
    });
    const pending = retryTransientSendStartup({
      signal: new AbortController().signal, run, attemptTimeoutMs: 1_000, retryDelayMs: () => 10,
    });
    await vi.advanceTimersByTimeAsync(1_010);
    await expect(pending).resolves.toBe('accepted');
    expect(signals[0].aborted).toBe(true);
    expect(run).toHaveBeenCalledTimes(2);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('forwards a user stop into the running attempt and never repeats it', async () => {
    const user = new AbortController();
    const signals: AbortSignal[] = [];
    const run = vi.fn((signal: AbortSignal) => {
      signals.push(signal);
      return stalledUntilAbort(signal);
    });
    const outcome = retryTransientSendStartup({ signal: user.signal, run, retryDelayMs: () => 0 })
      .catch((error: unknown) => error);
    user.abort();
    expect(((await outcome) as DOMException).name).toBe('AbortError');
    expect(signals).toHaveLength(1);
    expect(signals[0].aborted).toBe(true);
  });

  it('keeps an acknowledgement that arrives after the user stopped', async () => {
    const user = new AbortController();
    let acknowledge!: (value: string) => void;
    const run = vi.fn(() => new Promise<string>((resolve) => { acknowledge = resolve; }));
    const outcome = retryTransientSendStartup({ signal: user.signal, run, retryDelayMs: () => 0 });
    user.abort();
    acknowledge('accepted');
    await expect(outcome).resolves.toBe('accepted');
    expect(run).toHaveBeenCalledTimes(1);
  });

  it('ends the backoff when the user stops and starts no further attempt', async () => {
    vi.useFakeTimers();
    const user = new AbortController();
    const run = vi.fn(async () => { throw failure({ status: 0, code: 'network' }); });
    const outcome = retryTransientSendStartup({ signal: user.signal, run, retryDelayMs: () => 5_000 })
      .catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(100);
    user.abort();
    expect(((await outcome) as DOMException).name).toBe('AbortError');
    await vi.advanceTimersByTimeAsync(60_000);
    expect(run).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe('send-time config resolution through a stalled forwarded port', () => {
  const conversations = () => createConversationsEndpoints(createEndpointPorts(apiTransport, () => 'en'));

  it('outlives three stalled resolver attempts instead of failing the send at 30 s', async () => {
    vi.useFakeTimers();
    const signals: AbortSignal[] = [];
    // The forwarded port answered nothing for ~31 s (14:39:52 to 14:40:23).
    const stallEndsAt = Date.now() + 31_000;
    vi.stubGlobal('fetch', vi.fn((_url: string, init: RequestInit) => {
      const signal = init.signal!;
      signals.push(signal);
      if (Date.now() < stallEndsAt) return stalledUntilAbort(signal);
      return Promise.resolve({
        ok: true, status: 200, headers: new Headers({ 'content-type': 'application/json' }),
        text: async () => JSON.stringify({ ok: true, model: 'resolved-model' }),
      });
    }));
    const api = conversations();
    let settled = false;
    const resolution = retryTransientSendStartup({
      signal: new AbortController().signal,
      run: (signal) => api.resolveConfig({ conv_settings: {} }, { signal }),
    });
    void resolution.finally(() => { settled = true; });
    // Each attempt times out at 10 s; the removed 30 s preparation timer fired
    // here while the resolver's third attempt was still in flight.
    await vi.advanceTimersByTimeAsync(30_000);
    expect(settled).toBe(false);
    expect(signals).toHaveLength(3);
    await vi.advanceTimersByTimeAsync(10_000);
    await expect(resolution).resolves.toMatchObject({ model: 'resolved-model' });
    expect(signals).toHaveLength(4);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('stops resolver retries as soon as the send is cancelled', async () => {
    vi.useFakeTimers();
    const signals: AbortSignal[] = [];
    vi.stubGlobal('fetch', vi.fn((_url: string, init: RequestInit) => {
      signals.push(init.signal!);
      return stalledUntilAbort(init.signal!);
    }));
    const api = conversations();
    const user = new AbortController();
    const outcome = retryTransientSendStartup({
      signal: user.signal,
      run: (signal) => api.resolveConfig({ conv_settings: {} }, { signal }),
    }).catch((error: unknown) => error);
    await vi.advanceTimersByTimeAsync(12_000);
    expect(signals).toHaveLength(2);
    expect(signals[1].aborted).toBe(false);
    user.abort();
    expect(await outcome).toMatchObject({ name: 'ApiError', code: 'aborted' });
    // Released at the stop, not left to run out its own 10 s timeout.
    expect(signals[1].aborted).toBe(true);
    await vi.advanceTimersByTimeAsync(120_000);
    expect(signals).toHaveLength(2);
    expect(vi.getTimerCount()).toBe(0);
  });
});
