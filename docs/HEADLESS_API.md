# Tofu Headless API Guide

> Tofu's full backend is a fully featured agent foundation. This document is
> the canonical reference for **API-only callers** of that full application —
> SDKs, CLIs, n8n/Zapier nodes, custom backends, evaluation harnesses, and
> scripts.

There are now two headless deployment boundaries:

| Deployment | Contains | State |
|---|---|---|
| `tofu-agent` package/sidecar | Agent run, model routing, tools, MCP, network, task replay/abort, and one v2 setup page | Bounded task memory + encrypted model-routing file; no database/full Tofu frontend |
| Full Tofu backend | Everything in this guide, including conversations, accounts, feature agents, ProviderAccess management, billing, and scheduling | Storage Sidecar |

For `pip install tofu-agent`, the agent-only OCI image, provider ownership,
authentication, and exact lightweight route list, read
[DEVELOPER_RUNTIME.md](DEVELOPER_RUNTIME.md). Both compositions use the same
production agent orchestrator.

If you only want to drop Tofu into an OpenAI- or Anthropic-SDK app,
skip to the [Compatibility adapters](#compatibility-adapters) section
— it's a one-liner.

---

## 1. Surfaces at a glance

| Surface             | Path prefix     | Best for                                       |
|---------------------|-----------------|------------------------------------------------|
| **Tofu v4 bootstrap** | `/api/v4/*`   | Version negotiation and the canonical v4 contract |
| **Tofu native v1**  | `/api/v1/*`     | Current full feature surface during migration |
| **OpenAI compat**   | `/v1/...`       | Existing code using `openai` / `langchain-openai` / OpenWebUI / LangChain / Cline / Aider / Continue.dev |
| **Anthropic compat**| `/v1/messages`  | Existing code using the Anthropic SDK / Claude Code-style tools |
| **Browser transports** | `/api/...`   | Streaming, uploads, redirects, and binary assets used by the UI |

Self-describing endpoints:

* `GET /api/v4/meta` — API/schema/server build and minimum client builds
* `GET /api/v4/openapi.json` — canonical, generated v4 OpenAPI 3.1 contract
* `GET /api/openapi.json`  — full OpenAPI 3.1 spec
* `GET /api/openapi.yaml`  — same, YAML
* `GET /api/docs`          — interactive **Swagger UI**
* `GET /api/redoc`         — alternative ReDoc viewer
* `GET /api/v1/capabilities` — runtime model/tool/agent registry

New clients should probe `/api/v4/meta` before performing operations. Product
operations remain on v1 until each route and all generated clients move to the
v4 contract together; the server's release latch therefore remains on major 1.
At the atomic cutover, old `/api/v1` and `/api/v3` calls return 426 with the v4
metadata URL instead of executing compatibility logic.

---

## 2. Authentication

One credential, with transports suited to each client:

| Use case            | How                                        |
|---------------------|--------------------------------------------|
| Browser / UI        | API key installed as the HttpOnly `tofu_session` cookie by the first `?token=` visit |
| Programmatic / CI   | **Bearer API key** — `Authorization: Bearer tofu_live_…` |

API keys are issued by the admin (Settings → API Keys, or the CLI
`tofu keys create …`). Every key has:

* a **prefix** (e.g. `tofu_live_a3f2c1`) — public, shown in the UI
* a **scope set** drawn from the closed enum below
* per-key **rate limits** (RPM and tokens-per-day; either may be 0 = unlimited)
* optional **expiration**

### 2.1 Issuing a key (admin)

```bash
curl -X POST https://your-tofu/api/v1/keys \
  -H "Authorization: Bearer tofu_admin_…" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "build-bot",
    "scopes": ["chat","tasks","agents:translate"],
    "rate_limit_rpm": 60,
    "rate_limit_tpd": 1000000
  }'
```

The plaintext token is in the response's `token` field — **shown only
once.** After that, only its SHA-256 hash is stored.

### 2.2 Scope vocabulary

| Scope                | Grants                                                  |
|----------------------|---------------------------------------------------------|
| `chat`               | `/api/v1/chat/completions`, `/v1/chat/completions`, `/v1/messages` |
| `tasks`              | All `/api/v1/tasks/*`                                  |
| `conversations`      | All `/api/v1/conversations/*`                          |
| `files`              | All `/api/v1/files/*` (uploads, attachments)           |
| `agents:paper`       | Paper report / translate                               |
| `agents:translate`   | Generic translation                                    |
| `agents:swarm`       | Swarm orchestration                                    |
| `agents:scheduler`   | Cron / proactive agent                                 |
| `agents:memory`      | Memory layer                                           |
| `agents:browser`     | Server-side fetch                                      |
| `agents:trading`     | Trading                                                |
| `agents:image`       | Image generation                                       |
| `agents:mcp`         | MCP bridge                                             |
| `agents:run`         | `/api/v1/agent/run` (single-call agent runtime)        |
| `providers`          | Owner model-routing and ProviderAccess management       |
| `webhooks`           | Outbound delivery subscriptions                        |
| `capabilities`       | (public)                                               |
| `usage`              | Per-key analytics                                      |
| `admin`              | Implies every other scope; can manage keys/webhooks    |

### 2.3 Rate limit headers

Every authenticated response carries:

```
X-RateLimit-Limit-Requests:      60
X-RateLimit-Remaining-Requests:  58
X-RateLimit-Limit-Tokens:        1000000
X-RateLimit-Remaining-Tokens:    998213
```

A 429 also carries `Retry-After: <seconds>`.

---

## 3. Native v1 API

### 3.1 `POST /api/v1/chat/completions`

The headless analog of the UI's `/api/chat/start` + `/stream`. Reuses
the same orchestrator — every Tofu capability (tool use, thinking,
fallback chain, MCP, project tools, memory, swarm, scheduler) is
available via `config`.

**Body**

```jsonc
{
  // Required native identity. Provider preference is separate from identity.
  "model": {"creator_id": "anthropic", "model_id": "claude-opus-4-7"},
  "routing": {"preferred_provider_id": "provider-anthropic"}, // optional
  "messages": [
    {"role":"system","content":"…"},
    {"role":"user","content":"Hi"}
  ],
  "tools": [...],                                       // optional, OpenAI-shaped
  "tool_choice": "auto",                                // optional
  "response_format": {"type": "json_object"},           // optional; forwarded to the engine (JSON mode)
  "temperature": 1.0,
  "max_tokens": 32768,
  "stream": false,                                      // true → SSE
  "config": {                                           // Tofu-specific, see /capabilities
    "thinkingDepth": "high",
    "searchMode": "multi",
    "fetchEnabled": true,
    "memoryEnabled": true,
    "projectPath": "",
    "agentBackend": "builtin",
    "mcpEnabled": true,
    "tools": {
      "toolSearch": "auto",
      "programmaticCalling": "off",
      "nativeExposure": "full"
    },
    "responses": {
      "transport": "sse",
      "reasoningMode": "standard",
      "verbosity": "medium",
      "imageDetail": "auto"
    },
    "orchestration": {
      "multiAgent": "off",
      "maxConcurrentAgents": 3
    },
    "disableModelFallback": false
  },
  "conversation_id": "my-headless-job-001",             // optional
  "idempotency_key": "uuid-or-anything-stable",          // optional, replays cached response
  "timeout_s": 600
}
```

`tools.toolSearch`, `tools.programmaticCalling`, and
`orchestration.multiAgent` apply to every tool-capable provider: `auto` uses a
verified native implementation where available and otherwise the local
gateway, ToolScript, or Swarm backend. PTC and Multi-agent can be active in the
same request because they solve different task shapes. Legacy
`responses.multiAgent` and `responses.maxConcurrentSubagents` inputs remain
accepted as migration aliases, but normalized configuration is always returned
under `orchestration`.

Tool Search `auto` uses a verified native
implementation where available and otherwise the local `search_tools` +
direct native tool calls; `native` still falls back locally when capability is
unverified. The `responses.*` controls remain limited to `protocol: responses`
with an effective `responses_profile` of `openai`. See
[Tool Search and direct execution](TOOL_SEARCH_EXECUTION_GATEWAY.md) and the
[OpenAI Responses feature policy](OPENAI_RESPONSES_FEATURES.md).

> **⚠️ Automatic model fallback (important for pinned-model callers).**
> The server admin can configure a global *fallback model* (Settings →
> model defaults). When set, a transient error on your requested model
> causes Tofu to **silently re-run that round on the fallback model** —
> so a request pinned to a structured `model` can return output from a different
> model. The done event / task snapshot expose this via
> `fallbackModel` / `fallbackFrom` / `fallbackReason`, so always inspect
> them if model identity matters. For reproducible runs, benchmarks, or
> evals where you must measure ONLY the requested model, set
> `config.disableModelFallback: true`: the round then surfaces the
> primary error (envelope `context: "fallback-disabled"`) instead of
> switching. The fallback *target* itself is admin-only; this flag is
> the per-request opt-out. Whether a deployment has a fallback model is
> not exposed in `/capabilities` (it's a server secret), so treat the
> opt-out as the safe default for deterministic pipelines.

**Sync response** (`stream:false`):

```jsonc
{
  "ok": true,
  "id": "chatcmpl-…",
  "object": "chat.completion",
  "created": 1701000000,
  "model": "claude-opus-4-7",
  "choices": [{
    "index": 0,
    "message": {"role":"assistant","content":"…","reasoning_content":"…"},
    "finish_reason": "stop"
  }],
  "usage": {"prompt_tokens": 412, "completion_tokens": 1536, "total_tokens": 1948},
  "task_id": "abc123…"
}
```

**Streaming response** (`stream:true`): SSE stream of OpenAI-shaped
`chat.completion.chunk` frames, plus Tofu-native event envelopes
attached as a `tofu` field on chunks for non-text events
(phase, tool_call, snapshots).

Terminal success is evidence-based. A provider stream cut first emits a
`phase` event explaining that the already-delivered prefix is being preserved
while Tofu performs a bounded lossless continuation. If recovery is exhausted,
sync mode returns an HTTP error and stream mode emits an `error` object followed
by `[DONE]`; neither mode fabricates `finish_reason: "stop"`. OpenAI- and
Anthropic-compatible endpoints follow the same verdict, using their respective
error channels instead of `stop` / `end_turn`.

### 3.2 Generic task lifecycle — `/api/v1/tasks/*`

Once you have a `task_id` (from chat completions, paper, translate,
swarm, etc.), you can drive it uniformly:

```
GET    /api/v1/tasks                 — list (filter by kind, status)
GET    /api/v1/tasks/{id}            — current state (event cursor summary only)
GET    /api/v1/tasks/{id}/events?cursor=N — bounded long-poll cursor replay
GET    /api/v1/tasks/{id}/stream     — SSE replay-from-cursor
POST   /api/v1/tasks/{id}/abort      — graceful stop
DELETE /api/v1/tasks/{id}            — drop from registry (admin)
```

Cursor-based replay means the consumer can disconnect and reconnect
without losing events. Replay pages are bounded to 128 events and roughly
1 MiB of event JSON (a single larger event remains lossless). Continue with
`next_cursor` until `caught_up=true`; `done` and terminal result fields are
published on that caught-up page. The state endpoint deliberately omits event
bodies and exposes `event_replay.{retained_count,base_cursor,next_cursor}`
instead, so status refreshes never retransmit the replay window.

Chat tasks retain a short process-local late-poller window, then these three
per-task read endpoints fall back to the owner-scoped `task_results` row and
durable event log. A restart or terminal-registry eviction therefore does not
turn a known chat task into 404; a missing or foreign-owner ID remains 404.
Durable sequences can be sparse because provider-ingress deltas are allowed to
remain memory-local, so clients must always advance to the returned
`next_cursor` rather than adding the event count. Intermediate cold pages read
only compact status/clock metadata; the cumulative `content` / `thinking` is
loaded and emitted once on the caught-up terminal page. The list endpoint is a
bounded recent/live registry view, not a durable task-history listing.

Terminal task-result metadata exposes
`toolOrchestrationDecisions[]` as a bounded, provider-neutral trace. Each row
separates the selected semantic lanes from their resolved backend
(`programmaticBackend: native_openai | local | off` and
`multiAgentBackend: native_openai | local_swarm | off`). This is offer/routing
telemetry, not adoption evidence: use `programRuns[]` for programs that really
executed, `caller.type=multi_agent` for native worker tool calls, and persisted
`spawn_agents` handles for local Swarm execution.

### 3.3 Capabilities — `/api/v1/capabilities`

Runtime-derived registry of **this** deployment's models, tools,
agents, presets, backends, and config schema. Public; no auth needed.
Use it for client auto-config.

```jsonc
{
  "ok": true,
  "tofu_version": "1.x.x",
  "api_version": "v1",
  "features": {"trading_enabled": false, "optimizer_enabled": true, …},
  "models": [
    {"id":"claude-opus-4-7", "provider":"openrouter", "thinking":true,
     "vision":true, "capabilities":["text","vision","thinking"], …}
  ],
  "tools":  [{"name":"web_search", "group":"search", "description":"…"}],
  "agents": [{"id":"paper.report", "path":"/api/v1/agents/paper/report", "scope":"agents:paper"}],
  "presets": ["off","medium","high","xhigh","max"],
  "backends": ["builtin","codex","claude_code"],
  "scopes":   ["chat","tasks", …, "admin"],
  "config_schema": {…}
}
```

### 3.4 Agents — `/api/v1/agents/*`

Stable façades over higher-level features. Each is scope-gated.

| Endpoint                                | Scope             | Purpose                                |
|----------------------------------------|-------------------|----------------------------------------|
| POST `/agents/paper/report`            | `agents:paper`    | Long-form paper report task            |
| POST `/agents/paper/translate`         | `agents:paper`    | Babel-mode whole-paper translation     |
| POST `/agents/translate`               | `agents:translate`| Generic chunked translation            |
| POST `/agents/memory/search`           | `agents:memory`   | Memory similarity search               |
| POST `/agents/browser/fetch`           | `agents:browser`  | Server-side URL fetch (with PDF/HTML)  |
| POST `/agents/image-gen`               | `agents:image`    | Image generation                       |
| GET  `/agents/swarm/status/{task_id}`  | `agents:swarm`    | Swarm sub-agents                       |
| POST `/agents/swarm/abort/{task_id}`   | `agents:swarm`    | Stop a swarm                           |

Paper translation accepts at most 1,000,000 `paper_text` characters (HTTP 413
beyond the limit), produces at most 128 semantic slices, and has a two-hour
task deadline. `force=true` requests fresh slices; an explicit `model` is
strictly pinned. A task becomes `done` only after every validated slice is
confirmed in the owner-scoped artifact repository.

### 3.5 Webhooks — `/api/v1/webhooks/*`

Subscribe a URL to event delivery from the same `PushHub` that powers
the WebSocket channel.

```bash
curl -X POST /api/v1/webhooks \
  -H "Authorization: Bearer …" \
  -d '{"url":"https://my.app/hook","channel":"chat","event_types":["done"]}'
# → {ok:true, subscription:{id,url,secret,…}}
```

Every delivered POST includes:
```
X-Tofu-Timestamp: 1701000000
X-Tofu-Signature: v1=<hex hmac-sha256 of "{timestamp}.{body}">
X-Tofu-Subscription-Id: wh_…
```

Verify with the per-subscription `secret` (returned ONCE on creation).
Creation is atomically capped per owner and process; capacity exhaustion is a
409 and never overwrites another concurrent subscription. `event_types`
accepts at most 32 distinct non-empty names of at most 80 characters. Delivery
is intentionally bounded and best-effort: personal mode defaults to 64
subscriptions, 128 immediate items, 64 delayed retries, 16 MiB total retained
event data, 512 KiB per event, and five actual HTTP attempts. Deleted or
disabled subscriptions are rechecked against storage before every request, so
already queued work cannot outlive revocation. A transient failure gates later
events for that subscription behind the same exponential cooldown; those
deferrals do not spend attempts, and one probe request reopens delivery after
recovery.

### 3.6 Real-time push — `WS /api/push`

If you want a single WebSocket multiplexing every channel/task, this
is the same socket the UI uses. Send `{"action":"subscribe", "channel":"chat", "taskId":"…"}`
and the server pushes every event for that task. See
[`lib/agent_core/push.py`](../lib/agent_core/push.py) for the full protocol
(`lib.agent_core.push` is the sole PushHub owner).

### 3.6.1 Streaming event contract — the frontend↔backend sync interface

> **If you are building your own frontend, this is the section you read.**
> The agent runtime emits a fixed vocabulary of JSON events. They flow over
> the `/api/v1/tasks/{id}/stream` replay stream and the `/api/push` WebSocket —
> the same events, regardless of transport.

The vocabulary is **declared, versioned, and machine-discoverable**. The
single source of truth is [`lib/agent_core/events.py`](../lib/agent_core/events.py);
it is served as the `events` block of `GET /api/v1/capabilities`, so a client
can auto-configure without hardcoding:

> **Contributing to the backend?** If you are *emitting* events (not consuming
> them), see [`EVENTS.md`](EVENTS.md) — the required `build_event` / `EventType`
> discipline, how to add a new event type, and the drift guards. Raw
> `{'type': ...}` dict literals are forbidden.

```jsonc
GET /api/v1/capabilities
→ { …, "events": {
    "contract_version": 1,
    "transports": {
      "sse": ["/api/v1/tasks/<task_id>/stream"],
      "websocket": "/api/push",
      "cursor_replay": "/api/v1/tasks/<task_id>/events?cursor=N"
    },
    "terminal_types": ["done"],
    "interaction_types": ["approval_required","human_guidance_request",
                          "stdin_request","write_approval_request"],
    "categories": {
      "lifecycle": [{"type":"phase","purpose":"…","terminal":false,
                     "requires_response":false,"fields":{…},"since":1}, …],
      "content":   [{"type":"delta", …}],
      "tool":      [{"type":"tool_start", …}, …],
      …
    }
  } }
```

**Every event** is a JSON object with a `type` field plus the fields listed in
its spec. The categories and the most important events:

| Category | Events | Notes |
|----------|--------|-------|
| `lifecycle` | `state`, `phase`, `done`, `error` | `state` is the full snapshot sent first on (re)connect; `done` is the **only terminal** event |
| `content` | `delta` | Incremental assistant output (`content` and/or `thinking`) — append to the live bubble |
| `tool` | `tool_start`, `tool_progress`, `tool_result`, `tool_complete`, `tool_compacted` | Keyed by `toolCallId` + `roundNum` |
| `context` | `round_usage`, `round_committed`, `messages_snapshot`, `compaction`, `compaction_done`, `memory_prefetch`, `project_external_edit` | Token accounting, durable checkpoints, context-window mgmt |
| `interaction` | `human_guidance_request`, `write_approval_request`, `approval_required`, `stdin_request`, `stdin_resolved` | **Require a client response** before the task proceeds (see below) |
| `flow` | `flow_iteration`, `flow_planner_done`, `flow_critic_msg`, `flow_new_turn`, `flow_complete` | FlowExecutor chat projection |
| `swarm` | `swarm_phase`, `swarm_inbox_inject`, `swarm_agent_phase`, `swarm_agent_progress`, `swarm_agent_complete`, `swarm_agent_error`, `swarm_agent_tool_call` | Multi-agent orchestration |
| `autopilot` | `autopilot_vu_event`, `autopilot_vu_done`, `autopilot_vu_cancel` | Autonomous-loop value units |
| `artifact` / `scheduler` / `transport` | `artifact`, `timer_poll_check`, `sse_timeout`, `ping` | `ping` (WS keepalive) and `sse_timeout` are transport signals — ignore them |

**Minimal consumer** — the only events a basic frontend MUST handle:

```
state      → render the snapshot (messages, tool rounds)
delta      → append .content / .thinking to the current assistant message
phase      → optional: show a status spinner
tool_start → optional: show "running <toolName>"
tool_complete → optional: show the tool result
done       → finalize; if .error present, render the failure. STOP.
```

**Ordering & guarantees:**

- A stream begins with a `state` snapshot, then a mix of `phase` / `delta` /
  tool events, and ends with **exactly one** terminal `done` (its `error`
  field is set on failure; non-fatal issues arrive as inline `error` events).
- `tool_start` precedes the `tool_result` / `tool_complete` carrying the same
  `toolCallId`.
- Every event on the SSE/replay stream carries a monotonic `seq`; reconnect via
  `/api/v1/tasks/{id}/events?cursor=<last_seq>` (or `/stream`) to resume with
  no loss.

**Interaction events** pause the task until the client replies. Each carries a
correlation id you echo back to the matching endpoint:

| Event | Correlation id | Reply via |
|-------|----------------|-----------|
| `human_guidance_request` | `guidanceId` | `POST /api/v1/chat/human-response` — `{guidanceId, response}` |
| `stdin_request` | `stdinId` | `POST /api/v1/chat/stdin-response` — `{stdinId, input, eof?}` |
| `write_approval_request` | `approvalId` | `POST /api/v1/project/write-approval` — `{approvalId, approved}` |

A `stdin_resolved` event clears a pending `stdin_request` prompt. The
`approval_required` event is a generic gate emitted by mode-based external
backends; resolve it through the same write-approval endpoint.

**Versioning:** `contract_version` bumps only on a *breaking* change to an
existing event's shape (a field removed/renamed/retyped). New event types and
new optional fields are additive and do **not** bump it — clients should ignore
unknown event types and unknown fields. A server-side drift test
(`tests/test_event_registry.py`) guarantees the registry stays in lockstep with
what the runtime actually emits and what the bundled frontend consumes.

### 3.7 Model routing and ProviderAccess

> The owner configures service access once, so ordinary callers supply only a
> Tofu URL, token, and structured Model identity. One-run access uses the same
> complete v2 aggregate; inline endpoint/key/model blocks do not exist.

The persistent authority and runtime request are separate layers:

#### 3.7.1 Persistent model routing — `/api/v1/model-routing`

The owner-scoped [`tofu.model-routing/v2`](../contracts/model_routing_v2.schema.json)
aggregate is the sole model/provider authority. It separates official model
identity, ProviderAccess, Connection, encrypted Credential metadata, Offering,
and Deployment. Every write is revision-CAS; callers never encode a provider
inside a model string, and plaintext credentials never enter the aggregate.

| Endpoint | Scope | Purpose |
|---|---|---|
| `GET /api/v1/model-routing` | `providers` | Read the redacted owner aggregate and revision |
| `PUT /api/v1/model-routing` | `providers` | CAS-replace the complete aggregate |
| `GET/POST /api/v1/providers` | `providers` | List/create ProviderAccess bundles |
| `POST /api/v1/providers/probe` | `providers` | Probe an endpoint and return a secret-free ProviderAccess draft without persisting it |
| `GET/PATCH/DELETE /api/v1/providers/{provider_id}` | `providers` | Read/CAS-replace/delete one bundle |
| `PUT /api/v1/model-routing/credentials/{credential_id}/secret` | `providers` | Replace one encrypted secret outside the aggregate |
| `POST /api/v1/model-routing/migration/{plan,commit}` | `providers` | Preview/commit the one-way legacy migration |

Native chat selects an official model with
`{"creator_id":"…","model_id":"…"}` and may add
`routing.preferred_provider_id`. A provider-scoped pending identity uses
`{"provider_id":"…","offering_id":"…"}`. The dispatcher computes a
bounded candidate set, mints request-scoped slots, records a route snapshot,
and disposes the slots on completion.

Non-chat model calls follow the same rule. Image, audio/transcription, TTS, and
`POST /v1/embeddings` list or mint only routes visible to the authenticated
owner. A model string never authorizes access to another owner's provider, and
an unavailable v2 route cannot fall back to process-global environment keys.

#### 3.7.2 Single-call agent runtime — `POST /api/v1/agent/run`

Headline endpoint for running one agent turn end-to-end. With a deployment
route already configured, the request supplies a structured model identity,
messages, and optional routing policy, agent configuration, and trajectory
format.

```jsonc
POST /api/v1/agent/run
Authorization: Bearer tofu_live_…
{
  "messages": [{"role":"user","content":"Refactor lib/foo.py"}],

  // 1. Official identity; provider preference is a separate concern.
  "model": {"creator_id":"deepseek","model_id":"deepseek-v4-pro"},
  "routing": {
    "preferred_provider_id": "provider-cluster-a",
    "required_context": 131072,
    "price_budget": {"max_input": 1.0, "max_output": 4.0, "currency":"USD"}
  },

  // A provider-scoped pending identity uses this form instead:
  // "model": {"provider_id":"provider-cluster-a","offering_id":"offering-v4"},

  // 3. unified config — aliases + raw orchestrator keys mix freely
  "config": {
    "thinking":     "high",          // alias → thinkingDepth + thinkingEnabled
    "tools":        ["search","fetch","memory","mcp"],   // or ["*"] / "*"
    "memory":       true,             // alias → memoryEnabled
    "project":      "/abs/path/to/repo",  // alias → projectPath
    "max_tokens":   4096,             // alias → maxTokens
    "thinkingDepth": "max"            // raw key — wins on conflict
  },

  // 4. optional trajectory shaping
  "trajectory": "sharegpt",   // sharegpt | openai-finetune | anthropic | tofu-native
  "stream":     false,
  "timeout_s":  600
}
```

The owner routing aggregate resolves eligible Deployments, credentials, and
failover candidates. Each request gets a bounded route group whose slots are
disposed on terminal completion, including async and disconnected streaming
requests. Successful responses and stream chunks expose the selected
`provider_id`; plaintext secrets never cross the HTTP boundary. Opaque,
owner-scoped `secret_reference` values remain visible so clients can perform
lossless aggregate CAS edits without retrieving the encrypted value.

Native requests deliberately reject plain model strings, `model@provider`,
and inline `provider` blocks. Compatibility `/v1` endpoints retain their
upstream string model field and translate it into the same v2 authority.

> **Header allowlist**: Connection and encrypted-credential `extra_headers`
> reject `Authorization`,
> `x-api-key`, `Cookie`, `Host`, `Content-Length`,
> `Transfer-Encoding`, `Proxy-Authorization` — names that would
> impersonate Tofu's own outbound auth. Up to 16 entries, 2048 chars
> per value.

The `config` field accepts both **curated aliases** and **raw
orchestrator keys**. Aliases translate first; raw keys flow through
unchanged and override the alias when both are present (last write
wins). Unknown keys pass through (forward-compat extension point).
The legacy `capabilities` field name is still accepted and merged
into `config`.

The response always carries `task_id` so callers can switch to
`/api/v1/tasks/{id}/*` for streaming, replay, or abort. When
`trajectory` is set, the response carries top-level
`trajectory_format` + `trajectory` fields (no nested envelope).

Set `"async": true` or send `Prefer: respond-async` to receive HTTP 202 with
`Location` and `X-Tofu-Task-Id` instead of waiting for the terminal result.
SDK streaming uses that handle and reconnects to the existing task stream; it
does not resubmit the agent request after a transport loss.

| `trajectory` value | `trajectory` field shape                     |
|--------------------|----------------------------------------------|
| `sharegpt`         | `[{from:"human"\|"gpt"\|"tool", value:"…"}]` |
| `openai-finetune`  | `{messages:[{role,content,tool_calls?}]}`    |
| `anthropic`        | `{system?, messages:[{role,content:[…]}]}`   |
| `tofu-native`      | Full event log + final state (lossless)      |

#### 3.7.3 New scopes

| Scope        | Grants                                  |
|--------------|-----------------------------------------|
| `providers`  | All `/api/v1/providers/*`               |
| `agents:run` | `/api/v1/agent/run`                     |

A typical model-routing + trajectory key is created with:

```bash
curl -X POST /api/v1/keys \
  -H "Authorization: Bearer tofu_admin_…" \
  -d '{"name":"trajectory-pipeline",
       "scopes":["providers","agents:run","tasks"]}'
```

Note that **`agents:run` does not include `chat`**. A key with only
`agents:run` may execute `/api/v1/agent/run` against the owner's v2 authority,
but cannot call `/api/v1/chat/completions`. Add `providers` only when the
client must administer access; ordinary run callers do not need it.

#### 3.7.4 Discovery via OpenAI-compatible `/v1/models`

`GET /v1/models` projects the owner-visible official models. A string is
unambiguous only when one creator owns that `model_id`; otherwise compatible
clients add `tofu.creator_id`. Provider preference is carried separately as
`tofu.preferred_provider_id`, never as a model suffix:

```jsonc
GET /v1/models
Authorization: Bearer tofu_live_…
→ {
  "object": "list",
  "data": [
    {"id":"gpt-5.6","object":"model","owned_by":"openai",
     "tofu":{"creator_id":"openai"}},
    {"id":"deepseek-v4-pro","object":"model","owned_by":"deepseek",
     "tofu":{"creator_id":"deepseek"}}
  ]
}
```

Stock OpenAI SDKs (Python `openai`, JS `openai`, LangChain, Cline,
Aider, OpenWebUI) populate their model dropdowns from this endpoint —
no custom client code required.

#### 3.7.5 Forbidden errors are structured

When a route returns 403 the response body has top-level
`missing_scope` / `required_scopes` / `granted_scopes` fields a
client can branch on:

```jsonc
HTTP/1.1 403 Forbidden
{
  "ok": false,
  "error": "Missing required scope: agents:run",
  "missing_scope": "agents:run",
  "required_scopes": ["agents:run"],
  "granted_scopes": ["chat", "tasks"]
}
```

### 3.8 Error model

Tofu uses **two** complementary error channels. Match on the structured
fields below — never substring-match `error.message` / `detail`, which
are human-facing and may change.

**1. HTTP-level errors** (request rejected before/around dispatch) come
back as `{ok: false, error: <string|envelope>}` with the HTTP status
set. Some carry extra top-level fields a client can branch on:

| Status | Extra top-level fields | Meaning |
|--------|------------------------|---------|
| 400 | `field` | Malformed request; `field` names the offending key |
| 401 | — | Missing / invalid API key |
| 403 | `missing_scope`, `required_scopes`, `granted_scopes` | Key lacks a scope (see §3.7.5) |
| 402 | `error_kind: "insufficient_funds"`, `balance_micro`, `needed_micro` | Pre-flight credit reservation failed (multi-user installs) |
| 404 | — | Unknown task / resource |
| 429 | `Retry-After` header | Rate / token limit hit |
| 500 | `request_id` | Internal error; quote `request_id` in bug reports |

**2. Task-level errors** (the LLM call or a tool failed mid-task) arrive
as a **typed error envelope** — on `task['error']`, in the terminal
`done` event's `error` field (SSE / WebSocket), and in
`GET /api/v1/tasks/{id}`. The envelope is the discoverable contract:

```jsonc
{
  "kind":      "ratelimit",     // closed enum — classify on THIS
  "severity":  "warning",        // "warning" | "error"
  "retryable": true,             // is retrying the same request likely to help?
  "message":   "…",              // short bilingual title (display)
  "hint":      "…",              // bilingual recovery hint (display)
  "detail":    "HTTP 429: …",    // technical detail (truncated)
  "model":     "claude-opus-4-7",
  "context":   "fallback",
  "source":    "llm-stream",
  "raw":       "…"               // raw upstream text (≤300 chars)
}
```

`kind` is a **closed enum** — a typo never leaks through as a silent
generic; unknown values are downgraded to `generic` server-side. Stable
values:

| `kind` | `retryable` | Meaning / typical fix |
|--------|:-----------:|------------------------|
| `quota` | no | API-key balance / quota exhausted → top up or swap key |
| `ratelimit` | yes | 429 / TPM-RPM throttle → wait and retry |
| `permission` | no | 401 / 403 from the upstream provider; key invalid or lacks model access |
| `no_slot` | yes | Dispatcher found zero usable key slots |
| `dispatch_exhausted` | no | Every slot for this capability was tried |
| `timeout` | yes | Upstream / network read timeout |
| `network` | yes | Connection error, DNS, proxy reset |
| `content_filter` | no | Provider safety filter blocked the response |
| `invalid_image` | no | Image content rejected (too large / corrupt) |
| `prompt_too_long` | no | Context overflow after auto-compaction |
| `stream_only` | no | Model rejects non-streaming calls |
| `model_limit` | no | `max_tokens` exceeded the model's learned cap |
| `tool_rounds_exhausted` | no | Legacy persisted value only; current runs do not emit it |
| `tool_timeout` | yes | Repeated tool-execution timeouts |
| `premature_close` | yes | SSE stream cut off (retries exhausted) |
| `abnormal_stop` | yes | Missing finish marker / partial reply |
| `aborted` | no | User cancelled |
| `server_offline` | yes | Client lost contact with the server |
| `internal` | no | Backend bug — check `logs/error.log` |
| `generic` | no | Unrecognised — last-resort fallback |

> A successful HTTP 200 can still carry a task-level failure: in
> non-stream mode the body's `finish_reason` is `stop` while a
> `tofu_error` / `error` envelope is present; in stream mode the
> terminal `done` frame carries it. Always inspect the envelope before
> treating a 200 as success.

The enum is the single source of truth in
[`lib/error_envelope/__init__.py`](../lib/error_envelope/__init__.py) (`KINDS`); a drift
test keeps it honest.

> The envelope's `context` field is a free-form diagnostic tag (not a
> closed enum). One value worth recognising: `context:
> "fallback-disabled"` means the primary model errored and automatic
> fallback was suppressed because this request set
> `config.disableModelFallback: true`. The error you see is the real
> primary-model error — branch on `kind` / `retryable` as usual and
> retry on the SAME model rather than expecting a fallback to have
> masked it.

### 3.9 Motion-video production — `/api/v1/motion/*`

Start a topic, SRT, or pre-authored storyboard with
`POST /api/v1/motion/videos`. The task lifecycle uses the motion-specific
poll/abort routes and also appears in the generic task registry. Important
discovery and delivery endpoints:

```text
GET  /api/v1/motion/status
GET  /api/v1/motion/shot-recipes
GET  /api/v1/motion/audio-contract
POST /api/v1/motion/videos
GET  /api/v1/motion/videos/{id}/scenes
POST /api/v1/motion/videos/{id}/scenes/{scene_id}/regen
GET  /api/v1/motion/videos/{id}/file
GET  /api/v1/motion/videos/{id}/file?part=srt
GET  /api/v1/motion/videos/{id}/file?part=audio-plan
GET  /api/v1/motion/videos/{id}/file?part=audio-attribution
GET  /api/v1/motion/videos/{id}/file?part=media-attribution
```

Topic requests accept `creative_mode: "director" | "standard"`.
`director` is the default two-candidate, deterministic-gate, independent-critic
path; `standard` is the single-plan A/B control. The field is part of dedup and
checkpoint identity. When `PEXELS_API_KEY` is configured, bounded image/video
queries can materialise local stock assets; `media-attribution` returns the
provider/creator credit ledger when such assets were used.

`audio_plan` can be supplied inline or by `audio_plan_path` (not both), up to
256 KB. Relative asset paths resolve from `audio_base_dir` for inline plans or
beside the plan file for path input. Audio assets must already be local and
carry license/source metadata; the job content-addresses them before render.
The resulting scene list exposes the `motion-timeline-v1` content/render
durations and real overlap transition fields. The final file clock always
remains the spoken/SRT program duration even when visual handles are rendered
for `xfade`.

---

## 4. Compatibility adapters

### 4.1 OpenAI SDK

```python
from openai import OpenAI
client = OpenAI(api_key="tofu_live_…",
                base_url="https://your-tofu/v1")

resp = client.chat.completions.create(
    model="claude-opus-4-7",
    messages=[{"role":"user","content":"Hi"}],
    tools=[…],            # OpenAI-shaped tool defs
    stream=False,
)
print(resp.choices[0].message.content)
```

* `model` remains the upstream-compatible string. Native
  `/api/v1/chat/completions` instead requires the structured creator/model
  identity shown in §3.1.
* Streaming returns standard `chat.completion.chunk` SSE frames.
* `reasoning_effort=low|medium|high` maps to Tofu's thinking-depth ladder.
* When `tools` is supplied, Tofu's auto-injected tools (web_search,
  memory, etc.) are turned off so the model only sees what you sent.
