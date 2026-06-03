# tofu-sdk

Python client for the Tofu headless API.

## Install (development)

```
cd clients/python
pip install -e .[cli]
```

## Quick start

```python
from tofu_sdk import Tofu

client = Tofu(base_url="https://your-tofu", api_key="tofu_live_…")

# Sync
resp = client.chat(model="claude-opus-4-7",
                    messages=[{"role": "user", "content": "Hi"}])
print(resp["choices"][0]["message"]["content"])

# Streaming
for ev in client.stream(model="claude-opus-4-7",
                         messages=[{"role": "user", "content": "Hi"}]):
    delta = (ev.get("choices") or [{}])[0].get("delta", {})
    if delta.get("content"):
        print(delta["content"], end="", flush=True)

# Self-describe
caps = client.capabilities()
print("Models:", [m["id"] for m in caps["models"]])

# Memory search
hits = client.agents.memory_search(query="rate limit pattern")
print(hits["results"])
```

## CLI

```
tofu --base-url https://your-tofu --api-key tofu_live_… capabilities
tofu chat "Hello" --model claude-opus-4-7 --stream
tofu keys list
tofu tasks watch <task_id>
```

Auth resolves from CLI flags → `TOFU_BASE_URL` / `TOFU_API_KEY` env →
`~/.tofu/config.toml` (`[default]` section, keys `base_url` and `api_key`).

## API surface mapped 1:1

| SDK call                        | Endpoint                                |
|---------------------------------|-----------------------------------------|
| `client.chat(...)`              | `POST /api/v1/chat/completions`         |
| `client.stream(...)`            | `POST /api/v1/chat/completions` (SSE)   |
| `client.capabilities()`         | `GET  /api/v1/capabilities`             |
| `client.tasks.start(kind, …)`   | Routed to the agent endpoint per kind   |
| `client.tasks.get(id)`          | `GET  /api/v1/tasks/{id}`               |
| `client.tasks.events(id)`       | `GET  /api/v1/tasks/{id}/events`        |
| `client.tasks.stream(id)`       | `GET  /api/v1/tasks/{id}/stream`        |
| `client.tasks.abort(id)`        | `POST /api/v1/tasks/{id}/abort`         |
| `client.agents.paper_report()`  | `POST /api/v1/agents/paper/report`      |
| `client.agents.translate(…)`    | `POST /api/v1/agents/translate`         |
| `client.agents.image_gen(…)`    | `POST /api/v1/agents/image-gen`         |
| `client.agents.memory_search()` | `POST /api/v1/agents/memory/search`     |
| `client.agents.fetch(url)`      | `POST /api/v1/agents/browser/fetch`     |
| `client.keys.list/create/revoke`| `/api/v1/keys/*`                         |
| `client.webhooks.subscribe(…)`  | `POST /api/v1/webhooks`                 |
