# Developer runtime contract

This is the authority for consuming Tofu's agent kernel without installing the
database or ChatUI application frontend. It defines the Python composition,
the lightweight HTTP/SSE sidecar and Provider setup page, provider ownership,
process-memory state, and release artifacts. The full application's wider API
remains documented in
[HEADLESS_API.md](HEADLESS_API.md).

## Product boundary

The developer runtime has three delivery forms and one execution owner:

```text
Python caller ── tofu_agent.AgentRuntime ─┐
                                         ├─ transient TaskRuntime
HTTP caller ── tofu-agent sidecar ────────┘          │
                                                    ▼
                                        production agent orchestrator
                                                    │
                                     model dispatch / tools / MCP / network
```

`AgentRuntime` calls the same `lib.tasks_pkg.create_task` and `spawn_task`
seams as the full application. A task is marked `transient=True` before its
first event. Transient tasks:

- carry an explicit `PrincipalContext` and numeric owner;
- use the normal agent loop, provider dispatch, tools, retries, compaction,
  cancellation, terminal callbacks, and event vocabulary;
- keep their task state and replay log in bounded process memory;
- skip durable birth rows, event persistence, result persistence, project
  feeds, supersede/latest indexes, and storage-affinity mirrors;
- omit storage-backed knowledge, durable memory, cross-conversation project
  state, and long-lived scheduler tools while retaining network, project-file,
  MCP, custom, media, and in-process orchestration tools;
- never initialize the application server, storage sidecar, billing, accounts,
  ChatUI application frontend, or conversation repositories.

This is a composition mode, not a second orchestrator.

## Release artifacts

All public artifacts use the repository [`VERSION`](../VERSION):

| Artifact | Public name | Entry point |
|---|---|---|
| Python runtime | `tofu-agent` | `import tofu_agent`; `tofu-agent` CLI |
| Python remote SDK | `tofu-sdk` | `import tofu_sdk` |
| TypeScript SDK | `@rangehow/tofu-sdk` | `import { Tofu } …` |
| OCI runtime | `ghcr.io/rangehow/tofu-agent:<version>` | port `15001` |

The runtime wheel includes Agent, provider, network, MCP, document, media, and
tool code plus their package data. It also contains one dependency-free static
Provider setup page. The wheel and source distribution exclude the full HTTP
application routes, storage implementations, full Tofu application
assets, tests, and runtime data; the default dependency set also excludes
SQLAlchemy, PostgreSQL drivers, and the application updater. The OCI `agent`
target installs that wheel into a dependency-only environment and does not copy
the repository into the final image.

The distribution name is intentionally `tofu-agent`; the Python import package
is `tofu_agent`.

## Provider ownership

### Managed default through `/setup` (recommended)

A fresh sidecar is deliberately allowed to start without a model:

```bash
pip install tofu-agent
tofu-agent serve
```

`GET /health/live` is immediately 200, while `GET /health/ready` is 503 with
`setup_required: true`. Open <http://127.0.0.1:15001/setup> and use the bundled
control plane:

1. Choose an OpenAI, OpenRouter, DeepSeek, local-engine, or custom template.
2. Enter an OpenAI-compatible endpoint and API key. Local engines may leave the
   key empty.
3. Discover `/models` or type the exact wire model id.
4. Send one minimal real `/chat/completions` probe, then save.

Saving atomically encrypts the Provider, applies it to subsequently started
runs, and turns readiness 200 without restarting. Already admitted runs retain
their original ephemeral Provider slot. The browser receives only a short key
hint; leaving the key field blank preserves the saved key, changing the
endpoint requires re-entering it, and clearing it requires an explicit checkbox.

Every application call can then omit `model` and `provider`. Remote applications
only retain the Tofu service URL and its bearer token. Changing the managed
model is an operator action, not a consumer-code change.

The database-free settings authority is one encrypted file:

| Item | Default / override |
|---|---|
| Provider document | `~/.config/tofu-agent/provider.json`; `TOFU_AGENT_CONFIG_PATH` overrides it |
| Encryption key | Adjacent `.provider.json.key`; `TOFU_AGENT_CONFIG_KEY` overrides it |
| File permissions | Owner read/write only (`0600`) |