* Every response includes a non-standard `task_id` field for follow-up
  polling/abort via `/api/v1/tasks/`. SDK clients ignore it safely.

### 4.2 Anthropic SDK

```python
from anthropic import Anthropic
client = Anthropic(api_key="tofu_live_…",
                   base_url="https://your-tofu")

msg = client.messages.create(
    model="claude-opus-4-7",
    max_tokens=8192,
    messages=[{"role":"user","content":"Hi"}],
    thinking={"type":"enabled","budget_tokens":16384},
)
```

* Both `Authorization: Bearer …` and `x-api-key` headers are accepted.
* `thinking.budget_tokens` maps onto Tofu's depth ladder.
* Streaming uses Anthropic's named events
  (`message_start`/`content_block_delta`/`message_stop`) — full SDK
  compatibility.
* `POST /v1/messages/count_tokens` works.

### 4.3 LangChain, OpenWebUI, Cline, etc.

Anything that accepts an OpenAI- or Anthropic-compatible base URL
works unchanged. Common bases:

* OpenWebUI / LangChain `ChatOpenAI(base_url="…/v1", api_key=…)`
* Cline / Continue.dev: pick "OpenAI-compatible" provider, paste
  `https://your-tofu/v1` and the Tofu key.
* Aider: `--openai-api-base https://your-tofu/v1 --openai-api-key tofu_live_…`

