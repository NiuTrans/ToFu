// @vitest-environment jsdom
/** task-stream: SSE frame parsing, cursor resume, bounded reconnect, close. */
import { afterEach, describe, expect, it } from 'vitest';

import { followTaskActivity, type EventSourceLike } from './task-stream';

afterEach(() => { window.history.replaceState({}, '', '/'); });

type FakeSource = EventSourceLike & { url: string; closed: boolean };

function harness() {
  const sources: FakeSource[] = [];
  const timers: Array<{ id: number; fn: () => void; ms: number }> = [];
  let nextId = 0;
  const deps = {
    eventSourceFactory: (url: string) => {
      const source: FakeSource = {
        url,
        closed: false,
        onmessage: null,
        onerror: null,
        close() { this.closed = true; },
      };
      sources.push(source);
      return source;
    },
    setTimeout: (fn: () => void, ms: number) => {
      const id = ++nextId;
      timers.push({ id, fn, ms });
      return id;
    },
    clearTimeout: (id: number) => {
      const index = timers.findIndex((timer) => timer.id === id);
      if (index >= 0) timers.splice(index, 1);
    },
  };
  return { sources, timers, deps };
}

function frame(source: FakeSource, seq: number, payload: object) {
  source.onmessage?.({ data: JSON.stringify(payload), lastEventId: String(seq) });
}

describe('followTaskActivity', () => {
  it('emits tool activity on tool_start and clears on tool_complete', () => {
    const { sources, deps } = harness();
    const seen: Array<string | null> = [];
    followTaskActivity('task-1', {
      toolStart: (activity) => seen.push(`${activity.tool}:${activity.query}`),
      toolEnd: (toolCallId) => seen.push(`end:${toolCallId}`),
    }, deps);
    expect(sources[0].url).toBe('/api/v4/tasks/task-1/stream?cursor=0');
    frame(sources[0], 3, { type: 'tool_start', toolName: 'web_search', query: 'tofu', toolCallId: 'c1' });
    frame(sources[0], 4, { type: 'tool_complete', toolCallId: 'c1' });
    expect(seen).toEqual(['web_search:tofu', 'end:c1']);
  });

  it('ignores malformed frames without throwing', () => {
    const { sources, deps } = harness();
    followTaskActivity('task-1', {}, deps);
    sources[0].onmessage?.({ data: 'not-json', lastEventId: '1' });
    sources[0].onmessage?.({ data: '{"type":"phase"}', lastEventId: '2' });
  });

  it('reconnects with the last cursor and bounded backoff', () => {
    const { sources, timers, deps } = harness();
    followTaskActivity('task-1', {}, deps);
    frame(sources[0], 9, { type: 'phase', label: 'thinking' });
    sources[0].onerror?.();
    expect(sources[0].closed).toBe(true);
    expect(timers).toHaveLength(1);
    expect(timers[0].ms).toBe(1000);
    timers[0].fn();
    expect(sources[1].url).toBe('/api/v4/tasks/task-1/stream?cursor=9');
    sources[1].onerror?.();
    expect(timers[1].ms).toBe(2000);
  });

  it('stops reconnecting after the terminal done frame', () => {
    const { sources, deps } = harness();
    let status = '';
    const stream = followTaskActivity('task-1', { done: (value) => { status = value; } }, deps);
    frame(sources[0], 5, { type: 'done', status: 'done' });
    expect(status).toBe('done');
    expect(sources[0].closed).toBe(true);
    sources[0].onerror?.();
    expect(sources).toHaveLength(1);
    stream.close();
  });

  it('close() tears down the source and pending reconnect', () => {
    const { sources, timers, deps } = harness();
    const stream = followTaskActivity('task-1', {}, deps);
    sources[0].onerror?.();
    expect(timers).toHaveLength(1);
    stream.close();
    expect(timers).toHaveLength(0);
    expect(sources).toHaveLength(1);
    timers.push({ id: 99, fn: () => { throw new Error('must not run'); }, ms: 0 });
  });

  it('encodes task ids in the replay URL', () => {
    const { sources, deps } = harness();
    followTaskActivity('task/with space', {}, deps);
    expect(sources[0].url).toBe('/api/v4/tasks/task%2Fwith%20space/stream?cursor=0');
  });
});


describe('task stream transport ownership', () => {
  it('uses the shared deployment prefix on every reconnect', () => {
    window.history.replaceState({}, '', '/proxy/9000/');
    const { sources, timers, deps } = harness();
    followTaskActivity('task/space', {}, deps);
    expect(sources[0].url).toBe('/proxy/9000/api/v4/tasks/task%2Fspace/stream?cursor=0');
    frame(sources[0], 3, { type: 'phase' });
    sources[0].onerror?.();
    timers[0].fn();
    expect(sources[1].url).toBe('/proxy/9000/api/v4/tasks/task%2Fspace/stream?cursor=3');
  });

  it('fences callbacks from replaced sources and closed streams', () => {
    const { sources, timers, deps } = harness();
    const seen: string[] = [];
    const stream = followTaskActivity('task-1', { done: (status) => seen.push(status) }, deps);
    const oldMessage = sources[0].onmessage!;
    const oldError = sources[0].onerror!;
    oldError();
    oldError();
    expect(timers).toHaveLength(1);
    timers[0].fn();
    oldMessage({ data: '{"type":"done","status":"done"}', lastEventId: '99' });
    oldError();
    expect(sources[1].closed).toBe(false);
    expect(seen).toEqual([]);
    const currentMessage = sources[1].onmessage!;
    stream.close();
    currentMessage({ data: '{"type":"done","status":"done"}', lastEventId: '100' });
    expect(seen).toEqual([]);
    expect(sources[1].onmessage).toBeNull();
    expect(sources[1].onerror).toBeNull();
  });

  it('keeps one monotonic safe cursor and rejects invalid JSON records', () => {
    const { sources, timers, deps } = harness();
    followTaskActivity('task-1', {}, deps);
    frame(sources[0], 9, { type: 'phase' });
    for (const cursor of ['-1', '3', '1.5', 'Infinity', '9007199254740992']) {
      sources[0].onmessage?.({ data: '{"type":"phase"}', lastEventId: cursor });
    }
    for (const data of ['null', '[]', '"text"', 'bad-json']) {
      expect(() => sources[0].onmessage?.({ data, lastEventId: '12' })).not.toThrow();
    }
    sources[0].onerror?.();
    timers[0].fn();
    expect(sources[1].url).toBe('/api/v4/tasks/task-1/stream?cursor=9');
  });

  it('backs off on constructor failure and cancels its retry on close', () => {
    const { timers, deps } = harness();
    let attempts = 0;
    const stream = followTaskActivity('task-1', {}, {
      ...deps,
      eventSourceFactory() { attempts += 1; throw new Error('transport unavailable'); },
    });
    expect(attempts).toBe(1);
    expect(timers[0].ms).toBe(1000);
    timers[0].fn();
    expect(attempts).toBe(2);
    expect(timers[1].ms).toBe(2000);
    const pending = timers[1].fn;
    stream.close();
    pending();
    expect(attempts).toBe(2);
  });
});
