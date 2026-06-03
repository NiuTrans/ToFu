# @tofu/sdk

TypeScript / JavaScript client for the Tofu headless API.

Works in Node ≥18, modern browsers, Cloudflare Workers, Vercel Edge,
Deno, and Bun. No external runtime dependencies.

## Install (development)

```bash
cd clients/typescript
npm install     # only if you want to compile to JS
# or just import .ts directly:
#   import { Tofu } from './clients/typescript/src/index.ts';
```

## Quick start

```ts
import { Tofu } from '@tofu/sdk';

const client = new Tofu({
  baseUrl: 'https://your-tofu',
  apiKey:  'tofu_live_…',
});

// Sync chat
const resp = await client.chat({
  model: 'claude-opus-4-7',
  messages: [{ role: 'user', content: 'Hi' }],
});
console.log(resp.choices[0].message.content);

// Streaming
for await (const ev of client.stream({
  model: 'claude-opus-4-7',
  messages: [{ role: 'user', content: 'Hi' }],
})) {
  const delta = (ev.choices as any[])?.[0]?.delta;
  if (delta?.content) process.stdout.write(delta.content);
}

// Self-describe
const caps = await client.capabilities();
console.log('Models:', (caps.models as any[]).map(m => m.id));

// Memory search
const hits = await client.agents.memorySearch({ query: 'rate limit pattern' });
console.log(hits.results);

// Stream a task
const task = await client.tasks.start('paper-report', {
  paper_text: '…', lang: 'zh',
});
for await (const ev of client.tasks.stream(task.task_id as string)) {
  console.log(ev);
}
```

## Auth

Pass your Tofu API key as `apiKey`. The SDK adds
`Authorization: Bearer <key>` to every request. For Anthropic-compat
clients that prefer `x-api-key`, the same key works there too.

## API surface mapped 1:1

| SDK method                          | Endpoint                              |
|-------------------------------------|---------------------------------------|
| `client.chat(req)`                  | `POST /api/v1/chat/completions`       |
| `client.stream(req)`                | `POST /api/v1/chat/completions` (SSE) |
| `client.capabilities()`             | `GET  /api/v1/capabilities`           |
| `client.tasks.start(kind, params)`  | (routed per kind)                     |
| `client.tasks.get(id)`              | `GET  /api/v1/tasks/{id}`             |
| `client.tasks.events(id, cursor)`   | `GET  /api/v1/tasks/{id}/events`      |
| `client.tasks.stream(id)`           | `GET  /api/v1/tasks/{id}/stream`      |
| `client.tasks.abort(id)`            | `POST /api/v1/tasks/{id}/abort`       |
| `client.agents.paperReport(p)`      | `POST /api/v1/agents/paper/report`    |
| `client.agents.translate(p)`        | `POST /api/v1/agents/translate`       |
| `client.agents.imageGen(p)`         | `POST /api/v1/agents/image-gen`       |
| `client.agents.memorySearch(p)`     | `POST /api/v1/agents/memory/search`   |
| `client.agents.fetch({url})`        | `POST /api/v1/agents/browser/fetch`   |
| `client.keys.list/create/revoke()`  | `/api/v1/keys/*`                       |
| `client.webhooks.subscribe(p)`      | `POST /api/v1/webhooks`               |

## Options

```ts
new Tofu({
  baseUrl: 'https://your-tofu',
  apiKey: 'tofu_live_…',
  timeoutMs: 600_000,        // default
  userAgent: 'my-app/1.0',
  fetchImpl: customFetch,    // useful for Cloudflare Workers etc.
});
```

## Cloudflare Worker example

```ts
import { Tofu } from '@tofu/sdk';

export default {
  async fetch(req: Request, env: { TOFU_KEY: string }) {
    const client = new Tofu({
      baseUrl: 'https://your-tofu',
      apiKey: env.TOFU_KEY,
    });
    const resp = await client.chat({
      messages: [{ role: 'user', content: await req.text() }],
    });
    return new Response(resp.choices[0].message.content);
  },
};
```
