import assert from 'node:assert/strict';

import { readFileSync } from 'node:fs';

import {
  requireDesktopApiCompatibility,
  Tofu,
  TofuError,
  VERSION,
} from './dist/index.js';


const packageMetadata = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8'));
const packageLock = JSON.parse(readFileSync(new URL('./package-lock.json', import.meta.url), 'utf8'));
assert.equal(VERSION, packageMetadata.version);
assert.equal(packageLock.version, packageMetadata.version);
assert.equal(packageLock.packages[''].version, packageMetadata.version);

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

function success(body, status = 200) {
  const { ok, ...data } = body;
  return new Response(JSON.stringify({ data, meta: { requestId: 'sdk-test', serverTimeMs: 1 } }),
    { status, headers: { 'Content-Type': 'application/json' } });
}

const requests = [];
const fetchImpl = async (url, init = {}) => {
  const path = new URL(String(url)).pathname;
  const body = init.body ? JSON.parse(String(init.body)) : null;
  const headers = new Headers(init.headers || {});
  requests.push({ path, body, headers });

  assert.equal(headers.get('Authorization'), 'Bearer sidecar-token');
  if (path === '/api/v4/agent/run' && body.async === true) {
    return success({
      ok: true,
      id: 'run-stream',
      task_id: 'task-stream',
      status: 'running',
      model: 'managed-model',
    }, 202);
  }
  if (path === '/api/v4/agent/run') {
    return success({
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
    });
  }
  if (path === '/api/v4/tasks/task-stream/stream') {
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
assert.equal(result.ok, true);
assert.equal(requests[0].body.model, undefined);
assert.equal(requests[0].body.stream, false);
assert.equal(requests[0].headers.get('Idempotency-Key'), 'stable-run-key');

assert.equal(requests[0].headers.get('User-Agent'), `tofu-sdk-ts/${packageMetadata.version}`);

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

// Public methods restore the historical success discriminator once only.
for (const [data, expected] of [
  [{ status: 'connected' }, { ok: true, status: 'connected' }],
  [{ ok: false }, { ok: false }],
  [{ data: { value: 1 }, meta: { domain: true } },
   { ok: true, data: { value: 1 }, meta: { domain: true } }],
]) {
  const sdk = new Tofu({ baseUrl: 'https://tofu.example', fetchImpl: async () =>
    new Response(JSON.stringify({ data, meta: {} })) });
  assert.deepEqual(await sdk.capabilities(), expected);
}
const errorBody = { status: 403, code: 'permission_denied', data: { private: 'shape' }, meta: {} };
const rejectedSdk = new Tofu({ baseUrl: 'https://tofu.example', fetchImpl: async () =>
  new Response(JSON.stringify(errorBody), { status: 403 }) });
await assert.rejects(rejectedSdk.capabilities(), error => {
  assert.ok(error instanceof TofuError);
  assert.deepEqual(error.body, errorBody);
  return true;
});
