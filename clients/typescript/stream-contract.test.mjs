import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { Tofu, TofuError } from './dist/index.js';

const cases = JSON.parse(readFileSync(new URL('../../tests/fixtures/sdk/stream_frames.json', import.meta.url), 'utf8'));
for (const fixture of cases) {
  test(`shared SSE contract: ${fixture.name}`, async () => {
    const bytes = new TextEncoder().encode(fixture.wire);
    const client = new Tofu({ baseUrl: 'https://sdk.test', fetchImpl: async () => new Response(new ReadableStream({
      start(controller) {
        for (const byte of bytes) controller.enqueue(new Uint8Array([byte]));
        controller.close();
      },
    })) });
    const events = [];
    for await (const event of client.stream({ model: { creator_id: 'test', model_id: 'test' }, messages: [] })) events.push(event);
    assert.deepEqual(events, fixture.expected);
  });
}

test('typed errors retain the body and canonical retry metadata', () => {
  for (const [status, body, kind] of [
    [400, { code: 'invalid_request', status: 400, detail: 'bad' }, 'invalid_request'],
    [429, { error: { kind: 'rate_limited' }, retry_after: 2 }, 'rate_limited'],
    [503, { error_kind: 'upstream_unavailable' }, 'upstream_unavailable'],
  ]) {
    const error = new TofuError(status, body);
    assert.equal(error.body, body);
    assert.equal(error.kind, kind);
    assert.equal(error.retryable, status === 429 || status >= 500);
    assert.equal(error.retryAfter, body.retry_after ?? null);
  }
});

test('task replay ignores missing or invalid sequences and stops at a named terminal frame', async () => {
  const cursors = [];
  let stateReads = 0;
  const client = new Tofu({ baseUrl: 'https://sdk.test', fetchImpl: async input => {
    const url = new URL(input);
    if (url.pathname.endsWith('/stream')) {
      cursors.push(Number(url.searchParams.get('cursor')));
      return new Response(cursors.length === 1
        ? 'data: {"seq":null}\n\ndata: {"seq":true}\n\ndata: {"seq":"1e2"}\n\ndata: {"text":"unsequenced"}\n\n'
        : 'event: done\ndata: {"seq":0}\n\n');
    }
    stateReads += 1;
    return new Response(JSON.stringify({ status: 'running' }));
  } });
  const events = [];
  for await (const event of client.tasks.stream('task', 0, { maxReconnects: 1 })) events.push(event);
  assert.deepEqual(cursors, [0, 0]);
  assert.equal(stateReads, 1);
  assert.deepEqual(events.at(-1), { seq: 0, event: 'done' });
});
