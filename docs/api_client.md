# Frontend API ownership

> **Current state (2026-08-23):** browser HTTP has one transport owner:
> [`frontend/src/api/transport.ts`](../frontend/src/api/transport.ts).
> There is no classic-file or no-Vite transport fallback. The logical
> `api.js` section retained inside `frontend/src/runtime/app-runtime.js` owns
> endpoint names only and delegates every request to the typed transport.

## 1. Request path

```text
feature / retained UI
  -> retained Api.<domain> method OR generated typed client
  -> frontend/src/api/transport.ts
  -> fetch
```

The transport is the only owner of:

- page-relative URL resolution;
- query encoding, request bodies, timeouts, and response parsing;
- `X-Request-ID` correlation;
- task/conversation affinity headers and their lifecycle;
- `ApiError` construction and backend error-envelope normalization.

The retained `window.Api` registry provides thin domain methods for UI that has
not yet become a typed feature. Its `request`, verb, and stream helpers all
delegate to the same transport; they are not an independent implementation.

Conversation sync is contract generated. Its source of truth is
`contracts/conversation_sync_v3.yaml`; running
`npm run generate:conversation-sync` emits
`frontend/src/api/conversation-sync.generated.ts`. Do not hand-edit generated
schemas or recreate those endpoints in the retained registry.

## 2. Calling an endpoint

Prefer the narrowest existing owner:

```ts
const snapshot = await conversationSyncApi.snapshot(conversationId);
```

```js
const folders = await window.Api.folders.list();
```

Direct use of `request()` is reserved for defining one of those owners, not
feature call sites. A new `fetch('/api/...')` anywhere outside
`frontend/src/api/transport.ts` is a contract violation.

`onError: 'null'` is only for genuinely best-effort reads whose absence has an
explicit UI meaning. Mutations and failures that users need to act on must
throw `ApiError`.

## 3. Errors

`ApiError` preserves HTTP status, a stable machine `code`, request identifiers,
parsed response body, URL, and exactly one applicable server error channel:

- `problem` is a validated RFC 7807 body from
  `application/problem+json` (API v4);
- `envelope` is the normalized task/domain error envelope used by v1/v3;
- transport failures have neither and use `code: "timeout" | "aborted" |
  "network" | "parse"`.

The complete task/domain envelope shape is:

```ts
{
  kind: string;
  message: string;
  severity: string;
  retryable: boolean;
  hint: string;
  detail: string;
  model: string;
  context: string;
  source: string;
  raw: unknown;
}
```

For HTTP/transport policy, branch on `error.code` and/or HTTP status. For a
task/domain failure, branch on `error.envelope.kind`. Never branch on display
text. Render `problem.detail` for v4 or envelope `message`/`hint` for task
errors; keep `requestId` available for support. The transport completes older
partial `{kind, message}` payloads at the boundary so downstream task code has
one shape, but it never re-labels an RFC problem as a task envelope. HTTP 500
details remain only in correlated backend logs; the browser receives the
stable internal category and request ID.

## 4. Streaming and non-JSON traffic

- Turn-native chat state uses `ConversationSyncCoordinator`: one authoritative
  snapshot plus the conversation-scoped `/api/v3/.../events` stream. Its
  cursor is opaque and must not be interpreted as an attempt sequence.
- Push/WebSocket notifications are invalidation hints. They do not write
  conversation projections directly.
- Binary downloads, external OAuth endpoints, image hydration, and the desktop
  loopback broker are documented transport carve-outs. A carve-out must not be
  widened to same-origin JSON API traffic.

## 5. Adding or changing an endpoint

1. Define the backend request and response in the canonical API contract.
2. For a generated domain, update its machine-readable contract and regenerate
   the client. Otherwise add one thin method to the retained `Api.<domain>`
   registry in `frontend/src/runtime/app-runtime.js`.
3. Call the owner from features; do not assemble the URL or parse the response
   again at the call site.
4. Add success, typed-failure, and retry/idempotency coverage appropriate to
   the operation.
5. Run `npm run check:conversation-sync` when that contract is touched,
   `tests/test_frontend_api_isolation.py`, module typechecking, and the frontend
   production build.

## 6. Enforcement

- `tests/test_frontend_api_isolation.py` proves the typed transport is the only
  native `fetch` owner and scans every retained runtime section, including the
  logical `api.js` registry.
- `tests/test_api_transport_cutover.py` prevents a shadow transport or fallback
  from returning.
- `tests/test_frontend_api_transport_vite.py` covers correlation, affinity,
  structured failures, and idempotent command retries.
- `tests/test_conversation_sync_v3.py` verifies generated-contract freshness,
  snapshot/event semantics, opaque cursor recovery, and shipped-source
  visibility.

The full wire-envelope and backend checklist live in
[`docs/API_CONTRACT.md`](API_CONTRACT.md).
