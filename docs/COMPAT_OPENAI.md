# OpenAI Compatibility Adapter

Use any OpenAI SDK or downstream tool with Tofu by changing two lines:

```python
from openai import OpenAI
client = OpenAI(
    api_key="tofu_live_…",                 # ← Tofu API key
    base_url="https://your-tofu/v1",        # ← Tofu server
)
```

## Endpoints

| Path                       | Status      | Notes                              |
|----------------------------|-------------|------------------------------------|
| `POST /v1/chat/completions`| **Stable**  | Sync + streaming. Tools supported. |
| `GET  /v1/models`          | **Stable**  | Lists every model in the dispatcher.|
| `POST /v1/embeddings`      | **Stable**  | Proxies to dispatcher's embedding model. |

## Mapping rules

### Request

| OpenAI field         | Tofu mapping                                   |
|----------------------|------------------------------------------------|
| `model`              | Used directly; resolved via dispatcher.        |
| `messages`           | Passed through; vision blocks supported.       |
| `tools`              | Passed through. Disables Tofu's auto tools.    |
| `tool_choice`        | Passed through.                                |
| `temperature`        | `cfg.temperature`                              |
| `max_tokens`         | `cfg.maxTokens`                                |
| `top_p`              | `cfg.topP`                                     |
| `stop`               | `cfg.stop`                                     |
| `seed`               | `cfg.seed`                                     |
| `response_format`    | `cfg.responseFormat` → forwarded verbatim to the upstream engine (JSON mode / `json_schema`). Enforcement depends on the provider. |
| `stream`             | Streaming on/off                               |
| `user`               | `cfg.user` (audit only)                        |
| `reasoning_effort`   | Maps `low/medium/high` → `medium/high/max` for `cfg.thinkingDepth` and turns on `cfg.thinkingEnabled`. The legacy Tofu label `ultra` is normalized to GPT-5.6's official top value, `max`. |

### Response

* Standard `chat.completion` shape.
* Bonus field: `task_id` — pass to `/api/v1/tasks/…` for replay/abort.
* Streaming chunks have `tofu` envelope on non-text events (phase,
  tool_call, snapshots) — vanilla SDKs ignore unknown fields safely.

## Auth

* `Authorization: Bearer tofu_live_…` (preferred)

OpenAI SDKs put the key in `Authorization` automatically.

## Tested clients

* `openai` Python SDK (sync + async + streaming + tools)
* `langchain-openai`
* OpenWebUI
* Continue.dev / Cline (set provider to "OpenAI-compatible")
* Aider (`--openai-api-base /v1`)
* `litellm` proxy

## Caveats

* Function-call streaming is collated by the orchestrator; OpenAI's
  per-token tool argument deltas are not surfaced as separate frames.
  The final tool_calls array is correct.
* `n` > 1 is not supported — `n=1` only.
* Logprobs are not supported.
* `parallel_tool_calls=false` is honored when Tofu's underlying
  provider supports it (Anthropic always sequential).