### 4.4 Native SDKs

For full access to Tofu-only features (tasks, capabilities, agents,
webhooks):

| Language    | Path                  | Notes                              |
|-------------|-----------------------|-------------------------------------|
| Python      | `clients/python/`     | `pip install -e clients/python[cli]`. Provides the `tofu` CLI. |
| TypeScript  | `clients/typescript/` | Works in Node 18+, browsers, Cloudflare Workers, Vercel Edge, Deno, Bun. |

### 4.5 In-process façade (`import tofu`)

When your code runs **in the same Python process** as Tofu (an embedding
Flask/FastAPI app, a notebook, a worker that imported the package), use the
top-level `tofu` façade instead of the HTTP API — no socket, no SSE
re-parsing, and crucially **no vendoring of `lib/` internals**. It calls the
exact same orchestrator the HTTP route does.

```python
import tofu

# Blocking turn — mirrors POST /api/v1/chat/completions (stream=false).
res = tofu.chat(
    messages=[{"role": "user", "content": "Summarise this as JSON"}],
    model="claude-opus-4-7",
    response_format={"type": "json_object"},
    config={"thinkingDepth": "high", "tools": ["search"]},
)
if res.ok:
    print(res.content, res.usage)
else:
    print("failed:", res.error["kind"], res.error["message"])   # typed envelope

# Streaming — yields the SAME native event dicts as §3.6.1.
for ev in tofu.stream(messages=[{"role": "user", "content": "Hi"}],
                      model="claude-opus-4-7"):
    if ev["type"] == "delta" and ev.get("content"):
        print(ev["content"], end="", flush=True)

caps = tofu.capabilities()   # same payload as GET /api/v1/capabilities
```

