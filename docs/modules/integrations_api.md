# Integrations and API delivery

This domain exposes application services through native HTTP/SSE/WebSocket
interfaces and explicit compatibility adapters. The API constitution is
[`../API_CONTRACT.md`](../contracts/API_CONTRACT.md); external usage starts at
[`../HEADLESS_API.md`](../contracts/HEADLESS_API.md).

## Ownership

| Concern | Owner |
|---|---|
| Native v1 route composition | `routes/api_v1/` |
| Authentication boundary | `routes/api_v1/auth.py` |
| Request parsing | `lib/request_parser.py` and focused ingress modules |
| Success/error envelopes | `lib/api_response.py`, `lib/error_envelope/` |
| OpenAPI metadata | `lib/openapi/`, route `*_openapi.py` projectors |
| Generic task HTTP lifecycle | `routes/_task_routes.py`, `routes/task_http.py` |
| Push/SSE delivery | push/event owners and [`../EVENTS.md`](../contracts/EVENTS.md) |
| OpenAI adapter | `routes/compat_openai.py`, compat translator |
| Anthropic adapter | `routes/compat_anthropic.py`, compat translator |
| OpenAI wire values and chunk shape | `lib/compat/openai_projection.py` |
| MCP integration | `lib/mcp/`, MCP routes |
| Desktop bridge | `lib/desktop/`, desktop routes |
| Feishu bot bridge and its sender authority | `lib/feishu/`, `lib/feishu/authority.py` |
| External channels (大象, GitHub): ingress, subscriptions, outbox, 大象 bridge, GitHub poller | `lib/channels/`, `routes/api_v1/channels.py` — [`../CHANNELS.md`](../CHANNELS.md); tests `tests/channels/` |
| SDKs | `clients/python/`, `clients/typescript/` |

## Dependency direction

Problem: compatible chat, native chat and direct model execution maintained
three copies of the OpenAI finish mapping and repeated chunk dictionaries.
Decision: the pure `openai_projection.py` owner supplies closed finish values,
direct-response usage projection and fresh chunk structures. Its callers own
terminal verification, clocks, content selection, native events and settlement.
The existing `lib.compat.openai.project_completion_usage` import remains an
explicit export. Alternatives rejected: merging stream loops would conflate
narrator filtering, native event delivery and client tool handoff; projecting
all usage through one rule would erase native translation/billing semantics.

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
Webhook subscriptions persist without process/owner count quotas. The PushHub
listener resolves one write-through subscription projection; revocation is
rechecked at physical HTTP entry. One immutable, file-backed JSON frame is
shared across fan-out and retry references. Each owner/subscription preserves
FIFO order across retries, while unrelated subscriptions send independently.
Actual encoding/network failures remain visible and physical workers stay
owned through shutdown. Delivery has no item/byte admission or drop-on-pressure
path; see [`../RESOURCE_CONTROL.md`](../RESOURCE_CONTROL.md).

Authorized HTTP routes have no endpoint/IP request-frequency decorators.
Project, Project Brain, integration, media, and optional trading intelligence
handlers invoke their domain service on every admitted request. The former
`TOFU_RATE_LIMIT` kill-switch is retired; changing it has no effect. Authentication,
scopes, owner binding, typed domain errors, and trade-risk authority still apply.

## Native API flow

1. Middleware resolves `AuthContext` and request correlation.
2. A focused parser validates body, path, query, and headers.
3. The route invokes an injected application service.
4. Expected domain failures become the declared typed envelope.
5. Unexpected programmer errors cross the global internal-error boundary.
6. Streaming endpoints emit registered event types and settle exactly once.

Credential resolution may synchronously authenticate and stamp `last_used_at`
in the Sidecar. The async HTTP boundary therefore runs token/bridge credential
lookups and amortized usage-file flushes
on the serving loop's bounded sync executor. Storage deadlines may increase
one request's latency, but must not stop loop heartbeats or unrelated requests.
Usage telemetry retains 90 UTC days and bounds each day to 1,024 key buckets
and each key to 128 model buckets. Excess fan-out is added to `_overflow` and
`_other`, preserving aggregate counters while keeping the reconstructible JSON
store and resident cache finite; malformed legacy state is normalized and
atomically repaired on load.

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

The global error boundary resolves either the HTTP or WebSocket request context
before choosing an API error envelope. Problem: a rejected Push handshake has
no HTTP `request`, so reading it while mapping a 401/429 raised a second exception
and returned 500. Decision: retain the original status and use the WebSocket path
for diagnostics/envelopes. Alternative rejected: removing the shared handler
would lose consistent auth, capacity, and API v4 problem responses.

## Feishu bridge authority

Problem: the Feishu bot ran slash commands before mapping the sender to an
owner, admitted everyone when `FEISHU_ALLOWED_USERS` was empty, let `/project`
select any server directory and `/cat`/`/ls`/`/grep` read through it (arbitrary
server file read), and with `FEISHU_DEFAULT_OWNER_USER_ID` persisted any sender
as the owner's human input without saying who sent it. `/tool` also wrote
`project_path`, a key task configuration never reads.

