<p align="center">
  <img src="static/icons/tofu-welcome.svg" width="140" height="160" alt="Tofu logo" /><br/>
  <img src="static/icons/tofu-brand-title.svg" width="280" height="78" alt="Tofu" /><br/>
  <sub>豆腐 — Self-Hosted AI Assistant</sub>
</p>

<p align="center">
  <a href="https://github.com/rangehow/ToFu/actions/workflows/ci.yml"><img src="https://github.com/rangehow/ToFu/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
</p>

<p align="center">
  Multi-model chat · Autonomous agents · Project co-pilot · Multi-agent swarm<br/>
  Daily reports & to-do · Browser extension · Desktop agent · Feishu bot
</p>

<p align="center">
  <a href="README_CN.md">🇨🇳 中文文档</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.10+-3776ab?logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/PostgreSQL-18+-336791?logo=postgresql&logoColor=white" alt="PostgreSQL" />
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License" />
  <img src="https://img.shields.io/badge/platform-Linux%20·%20macOS%20·%20Windows-lightgrey" alt="Platform" />
</p>

---

## What is Tofu?

Tofu is a fully self-hosted AI assistant built with a **Flask backend** and **vanilla JS frontend**. It connects to any OpenAI-compatible LLM API and gives you autonomous tool-calling agents, a project co-pilot for any codebase, multi-agent swarm orchestration, a browser extension, and a desktop agent — all from a single `python server.py`.

---

## Features

### Multi-Model Chat

- **20+ LLM models** — OpenAI, Anthropic, Google Gemini, Qwen, DeepSeek, MiniMax, Doubao, GLM, Mistral, Grok, and any OpenAI-compatible API
- **Smart dispatch** — multi-key, multi-provider routing with real-time latency scoring, error-rate tracking, and per-key rate-limit cooldowns
- **Streaming responses** with per-model cost tracking (input/output/cache tokens × tiered pricing)
- **Multi-model comparison** — send the same prompt to several models side-by-side
- **Auto-translation** — bidirectional Chinese ↔ English translation per conversation

### Tool Calling & Agent Mode

- **Built-in tools** — web search (multi-engine: DDG, Brave, Bing, SearXNG in parallel), URL fetching, PDF parsing (text + VLM), image upload & generation, shell commands, Python execution
- **Project co-pilot** — point it at any codebase for file browsing, grep search, code editing, git operations, and AI-powered file indexing
- **Autonomous task execution** — multi-step tool chains with automatic retry, 3-layer context compaction, and configurable model fallback chains
- **Endpoint mode** — Planner → Worker → Critic review loop for long-running tasks with iterative refinement
- **Scheduled tasks** — cron-like recurring or one-shot tasks (shell, Python, LLM prompts)
- **Image generation** — multi-model dispatch across Gemini and GPT image models with automatic 429-retry cycling

### Multi-Agent Swarm

- **Swarm orchestration** — MasterOrchestrator plans and delegates to multiple SubAgents running in parallel
- **Streaming scheduler** — reactive task scheduling with no wave barriers, artifact sharing between agents
- **Review & synthesis** — automatic review of agent outputs and final synthesis into a coherent result

### Browser Extension

- Chrome extension bridging the assistant with your browser for real-time page reading and interaction
- Per-client command routing — multiple browsers connect simultaneously with independent command queues
- Navigate, screenshot, click, type, extract content

### Desktop Agent

- Runs on your local machine, connects back to the server
- File system operations, clipboard, screenshots, GUI automation (pyautogui), system info
- Security-gated with `--allow-write` / `--allow-exec` flags

### Feishu (Lark) Bot

- Full Feishu bot integration via WebSocket — multi-turn LLM conversations with tool support directly in team chat
- Slash commands, model/mode switching, conversation management

### Daily Reports & To-Do

Click the ☑️ **My Day** button in the sidebar header to open the daily dashboard — a personal work journal powered by LLM analysis.