* **Request knobs** mirror the HTTP chat body (`model`, `messages`,
  `response_format`, `tools`, `temperature`, `max_tokens`, `config`, …);
  explicit `config` values win over the top-level knobs.
* **`tofu.chat`** returns a `ChatResult` — inspect `res.ok` /
  `res.error["kind"]`, not just `res.content` (a turn can finish empty with a
  typed error envelope per §3.8).
* **`tofu.stream`** yields the native event vocabulary directly (switch on
  `ev["type"]`); the terminal `done` event carries `error` on failure.
* **Out of scope by design:** multi-user **billing** and **BYO ephemeral
  providers** are HTTP-key-scoped and remain `/api/v1/*`-only. The in-process
  façade is for trusted same-process embedders. Use the HTTP API / `tofu-sdk`
  when you need those.
* The kernel both surfaces share lives in `lib/tasks_pkg/entry.py`
  (`build_chat_config` / `run_chat_sync` / `run_chat_stream`), so the HTTP
  route and `import tofu` can never drift on how a request becomes a task.

---

## 5. Idempotency

Add an `Idempotency-Key: <client-uuid>` header to any POST that
creates a task or completion. If a duplicate arrives within 24 hours,
the server returns the cached response and adds `Idempotency-Replay: true`.

The key is salted with the authenticated principal so two different
API keys cannot collide.

