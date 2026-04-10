<p align="center">
  <img src="static/icons/tofu-welcome.svg" width="140" height="160" alt="Tofu logo" /><br/>
  <img src="static/icons/tofu-brand-title.svg" width="280" height="78" alt="Tofu" /><br/>
  <sub>豆腐 — Self-Hosted AI Assistant</sub>
</p>

<p align="center">
  <a href="https://github.com/rangehow/ToFu/actions/workflows/ci.yml"><img src="https://github.com/rangehow/ToFu/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.10+-3776ab?logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/PostgreSQL-18+-336791?logo=postgresql&logoColor=white" alt="PostgreSQL" />
  <img src="https://img.shields.io/badge/license-MIT-green" alt="License" />
  <img src="https://img.shields.io/badge/platform-Linux%20·%20macOS%20·%20Windows-lightgrey" alt="Platform" />
</p>

<p align="center">
  <a href="README_CN.md">🇨🇳 中文文档</a>
</p>

<p align="center">
  <img src="propaganda/mainpage.jpg" width="800" alt="Main Interface" />
</p>

---

## What is Tofu?

Tofu is a **fully self-hosted AI assistant** you run with a single command. It connects to any OpenAI-compatible LLM and gives you a complete AI workspace — from simple Q&A to autonomous multi-step agents that can search the web, edit your codebase, control your browser, and collaborate as a team of specialist agents.

Everything runs on your machine. Your data never leaves your infrastructure. One `python server.py` and you're live.

---

## Quick Start

### One-Command Install (recommended)

**Linux / macOS:**
```bash
curl -fsSL https://raw.githubusercontent.com/rangehow/ToFu/main/install.sh | bash
```

**Windows (PowerShell):**
```powershell
irm https://raw.githubusercontent.com/rangehow/ToFu/main/install.ps1 | iex
```

**Or with Python directly** (any OS with Python 3.10+):
```bash
git clone https://github.com/rangehow/ToFu.git && cd ToFu
python install.py
```

This creates a virtual environment, installs dependencies, bootstraps PostgreSQL, and starts the server. Open **http://localhost:15000** when it's ready.

```bash
# Pre-configure API key and port
python install.py --api-key sk-xxx --port 8080

# Install only, don't launch
python install.py --no-launch

# Use Docker instead
python install.py --docker
```

### Docker (zero dependencies)

```bash
git clone https://github.com/rangehow/ToFu.git && cd ToFu
docker compose up -d
```

Open **http://localhost:15000** — done. All data persists in Docker volumes.

<details>
<summary><strong>Manual Install</strong> (for full control)</summary>

**Prerequisites:** Python 3.10+, PostgreSQL 18+, ripgrep & fd-find (recommended)

```bash
git clone https://github.com/rangehow/ToFu.git && cd ToFu

# Create environment
python -m venv .venv && source .venv/bin/activate

# Install PostgreSQL (if not already)
# macOS:   brew install postgresql@18
# Ubuntu:  sudo apt install postgresql
# conda:   conda install -c conda-forge postgresql>=18

# Install ripgrep & fd-find (recommended — faster code search)
# macOS:   brew install ripgrep fd
# Ubuntu:  sudo apt install ripgrep fd-find

# Install dependencies
pip install -r requirements.txt

# Optional: browser automation
pip install playwright && playwright install chromium

# Run
python server.py
```

</details>

> **PostgreSQL** runs as a local userspace process — no `sudo`, no system service. On first launch, the database auto-bootstraps (initdb, schema creation, port selection).

> **Missing packages?** If any dependency is missing, `server.py` auto-delegates to `bootstrap.py`, which uses the LLM to diagnose the error and `pip install` the right packages — even when *every* pip package is missing.

---

## Connect Your LLM

<p align="center">
  <img src="propaganda/providersetting.jpg" width="700" alt="Provider Settings" />
</p>

Click **⚙️ Settings → 🔗 Providers** and add your API keys. Tofu works with any OpenAI-compatible API:

| Provider | Setup |
|---|---|
| OpenAI, Anthropic, Google Gemini, DeepSeek, Qwen, MiniMax, GLM, Doubao, Mistral, Grok, Baidu Qianfan, OpenRouter | Click **⚡ Add from template** — one click |
| Ollama, vLLM, or any local model server | Add as custom provider with your local endpoint |
| Azure OpenAI | Template available with deployment-specific base URL |

**Multiple keys per provider** — add several API keys and Tofu automatically rotates between them when one hits rate limits. Across providers, the smart dispatcher routes requests based on real-time latency scoring and error-rate tracking.

