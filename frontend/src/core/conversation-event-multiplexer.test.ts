// @vitest-environment jsdom
/** One page response carries every conversation stream with each stream's own semantics. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { conversationSyncApi } from '../api/conversation-sync.generated';
import {
  conversationStreamRetryDelayMs,
  createConversationEventMultiplexer,
  type ConversationEventSource,
} from './conversation-event-multiplexer';

class NativeSource extends EventTarget {
  readyState = 0;
  closed = false;
  constructor(readonly url: string) { super(); }
  close() { this.closed = true; this.readyState = 2; }
  get query() { return new URL(this.url, 'http://localhost').searchParams; }
  get streams() { return JSON.parse(this.query.get('streams') || '[]') as Array<Record<string, unknown>>; }
  open() { this.readyState = 1; this.dispatchEvent(new Event('open')); }
  fail(readyState: 0 | 2) { this.readyState = readyState; this.dispatchEvent(new Event('error')); }
  frame(index: number | string, type: string, fields: object, cursor = '') {
    this.dispatchEvent(new MessageEvent(type, { lastEventId: cursor, data:
      `${index}\n${JSON.stringify({ contract: 'tofu.conversation-sync.event/v1', type, ...fields })}` }));
  }
  closeStream(index: number, conversationId: string, status = 204, code = 'superseded') {
    this.dispatchEvent(new MessageEvent('stream.closed', { data: `${index}\n${JSON.stringify({
      contract: 'tofu.conversation-sync.stream-closed/v1', type: 'stream.closed',
      conversationId, status, code })}` }));
  }
}

interface Observed { source: ConversationEventSource; log: string[] }

function fixture(streamClientId = 'page-a') {
  const natives: NativeSource[] = [];
  const onProtocolError = vi.fn();
  const multiplexer = createConversationEventMultiplexer({
    api: conversationSyncApi, streamClientId, onProtocolError,
    eventSourceFactory: url => {
      const native = new NativeSource(url); natives.push(native);
      return native as unknown as EventSource;
    },
  });
  const open = (conversationId: string, after = `${conversationId}-1`,
    extra: { windowBytes?: number; cursorSource?: 'snapshot' } = {}): Observed => {
    const log: string[] = [];
    const source = multiplexer.open({ conversationId, after, ...extra });
    source.onopen = () => log.push('open');
    source.onerror = () => log.push(`error:${source.readyState}`);
    for (const type of ['sync.heartbeat', 'turn.upsert']) {
      source.addEventListener(type, event => {
        const message = event as MessageEvent<string>;
        log.push(`${type}:${message.lastEventId}:${JSON.parse(message.data).conversationId}`);
      });
    }
    return { source, log };
  };
  return { natives, open, onProtocolError };
}

const settle = () => vi.advanceTimersByTimeAsync(0);

beforeEach(() => { vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

describe('one shared response', () => {
  it('carries every stream opened in one task on a single EventSource', async () => {
    const { natives, open } = fixture();
    open('a', 'a-1', { windowBytes: 65536, cursorSource: 'snapshot' });
    open('b', 'b-4', { windowBytes: 65536 });
    open('c', 'c +/&1');
    await settle();
    expect(natives).toHaveLength(1);
    expect(natives[0].query.get('streamClientId')).toBe('page-a');
    expect(natives[0].query.get('diagnosticPayload')).toBe('none');
    expect(natives[0].streams).toEqual([
      { conversationId: 'a', after: 'a-1', streamGeneration: 1, windowBytes: 65536, cursorSource: 'snapshot' },
      { conversationId: 'b', after: 'b-4', streamGeneration: 1, windowBytes: 65536 },
      { conversationId: 'c', after: 'c +/&1', streamGeneration: 1 },
    ]);
  });

  it('routes each frame only to its stream with that stream cursor', async () => {
    const { natives, open } = fixture();
    const a = open('a'); const b = open('b');
    await settle();
    natives[0].open();
    natives[0].frame(1, 'sync.heartbeat', { conversationId: 'b' }, 'b-2');
    natives[0].frame(0, 'turn.upsert', { conversationId: 'a' }, 'a-7');
    expect(a.log).toEqual(['open', 'turn.upsert:a-7:a']);
    expect(b.log).toEqual(['open', 'sync.heartbeat:b-2:b']);
    expect(a.source.readyState).toBe(1);
  });
});

describe('membership', () => {
  it('replaces the response once per change from each stream newest cursor', async () => {
    const { natives, open } = fixture();
    const a = open('a', 'a-1', { cursorSource: 'snapshot' }); const b = open('b');
    await settle();
    natives[0].open();
    natives[0].frame(0, 'sync.heartbeat', { conversationId: 'a' }, 'a-3');
    b.source.close();
    const c = open('c');
    await settle();
    expect(natives).toHaveLength(2);
    expect(natives[0].closed).toBe(true);
    // A received a frame, so it resumes like Last-Event-ID: no snapshot marker.
    expect(natives[1].streams).toEqual([
      { conversationId: 'a', after: 'a-3', streamGeneration: 2 },
      { conversationId: 'c', after: 'c-1', streamGeneration: 2 },
    ]);
    natives[1].open();
    // A planned replacement is invisible to a stream that is already open.
    expect(a.log).toEqual(['open', 'sync.heartbeat:a-3:a']);
    expect(c.log).toEqual(['open']);
    // Leaving is the consumer's own act; it emits nothing further.
    expect(b.log).toEqual(['open']);
  });

  it('ignores frames for a stream that left before the replacement', async () => {
    const { natives, open } = fixture();
    const a = open('a'); open('b');
    await settle();
    a.source.close();
    natives[0].frame(0, 'sync.heartbeat', { conversationId: 'a' }, 'a-2');
    expect(a.log).toEqual([]);
    await settle();
    expect(natives).toHaveLength(2);
    expect(natives[1].streams.map(stream => stream.conversationId)).toEqual(['b']);
  });

  it('closes the response and leaves no timer once the last stream leaves', async () => {
    const { natives, open } = fixture();
    const a = open('a'); const b = open('b');
    await settle();
    natives[0].fail(0);
    expect(vi.getTimerCount()).toBe(1);
    a.source.close(); b.source.close();
    expect(vi.getTimerCount()).toBe(0);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(natives).toHaveLength(1);
    expect(natives[0].closed).toBe(true);
  });

  it('supersedes an older stream of the same conversation like the server would', async () => {
    const { natives, open } = fixture();
    const older = open('a', 'a-1');
    const newer = open('a', 'a-5');
    await settle();
    expect(older.source.readyState).toBe(2);
    expect(older.log).toEqual(['error:2']);
    expect(natives).toHaveLength(1);
    expect(natives[0].streams).toEqual([{ conversationId: 'a', after: 'a-5', streamGeneration: 1 }]);
    expect(newer.source.readyState).toBe(0);
  });

  it('requires the page stream client identity', () => {
    const { open } = fixture('');
    expect(() => open('a')).toThrow('stream client id');
  });
});

describe('stream and transport failure', () => {
  it('ends only the stream the server closed', async () => {
    const { natives, open, onProtocolError } = fixture();
    const a = open('a'); const b = open('b');
    await settle();
    natives[0].open();
    natives[0].closeStream(0, 'a', 404, 'not_found');
    expect(a.source.readyState).toBe(2);
    expect(a.log).toEqual(['open', 'error:2']);
    a.source.close();
    await settle();
    expect(natives).toHaveLength(1);
    expect(natives[0].closed).toBe(false);
    natives[0].frame(1, 'sync.heartbeat', { conversationId: 'b' }, 'b-2');
    expect(b.log).toEqual(['open', 'sync.heartbeat:b-2:b']);
    expect(onProtocolError).not.toHaveBeenCalled();
  });

  it('closes the response when its last stream ends', async () => {
    const { natives, open } = fixture();
    open('a');
    await settle();
    natives[0].closeStream(0, 'a');
    expect(natives[0].closed).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('replaces a dropped response after backoff from each stream newest cursor', async () => {
    const { natives, open } = fixture();
    const a = open('a'); const b = open('b');
    await settle();
    natives[0].open();
    natives[0].frame(0, 'sync.heartbeat', { conversationId: 'a' }, 'a-9');
    natives[0].fail(0);
    // Native retry would replay from the bootstrap URL, so it is never used.
    expect(natives[0].closed).toBe(true);
    expect(a.log.at(-1)).toBe('error:0');
    expect(b.log.at(-1)).toBe('error:0');
    const c = open('c');
    await settle();
    expect(natives).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(conversationStreamRetryDelayMs(1) - 1);
    expect(natives).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(natives).toHaveLength(2);
    expect(natives[1].streams).toEqual([
      { conversationId: 'a', after: 'a-9', streamGeneration: 2 },
      { conversationId: 'b', after: 'b-1', streamGeneration: 2 },
      { conversationId: 'c', after: 'c-1', streamGeneration: 2 },
    ]);
    natives[1].open();
    expect(a.log.at(-1)).toBe('open');
    expect(c.log).toEqual(['open']);
  });

  it('grows the backoff until a frame arrives', async () => {
    const { natives, open } = fixture();
    open('a');
    await settle();
    natives[0].fail(0);
    await vi.advanceTimersByTimeAsync(conversationStreamRetryDelayMs(1));
    natives[1].fail(0);
    await vi.advanceTimersByTimeAsync(conversationStreamRetryDelayMs(2) - 1);
    expect(natives).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(natives).toHaveLength(3);
    natives[2].frame(0, 'sync.heartbeat', { conversationId: 'a' }, 'a-2');
    natives[2].fail(0);
    await vi.advanceTimersByTimeAsync(conversationStreamRetryDelayMs(1));
    expect(natives).toHaveLength(4);
  });

  it('ends every stream of a refused response like its own refused stream', async () => {
    const { natives, open } = fixture();
    const a = open('a'); const b = open('b');
    await settle();
    natives[0].fail(2);
    expect([a.source.readyState, b.source.readyState]).toEqual([2, 2]);
    expect(a.log).toEqual(['error:2']);
    expect(b.log).toEqual(['error:2']);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(natives).toHaveLength(1);
  });

  it('reports a frame without a stream index and replaces the response', async () => {
    const { natives, open, onProtocolError } = fixture();
    const a = open('a');
    await settle();
    natives[0].open();
    natives[0].frame('x', 'sync.heartbeat', { conversationId: 'a' }, 'a-2');
    natives[0].frame(4, 'sync.heartbeat', { conversationId: 'a' }, 'a-2');
    expect(onProtocolError).toHaveBeenCalledTimes(1);
    expect(natives[0].closed).toBe(true);
    expect(a.log).toEqual(['open', 'error:0']);
    await vi.advanceTimersByTimeAsync(conversationStreamRetryDelayMs(1));
    expect(natives).toHaveLength(2);
  });
});