---

## 6. Examples

### 6.1 Generate a code change with the project tools

```bash
curl -X POST https://your-tofu/api/v1/chat/completions \
  -H "Authorization: Bearer tofu_live_…" \
  -H "Content-Type: application/json" \
  -d '{
    "model": {"creator_id":"anthropic","model_id":"claude-opus-4-7"},
    "messages": [{"role":"user","content":"Add a logger to lib/foo.py"}],
    "config": {
      "projectPath": "/abs/path/to/repo",
      "thinkingDepth": "high",
      "memoryEnabled": true
    }
  }'
```

### 6.2 Stream a paper report

```bash
TASK=$(curl -s -X POST /api/v1/agents/paper/report \
  -H "Authorization: Bearer …" \
  -d '{"paper_text":"…","lang":"zh"}' | jq -r .task_id)

curl -N "/api/v1/tasks/$TASK/stream"
```

### 6.3 Delegate to a webhook (serverless)

```bash
# 1. Subscribe
SUB=$(curl -X POST /api/v1/webhooks \
  -H "Authorization: Bearer …" \
  -d '{"url":"https://my-fn.lambda-url/aws.com","channel":"chat","event_types":["done"]}')

# 2. Issue a chat completion as fire-and-forget
curl -X POST /api/v1/chat/completions \
  -H "Authorization: Bearer …" \
  -d '{"model":{"creator_id":"anthropic","model_id":"claude-opus-4-7"},
       "messages":[{"role":"user","content":"Hi"}]}'
# Webhook receives the terminal `done` event with full result.
```

