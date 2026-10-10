import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Tofu } from './dist/index.js';

function stalledFetch(state) {
  return async (_url, { signal }) => {
    state.signal = signal;
    return new Response(new ReadableStream({
      start(controller) {
        signal.addEventListener('abort', () => controller.error(signal.reason), { once: true });
      },
      cancel() { state.cancelled = true; },
    }), { headers: { 'Content-Type': 'application/json' } });
  };
}
async function bounded(promise) {
  let timer;
  try {
    return await Promise.race([promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('test watchdog: request did not settle')), 500);
    })]);
  } finally { clearTimeout(timer); }
}

test('JSON body remains covered by request deadline, even with external signal', async () => {
  for (const external of [undefined, new AbortController().signal]) {
    const state = {};
    const client = new Tofu({ baseUrl: 'https://tofu.test', timeoutMs: 20, fetchImpl: stalledFetch(state) });
    await assert.rejects(bounded(client._json('GET', '/api/v4/capabilities', { signal: external })),
      { name: 'TimeoutError' });
    assert.equal(state.signal.aborted, true);
  }
});

test('external abort after headers interrupts body and preserves reason', async () => {
  const state = {};
  const aborter = new AbortController();
  const client = new Tofu({ baseUrl: 'https://tofu.test', timeoutMs: 200, fetchImpl: stalledFetch(state) });
  const request = client._json('GET', '/api/v4/capabilities', { signal: aborter.signal });
  await Promise.resolve();
  const reason = new Error('caller stopped');
  aborter.abort(reason);
  await assert.rejects(bounded(request), error => error === reason);
});

test('SSE early consumer return cancels its body', async () => {
  let cancelled = false;
  const client = new Tofu({ baseUrl: 'https://tofu.test', timeoutMs: 200, fetchImpl: async () => new Response(new ReadableStream({
    start(controller) { controller.enqueue(new TextEncoder().encode('data: {"type":"delta"}\n\n')); },
    cancel() { cancelled = true; },
  }), { headers: { 'Content-Type': 'text/event-stream' } }) });
  for await (const event of client.stream({ model: { creator_id: 'test', model_id: 'test' }, messages: [] })) {
    assert.equal(event.type, 'delta');
    break;
  }
  assert.equal(cancelled, true);
});

test('already aborted caller is never retried', async () => {
  let calls = 0;
  const aborter = new AbortController();
  aborter.abort(new Error('cancelled before send'));
  const client = new Tofu({ baseUrl: 'https://tofu.test', fetchImpl: async (_url, { signal }) => {
    calls++;
    throw signal.reason;
  } });
  await assert.rejects(bounded(client._jsonWithRetry('GET', '/api/v4/capabilities',
    { signal: aborter.signal })), /cancelled before send/);
  assert.ok(calls <= 1);
});

test('completed success and HTTP error release deadline and abort forwarding', async () => {
  for (const status of [200, 403]) {
    let signal;
    const aborter = new AbortController();
    const client = new Tofu({ baseUrl: 'https://tofu.test', timeoutMs: 20,
      fetchImpl: async (_url, init) => {
        signal = init.signal;
        return new Response(JSON.stringify({ data: {}, meta: {} }), { status });
      } });
    const request = client._json('GET', '/api/v4/capabilities', { signal: aborter.signal });
    if (status === 200) await request;
    else await assert.rejects(request, error => error.status === 403);
    aborter.abort();
    await new Promise(resolve => setTimeout(resolve, 35));
    assert.equal(signal.aborted, false, 'completed response retained deadline or caller listener');
  }
});

test('abort during retry backoff settles without another fetch', async () => {
  const aborter = new AbortController();
  let calls = 0;
  let discarded = false;
  const client = new Tofu({ baseUrl: 'https://tofu.test', timeoutMs: 100,
    fetchImpl: async () => {
      calls++;
      return new Response(new ReadableStream({ cancel() {
        discarded = true;
        queueMicrotask(() => aborter.abort(new Error('cancelled backoff')));
        // A peer cannot hold retry/cancellation cleanup hostage.
        return new Promise(() => {});
      } }), { status: 503, headers: { 'Retry-After': '30' } });
    } });
  await assert.rejects(bounded(client._jsonWithRetry('GET', '/api/v4/capabilities',
    { signal: aborter.signal })), /cancelled backoff/);
  assert.equal(calls, 1);
  assert.equal(discarded, true);
});