Or set environment variables for headless/Docker setups:
```bash
export LLM_API_KEY=sk-xxx
export LLM_BASE_URL=https://api.openai.com/v1
export LLM_MODEL=gpt-4o
```

---

## Features

### 💬 Chat with Any Model

<p align="center">
  <img src="propaganda/chatinner.jpg" width="700" alt="Chat Interface" />
</p>

The core experience: pick a model from the dropdown, type a message, get a streaming response. But Tofu goes much further than a basic chat UI.

**When you want to try different models on the same question** — switch models mid-conversation. Each message remembers which model generated it, so you can compare outputs naturally. Branch any assistant message to explore alternative responses from different models or with different parameters, all in the same thread.

**When you're working in Chinese but need English sources** — enable auto-translation per conversation. Your Chinese questions are translated to English for the model, and the English response is translated back. The original is always preserved — click to toggle.

**When conversations get long and you lose context** — Tofu's 3-layer compaction pipeline handles this automatically:
1. **Micro-compaction** (zero cost): old tool results are replaced with summaries, keeping only the recent "hot tail"
2. **Structural truncation**: thinking blocks, oversized arguments, and redundant screenshots are trimmed
3. **LLM summary** (force-triggered): when context pressure is high, a cheap model evaluates each turn for relevance and compresses accordingly

**When you want to organize your conversations** — create folders in the sidebar to group related threads. Drag conversations between folders, or leave them unfiled.

---

### 🔍 Web Search & Content Fetching

When the assistant needs current information — today's news, documentation updates, API references — it can search the web and read pages.

**How it works:** Enable the 🔍 toggle in the tool bar. The assistant searches across multiple engines in parallel (DuckDuckGo, Brave, Bing, SearXNG), deduplicates results, then fetches and extracts the most relevant pages. A content filter (LLM-powered, optional) strips navigation, ads, and boilerplate.

**When you paste a URL** — the assistant fetches it directly, handling HTML, PDFs, and plain text. For pages behind authentication, use the browser extension instead (see below).

**Configuration** — in **Settings → 🔍 Search & Fetch**:
- How many results to auto-fetch (default: 6)
- Per-page timeout and max characters
- Blocked domains the fetcher should never visit
- Whether to use the LLM content filter (disable for speed)

---

### 🛠️ Tool Calling & Autonomous Agents

This is where Tofu becomes more than a chatbot. When you enable tools, the assistant can take multi-step actions autonomously — searching the web, running code, editing files, generating images — chaining these together to solve complex tasks.

**Built-in tools:**
| Tool | What it does |
|---|---|
| `web_search` | Search the web (multi-engine parallel) |
| `fetch_url` | Read any URL (HTML, PDF, plain text) |
| `run_command` | Execute shell commands |
| `generate_image` | Create or edit images (Gemini, GPT-image) |
| `ask_human` | Pause and ask you a question mid-task |
| `list_conversations` / `get_conversation` | Reference past conversations |
| `create_memory` / `update_memory` / `delete_memory` | Save knowledge for future sessions |
| `check_error_logs` / `resolve_error` | Inspect and resolve errors in project logs |
| Browser tools | Control your browser (via extension) |
| Desktop tools | Control your local machine (via agent) |
| Project tools | Browse, search, edit any codebase |
| Scheduler tools | Create recurring automated tasks |
| Swarm tools | Spawn parallel sub-agents |

**When you need a quick answer with live data** — "What's the current price of NVDA?" The assistant searches, fetches the relevant page, and answers.

**When you need a multi-step workflow** — "Research the top 5 React state management libraries, compare them, and write a recommendation document." The assistant plans the steps, executes searches, reads documentation, and synthesizes the result — all autonomously.

**When the task is too complex for one pass** — enable **Endpoint mode** (Planner → Worker → Critic). A planner rewrites your request into a structured brief with acceptance criteria, a worker executes it, and a critic reviews against the checklist. If the result doesn't pass, the critic sends feedback and the worker iterates — up to 10 rounds.

**When something fails** — the assistant retries with exponential backoff. If the primary model fails entirely, it automatically falls back to a configured backup model and retries.

---

### 💻 Project Co-Pilot

Point Tofu at any codebase and it becomes a coding assistant that can read, search, edit, and run commands in your project.

**Getting started:** Click **Project** in the sidebar, enter the path to your codebase (e.g. `/home/you/myproject`). The assistant gains these tools:

| Tool | What it does |
|---|---|
| `list_dir` | Browse directory structure with file sizes and line counts |
| `read_files` | Read files (supports images, PDFs, Office docs, code — with line numbers) |
| `grep_search` | Search across files with ripgrep (regex, context lines, count mode) |
| `find_files` | Find files by glob pattern |
| `write_file` | Create or overwrite files |
| `apply_diff` | Surgical search-and-replace edits (batch mode for multiple edits) |
| `insert_content` | Add code before/after an anchor without replacing it |
| `run_command` | Execute shell commands in the project directory |

**When you need to understand a new codebase** — "Give me an overview of this project's architecture." The assistant explores the directory tree, reads key files, and maps out the structure.

**When you need to fix a bug** — "The login page shows a blank screen after submitting." The assistant greps for relevant code, reads the components, identifies the issue, and applies a fix with `apply_diff`.

**When you want safe experimentation** — every file modification is tracked per-conversation with full undo support. Click the undo button to roll back any changes the assistant made.

**Multi-root projects** — add multiple directories as roots (e.g. frontend + backend repos). The assistant resolves namespaced paths across all roots.

**Smart token management** — the `content_ref` mechanism lets the assistant write a previous tool result to a file without re-generating it, and `emit_to_user` ends a turn by pointing you to existing tool output instead of repeating it. This saves significant tokens on large files.

---

### 🤖 Multi-Agent Swarm

Some tasks are too big for a single agent. The swarm system lets a master orchestrator plan sub-tasks and delegate them to specialist agents running in parallel.

**When to use it:** "Refactor this microservice into 3 separate services, update the API docs, and write migration scripts." Instead of one agent doing everything sequentially, the master spawns parallel agents for each sub-task.

**How it works:**
1. The master LLM plans sub-tasks and assigns roles (coder, researcher, writer, reviewer…)
2. A **streaming DAG scheduler** launches agents as soon as their dependencies complete — no waiting for entire waves
3. Agents share data through an **artifact store** (key-value pairs visible to all agents)
4. As agents complete, the master reviews results and can spawn follow-up agents
5. Final results are synthesized into a coherent output

**Agent roles** — each agent gets role-specific system prompts, model tiers, and scoped tool access. A "researcher" agent gets search tools; a "coder" agent gets project tools; a "reviewer" gets read-only access.

**Rate limiting** — a shared semaphore prevents agents from overwhelming the LLM API with concurrent requests. Automatic exponential backoff on 429s.

---

### 🔀 CLI Backend Switching

Already using **Claude Code** or **OpenAI Codex**? Tofu can act as a pure web frontend for them — you get Tofu's UI, conversation management, and persistence, while the external CLI handles all LLM calls and tool execution with its own authentication.

**Setup:**
```bash
# Install Claude Code
npm install -g @anthropic-ai/claude-code && claude auth login

# Or install Codex
npm install -g @openai/codex && codex auth login
```

Click the **backend selector** (🤖) in the top bar to switch. The UI automatically adapts — model selector and Tofu-specific features are hidden when using an external backend.

| Feature | Built-in (Tofu) | Claude Code | Codex |
|---------|:-:|:-:|:-:|
| Chat & streaming | ✅ | ✅ | ✅ |
| Web search | ✅ | ✅ (CC's) | ✅ (Codex's) |
| File operations | ✅ | ✅ (CC's) | ✅ (Codex's) |
| Code execution | ✅ | ✅ (Bash) | ✅ (exec) |
| Model selection | ✅ | — | — |
| Image generation | ✅ | ❌ | ❌ |
| Browser extension | ✅ | ❌ | ❌ |
| Multi-agent swarm | ✅ | ❌ | ❌ |

> The CLI must be installed on the same machine as the Tofu server. Each conversation remembers its backend.

---

### 🌐 Browser Extension

When you need the assistant to read pages that require login — internal dashboards, JIRA tickets, authenticated admin panels — the browser extension bridges your real browser session to Tofu.

**Setup:**
1. Go to `chrome://extensions` → Enable Developer Mode
2. Load unpacked → select the `browser_extension/` folder
3. Click the extension icon → enter your Tofu server URL

**What it can do:**

| Tool | Use case |
|---|---|
| `browser_list_tabs` | See all your open tabs |
| `browser_read_tab` | Extract text content (with optional CSS selector) |
| `browser_screenshot` | Capture a visual screenshot |
| `browser_navigate` | Open a URL |
| `browser_click` | Click elements by selector or text |
| `browser_type` | Type into input fields |
| `browser_execute_js` | Run custom JavaScript for data extraction |
| `browser_get_interactive_elements` | Discover clickable/typeable elements |
| `browser_get_app_state` | Access Vue/React internal state |

