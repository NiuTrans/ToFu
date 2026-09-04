# @rangehow/tofu-sdk

Dependency-free TypeScript/JavaScript client for the Tofu agent runtime and
full headless API. It uses the standard Fetch API and runs on Node 18+, modern
browsers, Cloudflare Workers, Vercel Edge, Deno, and Bun.

## Install

```bash
npm install @rangehow/tofu-sdk
```

## Structured-model quick start

Configure the owner-scoped `tofu.model-routing/v2` aggregate once, then select
the official model independently from its provider:

```ts
import { Tofu } from '@rangehow/tofu-sdk';

const tofu = new Tofu({
  baseUrl: 'https://tofu-agent.internal',
  apiKey: 'sidecar-token',
});

const result = await tofu.agents.run({
  messages: [{ role: 'user', content: 'Research this issue' }],
  model: { creator_id: 'anthropic', model_id: 'claude-opus-4-7' },
  config: { thinking: 'high', tools: ['search', 'fetch'] },
});
console.log(result.content);
```

Provider preference is orthogonal to model identity:

```ts
const result = await tofu.agents.run({
  messages: [{ role: 'user', content: 'Evaluate this model' }],
  model: { creator_id: 'anthropic', model_id: 'claude-opus-4-7' },
  routing: { preferred_provider_id: 'provider-cluster-a' },
});
```

Native calls reject plain model strings, `model@provider`, and inline
secret-bearing provider blocks. A standalone `tofu-agent` that advertises a
configured default in `/api/v1/capabilities` may omit `model`; full ChatUI
native endpoints require the structured identity.

## Resumable streaming

```ts
for await (const event of tofu.agents.stream({
  messages: [{ role: 'user', content: 'Inspect this project' }],
  model: { creator_id: 'anthropic', model_id: 'claude-opus-4-7' },
  config: { tools: ['search', 'fetch'] },
})) {
  console.log(event);
}
```

`agents.run` retries with one stable idempotency key. `agents.start` returns an
HTTP 202 task handle. `agents.stream` submits once and resumes the existing SSE
task stream from `last_seq + 1` after a transport drop.

## Lightweight versus full server

| SDK call | Lightweight sidecar |
|---|---|
| `agents.run/start/stream` | Supported |
| `tasks.get/events/stream/abort` | Supported |
| `capabilities()` | Supported |
| Chat compatibility, feature agents, keys, webhooks | Full application only |

Unknown additive response fields and event types remain available as ordinary
wire objects. Use `capabilities()` to select only installed features.

## Options

```ts
new Tofu({
  baseUrl: 'https://tofu-agent.internal',
  apiKey: 'sidecar-token',       // optional for loopback tokenless mode
  timeoutMs: 600_000,
  userAgent: 'my-product/1.0',
  fetchImpl: customFetch,
});
```

The client adds Bearer auth when `apiKey` is present. It retries safe GETs and
idempotent agent submissions on transport errors, 429, and 5xx responses with
bounded backoff. `TofuError` retains the HTTP status and structured body.
