# Developer runtime contract

This is the authority for consuming Tofu's agent kernel without the full
application database or full Tofu UI. It defines Python composition, the lightweight
HTTP/SSE sidecar, model-routing setup, transient state, and release artifacts.
The full backend API is documented in
[HEADLESS_API.md](HEADLESS_API.md).

## Product boundary

```text
Python caller ── tofu_agent.AgentRuntime ─┐
                                         ├─ transient TaskRuntime
HTTP caller ── tofu-agent sidecar ────────┘          │
                                                    ▼
                                        production agent orchestrator
                                                    │
                                          v2 route / tools / MCP
```

`AgentRuntime` uses the same task and agent execution seams as the full
application. Each invocation carries an explicit `PrincipalContext`, owns one
bounded transient runtime, and uses the canonical
`tofu.model-routing/v2` candidate compiler. It does not initialize full-app
routes, conversations, accounts, billing, scheduler state, or the storage
sidecar. Task/event replay, idempotency, and active runs exist only in bounded
process memory and disappear at restart.

This is a composition mode, not another orchestrator or provider-routing state
machine.

## Release artifacts

All public artifacts use the repository [`VERSION`](../VERSION):

| Artifact | Public name | Entry point |
|---|---|---|
| Python runtime | `tofu-agent` | `import tofu_agent`; `tofu-agent` CLI |
| Python remote SDK | `tofu-sdk` | `import tofu_sdk` |
| TypeScript SDK | `@rangehow/tofu-sdk` | `import { Tofu } …` |
| OCI runtime | `ghcr.io/rangehow/tofu-agent:<version>` | port `15001` |

The runtime wheel contains the agent kernel, model-routing compiler, provider
wire adapters, tools/MCP, and a dependency-free static setup page. It excludes
the full application routes, storage implementations, Tofu UI assets, tests, and
runtime data. The OCI `agent` target installs that wheel into a
dependency-only final image.

## Model-routing ownership

The standalone runtime accepts one complete access envelope:

```json
{
  "model_routing": {
    "contract_version": "tofu.model-routing/v2",
    "revision": 0,
    "creators": [
      {"creator_id": "openai", "name": "OpenAI"}
    ],
    "models": [
      {
        "creator_id": "openai",
        "model_id": "gpt-x",
        "display_name": "GPT X",
        "capabilities": ["text", "thinking"],
        "context_window": 131072,
        "quality_rank": 100
      }
    ],
    "providers": [
      {"provider_id": "gateway-a", "name": "Gateway A", "scope": "owner"}
    ],
    "provider_accesses": [
      {
        "provider_access_id": "gateway-a-access",
        "provider_id": "gateway-a",
        "enabled": true,
        "quota_policy": {"rpm": 60}
      }
    ],
    "connections": [
      {
        "connection_id": "gateway-a-primary",
        "provider_access_id": "gateway-a-access",
        "base_url": "https://models.example/v1",
        "protocol": "openai",
        "enabled": true,
        "priority": 0,
        "extra_headers": {}
      }
    ],
    "credentials": [
      {
        "credential_id": "gateway-a-key",
        "provider_access_id": "gateway-a-access",
        "kind": "api_key",
        "secret_reference": "gateway-a-secret",
        "key_hint": "configured",
        "enabled": true,
        "authorization": {
          "connection_ids": ["gateway-a-primary"],
          "models": [{"creator_id": "openai", "model_id": "gpt-x"}]
        },
        "quota_policy": {}
      }
    ],
    "offerings": [
      {
        "offering_id": "gateway-a-gpt-x",
        "provider_access_id": "gateway-a-access",
        "identity_state": "confirmed",
        "model": {"creator_id": "openai", "model_id": "gpt-x"},
        "enabled": true,
        "capabilities": ["text", "thinking"],
        "context_window": 131072,
        "priority": 0
      }
    ],
    "deployments": [
      {
        "deployment_id": "gateway-a-gpt-x-primary",
        "offering_id": "gateway-a-gpt-x",
        "connection_id": "gateway-a-primary",
        "wire_model_id": "vendor/gpt-x",
        "enabled": true,
        "identity_confidence": "high",
        "probe_status": "passed",
        "priority": 0
      }
    ]
  },
  "model": {"creator_id": "openai", "model_id": "gpt-x"},
  "routing": {"preferred_provider_id": "gateway-a"},
  "credential_secrets": {"gateway-a-secret": "sk-..."}
}
```