**When the page uses Canvas/SVG rendering** (charts, DAG diagrams) — DOM text extraction returns nothing. Use `browser_screenshot` for visual analysis, `browser_get_app_state` for data, or `browser_execute_js` for custom extraction.

**Multiple browsers** can connect simultaneously with independent command queues — useful if you have work and personal browser profiles.

---

### 🖥️ Desktop Agent

When you need the assistant to interact with your local machine beyond the browser — take full-screen screenshots, read/write local files, automate GUI clicks, manage clipboard.

**Setup:**
```bash
pip install pyautogui pillow psutil
python lib/desktop_agent.py --server http://your-server:15000 --allow-write --allow-exec
```

The agent connects to your Tofu server and exposes tools for file operations, clipboard, screenshots, GUI automation (pyautogui), and system info. All dangerous operations require explicit `--allow-write` / `--allow-exec` flags.

---

### 🖼️ Image Generation

When you need visual content — illustrations, diagrams, logos, edited photos — the assistant can generate images mid-conversation.

**How to use:** Enable the 🖼️ toggle in the tool bar and describe what you want. The assistant calls `generate_image` with a detailed prompt.

- **Create from scratch** — "Draw a minimalist logo of a mountain with a sunrise"
- **Edit existing images** — upload an image and say "change the background to a beach sunset"
- **Save to project** — specify `output_path` to save directly into your codebase
- **SVG conversion** — add `svg: true` to auto-trace the generated PNG into a scalable vector

Multi-model dispatch cycles across Gemini and GPT image models, automatically retrying on rate limits.

---

### 🔗 MCP (Model Context Protocol)

When you want to connect external tool servers — GitHub, databases, custom APIs — MCP bridges them into Tofu's tool system.

**How it works:** MCP servers run as subprocesses and communicate via stdio/SSE (JSON-RPC 2.0). Tofu translates their tools into OpenAI function-calling format, so the LLM can discover and invoke them alongside native tools.

**Setup:** Go to **Settings** or configure in `data/config/mcp_servers.json`:
```json
{
  "github": {
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-github"],
    "env": { "GITHUB_TOKEN": "ghp_xxx" }
  }
}
```

The assistant can then call tools like `mcp__github__create_issue`, `mcp__github__search_code`, etc. — any MCP-compatible server works.

---

### ☑️ Daily Reports & My Day

Click the **☑️ My Day** button in the sidebar to open your personal work journal — an LLM-powered daily dashboard.

**When you want to see what you accomplished today** — the LLM reads all your conversations for the day and clusters them into 5–15 coherent work streams (e.g. "Fix image rendering bug", "Deploy staging environment"), marking each as *done*, *in progress*, or *blocked*.

**When you need tomorrow's plan** — the LLM synthesizes 3–8 actionable TODO items from unfinished work. Each comes with a detailed prompt and recommended tool configuration. Click ▶ to launch any TODO as a new conversation, pre-filled and ready to go.

**Calendar view** — month-at-a-glance with per-day conversation counts and cost heatmap. Click any date to view or generate its report.

**To-do management** — uncompleted TODOs carry forward to the next day. Add manual TODOs, toggle done/undone, or launch them as conversations. Cost tracking shows per-day and per-conversation spend in CNY.

**Auto-backfill** — a background scheduler generates yesterday's report on server boot if it's missing, and again daily at midnight.

---

### 🕐 Scheduled Tasks

When you need something to run automatically — daily data pulls, periodic health checks, recurring reports — create a proactive agent that runs on a schedule.

**How to use:** Enable the 🕐 Scheduler toggle and ask: "Run a health check on my API every 6 hours" or "Every morning at 9am, summarize overnight GitHub issues." The assistant creates a cron-like task.

**Task types:** Shell commands, Python scripts, or LLM prompts — all with full tool access.

**Manage tasks:** Click the **SCHEDULER** badge in the top status bar to see all active proactive agents and their recent run logs.

---

### 🐦 Feishu (Lark) Bot

When your team communicates in Feishu and you want AI assistance directly in group chats — Tofu connects as a Feishu bot via WebSocket.

