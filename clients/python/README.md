# tofu-sdk

Sync and async Python client for the Tofu agent runtime and full headless API.

## Install

```bash
pip install tofu-sdk
```

Use `pip install 'tofu-sdk[cli]'` for the optional `tofu` command.

## Structured-model quick start

Configure the owner-scoped `tofu.model-routing/v2` aggregate once, then select
the official model independently from its provider:

```python
from tofu_sdk import Tofu

tofu = Tofu(
    base_url="https://tofu-agent.internal",
    api_key="sidecar-token",
)

result = tofu.agents.run(
    messages=[{"role": "user", "content": "Research this issue"}],
    model={"creator_id": "anthropic", "model_id": "claude-opus-4-7"},
    config={"thinking": "high", "tools": ["search", "fetch"]},
)
print(result["content"])
```

To prefer one eligible ProviderAccess without changing model identity:

```python
result = tofu.agents.run(
    messages=[{"role": "user", "content": "Evaluate this model"}],
    model={"creator_id": "anthropic", "model_id": "claude-opus-4-7"},
    routing={"preferred_provider_id": "provider-cluster-a"},
)
```

Native calls reject plain model strings, `model@provider`, and inline
secret-bearing provider blocks. A standalone `tofu-agent` that advertises a
configured default in `/api/v1/capabilities` may omit `model`; full ChatUI
native endpoints require the structured identity.

## Async and resumable streaming

```python
from tofu_sdk import AsyncTofu

async with AsyncTofu(
    base_url="https://tofu-agent.internal",
    api_key="sidecar-token",
) as tofu:
    async for event in tofu.agents.stream(
        messages=[{"role": "user", "content": "Inspect this project"}],
        model={"creator_id": "anthropic", "model_id": "claude-opus-4-7"},
        config={"tools": ["search", "fetch"]},
    ):
        print(event)
```

`agents.run` retries with one stable idempotency key. `agents.start` returns an
HTTP 202 task handle. `agents.stream` submits once and reconnects to
`/api/v1/tasks/{id}/stream?cursor=<last_seq+1>` after a transport drop, so it
does not duplicate model or tool work.

## Lightweight versus full server

The database-free `tofu-agent` sidecar supports:

| SDK call | Endpoint |
|---|---|
| `agents.run/start/stream` | `POST /api/v1/agent/run` + task stream |
| `tasks.get/events/stream/abort` | `/api/v1/tasks/{id}/*` |
| `capabilities()` | `GET /api/v1/capabilities` |

The SDK also retains feature-shaped methods for the full Tofu application:
chat compatibility, paper/translation/image/search agents, API keys, webhooks,
and feature polling. Capability discovery tells callers which deployment they
are connected to; unsupported full-app methods correctly return 404 from the
lightweight sidecar.

## Authentication and retries

`api_key` may be omitted for a loopback tokenless sidecar. When present, it is
sent as `Authorization: Bearer …`. The client retries safe GETs and idempotent
agent submissions on transport failures, 429, and 5xx responses with bounded
backoff. `TofuError` exposes `status`, stable `kind`, `retryable`, and
`retry_after`.

## CLI

```bash
tofu --base-url https://your-tofu --api-key tofu_live_… capabilities
tofu tasks watch <task_id>
```

CLI auth resolves from flags, then `TOFU_BASE_URL` / `TOFU_API_KEY`, then
`~/.tofu/config.toml`.
