# Anthropic Compatibility Adapter

Use the Anthropic SDK or any tool that speaks the Messages API:

```python
from anthropic import Anthropic
client = Anthropic(
    api_key="tofu_live_…",
    base_url="https://your-tofu",   # NOT "/v1" — SDK appends it
)

msg = client.messages.create(
    model="claude-opus-4-7",
    max_tokens=8192,
    messages=[{"role":"user","content":"Hi"}],
)
```

## Endpoints

| Path                            | Status     | Notes                  |
|---------------------------------|------------|------------------------|
| `POST /v1/messages`             | **Stable** | Sync + streaming.      |
| `POST /v1/messages/count_tokens`| **Stable** | Estimate token count.  |

## Mapping rules

### Request

| Anthropic field           | Tofu mapping                                   |
|---------------------------|------------------------------------------------|
| `model`                   | Used directly.                                 |
| `system`                  | Translated to a leading `role:'system'` message.|
| `messages`                | Pass-through; tool_use / tool_result blocks supported. |
| `tools`                   | Pass-through. Disables Tofu's auto tools.      |
| `tool_choice`             | Pass-through.                                  |
| `max_tokens`              | `cfg.maxTokens` (required by Anthropic).       |
| `temperature`             | `cfg.temperature`                              |
| `stop_sequences`          | `cfg.stop`                                     |
| `metadata.user_id`        | `cfg.user`                                     |
| `thinking.type=enabled`   | `cfg.thinkingEnabled=true`                     |
| `thinking.budget_tokens`  | Mapped to `cfg.thinkingDepth` band:           |
|                           | ≤ 8192 → medium                                |
|                           | ≤ 16384 → high                                 |
|                           | ≤ 32768 → xhigh                                |
|                           | > 32768 → max                                  |

### Streaming

Standard Anthropic named events:

```
event: message_start
data: {"type":"message_start","message":{…}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"…"}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{…}}

event: message_stop
data: {"type":"message_stop"}
```

Thinking deltas surface as `delta.type:"thinking_delta"` blocks.

## Auth

* `Authorization: Bearer tofu_live_…` (RFC 6750)
* `x-api-key: tofu_live_…` (Anthropic SDK convention)

Both work. The SDK uses `x-api-key` automatically.

## Tested clients

* `anthropic` Python SDK (sync + async + streaming + thinking + tools)
* `@anthropic-ai/sdk` (TypeScript)
* Cline / Roo Code (Anthropic provider)
* Continue.dev (Claude provider)

## Caveats

* `model` must resolve via the Tofu dispatcher. Set up a provider whose
  `models` array includes the id you want to use.
* Tofu's response includes a non-standard `task_id` field — Anthropic
  SDKs ignore unknown fields, so it's safe.
* The `count_tokens` endpoint uses Tofu's token counter (tiktoken /
  Anthropic API / heuristic), which may diverge slightly from the
  upstream Anthropic counter. Treat as an estimate.