**Setup:**
1. Create an app at [open.feishu.cn](https://open.feishu.cn/app), enable Bot capability
2. Go to **Settings → 🐦 Feishu** → enter App ID and App Secret
3. The bot auto-connects on server restart

**Features:** Multi-turn conversations with full tool support (search, code, project), slash commands for model/mode switching, and conversation management — all within Feishu's native chat interface.

---

### 🧠 Memory System

When the assistant discovers something useful — a bug pattern, a project convention, your preferred coding style — it can save that knowledge as a **memory** for future sessions.

**How it works:** Memories are Markdown files stored in `.chatui/skills/` (project-scoped) or `.chatui/skills/global/` (all projects). The assistant creates them proactively or when you ask. In future conversations, relevant memories are automatically loaded into context.

**Tools:** `create_memory`, `update_memory`, `delete_memory`, `merge_memories` — the assistant manages its own knowledge base across sessions.

**When to use:** "Remember that our API always returns snake_case" — the assistant saves this convention and applies it in all future code generation for this project.

---

### 🔌 Conversation Branching

When you want to explore a different direction without losing the current thread — branch any assistant message.

**How it works:** Click the branch icon on any assistant message. A new branch opens inline, continuing from that point with its own independent history. Multiple branches can stream in parallel. Each branch can use a different model or parameters.

**Use cases:**
- Compare how different models answer the same question
- Try an alternative approach without losing the current one
- Let one branch research while another branch implements

---

## Settings Reference

All configuration is done through the **⚙️ Settings** panel (top-right gear icon). Changes save instantly — no restart needed.

| Tab | What you configure |
|---|---|
| **⚙️ General** | Theme (Dark/Light/Tofu), temperature, max tokens, thinking depth, system prompt |
| **🔗 Providers** | API keys, endpoints, model lists, multi-key rotation, auto-discovery |
| **📦 Display** | Which models appear in dropdowns, default model, fallback model |
| **🔍 Search & Fetch** | Result count, timeouts, character limits, blocked domains, content filter |
| **🌐 Network** | HTTP/HTTPS proxy, bypass domains |
| **🐦 Feishu** | App credentials, default project, allowed users |
| **`</>` Advanced** | Price overrides, cache management, server info |

### Environment Variables (fallback)

For headless/Docker setups, you can configure via environment variables. The Settings UI always takes priority.

```bash
cp .env.example .env
```

| Variable | Description | Example |
|---|---|---|
| `LLM_API_KEY` | API key (fallback) | `sk-abc123...` |
| `LLM_BASE_URL` | Endpoint (fallback) | `https://api.openai.com/v1` |
| `LLM_MODEL` | Default model (fallback) | `gpt-4o` |
| `PORT` | Server port | `15000` |
| `BIND_HOST` | Bind address | `0.0.0.0` |

---

## Project Structure

```
├── server.py                  Flask app entry, middleware, logging
├── bootstrap.py               Auto-dependency repair (LLM-guided)
├── index.html                 Main chat UI (single-page app)
│
├── lib/                       Core libraries
│   ├── agent_backends/        CLI backend switching (builtin/Claude Code/Codex)
│   ├── llm_client.py          LLM API client (streaming, retry)
│   ├── llm_dispatch/          Multi-key multi-model smart dispatcher
│   ├── database/              PostgreSQL (auto-bootstrap, migrations)
│   ├── tasks_pkg/             Task orchestration & context compaction
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
│   ├── memory/                Memory accumulation system
│   ├── mcp/                   Model Context Protocol bridge
│   ├── feishu/                Feishu bot integration
│   ├── scheduler/             Task scheduling (cron, proactive agents)
│   ├── image_gen.py           Image generation (multi-model dispatch)
│   ├── desktop_agent.py       Desktop automation agent
│   └── ...
│
├── routes/                    Flask Blueprints (21 API modules)
├── static/                    CSS, JS, icons
├── browser_extension/         Chrome extension (Manifest V3)
├── tests/                     Test suite (unit, API, E2E)
└── data/                      Runtime data (git-ignored)
```

---

## Platform Support

| Feature | Linux | macOS | Windows |
|---|:---:|:---:|:---:|
| Core chat & tools | ✅ | ✅ | ✅ |
| PostgreSQL auto-bootstrap | ✅ | ✅ | ✅ |
| Project co-pilot | ✅ | ✅ | ✅ |
| Shell commands | ✅ | ✅ | ✅ (`cmd.exe`) |
| Desktop agent | ✅ | ✅ | ✅ |
| Browser extension | ✅ | ✅ | ✅ |

Smoke test: `python debug/test_cross_platform.py`

---

## Testing

```bash
# All tests
python tests/run_all.py

# Individual suites
python -m pytest tests/test_backend_unit.py
python -m pytest tests/test_api_integration.py
python -m pytest tests/test_visual_e2e.py
```

---

## Security

- **No secrets in source** — all credentials loaded from environment variables or Settings UI
- **Single-user mode** — no multi-tenant auth; deploy behind a VPN or reverse proxy for production
- **Tool execution** — the assistant can run shell commands and edit files; dangerous patterns are blocked, but use with appropriate caution
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