test('native Fetch deadline interrupts a real HTTP body stalled after headers', async () => {
  const { createServer } = await import('node:http');
  let sentHeaders = false;
  const server = createServer((_req, response) => {
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.write('{"data":');
    sentHeaders = true;
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  try {
    const client = new Tofu({ baseUrl: `http://127.0.0.1:${server.address().port}`, timeoutMs: 100 });
    await assert.rejects(bounded(client.capabilities()),
      error => ['AbortError', 'TimeoutError'].includes(error.name));
    assert.equal(sentHeaders, true);
  } finally {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
});

function sseFetch(chunks, state) {
  return async () => {
    state.calls = (state.calls || 0) + 1;
    let index = 0;
    return new Response(new ReadableStream({
      pull(controller) {
        if (index < chunks.length) controller.enqueue(chunks[index++]);
        else state.exhausted = true; // Leave transport open: parser must cancel it.
      },
      cancel() { state.cancelled = true; },
    }), { headers: { 'Content-Type': 'text/event-stream' } });
  };
}

test('SSE byte limits reject an unterminated line and multi-line frame without reconnect', async () => {
  const { SDK_SSE_MAX_LINE_BYTES, SDK_SSE_MAX_EVENT_BYTES } = await import('./dist/api-v4.generated.js');
  const comment = new TextEncoder().encode(':' + 'x'.repeat(8190) + '\n');
  for (const chunks of [
    [new Uint8Array(SDK_SSE_MAX_LINE_BYTES + 1).fill(120)],
    Array(Math.floor(SDK_SSE_MAX_EVENT_BYTES / comment.length) + 1).fill(comment),
  ]) {
    const state = {};
    const client = new Tofu({ baseUrl: 'https://tofu.test', fetchImpl: sseFetch(chunks, state) });
    await assert.rejects(async () => { for await (const event of client.tasks.stream('task')) {} },
      error => error.status === 413 && error.body.error.kind === 'stream_resource_exhausted');
    assert.equal(state.calls, 1);
    assert.equal(state.cancelled, true);
  }
});

test('SSE framing preserves UTF-8 and CRLF split across network chunks', async () => {
  const wire = new TextEncoder().encode('id: 7\r\nevent: done\r\ndata: {"text":"界"}\r\n\r\n');
  const state = {};
  const chunks = Array.from(wire, byte => new Uint8Array([byte]));
  const client = new Tofu({ baseUrl: 'https://tofu.test', fetchImpl: sseFetch(chunks, state) });
  const events = [];
  for await (const event of client.stream({messages: [], model: {creator_id: 'test', model_id: 'test'}})) events.push(event);
  assert.deepEqual(events, [{text: '界', event: 'done', seq: 7}]);
});

test('SSE frame limit does not reject a transport chunk after its terminal event', async () => {
  const { SDK_SSE_MAX_EVENT_BYTES } = await import('./dist/api-v4.generated.js');
  const wire = new Uint8Array(SDK_SSE_MAX_EVENT_BYTES + 1).fill(120);
  wire.set(new TextEncoder().encode('data: {"type":"done"}\n\n'));
  const state = {};
  const client = new Tofu({ baseUrl: 'https://tofu.test', fetchImpl: sseFetch([wire], state) });
  const events = [];
  for await (const event of client.tasks.stream('task')) events.push(event);
  assert.deepEqual(events, [{type: 'done'}]);
  assert.equal(state.cancelled, true);
  assert.equal(state.calls, 1);
});

test('Anthropic message_stop ends the stream and releases its reader', async () => {
  const state = {};
  const client = new Tofu({ baseUrl: 'https://tofu.test', fetchImpl: sseFetch([
    new TextEncoder().encode('event: message_stop\ndata: {}\n\n'),
    new TextEncoder().encode('data: {"unexpected":true}\n\n'),
  ], state) });
  const events = [];
  for await (const event of client.stream({messages: [], model: {creator_id: 'test', model_id: 'test'}})) events.push(event);
  assert.deepEqual(events, [{event: 'message_stop'}]);
  assert.equal(state.cancelled, true);
});

test('dense SSE frames scan each input byte a bounded number of times', async () => {
  const { SSELineFramer } = await import('./dist/sse-framer.js');
  class MeasuredBytes extends Uint8Array {
    scanned = 0;
    indexOf(value, start = 0) {
      const result = super.indexOf(value, start);
      this.scanned += result >= 0 ? result - start + 1 : this.length - start;
      return result;
    }
  }
  const bytes = new MeasuredBytes(new TextEncoder().encode(':\n\n'.repeat(10000)));
  assert.equal(Array.from(new SSELineFramer().feed(bytes)).length, 20000);
  assert.ok(bytes.scanned <= 3 * bytes.length);
});
