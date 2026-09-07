import assert from 'node:assert/strict';

import {
  requireDesktopApiCompatibility,
  Tofu,
  TofuError,
  VERSION,
} from './dist/index.js';


assert.equal(VERSION, '0.17.0');

const apiMeta = {
  data: {
    apiMajor: 4,
    schemaVersion: 28,
    serverBuild: '0.17.0',
    minDesktopBuild: '0.16.0',
    minAndroidBuild: 17,
  },
  meta: { requestId: 'typescript-live-minimum', serverTimeMs: 1 },
};
assert.equal(requireDesktopApiCompatibility(apiMeta, '0.17.0'), apiMeta);
assert.throws(
  () => requireDesktopApiCompatibility({
    ...apiMeta,
    data: { ...apiMeta.data, minDesktopBuild: '99.0.0' },
  }, '0.17.0'),
  /below server minimum/,
);
assert.throws(
  () => requireDesktopApiCompatibility({
    ...apiMeta,
    data: { ...apiMeta.data, minDesktopBuild: 'latest' },
  }, '0.17.0'),
  /dotted numeric/,
);

const requests = [];
const fetchImpl = async (url, init = {}) => {
  const path = new URL(String(url)).pathname;
  const body = init.body ? JSON.parse(String(init.body)) : null;
  const headers = new Headers(init.headers || {});
  requests.push({ path, body, headers });

  assert.equal(headers.get('Authorization'), 'Bearer sidecar-token');
  if (path === '/api/v1/agent/run' && body.async === true) {
    return new Response(JSON.stringify({
      ok: true,
      id: 'run-stream',
      task_id: 'task-stream',
      status: 'running',
      model: 'managed-model',
    }), { status: 202, headers: { 'Content-Type': 'application/json' } });
  }
  if (path === '/api/v1/agent/run') {
    return new Response(JSON.stringify({
      ok: true,
      id: 'run-blocking',
      object: 'agent.run',
      task_id: 'task-blocking',
      status: 'done',
      model: 'managed-model',
      finish_reason: 'stop',
      content: 'sdk-ok',
      thinking: '',
      usage: {},
      n_tool_rounds: 0,
    }), { headers: { 'Content-Type': 'application/json' } });
  }
  if (path === '/api/v1/tasks/task-stream/stream') {
    return new Response(
      'id: 0\ndata: {"type":"delta","content":"sdk-"}\n\n'
      + 'id: 1\ndata: {"type":"done","finishReason":"stop"}\n\n',
      { headers: { 'Content-Type': 'text/event-stream' } },
    );
  }
  throw new Error(`unexpected request: ${path}`);
};

const client = new Tofu({
  baseUrl: 'https://tofu.example/',
  apiKey: 'sidecar-token',
  fetchImpl,
});

const result = await client.agents.run({
  messages: [{ role: 'user', content: 'hello' }],
}, { idempotencyKey: 'stable-run-key', maxRetries: 0 });
assert.equal(result.content, 'sdk-ok');
assert.equal(requests[0].body.model, undefined);
assert.equal(requests[0].body.stream, false);
assert.equal(requests[0].headers.get('Idempotency-Key'), 'stable-run-key');

const structuredModel = { creator_id: 'anthropic', model_id: 'claude' };
await client.agents.run({
  messages: [{ role: 'user', content: 'structured' }],
  model: structuredModel,
}, { maxRetries: 0 });
structuredModel.model_id = 'mutated-after-submit';
assert.deepEqual(requests[1].body.model, {
  creator_id: 'anthropic',
  model_id: 'claude',
});
assert.throws(
  () => client.agents.run({
    messages: [{ role: 'user', content: 'legacy' }],
    provider: { api_key: 'must-not-send' },
  }, { maxRetries: 0 }),
  /inline provider blocks were removed/,
);
assert.throws(
  () => client.chat({
    messages: [{ role: 'user', content: 'legacy chat' }],
    model: 'claude',
  }),
  /model must be/,
);

const events = [];
for await (const event of client.agents.stream({
  messages: [{ role: 'user', content: 'stream' }],
}, { idempotencyKey: 'stable-stream-key', maxReconnects: 0 })) {
  events.push(event);
}
assert.deepEqual(events.map(event => event.type), ['delta', 'done']);
assert.deepEqual(events.map(event => event.seq), [0, 1]);
assert.equal(requests[2].headers.get('Idempotency-Key'), 'stable-stream-key');
assert.equal(requests[2].headers.get('Prefer'), 'respond-async');

const failing = new Tofu({
  baseUrl: 'https://tofu.example',
  fetchImpl: async () => new Response('plain upstream failure', { status: 400 }),
});
await assert.rejects(
  failing.agents.run({
    messages: [{ role: 'user', content: 'fail' }],
  }, { maxRetries: 0 }),
  error => error instanceof TofuError && error.body === 'plain upstream failure',
);

console.log('TypeScript SDK contract: ok');