Decision: `lib/feishu/authority.py` is the one boundary every event passes
first. Admission is default deny: an explicit allowlist entry or an explicit
`FEISHU_USER_OWNER_MAP` entry. Only an owner-map entry makes the sender the
owner; only the owner runs commands (message and menu) and tool mode (now
`projectPath`) and is persisted as a human Turn. `FEISHU_DEFAULT_OWNER_USER_ID`
runs other admitted senders' chat under that owner's account as `virtual_user`
input. `/project` selects only the owner's registered projects
(`/api/v1/project/recent`), and access is rechecked on every use, so removing a
project revokes it. File commands run as a restricted principal of the
absolute-path guard whose only registered root is the selected project; the
guard's shared `_safe_path` now realpath-checks list/grep/read targets for
restricted principals, so absolute, `../` and symlinked paths cannot leave the
root (this also closes list_dir/grep for remote API callers). Each exchange
attempt's execution config records `_externalSender` (open ID and whether it
is the owner), because the Turn `origin` vocabulary is a closed contract.

Alternatives rejected: keeping allow-all for an empty list (any Feishu user of
the tenant gets the owner's account); treating default-owner senders as the
owner (third-party text becomes human authority, contrary to
[`../CHANNELS.md`](../CHANNELS.md) C7); confining paths inside the bridge with
its own resolver (a second guard that drifts from `abs_path_guard`); listing
`FEISHU_WORKSPACE_ROOT` subdirectories (a directory is not a registration).
TODO(enterprise): one bot app and default owner per deployment. Known limit:
`FEISHU_WORKSPACE_ROOT` no longer affects selection. Verify
`tests/feishu/test_feishu_authority.py` and `test_feishu_turn_persistence.py`.

### Third-party senders get a plain model reply

Problem: a default-owner sender's chat ran `run_task_sync` under the owner's
account with ordinary task defaults, and the third party reads the reply. Its
config carried no capability keys, so `memoryEnabled` (default true) and My
Context (on when `preferencesEnabled` is absent) applied, and the default
catalog held search/create/update/delete/merge memory, skill load/search and
install requests, `read_files`, image/video inspection, `fetch_url`,
`web_search`, server-side downloads, agent spawning and messaging,
scheduler/timer creation, `poll`/`kill` of the owner's background work,
tool-result artifacts, working facts, `todo_write` and, through Tool Search,
`run_command`, `update_search_settings` and `ask_human`. Context added My
Context, prefetched owner memories, the skills index (and skill bodies a
message names), the credential-vault index (names and notes), host
environment and swarm guidance; after the turn, My Context consolidation
could write facts the third party stated into the owner's profile. The owner's
MCP servers are on by default (`mcpEnabled`); conversation recall
(`list_conversations`/`get_conversation`) turned on for any message text
carrying the referenced-conversation wrapper (`[REFERENCED_CONVERSATION` with
`title="`); plugins, project files, code execution and browser/desktop tools
needed only one config key.

Decision: `FeishuSender.execution_profile` is `THIRD_PARTY_TEXT` for every
non-owner and `pipeline.run_task_pipeline` passes it to `run_task_sync`; the
owner's chat is unchanged. The profile (`lib/tasks_pkg/execution_profile.py`)
is attached only by `create_task(execution_profile=...)`, never read from
request configuration. `create_task` replaces the request config with the
profile's (model selection only; every capability off, personal capabilities
fail closed) and marks the task; tool assembly then admits no tool (empty
wire, gateway and executable catalogs, so dispatch refuses any call); the
Context Composer acquires as a request without project, tools or memory and
renders only the platform contract and the date, withholding required blocks
too; memory prefetch never runs and the turn is never eligible for My Context
consolidation. The last three key on the task mark, so a config key added
later cannot widen the profile. Known limit: chronological carriers
(`append_injection`) keep their producers, which are the loop's own recovery
notices and the conversation's inbox deliveries. Only the owner's own actions
in that conversation (a steer, or a background command or agent the owner
starts there from the web UI) produce inbox deliveries.

Alternatives rejected: setting `memoryEnabled`/`preferencesEnabled` false in
the bridge config (memory tools do not follow `memoryEnabled`, My Context and
several sources have no key, and every new capability would need a matching
bridge key); the `toolSpecAllowlist` config key (a caller key, and registry
assembly is not the only owner-data source); keeping `web_search`/`fetch_url`
(server-side fetches of third-party-chosen URLs and of search result pages
reach internal hosts and spend the owner's search quota — not clearly safe);
a separate bare model call (bypasses routing, usage and billing, and the
canonical turn path). Verify `tests/feishu/test_feishu_third_party_profile.py`
(effective catalog and context with owner data planted at every source, plus
owner controls) and `test_feishu_authority.py`.

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
pytest -q tests/api/test_api_contract_drift.py tests/api/test_api_v4_contract.py
pytest -q tests/error_envelope/test_error_envelope_internal_classification.py \
  tests/api/test_api_contract_auth_parity.py
pytest -q tests/compat/test_compat_openai.py tests/compat/test_compat_anthropic.py
pytest -q tests/tasks_pkg/test_task_http.py tests/tasks_pkg/test_task_owner.py
pytest -q tests/api/test_api_contract_orchestrations_parity.py
```

If a listed suite was renamed, locate the focused replacement with
`rg --files tests | rg '(api_contract|error_envelope|auth|compat|task_http)'`.
