// @vitest-environment jsdom
/** Read deadlines and cancellation use the real transport, including body reads. */
import { afterEach, expect, it, vi } from 'vitest';
import { apiTransport } from '../transport';
import { createEndpointPorts } from './ports';
import { createProjectEndpoints } from './project';

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

const project = () => createProjectEndpoints(createEndpointPorts(apiTransport, () => 'en'));
const reads = (api: ReturnType<typeof project>, signal?: AbortSignal) => [
  api.coordination('/project', signal), api.board('/project', signal),
  api.coordinatorReports('/project', signal), api.coordinatorBudget('/project', signal),
  api.coordinationAssignments('/project', undefined, undefined, signal),
  api.workerConversations('/project', undefined, signal),
];

it.each(['headers', 'body'])('settles stalled Coordinator %s reads at the transport deadline and allows retry', async phase => {
  vi.useFakeTimers();
  const signals: AbortSignal[] = [];
  vi.stubGlobal('fetch', vi.fn((_url: string, init: RequestInit) => {
    const signal = init.signal!; signals.push(signal);
    const stalled = new Promise<never>((_resolve, reject) => {
      signal.addEventListener('abort', () => reject(signal.reason), { once: true });
    });
    return phase === 'headers' ? stalled : Promise.resolve({
      ok: true, status: 200, headers: new Headers({ 'content-type': 'application/json' }),
      text: () => stalled,
    });
  }));
  const api = project(), completion = Promise.allSettled(reads(api));
  await vi.advanceTimersByTimeAsync(12_000);
  const outcomes = await completion;
  expect(signals).toHaveLength(6);
  expect(signals.every(signal => signal.aborted)).toBe(true);
  expect(outcomes[1]).toEqual({ status: 'fulfilled', value: null });
  for (const index of [0, 2, 3, 4, 5]) {
    expect(outcomes[index]).toMatchObject({ status: 'rejected', reason: { code: 'timeout' } });
  }
  expect(vi.getTimerCount()).toBe(0);
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ meta: {}, data: { conversationId: 'leader' } }),
    { headers: { 'content-type': 'application/json' } })));
  await expect(api.coordination('/project')).resolves.toMatchObject({ conversationId: 'leader' });
  expect(vi.getTimerCount()).toBe(0);
});

it('aborts every old-scope read without affecting the next scope or retaining deadline timers', async () => {
  vi.useFakeTimers();
  const signals: AbortSignal[] = [];
  vi.stubGlobal('fetch', vi.fn((_url: string, init: RequestInit) => new Promise((_resolve, reject) => {
    const signal = init.signal!; signals.push(signal);
    signal.addEventListener('abort', () => reject(signal.reason), { once: true });
  })));
  const api = project(), old = new AbortController(), next = new AbortController();
  const oldReads = Promise.allSettled(reads(api, old.signal));
  const nextRead = api.coordination('/next', next.signal).catch(error => error);
  old.abort(); await oldReads;
  expect(signals.slice(0, 6).every(signal => signal.aborted)).toBe(true);
  expect(signals[6].aborted).toBe(false);
  expect(vi.getTimerCount()).toBe(1);
  next.abort(); await nextRead;
  expect(vi.getTimerCount()).toBe(0);
});