`XDG_CONFIG_HOME` is honored on Unix and `APPDATA` on Windows. Endpoint, model,
thinking dialect, and capability names remain operational metadata in the
document. The API key and all custom-header values live only inside its Fernet
envelope. Save and key writes are atomic, secrets are never returned by setup,
health, capability, or result APIs, and a corrupt/missing key fails closed.
Back up or migrate the document and adjacent key together. Deleting a Provider
removes the document but retains the key so a later save does not churn secrets.
An injected `TOFU_AGENT_CONFIG_KEY` must be a URL-safe Fernet key; generate one
with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
and keep it in the deployment's secret manager.

### Unattended configuration

Environment variables remain the automation path:

```text
TOFU_AGENT_PROVIDER_BASE_URL=https://api.openai.com/v1
TOFU_AGENT_PROVIDER_API_KEY=sk-...
TOFU_AGENT_PROVIDER_MODEL=gpt-5.6
```

Local OpenAI-compatible engines may use an empty key. If a key is provided
without an endpoint, the endpoint defaults to `https://api.openai.com/v1`.

Environment resolution is deterministic, first non-empty value wins:

| Value | Precedence |
|---|---|
| Endpoint | `TOFU_AGENT_PROVIDER_BASE_URL` → `TOFU_PROVIDER_BASE_URL` → `LLM_BASE_URL` |
| API key | `TOFU_AGENT_PROVIDER_API_KEY` → `TOFU_PROVIDER_API_KEY` → `LLM_API_KEY` → first `LLM_API_KEYS` value |
| Model | `TOFU_AGENT_PROVIDER_MODEL` → `TOFU_PROVIDER_MODEL` → `TOFU_AGENT_MODEL` → `LLM_MODEL` |
| Extra headers | `TOFU_AGENT_PROVIDER_EXTRA_HEADERS` → `TOFU_PROVIDER_EXTRA_HEADERS` (JSON object) |
| Thinking dialect | `TOFU_AGENT_PROVIDER_THINKING_FORMAT` → `TOFU_PROVIDER_THINKING_FORMAT` |

The sidecar authority order is explicit CLI Provider arguments, environment,
encrypted saved Provider, then unconfigured. `.env` values do not override
values already present in the process environment. When CLI or environment
owns the active Provider, `/setup` remains available for diagnosis but is
read-only; remove the override and restart to edit the saved Provider. Direct
`AgentRuntime.local()` composition uses its explicit `ProviderConfig` or the
same environment resolution and does not start an HTTP setup control plane.

### Request-level provider

A request may override the runtime default with one object:

```json
{
  "endpoint": "https://models.example/v1",
  "api_key": "sk-...",
  "model": "model-name"
}
```

`base_url` and `endpoint` are aliases. `api_key` may be empty. Advanced callers
may additionally supply `extra_headers`, `thinking_format`, and
`capabilities`. Credential-bearing headers reserved by the outbound transport
are rejected. Each request provider is minted as an isolated ephemeral slot,
pinned to that task, and disposed exactly once at terminal settlement or
admission failure.

`ProviderConfig` excludes its key from `repr` and `public_dict`. The plaintext
key is not included in task snapshots, results, health, or capabilities;
the authenticated setup surface returns only a bounded recognition hint, and
ephemeral-slot logs record only whether a credential is configured.

## Python composition