The aggregate contains only redacted credential metadata and opaque secret
references. `credential_secrets` is a separate map keyed by those references;
it is excluded from `repr`, readiness, capabilities, setup reads, task
snapshots, and results. A non-local enabled Credential must have exactly one
configured secret value. Plain model strings, `model@provider`, provider
blocks, aliases, and request-ID pools are rejected.

The configured `model` is the process default. Each run may override it with
another structured official Model or provider-scoped pending Offering from the
same aggregate and may supply request `routing` policy. Each admitted run gets
a request-owned slot group and keeps it until terminal settlement; a setup
change affects only later runs.

### Authority order

The sidecar resolves configuration in this order:

1. `--model-routing-json` or `--model-routing-file`;
2. `TOFU_AGENT_MODEL_ROUTING`, containing the exact JSON envelope above;
3. the encrypted saved setup document;
4. unconfigured.

`TOFU_AGENT_MODEL_ROUTING_FILE` supplies the CLI file default. CLI/environment
authority makes the setup API read-only until the override is removed and the
process restarts. `.env` values never override already-present process
environment values.

A fresh sidecar may start unconfigured. `GET /health/live` remains 200 while
`GET /health/ready` returns 503 with `setup_required:true`. The bundled
`/setup` page edits the complete v2 aggregate, sends one minimal probe against
the computed primary Deployment, and hot-applies a successful save.

### Encrypted setup storage

| Item | Default / override |
|---|---|
| Model-routing document | `~/.config/tofu-agent/model-routing.json`; `TOFU_AGENT_CONFIG_PATH` overrides it |
| Encryption key | Adjacent `model-routing.json.key`; `TOFU_AGENT_CONFIG_KEY` overrides it |
| Permissions | Owner read/write only (`0600`) |

`XDG_CONFIG_HOME` is honored on Unix and `APPDATA` on Windows. The file
stores public aggregate metadata plus one Fernet-encrypted
`credential_secrets` envelope. File and key writes are atomic; a corrupt file,
missing key, unsupported schema, or decryption failure fails closed. Back up
the document and adjacent key together. Delete removes the document but retains
the key to avoid secret churn on a later save.

## Python composition

```python
from tofu_agent import AgentRuntime, ModelRoutingConfig

access = ModelRoutingConfig.from_mapping(model_routing_envelope)

with AgentRuntime.local(model_routing=access, max_inflight=4) as runtime:
    execution = runtime.start(
        [{"role": "user", "content": "Inspect this project"}],
        routing={"preferred_provider_id": "gateway-a"},
        config={
            "thinking": "high",
            "tools": ["search", "fetch"],
            "project": "/workspace/project",
        },
        request_id="run-owned-by-caller",
        timeout_s=600,
    )

    for event in execution.events(cursor=0):
        consume(event)
    result = execution.result()
```

`AgentRuntime.local()` constructs an explicit personal principal. A server or
enterprise adapter constructs `AgentRuntime(principal=..., model_routing=...)`
with its authenticated owner instead of relying on a module global. A single
request may pass `model_routing=...` to use a complete one-run envelope; it
does not persist or mutate the runtime default.

| Method | Contract |
|---|---|
| `run` / `run_async` | Submit once and wait for a typed terminal `AgentResult`. |
| `stream` / `stream_async` | Submit once and yield native events. |
| `start` | Return an `AgentExecution` immediately. |
| `events(cursor=N)` | Replay from the absolute next sequence, then follow live. |
| `event_page(cursor=N)` | Return one bounded replay page. |
| `snapshot()` | Return the stable `agent.run` projection. |
| `abort()` | Request cooperative cancellation for the owned task. |
| `close()` | Reject new work and perform bounded active-run teardown. |

Admission is bounded by `max_inflight`. Overload, configuration, timeout,
closed-runtime, and execution failures retain distinct public exception types.

## Lightweight HTTP/SSE contract

Start locally with `tofu-agent doctor` and `tofu-agent serve`.

