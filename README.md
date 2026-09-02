<p align="center">
  <img src="https://raw.githubusercontent.com/rangehow/ToFu/main/static/icons/tofu-welcome.svg" width="140" height="160" alt="Tofu logo" /><br/>
  <img src="https://raw.githubusercontent.com/rangehow/ToFu/main/static/icons/tofu-brand-title.svg" width="280" height="78" alt="Tofu" /><br/>
  <sub>One production agent kernel. Embed it, serve it, or run the full workspace.</sub>
</p>

<p align="center">
  <a href="https://github.com/rangehow/ToFu/blob/main/README_CN.md">中文</a> ·
  <a href="https://github.com/rangehow/ToFu/blob/main/docs/DEVELOPER_RUNTIME.md">Developer runtime</a> ·
  <a href="https://github.com/rangehow/ToFu/blob/main/docs/README.md">Documentation</a> ·
  <a href="https://github.com/rangehow/ToFu/blob/main/CONTRIBUTING.md">Development</a>
</p>

# Tofu

Tofu is a complete agent runtime with model routing, streaming, web search and
fetch, MCP, code/project tools, document and media tooling, retries,
compaction, cancellation, and typed events. You no longer need to clone this
repository to use that runtime in another product.

Choose the boundary that matches your product:

| Use Tofu as… | Install | Your application supplies | State |
|---|---|---|---|
| An embedded Python runtime | `pip install tofu-agent` | Provider once, then messages | Process memory |
| A language-neutral sidecar | `tofu-agent serve` or the OCI image | Tofu URL/token only | Runs in memory; encrypted Provider file |
| A remote Python/TypeScript service | `tofu-sdk` / `@rangehow/tofu-sdk` | Tofu URL/token only | Chosen by the server |
| The full AI workspace | Installer or source checkout | Provider in Settings or env | SQLite/PostgreSQL + UI |

The first three options do not start the ChatUI application frontend and
require no database. They execute the same `lib.tasks_pkg` orchestrator as the
full application—not a smaller second agent loop. The sidecar adds only a tiny
static `/setup` control plane for no-code default-model configuration.

## Start Tofu Agent in 60 seconds

Install and start the runtime. A fresh installation does not require Provider
environment variables:

```bash
pip install tofu-agent
tofu-agent serve
```

Open <http://127.0.0.1:15001/setup>. Choose an OpenAI, OpenRouter, DeepSeek,
local-model, or custom template; enter the endpoint and key; discover and
select a model; then send one minimal real test and save. The Provider is
applied to new runs immediately and restored after a restart.

After that, applications send messages only:

```bash
curl http://127.0.0.1:15001/api/v1/agent/run \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Hello"}]}'
```

On a fresh installation, `tofu-agent doctor` reporting `ready: false` is the
normal configurable state, not an installation failure. Readiness changes to
HTTP 200 after the Provider is saved.

## Embed in a Python process

A Python application that does not need the HTTP boundary can pass a
`ProviderConfig` directly or inject one through its process environment:

```bash
export TOFU_AGENT_PROVIDER_BASE_URL=https://api.openai.com/v1
export TOFU_AGENT_PROVIDER_API_KEY=sk-...
export TOFU_AGENT_PROVIDER_MODEL=gpt-5.6
```

```python
from tofu_agent import AgentRuntime

with AgentRuntime.local() as agent:
    result = agent.run(
        [{"role": "user", "content": "Research this issue and propose a fix"}],
        config={"tools": ["search", "fetch"], "thinking": "high"},
    )

print(result.content)
print(result.usage)
```

The provider is configured once. Calls may omit endpoint, key, and model.
`run_async`, `stream`, `stream_async`, `start`, task replay, abort, custom tools,
and request-level provider overrides are also available. Use the sidecar and a
remote SDK when the application should contain no Provider configuration code.

For a local OpenAI-compatible engine, use its endpoint and model and leave the
key empty:

```python
from tofu_agent import AgentRuntime, ProviderConfig

provider = ProviderConfig(
    base_url="http://127.0.0.1:8000/v1",
    api_key="",
    model="Qwen3.5-32B",
)

with AgentRuntime.local(provider=provider) as agent:
    print(agent.run([{"role": "user", "content": "Hello"}]).content)
```

## Deploy the sidecar

The CLI loads `.env`, validates a redacted configuration, and serves the
database-free HTTP/SSE boundary:

```bash
tofu-agent doctor
tofu-agent serve                         # loopback-only development default
```

Remote binds are default-deny. Set a bearer token before exposing the process:

```bash
TOFU_AGENT_HOST=0.0.0.0 \
TOFU_AGENT_TOKEN='replace-with-a-secret' \
tofu-agent serve
```