```python
from tofu_agent import AgentRuntime, ProviderConfig

provider = ProviderConfig(
    base_url="https://models.example/v1",
    api_key="sk-...",
    model="model-name",
)

with AgentRuntime.local(provider=provider, max_inflight=4) as runtime:
    execution = runtime.start(
        [{"role": "user", "content": "Inspect this project"}],
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

`AgentRuntime.local()` is the personal composition boundary and creates an
explicit local principal. A server or future enterprise adapter constructs
`AgentRuntime(principal=...)` with its authenticated principal instead of
relying on a module-level user.

Public lifecycle methods:

| Method | Contract |
|---|---|
| `run` / `run_async` | Submit once, wait for a typed terminal `AgentResult` |
| `stream` / `stream_async` | Submit once and yield native events |
| `start` | Return an `AgentExecution` immediately |
| `AgentExecution.events(cursor=N)` | Replay from absolute next sequence, then follow live |
| `event_page(cursor=N)` | Return one bounded replay page |
| `snapshot()` | Return the current stable `agent.run` projection |
| `abort()` | Request cooperative cancellation for this owned task |
| `close()` | Reject new work; optionally abort and briefly join active work |

Admission is process-local and bounded by `max_inflight`. Overload raises
`AgentOverloadedError`; configuration, timeout, closed-runtime, and generic
runtime failures retain distinct public exception types.

## Lightweight HTTP/SSE contract

Start locally:

```bash
tofu-agent doctor
tofu-agent serve
```

The sidecar exposes only:

| Method and path | Purpose |
|---|---|
| `GET /setup` | Static no-code Provider control plane |
| `GET /api/v1/setup/provider` | Redacted active Provider, source, editability, and templates |
| `POST /api/v1/setup/provider/discover` | Read the OpenAI-compatible model catalogue without saving |
| `POST /api/v1/setup/provider/test` | Send one minimal real completion without saving |
| `PUT /api/v1/setup/provider` | Encrypt, save, and hot-apply the managed Provider |
| `DELETE /api/v1/setup/provider` | Remove the saved Provider and unconfigure new runs |
| `GET /health/live` | Dependency-free liveness |
| `GET /health/ready` | Redacted model-readiness and capacity |
| `GET /api/v1/capabilities` | Installed runtime, state, config, and event contract |
| `POST /api/v1/agent/run` | Blocking result, SSE (`stream:true`), or HTTP 202 (`async:true`) |
| `GET /api/v1/tasks/{id}` | Current run projection |
| `GET /api/v1/tasks/{id}/events?cursor=N` | Bounded replay page |
| `GET /api/v1/tasks/{id}/stream?cursor=N` | Native SSE replay and live follow |
| `POST /api/v1/tasks/{id}/abort` | Cooperative abort |
| `POST /api/v1/tasks/{id}/tools/{call_id}/result` | Resolve one client tool call |

It deliberately does not expose conversations, multi-provider registration
CRUD, accounts, billing, scheduling, files, or feature-specific full-app routes.
The setup routes manage exactly one process-wide default Provider; they are not
the full application's user-owned `/api/v1/providers/*` repository.

### Submission

Minimal managed-model request:

```http
POST /api/v1/agent/run
Authorization: Bearer <TOFU_AGENT_TOKEN>
Idempotency-Key: <stable caller key>
Content-Type: application/json

{"messages":[{"role":"user","content":"Hello"}]}
```

`Prefer: respond-async` or `"async": true` returns `202`, `Location`, and
`X-Tofu-Task-Id`. `"stream": true` returns `agent.run.chunk` SSE. For durable
SDK streaming, prefer async submission followed by the task stream: it never
resubmits a run after a connection loss.

Idempotency is scoped to the explicit runtime principal, bounded by count and
TTL, and retained for the process lifetime. The same key/body returns the same
execution; a different body with that key returns `409 idempotency_conflict`.

Every real SSE event has an absolute `id`/`seq`. Resume with
`?cursor=<last_seq+1>` or `Last-Event-ID: <last_seq>`. Terminal event types are
`done`, `error`, and `aborted` on this lightweight surface.

### Authentication

- `/`, `/health/live`, `/health/ready`, `/setup`, and its static assets are
  public and contain no Provider data.
- Every `/api/v1/setup/*` data/test/mutation route follows the same auth policy
  as Agent/task APIs. It also rejects cross-site browser requests using Origin
  and Fetch Metadata checks.
- With `TOFU_AGENT_TOKEN`, every API/task route requires an exact Bearer token.
- Without a token, automatic mode accepts only a loopback bind and loopback
  peer. The CLI refuses a non-loopback bind before starting.
- `--allow-unauthenticated` is an explicit unsafe override; it is never inferred.
- The OCI image binds `0.0.0.0`, so it requires `TOFU_AGENT_TOKEN` to start.

TLS should terminate at an ingress or service mesh. Bind the published host
port to loopback unless remote access is intentional.

The setup page keeps a bearer token only in JavaScript memory; it never uses
local storage, session storage, cookies, or external resources. For a one-time
remote link, `/setup#token=<TOFU_AGENT_TOKEN>` is accepted: URL fragments are
not sent to the server and the page immediately removes the fragment from the
address bar. A reload therefore requires the token again. Disable the entire
control plane with `--no-setup` or `TOFU_AGENT_SETUP_ENABLED=0`.

## SDK reliability behavior

The Python and TypeScript SDKs share these rules:

- `model` is optional when the server owns a default;
- a request provider accepts endpoint/key/model without provider registration;
- agent POST retries carry one stable `Idempotency-Key`;
- 429 and 5xx responses honor bounded retry/backoff behavior;
- streaming submits once, then reconnects to the existing task by cursor;
- unknown additive fields and events remain available as wire dictionaries;
- task GET, replay, abort, sync/async clients, and context-managed connections
  are first-class APIs.

The SDKs also retain methods for the full application's wider API. Calling one
of those feature-specific methods against the lightweight sidecar correctly
returns 404; capability discovery is the boundary test.

## State and restart semantics

| Property | Developer runtime | Full application |
|---|---|---|
| Task/event authority | Bounded process memory | Storage sidecar |
| Network reconnect | Cursor resume while process lives | Durable replay per full task contract |
| Process restart | Runs and idempotency records are lost | Durable domains recover by policy |
| Managed Provider | Encrypted file; survives restart | User-owned storage repository |
| Database/ChatUI frontend | Never initialized | Declared application lifecycle |
| Multi-process coordination | None | Role/Redis/storage contracts |
| Accounts/billing | None | Auth and repository boundaries |

Provider durability does not imply task durability. The developer runtime never
promises durable resume; a consumer that needs it must select the full
deployment rather than inferring persistence from task IDs.

## Migrating a downstream checkout-based integration

An integration that previously built Tofu with a sibling repository context:

```yaml
services:
  tofu:
    build:
      context: ../tofu
```

can become an image dependency:

```yaml
services:
  tofu-agent:
    image: ghcr.io/rangehow/tofu-agent:0.17.0
    environment:
      TOFU_AGENT_TOKEN: ${TOFU_AGENT_TOKEN}
    volumes:
      - tofu-agent-config:/home/tofu/.config/tofu-agent

volumes:
  tofu-agent-config:
```

The operator visits `/setup` once. For an immutable/unattended deployment,
replace the volume with the three `TOFU_AGENT_PROVIDER_*` variables (and a
secret manager for the key); that makes the panel intentionally read-only.

The downstream application replaces copied polling/SSE code with `tofu-sdk` or
`@rangehow/tofu-sdk`, stores only the Tofu URL/token, and omits the model when
the sidecar manages it. No Tofu source path, Docker build context, database
volume, ChatUI port, or copied internal module remains in the downstream
repository; the small named volume above is Provider configuration only.

## Verification and release

The minimum release gates are:

```bash
uv lock --check
uvx --from build --with 'setuptools>=77.0' --with wheel \
  pyproject-build --no-isolation
python scripts/check_developer_runtime_artifacts.py dist/tofu_agent-*.whl
pytest -q tests/test_agent_provider_setup.py tests/test_public_agent_runtime.py \
  tests/test_headless_agent_server.py
pytest -q tests/test_inprocess_facade.py tests/test_sdk_sse_parser.py
npm run build --prefix clients/typescript
```

A clean-wheel smoke installs only the wheel into a new environment outside the
repository, verifies that `sqlalchemy`/`psycopg` and full Tofu application assets
are absent while `/setup` assets are present, runs `tofu-agent doctor`, and
completes one real agent turn against a mock OpenAI-compatible server.
The `uvx` command provides the PyPA builder and its declared backend without
adding release-only dependencies to the runtime environment. The console
entry point cannot be shadowed by a stale local `build/` tree, and the default
sdist-first build prevents that tree from contaminating the wheel.

The `publish-developer-runtime.yml` tag workflow builds Python distributions,
packs the TypeScript SDK, publishes the multi-platform `agent` OCI target, and
attaches the exact artifacts and CycloneDX documents to the GitHub release.
Before any registry publish, the tag workflow independently gates checked-in
secrets, release-input/deployment findings, and HIGH/CRITICAL vulnerabilities
in a rootless `agent` image candidate. PyPI uses trusted publishing; npm uses
provenance.