### 6.4 Verify a webhook signature (Python)

```python
import hashlib, hmac
def verify(secret: str, body: bytes, ts: str, signature: str) -> bool:
    expected = 'v1=' + hmac.new(secret.encode(), f'{ts}.{body.decode()}'.encode(),
                                 hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
```

---

## 7. Observability

### 7.1 Per-key usage analytics

```bash
# Your own usage
curl -H "Authorization: Bearer tofu_live_…" \
  https://your-tofu/api/v1/usage?days=30

# Admins can inspect any key
curl -H "Authorization: Bearer tofu_admin_…" \
  https://your-tofu/api/v1/usage?key_id=k_a3f2c1&days=30

# Aggregate summary across all keys (admin only)
curl -H "Authorization: Bearer tofu_admin_…" \
  https://your-tofu/api/v1/usage/summary?days=7
```

Each daily bucket carries `requests`, `tokens`, and a `by_model`
breakdown. Retention is 90 days rolling, with at most 1,024 key buckets per
day and 128 model buckets per key. Higher-cardinality telemetry is accumulated
under `_overflow` / `_other`, so aggregate request and token totals remain
available without unbounded analytics state.

### 7.2 Prometheus metrics

`GET /metrics` (admin-scoped) returns standard Prometheus text-format
exposition. Configure your scraper with `Authorization: Bearer
tofu_admin_…`.