| Method and path | Purpose |
|---|---|
| `GET /setup` | Static model-routing control plane. |
| `GET /api/v1/setup/model-routing` | Redacted active aggregate, source, editability, and storage state. |
| `POST /api/v1/setup/model-routing/test` | Validate the full envelope and probe its computed primary Deployment without saving. |
| `PUT /api/v1/setup/model-routing` | Encrypt, save, and hot-apply the full envelope. |
| `DELETE /api/v1/setup/model-routing` | Remove saved access and unconfigure later runs. |
| `GET /health/live` | Dependency-free liveness. |
| `GET /health/ready` | Redacted readiness and bounded capacity. |
| `GET /api/v1/capabilities` | Installed runtime, model-routing state, events, and limits. |
| `POST /api/v1/agent/run` | Blocking result, SSE (`stream:true`), or HTTP 202 (`async:true`). |
| `GET /api/v1/tasks/{id}` | Current run projection. |
| `GET /api/v1/tasks/{id}/events?cursor=N` | Bounded replay page. |
| `GET /api/v1/tasks/{id}/stream?cursor=N` | Native replay and live SSE. |
| `POST /api/v1/tasks/{id}/abort` | Cooperative abort. |
| `POST /api/v1/tasks/{id}/tools/{call_id}/result` | Resolve one client tool call. |

The sidecar deliberately omits conversations, full-app provider repositories,
accounts, billing, scheduling, files, and feature routes. The setup service
owns one process default aggregate; it is not the full app's owner repository.

A minimal managed-default request is:

```http
POST /api/v1/agent/run
Authorization: Bearer <TOFU_AGENT_TOKEN>
Idempotency-Key: <stable caller key>
Content-Type: application/json

{"messages":[{"role":"user","content":"Hello"}]}
```

`Prefer: respond-async` or `"async":true` returns 202, `Location`, and
`X-Tofu-Task-Id`. `"stream":true` returns `agent.run.chunk` SSE. Every
event has an absolute `id`/`seq`; resume an existing task with
`cursor=last_seq+1` or `Last-Event-ID:last_seq`. Streaming never resubmits a
run after a transport disconnect.

Idempotency is scoped to the runtime principal and bounded by count and TTL.
The same key/body returns the same execution; a different body returns
`409 idempotency_conflict`.

### Authentication

`/`, liveness/readiness, `/setup`, and setup assets are public and contain
no configuration. Every `/api/v1/setup/*`, agent, and task route follows the
same auth policy; setup data routes also reject cross-site browser requests.
With `TOFU_AGENT_TOKEN`, an exact Bearer token is required. Without a token,
automatic mode accepts only loopback bind, peer, and Host authority. The CLI
refuses an exposed tokenless bind unless `--allow-unauthenticated` is explicit;
the OCI image therefore requires a token.

The setup page holds a bearer token only in JavaScript memory. It uses no
cookies, browser storage, or external resources. A remote one-time
`/setup#token=...` fragment is removed from the address bar immediately and
is never sent to the server. Disable setup with `--no-setup` or
`TOFU_AGENT_SETUP_ENABLED=0`.

## State and restart semantics

| Property | Developer runtime | Full application |
|---|---|---|
| Task/event authority | Bounded process memory | Storage sidecar |
| Reconnect | Cursor resume while this process lives | Durable task replay |
| Restart | Runs and idempotency records are lost | Durable domains recover by policy |
| Model access | Encrypted v2 file or explicit envelope | Owner-aware v2 repository |
| Database/full Tofu UI | Never initialized | Declared application lifecycle |
| Accounts/billing | None | Auth and repository boundaries |

Model-access durability does not imply task durability.

## Verification and release

Minimum gates for a public runtime change are:

```bash
python3 scripts/check_developer_runtime_artifacts.py
pytest -q tests/test_agent_provider_setup.py tests/test_public_agent_runtime.py \
  tests/test_headless_agent_server.py
pytest -q tests/test_inprocess_facade.py tests/test_sdk_sse_parser.py
npm run build --prefix clients/typescript
uvx --from build --with 'setuptools==80.10.2' --with 'wheel==0.48.0' \
  pyproject-build --no-isolation
python3 scripts/check_developer_runtime_artifacts.py dist/tofu_agent-0.17.0-*.whl
```

The clean-wheel smoke installs only the wheel outside the repository, checks
that database drivers and full-app assets are absent while setup assets are
present, runs `tofu-agent doctor`, and completes one turn against a controlled
OpenAI-compatible server. Release workflows publish Python/TypeScript/OCI
artifacts from the same version and gate secrets, SBOMs, and image
vulnerabilities before registry publication.
