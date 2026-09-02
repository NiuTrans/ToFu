# Integrations and API delivery

This domain exposes application services through native HTTP/SSE/WebSocket
interfaces and explicit compatibility adapters. The API constitution is
[`../API_CONTRACT.md`](../API_CONTRACT.md); external usage starts at
[`../HEADLESS_API.md`](../HEADLESS_API.md).

## Ownership

| Concern | Owner |
|---|---|
| Native v1 route composition | `routes/api_v1/` |
| Authentication boundary | `routes/api_v1/auth.py` |
| Request parsing | `lib/request_parser.py` and focused ingress modules |
| Success/error envelopes | `lib/api_response.py`, `lib/error_envelope/` |
| OpenAPI metadata | `lib/openapi/`, route `*_openapi.py` projectors |
| Generic task HTTP lifecycle | `routes/_task_routes.py`, `routes/task_http.py` |
| Push/SSE delivery | push/event owners and [`../EVENTS.md`](../EVENTS.md) |
| OpenAI adapter | `routes/compat_openai.py`, compat translator |
| Anthropic adapter | `routes/compat_anthropic.py`, compat translator |
| MCP integration | `lib/mcp/`, MCP routes |
| Desktop bridge | `lib/desktop/`, desktop routes |
| SDKs | `clients/python/`, `clients/typescript/` |

## Dependency direction

Delivery adapters depend on structural application-service ports. They may:

- authenticate and authorize;
- parse bounded transport input;
- invoke one use case;
- project its typed result to HTTP/events;
- attach correlation, cache, and version headers.

They may not execute SQL, choose a filesystem store, construct domain engines,
or duplicate domain error/outcome policy. Late-bound providers are resolved per
request when configuration or identity can vary; request handlers remain
stateless.

Outbound adapters resolve the shared synchronous HTTP transport only at the
actual egress boundary; registering webhook, skill-catalog, or paper routes
must not initialize `requests`/`urllib3`. Webhook URLs are still checked at
registration and again immediately before the no-redirect signed delivery.

## Native API flow

1. Middleware resolves `AuthContext` and request correlation.
2. A focused parser validates body, path, query, and headers.
3. The route invokes an injected application service.
4. Expected domain failures become the declared typed envelope.
5. Unexpected programmer errors cross the global internal-error boundary.
6. Streaming endpoints emit registered event types and settle exactly once.

Credential resolution may synchronously authenticate and stamp `last_used_at`
in the Sidecar. The async HTTP boundary therefore runs token/bridge credential
lookups, the optional shared open-mode limiter, and amortized usage-file flushes
on the serving loop's bounded sync executor. Storage deadlines may increase
one request's latency, but must not stop loop heartbeats or unrelated requests.

Schemas and runtime parsers derive from the same contract owner. A route-local
OpenAPI object is acceptable only as a projection of that owner, not as a
second definition.

## Compatibility adapters

OpenAI and Anthropic routes are explicit protocol translators into the same
task/LLM application paths as native v1. They preserve their published
external vocabulary but do not maintain alternate orchestration, billing,
tool, storage, or identity implementations.

Compatibility is scoped to those named external products. Internal retired
payload aliases, legacy database readers, and rolling-client fallbacks are not
allowed. When an internal contract changes, update its versioned source and
generated clients together.

## Streaming

The producer owns cursor and terminal semantics. SSE/WebSocket/polling are
delivery modes over the same registered event vocabulary. Reconnect resumes
from an explicit cursor or revision; the client does not fabricate missing
events or terminal state.

Backpressure, cancellation, heartbeat, and disconnect cleanup are mandatory.
A disconnected delivery transport must not automatically cancel durable work;
ephemeral cancellation follows its declared lifecycle.

## Invariants

- Default-deny authentication and explicit scope enforcement.
- One request parser and one response/error taxonomy per semantic interface.
- Domain failures are typed; programmer errors are not swallowed as `200`.
- OpenAPI, generated clients, and live routes share contract sources.
- Compatibility routes reuse native use cases.
- Streaming transports share event and settlement semantics.
- Correlation IDs survive dependency and internal error projection.
- Routes contain no SQL and no storage-path assumptions.
- Owner identity flows into repositories; it is never inferred from process
  globals or public resource IDs.

## Change routing

| Change | Start here | Verify |
|---|---|---|
| Native endpoint | application service/contract, then focused route | contract drift, auth, errors |
| Request field | machine-readable schema/parser owner | OpenAPI and SDK generation |
| Error kind | error-envelope authority | status mapping and client parser |
| Stream event | event registry | producer, replay, frontend dispatch |
| OpenAI/Anthropic mapping | named compat adapter | native use-case parity |
| Desktop/MCP bridge | focused bridge service | capability/auth and lifecycle tests |

## Test map

```bash
pytest -q tests/test_api_contract_drift.py tests/test_api_v4_contract.py
pytest -q tests/test_error_envelope_internal_classification.py \
  tests/test_api_contract_auth_parity.py
pytest -q tests/test_compat_openai.py tests/test_compat_anthropic.py
pytest -q tests/test_task_http.py tests/test_task_owner.py
pytest -q tests/test_api_contract_orchestrations_parity.py
```

If a listed suite was renamed, locate the focused replacement with
`rg --files tests | rg '(api_contract|error_envelope|auth|compat|task_http)'`.