Exposed metrics:

| Metric                              | Type    | Labels                |
|-------------------------------------|---------|-----------------------|
| `tofu_usage_requests_total`         | counter | `key_id`, `window`    |
| `tofu_usage_tokens_total`           | counter | `key_id`, `window`    |
| `tofu_active_keys`                  | gauge   | —                     |
| `tofu_tasks_inflight`               | gauge   | `kind`                |
| `tofu_tasks_total`                  | gauge   | `kind`, `status`      |
| `tofu_idempotency_cache_size`       | gauge   | —                     |
| `tofu_rate_limit_buckets`           | gauge   | —                     |
| `tofu_push_subscribers`             | gauge   | —                     |
| `tofu_cgroup_relief_attempts_total` | counter | —                     |
| `tofu_cgroup_relief_reclaimed_bytes_total` | counter | `source`        |
| `tofu_cgroup_relief_reclaimed_bytes_latest` | gauge | `source`          |
| `tofu_cgroup_relief_duration_seconds` | histogram | —                 |

The `window` label takes values `1d`, `7d`, `30d` so dashboards can
graph short- and long-term trends from the same scraper.

---

## 8. Versioning policy

* **Versioned `/api/vN/*` routes**: stable within a major version; additive
  changes stay in-place and breaking changes use a new major namespace.
  Deprecated fields are kept for 6 months.
* **`/v1/chat/completions`** and **`/v1/messages`**: track upstream
  OpenAI / Anthropic shapes. We update when they update.
* **Legacy `/api/*`**: tied to the UI; not stable for headless callers.

---

## 9. Operational notes

* **CORS**: enable on the front-proxy. Tofu does not set CORS headers.
* **TLS**: proxy-safe HTTP/1.1 by default. Set `TOFU_TLS=1` (or configure a
  certificate pair) for direct HTTPS + HTTP/2; `--no-tls` explicitly keeps
  HTTP. For SSE
  consumers, ensure intermediate proxies support streaming
  (`X-Accel-Buffering: no` is set on every SSE response).
* **Logs**: every authenticated request is audit-logged
  (`logs/audit.log`) with `key_id` so you can post-hoc trace usage.
* **Long-running tasks**: tasks survive the request lifecycle. Even if
  your client disconnects, the orchestrator continues; reconnect via
  `/api/v1/tasks/{id}/stream` to resume.