- **Auto-generated work streams** — the LLM reads all your conversations for the day, clusters them into 5–15 coherent work streams (e.g. "修复图片回显", "项目部署调试"), and marks each as *done*, *in progress*, or *blocked*
- **Calendar view** — month-at-a-glance calendar with per-day conversation counts and cost heatmap; click any date to view or generate its report
- **Tomorrow's plan** — the LLM synthesizes 3–8 actionable TODO items from unfinished work, each with a detailed prompt and recommended tool configuration
- **One-click launch** — click the ▶ button on any TODO to instantly open a new conversation pre-filled with the task prompt and the right tools enabled (search, code, browser, project, etc.)
- **To-do carry-forward** — uncompleted TODOs automatically carry over to the next day as "今日待办" (Today's Tasks); the LLM tracks which ones you addressed and marks them done
- **Manual TODOs** — add your own to-do items via the ＋ input at the bottom; toggle done/undone, delete, or launch as conversations
- **Cost tracking** — per-day and per-conversation cost breakdown in CNY, calculated from token usage and model pricing
- **Auto-backfill** — a background scheduler automatically generates yesterday's report on server boot and daily at midnight if it's missing
- **Motivational quotes** — a random quote appears at the top of each report ("人生苦短，我用 AI" 🧈)

### Scheduled Tasks

- **Proactive agent scheduler** — create cron-like recurring or one-shot tasks (Shell, Python, LLM prompts) via conversation or the scheduler panel
- **SCHEDULER badge** — appears in the top status bar; click to see all active proactive agents and their recent run logs
- Enable via the 🕐 **Scheduler** toggle in the tool submenu

### More

- **Skills system** — persistent reusable knowledge (Markdown files) — the assistant learns project conventions, bug patterns, and workflows across sessions
- **3-layer context compaction** — micro-compact → structural truncation → LLM summary for very long conversations
- **IndexedDB caching** — read-through conversation cache with LRU eviction for fast page loads
- **Error tracking** — universal project error tracker with fingerprinting, resolution tracking, and digest reports
- **Dark theme UI** with responsive layout, syntax highlighting, LaTeX rendering, and image previews
- **Cross-platform** — runs on Linux, macOS, and Windows (see [Platform Support](#platform-support) below)
- **Auto-dependency repair** — `bootstrap.py` auto-installs missing pip packages via LLM diagnosis

---

## Quick Start

### Prerequisites

- Python 3.10+
- conda (for PostgreSQL server installation)

### 1. Clone & Install

```bash
git clone https://github.com/rangehow/ToFu.git
cd ToFu

# Update conda to the latest version
conda update -n base -c defaults conda

# Create a new environment named 'tofu' with Python 3.12
conda create -n tofu python=3.12 -y

# Activate the environment
conda activate tofu

# Install PostgreSQL 18+ server (userspace, no root required)
conda install -c conda-forge postgresql>=18

# Install Python dependencies
pip install -r requirements.txt

# Install browser automation engine (required for advanced fetching)
playwright install chromium
```

> PostgreSQL runs as a local userspace process — no `sudo`, no system service.
> On first `python server.py`, the database auto-bootstraps (`initdb`, schema creation, port selection).

### 2. Run

```bash
python server.py
```

Open **http://localhost:15000** in your browser — that's it! Configure everything from the Settings UI.

#### Automatic Dependency Repair

If any Python package is missing, `server.py` automatically delegates to `bootstrap.py`:

1. Detects the `ImportError` and hands off to `bootstrap.py`
2. Opens a live status page in your browser on the same port
3. The LLM API diagnoses the traceback and determines which packages to install
4. Packages are `pip install`ed automatically, with up to 10 retry rounds
5. Once everything resolves, the real server starts

This uses only Python stdlib — works even when *every* pip package is missing.

---

## Configuration

**All configuration is done through the Settings UI** — click the ⚙️ gear icon in the top-right corner of the chat interface. Changes are saved to the server instantly, no restart needed (unless noted).

The Settings panel has **7 tabs**, each with a dedicated icon in the left sidebar:

### ⚙️ General

Core model parameters and global preferences.

- **Theme** — Dark, Light, or Tofu (豆腐) theme
- **Temperature** — controls response randomness (0 = deterministic, 1 = creative)
- **Max tokens** — maximum output token limit
- **Image max width** — auto-compress uploaded images (0 = no compression)
- **PDF max pages** — limit page count when parsing PDFs
- **Thinking depth** — default thinking budget for new conversations (Off / Medium / High / Max)
- **System prompt** — custom instructions prepended to every conversation

### 🔗 Providers

Multi-provider API management — this is where you add your LLM API keys.

- **⚡ Add from template** — one-click setup for OpenAI, Anthropic, Google Gemini, DeepSeek, Qwen, MiniMax, GLM, Doubao, Mistral, Grok, OpenRouter, Azure, Ollama, and more
- **Custom provider** — add any OpenAI-compatible endpoint with custom base URL
- **Per-provider settings** — each provider has its own API key(s), base URL, and model list
- **Auto-discover models** — fetches available models from the provider's `/v1/models` endpoint
- **Multi-key rotation** — add multiple API keys per provider for automatic rate-limit rotation

### 📦 Display

Control what appears in the model selector and image generation picker.

- **Image generation models** — show/hide specific models in the image gen selector
- **Model dropdown** — show/hide models in the main chat model switcher
- **Fallback model** — auto-switch to this model when the primary model fails
- **Default model** — override the default model for new conversations

### 🔍 Search & Fetch

Web search and content fetching behavior.

- **LLM content filter** — use the model to strip navigation/ads from fetched pages (disable for speed)
- **Fetch top N** — how many search results to auto-fetch (default: 6)
- **Fetch timeout** — per-page timeout in seconds (default: 15)
- **Max characters** — separate limits for search results, direct URL fetch, and PDF files
- **Max download size** — byte limit for fetched content (default: 20 MB)
- **Blocked domains** — domains the fetcher will never visit (one per line)

### 🌐 Network

Proxy configuration for all outbound requests.

- **HTTP / HTTPS proxy** — proxy URL for LLM API calls, search, and page fetching
- **Bypass domains** — domain suffixes that skip the proxy entirely (one per line, suffix-matched)
- 💡 **Tip**: Add your LLM API domains here if your corporate/VPN proxy silently drops SSE long-connections, causing `BrokenPipeError`

### 🐦 Feishu (Lark)

Feishu bot integration settings.

- **Connection status** — live indicator showing bot connection state
- **App ID / App Secret** — credentials from [open.feishu.cn](https://open.feishu.cn/app) (restart required after change)
- **Default project path** — project co-pilot root for Feishu conversations
- **Workspace root** — base directory for project switching
- **Allowed users** — restrict bot access to specific Feishu user IDs (blank = allow all)

### `</>` Advanced

Pricing and cache management.

- **Price overrides** — customize per-model pricing (USD per million tokens) as JSON
- **Local cache** — view IndexedDB cache stats and clear cached conversations
- **Server info** — server status and version information

---

### Environment Variables (fallback)

For first-time setup, headless servers, or Docker deployments, you can also configure via environment variables. **The Settings UI always takes priority** — env vars are only used as initial fallback values.

```bash
cp .env.example .env
```

| Variable | Description | Example |
|---|---|---|
| `LLM_API_KEY` | LLM provider API key (fallback) | `sk-abc123...` |
| `LLM_BASE_URL` | Chat completions endpoint (fallback) | `https://api.openai.com/v1` |
| `LLM_MODEL` | Default model (fallback) | `gpt-4o` |
| `PORT` | Server port | `15000` |
| `BIND_HOST` | Bind address | `0.0.0.0` |
| `PROXY_BYPASS_DOMAINS` | Comma-separated proxy bypass domains | `.corp.net,.internal.com` |
| `FEISHU_APP_ID` | Feishu bot app ID | `cli_xxxx` |
| `FEISHU_APP_SECRET` | Feishu bot app secret | |

> 💡 **After first launch, we recommend configuring everything through the Settings UI.**
> It's more intuitive, changes take effect immediately, and supports features like
> provider templates and model auto-discovery that env vars cannot.

---

## Project Structure

```
├── server.py                  Flask app entry, middleware, logging
├── bootstrap.py               Auto-dependency repair (LLM-guided)
├── index.html                 Main chat UI (single-page app)
├── .env.example               Environment variable template
│
├── lib/                       Core libraries
│   ├── llm_client.py          LLM API client (streaming, retry)
│   ├── llm_dispatch/          Multi-key multi-model dispatcher
│   ├── database.py            PostgreSQL (auto-bootstrap)
│   ├── tasks_pkg/             Task orchestration & compaction
│   │   ├── orchestrator.py    Main LLM ↔ tool loop
│   │   ├── executor.py        Tool execution engine
│   │   ├── endpoint.py        Planner → Worker → Critic loop
│   │   └── compaction.py      3-layer context compaction
│   ├── tools/                 Tool definitions & schemas
│   ├── swarm/                 Multi-agent orchestration
│   ├── fetch/                 Content fetching & extraction
│   ├── search/                Multi-engine web search
│   ├── browser/               Browser extension bridge
│   ├── project_mod/           Project co-pilot (scan, edit, undo)
│   ├── skills/                Skill accumulation system
│   ├── feishu/                Feishu bot integration
│   └── ...
│
├── routes/                    Flask Blueprints (21 modules)
├── static/                    CSS, JS, icons
├── browser_extension/         Chrome extension (MV3)
├── tests/                     Test suite (unit, API, E2E)
└── data/                      Runtime data (git-ignored)
```

---

## Advanced Usage

### Project Co-Pilot

Click **Project** in the sidebar, enter the path to any codebase. The assistant can browse files, search code, edit files, run commands, and track modifications with per-round undo.

### Multi-Agent Swarm

For complex tasks, the assistant automatically plans sub-tasks and delegates to specialist agents running in parallel. Results are reviewed and synthesized into a coherent output.

### Browser Extension

1. `chrome://extensions` → Enable Developer Mode
2. Load unpacked → select `browser_extension/`
3. Click the extension icon → enter your server URL
4. The assistant can now read and interact with your browser tabs

### Desktop Agent

```bash
pip install pyautogui pillow psutil
python lib/desktop_agent.py --server http://your-server:15000 --allow-write --allow-exec
```

### Feishu Bot

1. Create an app at [open.feishu.cn](https://open.feishu.cn/app), enable Bot capability
2. Open Settings → 🐦 Feishu tab → enter **App ID** and **App Secret**
3. The bot auto-connects on server restart

### Scheduled Tasks

Ask the assistant to "create a scheduled task" or "set up a daily cron job" — it will create a proactive agent that runs on your specified schedule. Manage all tasks from the SCHEDULER badge in the status bar.

### Healthcheck

```bash
python healthcheck.py
```

---

## Platform Support

Tofu runs on **Linux**, **macOS**, and **Windows**. All platform-specific code is isolated in `lib/compat.py`.

| Feature | Linux | macOS | Windows |
|---|:---:|:---:|:---:|
| Core chat & tools | ✅ | ✅ | ✅ |
| PostgreSQL auto-bootstrap | ✅ | ✅ | ✅ (PG bin/ must be in PATH) |
| Project co-pilot (file tools) | ✅ | ✅ | ✅ |
| `run_command` (basic) | ✅ | ✅ | ✅ (uses `cmd.exe`) |
| `run_command` interactive stdin | ✅ (via `/proc`) | ❌ (non-interactive) | ❌ (non-interactive) |
| FUSE keepalive daemon | ✅ (DolphinFS) | — (not needed) | — (not needed) |
| Desktop agent | ✅ | ✅ | ✅ |
| Browser extension | ✅ | ✅ | ✅ |
| Dangerous command blocking | ✅ (Unix + Windows patterns) | ✅ | ✅ |

**Smoke test**: `python debug/test_cross_platform.py` validates the compat layer on any platform.

---

## Testing

```bash
# All tests
python tests/run_all.py

# Individual suites
python -m pytest tests/test_backend_unit.py
python -m pytest tests/test_api_integration.py
python -m pytest tests/test_visual_e2e.py
python -m pytest tests/test_db_bug_regressions.py
```

---

## Security

- **No secrets in source** — all credentials loaded from environment variables or Settings UI
- **Single-user mode** — no multi-tenant auth; deploy behind a VPN or reverse proxy
- **Tool execution** — the assistant can run shell commands and edit files; use with caution
- **Desktop agent** — requires explicit `--allow-write` / `--allow-exec` flags

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full guide. Quick version:

1. Fork → feature branch
2. `python healthcheck.py && python tests/run_all.py`
3. Submit a pull request

---

## License

MIT
