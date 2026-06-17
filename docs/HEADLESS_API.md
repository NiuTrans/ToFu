# Tofu Headless API Guide

> Tofu's backend is a fully featured agent foundation. Every UI feature
> is reachable as a HTTP API. This document is the canonical reference
> for **API-only callers** — SDKs, CLIs, n8n / Zapier nodes, custom
> backends, evaluation harnesses, scripts.

If you only want to drop Tofu into an OpenAI- or Anthropic-SDK app,
skip to the [Compatibility adapters](#compatibility-adapters) section
— it's a one-liner.

---

## 1. Surfaces at a glance

| Surface             | Path prefix     | Best for                                       |
|---------------------|-----------------|------------------------------------------------|
| **Tofu native v1**  | `/api/v1/*`     | Full feature parity with the UI; new clients   |
| **OpenAI compat**   | `/v1/...`       | Existing code using `openai` / `langchain-openai` / OpenWebUI / LangChain / Cline / Aider / Continue.dev |
| **Anthropic compat**| `/v1/messages`  | Existing code using the Anthropic SDK / Claude Code-style tools |
| **Legacy `/api/*`** | `/api/...`      | The browser UI; still maintained, no auth-key support |

Self-describing endpoints:

* `GET /api/openapi.json`  — full OpenAPI 3.1 spec
* `GET /api/openapi.yaml`  — same, YAML
* `GET /api/docs`          — interactive **Swagger UI**
* `GET /api/redoc`         — alternative ReDoc viewer
* `GET /api/v1/capabilities` — runtime model/tool/agent registry

---

## 2. Authentication

Two complementary mechanisms:

| Use case            | How                                        |
|---------------------|--------------------------------------------|
| Browser / UI        | `TUNNEL_TOKEN` env var; cookie set on first `?token=` visit |
| Programmatic / CI   | **Bearer API key** — `Authorization: Bearer tofu_live_…`    |

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
| `agents:run`         | `/api/v1/agent/run` (single-call agent runtime, BYOM)  |
| `providers`          | `/api/v1/providers/*` (BYO model-endpoint CRUD)        |
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
  "model": "claude-opus-4-7",                         // optional; falls back to server default
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
    "endpointMode": false,
    "swarmEnabled": false,
    "mcpEnabled": true,
    "disableModelFallback": false
  },
  "conversation_id": "my-headless-job-001",             // optional
  "idempotency_key": "uuid-or-anything-stable",          // optional, replays cached response
  "timeout_s": 600
}
```

> **⚠️ Automatic model fallback (important for pinned-model callers).**
> The server admin can configure a global *fallback model* (Settings →
> model defaults). When set, a transient error on your requested model
> causes Tofu to **silently re-run that round on the fallback model** —
> so a request pinned to `model: "X"` can return output from a different
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

### 3.2 Generic task lifecycle — `/api/v1/tasks/*`

Once you have a `task_id` (from chat completions, paper, translate,
swarm, etc.), you can drive it uniformly:

```
GET    /api/v1/tasks                 — list (filter by kind, status)
GET    /api/v1/tasks/{id}            — full state snapshot
GET    /api/v1/tasks/{id}/events?cursor=N — long-poll cursor replay
GET    /api/v1/tasks/{id}/stream     — SSE replay-from-cursor
POST   /api/v1/tasks/{id}/abort      — graceful stop
DELETE /api/v1/tasks/{id}            — drop from registry (admin)
```

Cursor-based replay means the consumer can disconnect and reconnect
without losing events.

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

### 3.6 Real-time push — `WS /api/push`

If you want a single WebSocket multiplexing every channel/task, this
is the same socket the UI uses. Send `{"action":"subscribe", "channel":"chat", "taskId":"…"}`
and the server pushes every event for that task. See
[`lib/agent_core/push.py`](../lib/agent_core/push.py) for the full protocol
(`lib/push.py` remains as a re-export shim).

### 3.6.1 Streaming event contract — the frontend↔backend sync interface

> **If you are building your own frontend, this is the section you read.**
> The agent runtime emits a fixed vocabulary of JSON events. They flow over
> the SSE chat stream, the `/api/v1/tasks/{id}/stream` replay stream, and the
> `/api/push` WebSocket — the same events, regardless of transport.

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
      "sse": ["/api/chat/stream/<task_id>", "/api/v1/tasks/<task_id>/stream"],
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
| `endpoint` | `endpoint_iteration`, `endpoint_planner_done`, `endpoint_critic_msg`, `endpoint_new_turn`, `endpoint_complete` | Planner→Worker→Critic loop |
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

### 3.7 Bring Your Own Model (BYOM)

> External callers supply the LLM endpoint; Tofu supplies the agent
> runtime, tools, memory, swarm, and trajectory capture. This is the
> "Tofu is an agent runtime; you bring the model" surface.

Three layered ways to attach a custom endpoint, each strictly more
powerful than the one below:

#### 3.7.1 Persistent providers — `/api/v1/providers/*`

Register an OpenAI-compatible endpoint (vLLM / SGLang / Ollama /
in-house gateway) once and reuse it across many runs. Providers are
**scoped to the calling API key** — caller A never sees caller B's
endpoint or secret.

```bash
curl -X POST https://your-tofu/api/v1/providers \
  -H "Authorization: Bearer tofu_live_…" \
  -d '{
    "name": "deepseek-cluster-A",
    "base_url": "http://33.236.230.114:8080/v1",
    "api_key": "sk-internal-…",
    "models": [{"model_id":"deepseek-v4-pro"}]
  }'
# → { provider: { id: "prov_a3f2c1", key_hint: "sk-int…ar", … } }
```

Registration is fast and unconditional — `auto_discover` defaults to
**false**. To ingest the served model list, follow up with
`POST /api/v1/providers/{id}/probe` (or pass `auto_discover: true`
on creation if you're willing to pay for the synchronous round-trip).

| Endpoint                                 | Scope       | Purpose                              |
|------------------------------------------|-------------|--------------------------------------|
| `POST   /api/v1/providers`               | `providers` | Register the endpoint                |
| `GET    /api/v1/providers`               | `providers` | List own providers (no api_keys)     |
| `GET    /api/v1/providers/{id}`          | `providers` | Get one (key redacted to `key_hint`) |
| `PATCH  /api/v1/providers/{id}`          | `providers` | Update name / url / key / models     |
| `DELETE /api/v1/providers/{id}`          | `providers` | Drop                                 |
| `POST   /api/v1/providers/{id}/probe`    | `providers` | Re-discover models                   |

Once registered, pin any chat / agent run to it via the model-string
suffix `<model_id>@<prov_id>`:

```bash
curl -X POST https://your-tofu/api/v1/chat/completions \
  -H "Authorization: Bearer tofu_live_…" \
  -d '{"model":"deepseek-v4-pro@prov_a3f2c1",
       "messages":[{"role":"user","content":"Hi"}]}'
```

The dispatcher mints an ephemeral slot for that one request, runs it
through the provider, and tears the slot down on completion. The
api_key is held in process memory only — never logged, never echoed
back in any response or `/tasks/{id}` snapshot.

#### 3.7.2 Single-call agent runtime — `POST /api/v1/agent/run`

Headline endpoint for "I have my own model and I want to run an agent
turn end-to-end." One request bundles the prompt, the LLM endpoint,
the agent capabilities, and the trajectory format.

```jsonc
POST /api/v1/agent/run
Authorization: Bearer tofu_live_…
{
  "messages": [{"role":"user","content":"Refactor lib/foo.py"}],

  // 1. model is ALWAYS a string
  "model": "deepseek-v4-pro",                  // (a) plain alias
  // "model": "deepseek-v4-pro@prov_a3f2c1",    // (b) registered BYO

  // 2. (c) inline BYO: pair `model` with a `provider` block
  // "provider": {
  //   "base_url": "http://33.236.230.114:8080/v1",
  //   "api_key":  "sk-…",
  //   "extra_headers": { "X-Internal-Tag": "..." }
  // },

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

**Shape (a) — plain alias**. Resolves against the global slot pool;
the operator-curated set of models. No ephemeral slot.

**Shape (b) — BYO suffix**. Resolves against the caller's providers
(see 3.7.1); mints + disposes an ephemeral slot for this task.

**Shape (c) — inline `provider` block**. The whole
`(base_url, api_key, [extra_headers])` is supplied per-request.
The same one-shot ephemeral slot lifecycle applies. Use this for
one-off evaluation runs or trajectory generation when you don't
want to persist the endpoint.

> **Header allowlist**: `extra_headers` rejects `Authorization`,
> `x-api-key`, `Cookie`, `Host`, `Content-Length`,
> `Transfer-Encoding`, `Proxy-Authorization` — names that would
> impersonate Tofu's own outbound auth. Up to 16 entries, 2048 chars
> per value.

##### Thinking-format dialect (`thinking_format`)

Most BYO callers never need to set this — Tofu auto-detects from the
model name and (for registered providers) the `/v1/models` `owned_by`
field. Set it explicitly when the auto-detect would be wrong, most
commonly when a self-hosted Qwen3 / GLM / DeepSeek-V4 dual-mode model
is served via **sglang** or **vLLM**: those engines accept
`chat_template_kwargs.enable_thinking` rather than the cloud-API
top-level `enable_thinking` field, and silently ignore anything else.

Legal values:

| `thinking_format`      | Body shape sent to the engine                       | Engines       |
|------------------------|-----------------------------------------------------|---------------|
| `""` (default)         | Auto-detect from model name + brand                 | —             |
| `enable_thinking`      | top-level `{"enable_thinking": bool}`               | Bailian Qwen, LongCat, ERNIE |
| `thinking_type`        | `{"thinking": {"type": "enabled"\|"disabled"}}`     | Doubao, GLM cloud, Kimi, Claude |
| `reasoning_effort`     | top-level `{"reasoning_effort": "minimal"\|"low"\|"medium"\|"high"}` | Gemini 3.x (maps to Vertex `thinkingLevel`) |
| `chat_template_kwargs` | `{"chat_template_kwargs": {"enable_thinking": …}}`  | sglang, vLLM, any OpenAI-shim engine that gates thinking through Jinja |
| `none`                 | nothing thinking-related sent                       | DeepSeek-Reasoner (always-thinking) |

The same value lives on `provider:` block (inline path), on registered
provider rows (set automatically by `auto_discover` / `/probe`, or via
`PATCH /api/v1/providers/{id}`), and on the persistent
`Slot.thinking_format` field. Slot validates at construction —
unknown values raise `ValueError` rather than silently degrade to
auto-detect.

Probing via `POST /api/v1/providers` with `auto_discover: true` (or
`POST /api/v1/providers/{id}/probe`) returns the suggested value so
your client can echo it back to the user.

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

A typical "BYO + trajectory" key is created with:

```bash
curl -X POST /api/v1/keys \
  -H "Authorization: Bearer tofu_admin_…" \
  -d '{"name":"trajectory-pipeline",
       "scopes":["providers","agents:run","tasks"]}'
```

Note that **`agents:run` does not include `chat`** — keys minted with
just `agents:run` cannot use the operator-curated slot pool via
`/api/v1/chat/completions`; they must supply their own model
endpoint. Combine with `chat` if you want both.

#### 3.7.4 Discovery via OpenAI-compatible `/v1/models`

When called with a Bearer key that owns BYO providers, `GET /v1/models`
includes those providers' models with the BYO suffix already
attached:

```jsonc
GET /v1/models
Authorization: Bearer tofu_live_…
→ {
  "object": "list",
  "data": [
    {"id":"gpt-5","object":"model","owned_by":"openai", ...},
    {"id":"deepseek-v4-pro@prov_a3f2c1",      // ← BYO model
     "object":"model","owned_by":"prov_a3f2c1",
     "tofu_provider_name":"deepseek-cluster-A",
     "capabilities":["text","thinking"]}
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
| `tool_rounds_exhausted` | no | Hit the per-task tool-round budget |
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
[`lib/error_envelope.py`](../lib/error_envelope.py) (`KINDS`); a drift
test keeps it honest.

> The envelope's `context` field is a free-form diagnostic tag (not a
> closed enum). One value worth recognising: `context:
> "fallback-disabled"` means the primary model errored and automatic
> fallback was suppressed because this request set
> `config.disableModelFallback: true`. The error you see is the real
> primary-model error — branch on `kind` / `retryable` as usual and
> retry on the SAME model rather than expecting a fallback to have
> masked it.

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

* `model` resolves through Tofu's dispatcher — supply the same id you'd
  use in `/api/v1/chat/completions`.
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
    "model": "claude-opus-4-7",
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
  -d '{"messages":[{"role":"user","content":"Hi"}]}'
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
breakdown. Retention: 90 days, rolling.

### 7.2 Prometheus metrics

`GET /metrics` (admin-scoped) returns standard Prometheus text-format
exposition. Configure your scraper with `Authorization: Bearer
tofu_admin_…` (or `X-Tunnel-Token`).

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

The `window` label takes values `1d`, `7d`, `30d` so dashboards can
graph short- and long-term trends from the same scraper.

---

## 8. Versioning policy

* **`/api/v1/*`**: stable, additive changes only. Breaking changes go
  to `/api/v2`. Deprecated fields are kept for 6 months.
* **`/v1/chat/completions`** and **`/v1/messages`**: track upstream
  OpenAI / Anthropic shapes. We update when they update.
* **Legacy `/api/*`**: tied to the UI; not stable for headless callers.

---

## 9. Operational notes

* **CORS**: enable on the front-proxy. Tofu does not set CORS headers.
* **TLS**: HTTPS + HTTP/2 by default (`--no-tls` to disable). For SSE
  consumers, ensure intermediate proxies support streaming
  (`X-Accel-Buffering: no` is set on every SSE response).
* **Logs**: every authenticated request is audit-logged
  (`logs/audit.log`) with `key_id` so you can post-hoc trace usage.
* **Long-running tasks**: tasks survive the request lifecycle. Even if
  your client disconnects, the orchestrator continues; reconnect via
  `/api/v1/tasks/{id}/stream` to resume.