Or run the release image without cloning the repository:

```bash
docker run --rm --name tofu-agent \
  -p 127.0.0.1:15001:15001 \
  -e TOFU_AGENT_TOKEN='replace-with-a-secret' \
  -v tofu-agent-config:/home/tofu/.config/tofu-agent \
  ghcr.io/rangehow/tofu-agent:0.17.0
```

Open `/setup` and enter the sidecar token to configure the model. The named
volume retains both the encrypted configuration and its key across container
replacement. The image contains the installed wheel, agent dependencies, and
small setup page only: no source checkout, ChatUI application bundle,
application data, SQLAlchemy, or database driver.

## Let the server own the model

This is the recommended product integration. The operator configures endpoint,
API key, and model once in `/setup`. Every downstream application then needs
only:

```text
TOFU_BASE_URL=https://tofu-agent.internal
TOFU_API_KEY=<sidecar bearer token>
```

Unattended deployments can still use environment variables:

```text
TOFU_AGENT_PROVIDER_BASE_URL
TOFU_AGENT_PROVIDER_API_KEY
TOFU_AGENT_PROVIDER_MODEL
```

Command-line arguments take precedence over environment variables, which take
precedence over the saved `/setup` configuration. When env or CLI owns the
Provider, the panel is explicitly read-only so it cannot pretend to save a
value that startup configuration would override. No model name is required in
application code, so an operator can replace or upgrade the model without
redeploying each consumer.

The default path is `~/.config/tofu-agent/provider.json`; override it with
`TOFU_AGENT_CONFIG_PATH`. API keys and custom-header values are Fernet-encrypted
and never returned to the page or API. By default the encryption key is the
adjacent `.provider.json.key`; `TOFU_AGENT_CONFIG_KEY` can inject it instead.
Move the configuration and key together when migrating a server, or save the
Provider again on the new host.

If a caller must bring its own endpoint, pass exactly one request-level block;
`endpoint` is an alias for `base_url`:

```json
{
  "messages": [{"role": "user", "content": "Hello"}],
  "provider": {
    "endpoint": "https://models.example/v1",
    "api_key": "sk-...",
    "model": "model-name"
  }
}
```

Provider secrets use redacting value types, are never returned in results or
capabilities, and one-shot provider slots are disposed at terminal settlement.

## Use the remote SDKs

Python, sync or async:

```bash
pip install tofu-sdk
```

```python
from tofu_sdk import AsyncTofu

async with AsyncTofu(
    base_url="https://tofu-agent.internal",
    api_key="sidecar-token",
) as tofu:
    result = await tofu.agents.run(
        messages=[{"role": "user", "content": "Summarize this repository"}],
        config={"tools": ["search", "fetch"]},
    )
    print(result["content"])
```

TypeScript/JavaScript (Node 18+, browsers, workers, Deno, and Bun):

```bash
npm install @rangehow/tofu-sdk
```

```ts
import { Tofu } from '@rangehow/tofu-sdk';

const tofu = new Tofu({
  baseUrl: 'https://tofu-agent.internal',
  apiKey: 'sidecar-token',
});

const result = await tofu.agents.run({
  messages: [{ role: 'user', content: 'Summarize this repository' }],
  config: { tools: ['search', 'fetch'] },
});
console.log(result.content);
```

Both SDKs generate a stable idempotency key for retried runs. `agents.start`
returns HTTP 202 immediately; `agents.stream` submits once and resumes the task
stream from the last absolute event sequence after a transport drop.

## Headless state contract

The lightweight runtime deliberately has one simple guarantee:

- Runs, idempotency records, and replay events live in bounded process memory.
- They can be resumed after a network disconnect while that process is alive.
- They do not survive a process restart and never silently create a database.
- The managed Provider saved by `/setup` is the only intentionally durable
  sidecar configuration. Its standalone encrypted file does not persist tasks,
  messages, or conversations.
- Durable memory, knowledge bases, cross-conversation state, and long-lived
  scheduling therefore stay on the full application; network, project-file,
  MCP, custom, media, and in-process orchestration tools remain available.
- Use the full application when durable conversations, cross-process workers,
  billing, accounts, or long-lived scheduling are required.

Inspect the exact installed contract at `GET /api/v1/capabilities`. See the
[developer runtime guide](https://github.com/rangehow/ToFu/blob/main/docs/DEVELOPER_RUNTIME.md)
and [headless API reference](https://github.com/rangehow/ToFu/blob/main/docs/HEADLESS_API.md)
for request/event details.

## Run the full workspace

The full product adds conversations, browser UI, durable storage, accounts,
papers and media libraries, scheduling, and operational controls.

| Platform | Start |
|---|---|
| Windows | Download `Tofu-Setup-*-win64.exe` from the [latest release](https://github.com/rangehow/ToFu/releases/latest). |
| Linux / macOS | `curl -fsSL https://raw.githubusercontent.com/rangehow/ToFu/main/install.sh \| bash` |
| Source / full Docker | `git clone https://github.com/rangehow/ToFu.git && cd ToFu && docker compose up -d` |

Open <http://localhost:15000> and configure a provider under **Settings →
Providers**, or use the existing `LLM_BASE_URL`, `LLM_API_KEYS`, and `LLM_MODEL`
deployment variables. SQLite is the personal default; PostgreSQL is an explicit
distributed alternative behind the same storage contract. The Kubernetes
package remains a single-replica preview; its enforced activation and scaling
boundary is documented in the
[distributed rollout runbook](docs/EPIC_D_SCALE_ROLLOUT_RUNBOOK.md#preview-safety-boundary).

## Browser Extension

The full workspace supports the same unpacked extension in Chrome and Edge.
Open **Settings → Local Control** for the canonical install or upgrade action;
it detects which supported browser is available and opens that browser's own
extensions page when Tofu and the browser run on the same machine.

For a manual local install, enable Developer mode on `chrome://extensions` in
Chrome or `edge://extensions` in Edge, then load the repository's
`browser_extension/` directory as an unpacked extension. If the browser runs on
a different machine from Tofu, use **Settings → Local Control → Download
extension ZIP** instead; a remote browser cannot read the server's filesystem.

## Develop and release

Requirements are Python 3.12 and Node 20.19.x:

```bash
uv sync --frozen --extra dev
uv run make docs-check
npm run check:frontend
uv run make test-unit
```

`vX.Y.Z` tags build and verify `tofu-agent`, `tofu-sdk`,
`@rangehow/tofu-sdk`, and the multi-platform `tofu-agent` OCI target before
publishing them. Package versions are kept equal to
[`VERSION`](https://github.com/rangehow/ToFu/blob/main/VERSION).

The [documentation map](https://github.com/rangehow/ToFu/blob/main/docs/README.md)
identifies the current contract and owner for every subsystem. Repository
development rules live in
[AGENTS.md](https://github.com/rangehow/ToFu/blob/main/AGENTS.md).

## Project Structure

This is the maintained first-hop map for the distributable Agent runtime and
its integration boundaries. The full subsystem catalogue lives in
`docs/README.md`.

```text
├── tofu_agent/
│   ├── __init__.py
│   ├── cli.py
│   ├── models.py
│   ├── runtime.py
│   ├── server.py
│   ├── provider_setup.py
│   ├── provider_store.py
│   └── setup_ui/
│       ├── index.html
│       ├── setup.css
│       └── setup.js
├── clients/
│   ├── python/
│   │   └── tofu_sdk/
│   └── typescript/
│       └── src/
├── lib/
│   ├── llm/
│   ├── llm_dispatch/
│   ├── tasks_pkg/
│   │   ├── compaction/
│   │   ├── handlers/
│   │   └── orchestrator/
│   ├── byo_egress.py
│   └── provider_probe.py
├── docs/
│   ├── README.md
│   ├── DEVELOPER_RUNTIME.md
│   ├── HEADLESS_API.md
│   └── API_CONTRACT.md
├── tests/
│   ├── test_agent_provider_setup.py
│   ├── test_headless_agent_server.py
│   └── test_public_agent_runtime.py
├── scripts/
│   └── check_developer_runtime_artifacts.py
├── frontend/
├── routes/
├── server.py
├── serverctl.py
├── Dockerfile
├── pyproject.toml
└── MANIFEST.in
```

## Security

- Tokenless headless mode is loopback-only; a non-loopback CLI bind is refused.
- `/setup` assets may load publicly, but the APIs that read, test, or mutate the
  Provider use the same bearer token as Agent APIs; cross-site setup requests
  are rejected.
- The full application centralizes authentication and authorization at its
  middleware boundary and passes explicit owner identity into repositories.
- Provider endpoints are validated against the outbound egress policy.
- Do not put API keys in command-line arguments or committed environment files;
  treat the encrypted Provider file and its key as one secret when backing up.

See [API contract](https://github.com/rangehow/ToFu/blob/main/docs/API_CONTRACT.md),
[identity](https://github.com/rangehow/ToFu/blob/main/docs/IDENTITY.md), and
[reliability](https://github.com/rangehow/ToFu/blob/main/docs/RELIABILITY_RUNBOOK.md).

## License

MIT. See [LICENSE](https://github.com/rangehow/ToFu/blob/main/LICENSE).
